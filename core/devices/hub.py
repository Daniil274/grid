"""The server's side of devices: who is connected, who may use them, calls and the live view.

Every device - a browser machine on the server, a user's PC or phone - runs a
node (grid_node) that serves its tools over one tunnel. The hub holds the
connected devices of every user of this process:

- **calls** go to the device's MCP server; a device is used by one
  conversation at a time (a lease that lapses after ``LEASE_SECONDS`` without
  a call), so two chats never click in the same page;
- **classes**: a device tool is a look (reads), a do (acts in the open world:
  a page) or a run (changes the device: a command, a file) by its MCP
  annotations, and agents reach each class through a tool of its own
  (tools.device_tools), which the action policy judges apart;
- **the live view**: frames go to every viewer of the device; a person who
  takes control holds it - agents' calls wait it out - until they give it back.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Set

from mcp import ClientSession, types

from grid_node.tunnel import CTL, VIEW, Tunnel

logger = logging.getLogger("grid.devices")

LOOK, DO, RUN = "look", "do", "run"
#: Seconds a conversation keeps a device after its last call.
LEASE_SECONDS = 300
#: Seconds a device gets to say hello after connecting.
HELLO_SECONDS = 20
#: Seconds one call may take.
CALL_SECONDS = 120

Sink = Callable[[Dict[str, Any]], Awaitable[None]]


class DeviceError(Exception):
    """A call the hub refuses or cannot make; its text is for the agent."""


def tool_class(tool: types.Tool) -> str:
    """LOOK, DO or RUN by the tool's MCP annotations; a tool that says nothing is a RUN."""
    hints = tool.annotations
    if hints is None:
        return RUN
    if hints.readOnlyHint:
        return LOOK
    if hints.destructiveHint is False:
        return DO
    return RUN


@dataclass
class Device:
    id: str
    user_id: str
    name: str
    kind: str
    platform: str
    capabilities: List[str]
    view: bool
    tools: Dict[str, types.Tool]
    origin: str
    tunnel: Tunnel
    session: ClientSession
    connected_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    holder: Optional[str] = None
    held_until: float = 0.0
    person: bool = False
    calls: int = 0

    def held_by_other(self, holder: str) -> bool:
        return bool(self.holder) and self.holder != holder and self.held_until > time.time()

    def summary(self, holder: Optional[str] = None) -> Dict[str, Any]:
        if self.person:
            status = "a person has control"
        elif holder is not None and self.held_by_other(holder):
            status = "in use by another conversation"
        else:
            status = "free"
        return {
            "device": self.id,
            "name": self.name,
            "kind": self.kind,
            "platform": self.platform,
            "capabilities": self.capabilities,
            "status": status,
            "tools": {
                name: {"class": tool_class(tool), "description": tool.description,
                       "parameters": sorted((tool.inputSchema or {}).get("properties", {}))}
                for name, tool in self.tools.items()
            },
        }


class DeviceHub:
    """The connected devices of this process, by user."""

    def __init__(self) -> None:
        self._devices: Dict[str, Device] = {}
        self._viewers: Dict[str, Set[Sink]] = {}
        self._changed: List[Callable[[str], None]] = []

    # -- connections ----------------------------------------------------------

    def on_change(self, callback: Callable[[str], None]) -> None:
        """Call *callback(user_id)* when a device of that user connects or goes."""
        self._changed.append(callback)

    def _notify(self, user_id: str) -> None:
        for callback in self._changed:
            try:
                callback(user_id)
            except Exception:
                logger.debug("A device change listener failed", exc_info=True)

    async def serve(self, tunnel: Tunnel, *, device_id: str, user_id: str, origin: str) -> None:
        """Hold the device on *tunnel* until it disconnects."""
        hello: Dict[str, Any] = {}
        said_hello = asyncio.Event()

        async def control(message: Any) -> None:
            if isinstance(message, dict) and message.get("type") == "hello":
                hello.update(message)
                said_hello.set()

        async def frames(message: Any) -> None:
            for sink in list(self._viewers.get(device_id, ())):
                try:
                    await sink(message)
                except Exception:
                    self._viewers.get(device_id, set()).discard(sink)

        tunnel.on(CTL, control)
        tunnel.on(VIEW, frames)
        reader = asyncio.create_task(tunnel.run())
        try:
            try:
                await asyncio.wait_for(said_hello.wait(), HELLO_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("Device %s connected but said no hello; dropped", device_id)
                return
            async with tunnel.mcp_streams() as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), HELLO_SECONDS)
                    listed = await asyncio.wait_for(session.list_tools(), HELLO_SECONDS)
                    device = Device(
                        id=device_id,
                        user_id=user_id,
                        name=str(hello.get("name") or device_id)[:80],
                        kind=str(hello.get("kind") or "device")[:20],
                        platform=str(hello.get("platform") or "")[:60],
                        capabilities=[str(c)[:40] for c in hello.get("capabilities") or []][:20],
                        view=bool(hello.get("view")),
                        tools={tool.name: tool for tool in listed.tools},
                        origin=origin,
                        tunnel=tunnel,
                        session=session,
                    )
                    previous = self._devices.get(device_id)
                    if previous is not None:
                        await previous.tunnel.close("replaced by a new connection")
                    self._devices[device_id] = device
                    self._notify(user_id)
                    logger.info("Device %s (%s, %s) of %s connected", device_id, device.kind, device.name, user_id)
                    if self._viewers.get(device_id):
                        await tunnel.send(VIEW, {"type": "watch", "on": True})
                    try:
                        await tunnel.closed.wait()
                    finally:
                        if self._devices.get(device_id) is device:
                            del self._devices[device_id]
                            self._notify(user_id)
                        logger.info("Device %s of %s disconnected", device_id, user_id)
        finally:
            reader.cancel()

    # -- what a user has ------------------------------------------------------

    def devices(self, user_id: str) -> List[Device]:
        return [d for d in self._devices.values() if d.user_id == user_id]

    def get(self, user_id: str, device_id: str) -> Device:
        device = self._devices.get(device_id)
        if device is None or device.user_id != user_id:
            raise DeviceError(f"No device {device_id} is connected for you. Call devices to see yours.")
        return device

    def in_use(self, device_id: str) -> bool:
        """Whether someone is using or watching the device: not idle."""
        device = self._devices.get(device_id)
        return bool(self._viewers.get(device_id)) or (device is not None and (device.person or device.held_until > time.time()))

    def last_used(self, device_id: str) -> Optional[float]:
        device = self._devices.get(device_id)
        return device.last_used if device is not None else None

    # -- calls ------------------------------------------------------------------

    async def call(
        self,
        user_id: str,
        device_id: str,
        tool: str,
        arguments: Dict[str, Any],
        *,
        holder: str,
        allowed: str,
        timeout: float = CALL_SECONDS,
    ) -> types.CallToolResult:
        """Call *tool* of the device for conversation *holder*; *allowed* is the class the caller may use."""
        device = self.get(user_id, device_id)
        spec = device.tools.get(tool)
        if spec is None:
            raise DeviceError(f"Device {device_id} has no tool {tool}; it has: {', '.join(sorted(device.tools))}.")
        kind = tool_class(spec)
        if kind != allowed:
            raise DeviceError(f"{tool} is a {kind} tool: call it with device_{kind}, not device_{allowed}.")
        if device.person:
            raise DeviceError(
                f"A person has taken control of {device.name}; wait until they give it back, then look again."
            )
        if device.held_by_other(holder):
            raise DeviceError(
                f"{device.name} is in use by another conversation; use another device or start a new machine."
            )
        device.holder, device.held_until = holder, time.time() + LEASE_SECONDS
        try:
            result = await asyncio.wait_for(device.session.call_tool(tool, arguments), timeout)
        except asyncio.TimeoutError as exc:
            raise DeviceError(f"{tool} on {device.name} gave no answer within {timeout:g} s.") from exc
        finally:
            device.calls += 1
            device.last_used = time.time()
            device.held_until = time.time() + LEASE_SECONDS
        return result

    def release(self, holder: str) -> None:
        """End every lease of conversation *holder* (its turn ended for good, the chat was deleted)."""
        for device in self._devices.values():
            if device.holder == holder:
                device.holder, device.held_until = None, 0.0

    # -- the live view ------------------------------------------------------

    @asynccontextmanager
    async def watching(self, user_id: str, device_id: str, sink: Sink) -> AsyncIterator[Device]:
        """Send the device's frames to *sink* while the block runs."""
        device = self.get(user_id, device_id)
        viewers = self._viewers.setdefault(device_id, set())
        first = not viewers
        viewers.add(sink)
        try:
            if first and device.view:
                await device.tunnel.send(VIEW, {"type": "watch", "on": True})
            yield device
        finally:
            viewers.discard(sink)
            if not viewers:
                self._viewers.pop(device_id, None)
                current = self._devices.get(device_id)
                if current is not None and current.view:
                    try:
                        await current.tunnel.send(VIEW, {"type": "watch", "on": False})
                    except Exception:
                        pass

    async def take_control(self, user_id: str, device_id: str, taken: bool) -> None:
        """A person takes the device from agents, or gives it back."""
        device = self.get(user_id, device_id)
        device.person = bool(taken)
        self._notify(user_id)

    async def person_input(self, user_id: str, device_id: str, event: Dict[str, Any]) -> None:
        """A person's input from the live view; taking control comes first."""
        device = self.get(user_id, device_id)
        if not device.person:
            raise DeviceError("Take control of the device first.")
        await device.tunnel.send(VIEW, {**event, "type": "input"})


_hub: Optional[DeviceHub] = None


def hub() -> DeviceHub:
    """The device hub of this process."""
    global _hub
    if _hub is None:
        _hub = DeviceHub()
    return _hub

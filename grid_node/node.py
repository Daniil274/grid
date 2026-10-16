"""A device's side of Grid: its tools, served to the server over a tunnel.

A node runs on the device it controls: a browser machine on the server, the
user's PC, later a phone. Its drivers (grid_node.drivers) each bring tools -
a browser's, a screen and keyboard, the system's shell and files - and what
they can do. The node serves them as one MCP server over a tunnel
(grid_node.tunnel), opens with a hello naming the device and its
capabilities, and streams the screen to whoever watches.

How the connection is made does not change what is said on it:

- :func:`dial` - the device calls the server (``wss://…/node/connect``)
  and keeps calling back after a drop: a PC or a phone behind NAT needs no
  open port;
- :func:`listen` - the server calls the device: a browser machine in a
  container on the server's own network.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import platform as _platform
import random
import socket
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from mcp import types
from mcp.server.lowlevel import Server

from grid_node import __version__
from grid_node.tunnel import CTL, VIEW, Tunnel, WebsocketsSocket

logger = logging.getLogger("grid.node")

#: What a tool hands back: text, or an image as ``{"type": "image", "data", "mime"}``.
Content = Dict[str, Any]
ToolHandler = Callable[[Dict[str, Any]], Awaitable[List[Content]]]
ViewSink = Callable[[Dict[str, Any]], Awaitable[None]]


#: How risky a tool is, as MCP tool annotations say it; the server gives agents
#: each class through a tool of its own, which the action policy judges apart.
LOOK = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}
DO = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
RUN = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}


@dataclass
class ToolSpec:
    """One tool a driver brings: LOOK reads, DO acts in the open world (a page),
    RUN changes the device itself (a command, a file)."""

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: ToolHandler
    annotations: Dict[str, Any] = field(default_factory=lambda: dict(DO))


class Driver:
    """What a node controls: its tools, its capabilities, its screen if it has one."""

    name = "driver"
    #: What the device can do through this driver, e.g. ``browser``, ``screen``, ``shell``.
    capabilities: List[str] = []

    async def start(self) -> None:
        """Ready the driver (launch the browser, open the screen)."""

    async def stop(self) -> None:
        """Release what :meth:`start` took."""

    def tools(self) -> List[ToolSpec]:
        return []

    @property
    def has_view(self) -> bool:
        return False

    async def start_view(self, sink: ViewSink) -> None:
        """Send frames of the screen to *sink* until :meth:`stop_view`."""

    async def stop_view(self) -> None:
        """Stop the frames."""

    async def person_input(self, event: Dict[str, Any]) -> None:
        """Apply a person's input (mouse, keys) from the live view."""


def text(value: str) -> Content:
    return {"type": "text", "text": value}


def image(data: bytes, mime: str = "image/png") -> Content:
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mime": mime}


@dataclass
class Node:
    """A device and its drivers, ready to serve on a tunnel."""

    name: str
    kind: str
    drivers: List[Driver]
    platform: str = field(default_factory=lambda: f"{_platform.system().lower()}-{_platform.machine().lower()}")

    def __post_init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}
        for driver in self.drivers:
            for spec in driver.tools():
                if spec.name in self._tools:
                    raise ValueError(f"two drivers bring a tool named {spec.name}")
                self._tools[spec.name] = spec
        self._view_driver = next((d for d in self.drivers if d.has_view), None)

    def hello(self) -> Dict[str, Any]:
        return {
            "type": "hello",
            "node": __version__,
            "name": self.name,
            "kind": self.kind,
            "platform": self.platform,
            "host": socket.gethostname(),
            "capabilities": sorted({c for d in self.drivers for c in d.capabilities}),
            "view": self._view_driver is not None,
        }

    def _server(self) -> Server:
        server: Server = Server("grid-node", version=__version__)

        @server.list_tools()
        async def list_tools() -> List[types.Tool]:
            return [
                types.Tool(
                    name=spec.name,
                    description=spec.description,
                    inputSchema=spec.input_schema,
                    annotations=types.ToolAnnotations(**spec.annotations),
                )
                for spec in self._tools.values()
            ]

        @server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> List[Any]:
            spec = self._tools.get(name)
            if spec is None:
                raise ValueError(f"no tool named {name}")
            parts = await spec.handler(arguments or {})
            out: List[Any] = []
            for part in parts:
                if part.get("type") == "image":
                    out.append(types.ImageContent(type="image", data=part["data"], mimeType=part.get("mime", "image/png")))
                else:
                    out.append(types.TextContent(type="text", text=str(part.get("text", ""))))
            return out

        return server

    async def serve(self, tunnel: Tunnel) -> None:
        """Serve the tools on *tunnel* until it closes."""
        watching = False

        async def view(message: Any) -> None:
            nonlocal watching
            if not isinstance(message, dict) or self._view_driver is None:
                return
            if message.get("type") == "watch":
                want = bool(message.get("on"))
                if want and not watching:
                    watching = True
                    await self._view_driver.start_view(lambda frame: tunnel.send(VIEW, frame))
                elif not want and watching:
                    watching = False
                    await self._view_driver.stop_view()
            elif message.get("type") == "input":
                await self._view_driver.person_input(message)

        async def control(message: Any) -> None:
            if isinstance(message, dict) and message.get("type") == "ping":
                await tunnel.send(CTL, {"type": "pong"})

        tunnel.on(VIEW, view)
        tunnel.on(CTL, control)
        server = self._server()
        reader = asyncio.create_task(tunnel.run())
        try:
            await tunnel.send(CTL, self.hello())
            async with tunnel.mcp_streams() as (read, write):
                serving = asyncio.create_task(server.run(read, write, server.create_initialization_options()))
                closed = asyncio.create_task(tunnel.closed.wait())
                await asyncio.wait({serving, closed}, return_when=asyncio.FIRST_COMPLETED)
                for task in (serving, closed):
                    task.cancel()
        finally:
            if watching and self._view_driver is not None:
                await self._view_driver.stop_view()
            reader.cancel()


def _bearer(headers: Any) -> str:
    value = headers.get("Authorization") or headers.get("authorization") or ""
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


async def dial(node: Node, url: str, token: str, *, stop: Optional[asyncio.Event] = None) -> None:
    """Keep *node* connected to the server at *url*, calling back after every drop."""
    import websockets

    delay = 1.0
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            async with websockets.connect(
                url,
                additional_headers={"Authorization": f"Bearer {token}"},
                max_size=None,
                ping_interval=20,
            ) as connection:
                logger.info("Connected to %s as %s", url, node.name)
                delay = 1.0
                await node.serve(Tunnel(WebsocketsSocket(connection)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Connection to %s failed: %s", url, exc)
        if stop.is_set():
            break
        await asyncio.sleep(delay + random.random())
        delay = min(delay * 2, 60.0)


async def listen(node: Node, host: str, port: int, token: str) -> None:
    """Serve *node* to the server that connects with *token*, one connection at a time."""
    import websockets

    busy = asyncio.Lock()

    async def handle(connection: Any) -> None:
        offered = _bearer(connection.request.headers)
        if not token or not hmac.compare_digest(offered.encode(), token.encode()):
            await connection.close(4401, "unauthorized")
            return
        if busy.locked():
            await connection.close(4409, "already connected")
            return
        async with busy:
            await node.serve(Tunnel(WebsocketsSocket(connection)))

    async with websockets.serve(handle, host, port, max_size=None, ping_interval=20):
        logger.info("Listening on %s:%s as %s", host, port, node.name)
        await asyncio.Future()

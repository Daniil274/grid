"""Channels over one WebSocket between a device and the Grid server.

A device - a browser machine on the server, the user's PC, a phone - and the
server keep one connection. Whichever side opened it, both speak the same
frames: JSON text ``{"ch": channel, "d": data}``.

- ``mcp``: the Model Context Protocol, JSON-RPC both ways. The device serves
  its tools; the server is the MCP client that calls them for agents.
- ``ctl``: the connection itself - the device's hello (who it is, what it can
  do), pings, and the reason it is going.
- ``view``: the live picture of the device's screen, and the input of a person
  who took control of it.

MCP runs over the SDK's in-memory streams (:meth:`Tunnel.mcp_streams`), so a
``ClientSession`` or a server's ``run`` works on a tunnel as on any transport.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional, Protocol, Tuple

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import types
from mcp.shared.message import SessionMessage

logger = logging.getLogger("grid.node.tunnel")

MCP, CTL, VIEW = "mcp", "ctl", "view"
#: The largest frame either side accepts: a screenshot or a file part, encoded.
MAX_FRAME = 48 * 1024 * 1024


class Closed(Exception):
    """The other side went away."""


class Socket(Protocol):
    """What a tunnel needs of a WebSocket, whichever library holds it."""

    async def send_text(self, text: str) -> None: ...

    async def receive_text(self) -> str: ...

    async def close(self, code: int = 1000) -> None: ...


class WebsocketsSocket:
    """A connection of the ``websockets`` library as a :class:`Socket`."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def send_text(self, text: str) -> None:
        await self.connection.send(text)

    async def receive_text(self) -> str:
        message = await self.connection.recv()
        return message if isinstance(message, str) else message.decode("utf-8")

    async def close(self, code: int = 1000) -> None:
        await self.connection.close(code)


Handler = Callable[[Any], Awaitable[None]]


class Tunnel:
    """One device connection: its channels, read by :meth:`run` until it closes."""

    def __init__(self, socket: Socket) -> None:
        self.socket = socket
        self.handlers: Dict[str, Handler] = {}
        self._mcp_in: Optional[MemoryObjectSendStream[SessionMessage | Exception]] = None
        self._send_lock = anyio.Lock()
        self.closed = anyio.Event()

    def on(self, channel: str, handler: Handler) -> None:
        """Hand every frame of *channel* to *handler* (``ctl`` and ``view``)."""
        self.handlers[channel] = handler

    async def send(self, channel: str, data: Any) -> None:
        text = json.dumps({"ch": channel, "d": data}, separators=(",", ":"), ensure_ascii=False)
        if len(text) > MAX_FRAME:
            raise ValueError(f"frame of {len(text)} bytes is larger than {MAX_FRAME}")
        async with self._send_lock:
            await self.socket.send_text(text)

    async def run(self) -> None:
        """Read frames until the connection closes; then the tunnel is closed."""
        try:
            while True:
                try:
                    text = await self.socket.receive_text()
                except Exception as exc:  # the libraries raise their own close errors
                    raise Closed(str(exc) or type(exc).__name__) from exc
                if len(text) > MAX_FRAME:
                    logger.warning("Dropped a frame of %d bytes", len(text))
                    continue
                try:
                    frame = json.loads(text)
                    channel, data = frame["ch"], frame.get("d")
                except (ValueError, KeyError, TypeError):
                    logger.warning("Dropped a malformed frame")
                    continue
                if channel == MCP:
                    await self._mcp_frame(data)
                elif channel in self.handlers:
                    try:
                        await self.handlers[channel](data)
                    except Exception:
                        logger.exception("Handler of %s failed", channel)
        except Closed:
            pass
        finally:
            self.closed.set()
            if self._mcp_in is not None:
                await self._mcp_in.aclose()

    async def _mcp_frame(self, data: Any) -> None:
        if self._mcp_in is None:
            return
        try:
            message: SessionMessage | Exception = SessionMessage(types.JSONRPCMessage.model_validate(data))
        except Exception as exc:  # a message MCP cannot read reaches the session as one
            message = exc
        try:
            await self._mcp_in.send(message)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass

    @asynccontextmanager
    async def mcp_streams(
        self,
    ) -> AsyncIterator[Tuple[MemoryObjectReceiveStream, MemoryObjectSendStream]]:
        """(read stream, write stream) of the MCP channel, for an MCP session or server."""
        incoming_writer, incoming = anyio.create_memory_object_stream[SessionMessage | Exception](64)
        outgoing, outgoing_reader = anyio.create_memory_object_stream[SessionMessage](64)
        self._mcp_in = incoming_writer

        async def forward() -> None:
            async with outgoing_reader:
                async for message in outgoing_reader:
                    try:
                        await self.send(MCP, message.message.model_dump(by_alias=True, mode="json", exclude_none=True))
                    except Exception:
                        logger.warning("Could not send an MCP message", exc_info=True)
                        return

        async with anyio.create_task_group() as group:
            group.start_soon(forward)
            try:
                yield incoming, outgoing
            finally:
                await outgoing.aclose()
                group.cancel_scope.cancel()

    async def close(self, reason: str = "") -> None:
        if not self.closed.is_set():
            try:
                await self.send(CTL, {"type": "bye", "reason": reason})
            except Exception:
                pass
            try:
                await self.socket.close()
            except Exception:
                pass

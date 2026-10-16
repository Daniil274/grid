"""A device and the hub over one tunnel: tools, classes, leases, control and the live view."""

import asyncio
from typing import Any, Dict, List

import pytest

from core.devices.hub import DO, LOOK, RUN, DeviceError, DeviceHub
from grid_node.node import LOOK as LOOK_HINTS
from grid_node.node import RUN as RUN_HINTS
from grid_node.node import Driver, Node, ToolSpec, image, text
from grid_node.tunnel import Tunnel


class MemorySocket:
    """One end of an in-memory WebSocket."""

    def __init__(self, inbox: asyncio.Queue, outbox: asyncio.Queue) -> None:
        self.inbox, self.outbox = inbox, outbox
        self.closed = False

    async def send_text(self, text: str) -> None:
        if self.closed:
            raise ConnectionError("closed")
        await self.outbox.put(text)

    async def receive_text(self) -> str:
        message = await self.inbox.get()
        if message is None:
            raise ConnectionError("closed")
        return message

    async def close(self, code: int = 1000) -> None:
        if not self.closed:
            self.closed = True
            await self.outbox.put(None)
            await self.inbox.put(None)


def socket_pair():
    a, b = asyncio.Queue(), asyncio.Queue()
    return MemorySocket(a, b), MemorySocket(b, a)


class FakeScreen(Driver):
    name = "fake"
    capabilities = ["screen", "shell"]

    def __init__(self) -> None:
        self.inputs: List[Dict[str, Any]] = []
        self.sink = None
        self.ran: List[str] = []

    def tools(self):
        async def look(args):
            return [text("a page"), image(b"\x89PNG fake")]

        async def click(args):
            return [text(f"clicked {args['ref']}")]

        async def shell(args):
            self.ran.append(args["command"])
            return [text("done")]

        return [
            ToolSpec("screen_look", "Look", {"type": "object", "properties": {}}, look, LOOK_HINTS),
            ToolSpec("screen_click", "Click", {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]}, click),
            ToolSpec("shell_run", "Run", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}, shell, RUN_HINTS),
        ]

    @property
    def has_view(self) -> bool:
        return True

    async def start_view(self, sink) -> None:
        self.sink = sink
        await sink({"type": "frame", "mime": "image/jpeg", "data": "AAAA", "width": 10, "height": 10})

    async def stop_view(self) -> None:
        self.sink = None

    async def person_input(self, event) -> None:
        self.inputs.append(event)


async def connected(hub: DeviceHub, driver: Driver, device_id="dev-1", user="alice"):
    node_end, hub_end = socket_pair()
    node = Node(name="test machine", kind="browser", drivers=[driver])
    tasks = [
        asyncio.create_task(node.serve(Tunnel(node_end))),
        asyncio.create_task(hub.serve(Tunnel(hub_end), device_id=device_id, user_id=user, origin="machine")),
    ]
    for _ in range(200):
        if hub.devices(user):
            break
        await asyncio.sleep(0.01)
    return tasks, hub_end


async def test_a_device_connects_and_its_tools_are_called_by_class():
    hub = DeviceHub()
    driver = FakeScreen()
    tasks, hub_end = await connected(hub, driver)
    try:
        (device,) = hub.devices("alice")
        assert device.name == "test machine" and set(device.capabilities) == {"screen", "shell"}
        summary = device.summary("chat-a")
        assert {name: spec["class"] for name, spec in summary["tools"].items()} == {
            "screen_look": LOOK, "screen_click": DO, "shell_run": RUN,
        }

        result = await hub.call("alice", "dev-1", "screen_look", {}, holder="chat-a", allowed=LOOK)
        assert [part.type for part in result.content] == ["text", "image"]
        result = await hub.call("alice", "dev-1", "screen_click", {"ref": "e3"}, holder="chat-a", allowed=DO)
        assert result.content[0].text == "clicked e3"

        with pytest.raises(DeviceError, match="is a run tool: call it with device_run"):
            await hub.call("alice", "dev-1", "shell_run", {"command": "rm -rf /"}, holder="chat-a", allowed=DO)
        assert driver.ran == []

        # Another user's agent never reaches it.
        with pytest.raises(DeviceError, match="No device dev-1"):
            await hub.call("bob", "dev-1", "screen_look", {}, holder="chat-b", allowed=LOOK)
    finally:
        await hub_end.close()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert hub.devices("alice") == []


async def test_one_conversation_at_a_time_and_a_person_comes_first():
    hub = DeviceHub()
    driver = FakeScreen()
    tasks, hub_end = await connected(hub, driver)
    try:
        await hub.call("alice", "dev-1", "screen_look", {}, holder="chat-a", allowed=LOOK)
        with pytest.raises(DeviceError, match="in use by another conversation"):
            await hub.call("alice", "dev-1", "screen_look", {}, holder="chat-b", allowed=LOOK)
        hub.release("chat-a")
        await hub.call("alice", "dev-1", "screen_look", {}, holder="chat-b", allowed=LOOK)

        with pytest.raises(DeviceError, match="Take control"):
            await hub.person_input("alice", "dev-1", {"kind": "click", "x": 1, "y": 2})
        await hub.take_control("alice", "dev-1", True)
        with pytest.raises(DeviceError, match="A person has taken control"):
            await hub.call("alice", "dev-1", "screen_look", {}, holder="chat-b", allowed=LOOK)
        await hub.person_input("alice", "dev-1", {"kind": "click", "x": 1, "y": 2})
        for _ in range(100):
            if driver.inputs:
                break
            await asyncio.sleep(0.01)
        assert driver.inputs[0]["kind"] == "click"
        await hub.take_control("alice", "dev-1", False)
        await hub.call("alice", "dev-1", "screen_look", {}, holder="chat-b", allowed=LOOK)
    finally:
        await hub_end.close()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_the_live_view_starts_with_the_first_viewer_and_stops_with_the_last():
    hub = DeviceHub()
    driver = FakeScreen()
    tasks, hub_end = await connected(hub, driver)
    frames = []

    async def sink(frame):
        frames.append(frame)

    try:
        async with hub.watching("alice", "dev-1", sink):
            for _ in range(100):
                if frames:
                    break
                await asyncio.sleep(0.01)
            assert frames[0]["type"] == "frame" and driver.sink is not None
        for _ in range(100):
            if driver.sink is None:
                break
            await asyncio.sleep(0.01)
        assert driver.sink is None
    finally:
        await hub_end.close()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_a_real_browser_is_read_and_driven_through_the_hub(tmp_path):
    pytest.importorskip("playwright")
    import re

    from grid_node.drivers.browser import BrowserDriver

    page = tmp_path / "page.html"
    page.write_text(
        "<title>Shop</title><h1>Hello</h1><input aria-label='name'>"
        "<button onclick=\"document.title='bought ' + document.querySelector('input').value\">Buy</button>"
    )
    driver = BrowserDriver(tmp_path / "profile", headless=True)
    try:
        await driver.start()
    except Exception as exc:  # no browser installed here
        pytest.skip(f"Chromium is not available: {exc}")
    hub = DeviceHub()
    tasks, hub_end = await connected(hub, driver, device_id="m-1")
    try:
        call = lambda tool, args, allowed=DO: hub.call("alice", "m-1", tool, args, holder="chat", allowed=allowed)
        opened = await call("browser_navigate", {"url": page.as_uri()})
        assert "Shop" in opened.content[0].text

        snapshot = (await call("browser_snapshot", {}, LOOK)).content[0].text
        box = re.search(r'textbox "name" \[ref=(e\d+)\]', snapshot).group(1)
        button = re.search(r'button "Buy" \[ref=(e\d+)\]', snapshot).group(1)

        await call("browser_type", {"ref": box, "text": "tea"})
        clicked = await call("browser_click", {"ref": button})
        assert "bought tea" in clicked.content[0].text

        shot = await call("browser_screenshot", {}, LOOK)
        assert shot.content[1].type == "image" and shot.content[1].mimeType == "image/png"

        frames = []

        async def sink(frame):
            frames.append(frame)

        async with hub.watching("alice", "m-1", sink):
            await call("browser_scroll", {"dy": 100})
            for _ in range(200):
                if frames:
                    break
                await asyncio.sleep(0.02)
        assert frames and frames[0]["mime"] == "image/jpeg"
    finally:
        await hub_end.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        await driver.stop()

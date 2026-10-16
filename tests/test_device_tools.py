"""Agents' device tools, pairing a device of one's own, the devices API and the machine pool."""

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest
from agents.tool import ToolOutputImage, ToolOutputText
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from core.devices import hub as hub_module
from core.devices import machines as machines_module
from core.devices.hub import DeviceHub
from core.devices.machines import MachineError, MachinePool
from core.devices.pairing import PairingStore
from grid_node.node import Node, listen
from tests.test_device_hub import FakeScreen, connected
from tools import device_tools
from utils import tool_effects
from utils.tool_isolation import DEVICES, is_confined


def run_ctx(user="alice", conversation="chat-a"):
    return SimpleNamespace(context=SimpleNamespace(user_id=user, context_id=conversation))


async def invoke(function, ctx, /, **arguments):
    return await function.on_invoke_tool(ctx, json.dumps(arguments))


@pytest.fixture
def fresh_hub(monkeypatch):
    hub = DeviceHub()
    monkeypatch.setattr(hub_module, "_hub", hub)
    monkeypatch.setattr(machines_module, "_pool", None)
    return hub


async def test_agents_list_devices_and_call_each_class_through_its_tool(fresh_hub):
    tasks, end = await connected(fresh_hub, FakeScreen())
    try:
        listing = json.loads(await invoke(device_tools.devices, run_ctx()))
        (device,) = listing["devices"]
        assert device["device"] == "dev-1" and device["tools"]["shell_run"]["class"] == "run"

        looked = await invoke(device_tools.device_look, run_ctx(), device="dev-1", tool="screen_look")
        assert isinstance(looked[0], ToolOutputText) and isinstance(looked[1], ToolOutputImage)
        assert looked[1].image_url.startswith("data:image/png;base64,")

        did = await invoke(device_tools.device_do, run_ctx(), device="dev-1", tool="screen_click",
                           arguments=json.dumps({"ref": "e7"}))
        assert did == "clicked e7"

        refused = await invoke(device_tools.device_do, run_ctx(), device="dev-1", tool="shell_run",
                               arguments='{"command": "ls"}')
        assert refused.startswith("❌ shell_run is a run tool: call it with device_run")

        assert (await invoke(device_tools.device_look, run_ctx(), device="dev-1", tool="screen_look",
                             arguments="[1]")).startswith("❌ arguments must be a JSON object")
        # Another user does not see it; another conversation waits its turn.
        bobs = json.loads(await invoke(device_tools.devices, run_ctx(user="bob")))
        assert bobs["devices"] == [] and "this server starts no browser machines" in bobs["hint"]
        busy = await invoke(device_tools.device_look, run_ctx(conversation="chat-b"), device="dev-1", tool="screen_look")
        assert "in use by another conversation" in busy
    finally:
        await end.close()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_device_tools_keep_to_the_users_devices_and_say_what_they_do():
    assert all(is_confined(device_tools.TOOL_ISOLATION[name]) for name in device_tools.DEVICE_TOOLS)
    assert set(device_tools.TOOL_ISOLATION.values()) == {DEVICES}
    effects = device_tools.TOOL_EFFECTS
    assert effects["device_look"].kind == tool_effects.READ and effects["device_look"].untrusted
    assert effects["device_do"].kind == tool_effects.EGRESS and effects["device_do"].untrusted
    assert effects["device_run"].kind == tool_effects.EXEC


# -- pairing -------------------------------------------------------------------


def test_a_code_pairs_one_device_once(tmp_path, monkeypatch):
    store = PairingStore(tmp_path / "devices.db")
    code, _ = store.new_code("alice")
    device, token = store.redeem(code, "Alice's laptop", "desktop")
    assert device.user_id == "alice" and device.name == "Alice's laptop"
    assert store.redeem(code, "again", "desktop") is None  # used
    assert store.verify(token).id == device.id
    assert store.verify("wrong") is None
    assert [d.id for d in store.devices("alice")] == [device.id] and store.devices("bob") == []

    assert not store.revoke("bob", device.id)
    assert store.revoke("alice", device.id)
    assert store.verify(token) is None and store.devices("alice") == []

    from core.devices import pairing

    expired, _ = store.new_code("alice")
    monkeypatch.setattr(pairing.time, "time", lambda: 10**12)
    assert store.redeem(expired, "late", "desktop") is None


def _app(tmp_path, hub, user="alice"):
    from web_chat.devices_api import register_device_routes

    app, api = FastAPI(), APIRouter()

    async def current_user():
        return SimpleNamespace(id=user)

    store = PairingStore(tmp_path / "devices.db")
    register_device_routes(app, api, current_user, hub=hub, machines=None, pairing=store)
    app.include_router(api)
    return app, store


def test_the_devices_api_pairs_lists_and_forgets(tmp_path):
    app, _ = _app(tmp_path, DeviceHub())
    client = TestClient(app)

    code = client.post("/api/devices/pairing").json()["code"]
    paired = client.post("/node/pair", json={"code": code, "name": "Laptop", "kind": "desktop"}).json()
    assert paired["device"].startswith("desktop-") and len(paired["token"]) > 30
    assert client.post("/node/pair", json={"code": code, "name": "x"}).status_code == 403

    listing = client.get("/api/devices").json()
    assert listing["paired"][0]["name"] == "Laptop" and listing["paired"][0]["online"] is False
    assert listing["machines"] is None and listing["devices"] == []
    assert client.post("/api/devices/machines").status_code == 404

    assert client.delete(f"/api/devices/paired/{paired['device']}").status_code == 200
    assert client.get("/api/devices").json()["paired"] == []


def test_a_device_without_a_valid_token_is_turned_away(tmp_path):
    from starlette.websockets import WebSocketDisconnect

    app, _ = _app(tmp_path, DeviceHub())
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/node/connect", headers={"Authorization": "Bearer nope"}) as ws:
            ws.receive_text()
    assert refused.value.code == 4401


# -- the machine pool --------------------------------------------------------


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakeDocker:
    """Containers whose node is a real one listening on loopback."""

    def __init__(self, port):
        self.port = port
        self.started, self.stopped = [], []
        self.servers = {}
        client = self

        class Containers:
            def run(self, image, **kwargs):
                token = kwargs["environment"]["NODE_TOKEN"]
                name = kwargs["name"]
                client.started.append(kwargs)
                node = Node(name=kwargs["environment"]["NODE_NAME"], kind="browser", drivers=[FakeScreen()])
                client.servers[name] = asyncio.run_coroutine_threadsafe(
                    listen(node, "127.0.0.1", client.port, token), client.loop
                )
                return SimpleNamespace(
                    id=name, reload=lambda: None,
                    attrs={"NetworkSettings": {"Networks": {machines_module.NETWORK: {"IPAddress": "127.0.0.1"}}}},
                )

            def get(self, container_id):
                return SimpleNamespace(stop=lambda timeout: client._stop(container_id))

            def list(self, filters):
                return []

        class Networks:
            def list(self, names):
                return ["exists"]

        self.containers, self.networks = Containers(), Networks()

    def _stop(self, name):
        self.stopped.append(name)
        self.servers.pop(name).cancel()


async def test_a_machine_starts_for_a_user_within_limits_and_stops(fresh_hub):
    docker = FakeDocker(_free_port())
    docker.loop = asyncio.get_running_loop()
    machines = MachinePool(fresh_hub, client=docker, limit=2, per_user=1, node_port=docker.port)

    device_id = await machines.start("alice")
    assert [d.id for d in fresh_hub.devices("alice")] == [device_id]
    run = docker.started[0]
    assert run["cap_drop"] == ["ALL"] and run["network"] == machines_module.NETWORK
    assert run["volumes"] == {"grid-browser-alice-0": {"bind": "/home/node/profile", "mode": "rw"}}
    assert machines.capacity("alice") == {"yours": [device_id], "you_may_start": 0}

    with pytest.raises(MachineError, match="the most one user may"):
        await machines.start("alice")
    with pytest.raises(MachineError, match="You have no browser machine"):
        await machines.stop("bob", device_id)

    await machines.stop("alice", device_id)
    for _ in range(100):
        if not fresh_hub.devices("alice"):
            break
        await asyncio.sleep(0.02)
    assert fresh_hub.devices("alice") == [] and docker.stopped == [f"grid-machine-{device_id}"]


async def test_an_idle_machine_is_stopped(fresh_hub):
    docker = FakeDocker(_free_port())
    docker.loop = asyncio.get_running_loop()
    machines = MachinePool(fresh_hub, client=docker, idle_seconds=0, node_port=docker.port)
    device_id = await machines.start("alice")
    await asyncio.sleep(0.01)
    assert await machines.reap() == [device_id]
    assert machines.machines("alice") == []


async def test_a_paired_computer_dials_in_and_its_tools_are_called(tmp_path):
    import httpx
    import uvicorn

    from core.devices.hub import LOOK
    from grid_node.node import dial

    hub = DeviceHub()
    app, store = _app(tmp_path, hub)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="auto"))
    serving = asyncio.create_task(server.serve())
    stop = asyncio.Event()
    dialing = None
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            code = (await client.post("/api/devices/pairing")).json()["code"]
            paired = (await client.post("/node/pair", json={"code": code, "name": "Laptop", "kind": "desktop"})).json()

        node = Node(name="Laptop", kind="desktop", drivers=[FakeScreen()])
        dialing = asyncio.create_task(dial(node, f"ws://127.0.0.1:{port}/node/connect", paired["token"], stop=stop))
        for _ in range(200):
            if hub.devices("alice"):
                break
            await asyncio.sleep(0.05)
        (device,) = hub.devices("alice")
        assert device.id == paired["device"] and device.origin == "paired"
        result = await hub.call("alice", device.id, "screen_look", {}, holder="chat", allowed=LOOK)
        assert result.content[0].text == "a page"
    finally:
        stop.set()
        if dialing is not None:
            for device in hub.devices("alice"):
                await device.tunnel.close("test over")
            dialing.cancel()
        server.should_exit = True
        await asyncio.gather(serving, *(t for t in [dialing] if t), return_exceptions=True)

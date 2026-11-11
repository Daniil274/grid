"""Agents' device tools, pairing a device of one's own, the devices API and the machine pool."""

import asyncio
import io
import json
import socket
import tarfile
import time
from contextlib import asynccontextmanager
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


class ConfirmingScreen(FakeScreen):
    """A device whose «Pay» needs a person's yes, as the browser's does."""

    def tools(self):
        from grid_node.node import NeedsConfirmation, ToolSpec, text

        async def pay(args):
            if not args.get("confirmed"):
                raise NeedsConfirmation("Press the button «Pay» on shop.example")
            self.ran.append("paid")
            return [text("paid")]

        schema = {"type": "object", "properties": {"confirmed": {"type": "boolean"}}}
        return [*super().tools(), ToolSpec("pay", "Pay", schema, pay)]


class Gate:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    async def ask_person(self, state, tool, kind, detail):
        self.asked.append((tool, kind, detail))
        return self.answer


def gated_ctx(gate):
    return SimpleNamespace(context=SimpleNamespace(
        user_id="alice", context_id="chat-a", action_state=object(), factory=SimpleNamespace(action_gate=gate),
    ))


@pytest.mark.parametrize("answer, outcome", [(True, "paid"), (False, "❌ The user declined"), (None, "they did not answer")])
async def test_a_step_with_consequences_waits_for_the_users_yes(fresh_hub, answer, outcome):
    driver = ConfirmingScreen()
    tasks, end = await connected(fresh_hub, driver)
    try:
        gate = Gate(answer)
        # The agent cannot confirm for the user: its own "confirmed" is dropped.
        out = await invoke(device_tools.device_do, gated_ctx(gate), device="dev-1", tool="pay",
                           arguments='{"confirmed": true}')
        assert outcome in out
        assert gate.asked == [("device_do", "external", "Press the button «Pay» on shop.example")]
        assert driver.ran == (["paid"] if answer else [])
    finally:
        await end.close()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_without_anyone_to_ask_the_step_is_not_taken(fresh_hub):
    driver = ConfirmingScreen()
    tasks, end = await connected(fresh_hub, driver)
    try:
        out = await invoke(device_tools.device_do, run_ctx(), device="dev-1", tool="pay")
        assert "no one can be asked here" in out and driver.ran == []
    finally:
        await end.close()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_runs_side_by_side_take_turns_but_a_run_inside_shares_its_callers_device(fresh_hub):
    from core.devices.hub import LOOK, DeviceError, holding

    tasks, end = await connected(fresh_hub, FakeScreen())
    look = lambda holder: fresh_hub.call("alice", "dev-1", "screen_look", {}, holder=holder, allowed=LOOK)
    try:
        await look("chat-a")  # the coordinator's turn uses it
        with holding("executor-1", default="chat-a") as first:
            assert first == "chat-a/executor-1"
            await look(first)  # its executor takes it over
            with holding("executor-2", default="chat-a"):
                pass
            with pytest.raises(DeviceError, match="in use by another"):
                await look("chat-a/executor-2")  # a sibling waits
        # The executor ended: its lease went with it.
        await look("chat-a/executor-2")
        fresh_hub.release("chat-a")
        await look("chat-b")
    finally:
        await end.close()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_the_single_user_server_starts_devices_and_browser_machines(tmp_path):
    """A one-user server keeps paired devices and runs machines, as one with accounts does."""
    import inspect

    from web_chat.__main__ import single_user_options
    from web_chat.server import WebChatServer

    logs = tmp_path / "logs"
    logs.mkdir()
    config = SimpleNamespace(
        get_working_directory=lambda: str(tmp_path / "work"),
        get_logs_directory=lambda: str(logs),
        get_absolute_path=lambda key: str(tmp_path / key),
    )
    deployment = SimpleNamespace(
        config=config, plans=SimpleNamespace(resolve=lambda **kwargs: None), review_policy=SimpleNamespace()
    )
    args = SimpleNamespace(
        user_id="default_user", browser_machines=2,
        host="127.0.0.1", port=8000, trusted_host=[], allowed_origin=[],
    )
    options = single_user_options(deployment, args)
    # The Devices page works here too: the one user pairs devices and runs machines.
    assert options["devices_dir"] == logs / "devices"
    assert options["browser_machines"] == 2
    # And the options are the server's own arguments, not stray keys.
    assert {"devices_dir", "browser_machines"} <= set(inspect.signature(WebChatServer.__init__).parameters)


def test_the_single_user_server_starts_devices_and_browser_machines(tmp_path):
    """A one-user server keeps paired devices and runs machines as a server with accounts does."""
    from web_chat.__main__ import single_user_options

    logs = tmp_path / "logs"
    logs.mkdir()
    config = SimpleNamespace(
        get_working_directory=lambda: str(tmp_path / "work"),
        get_logs_directory=lambda: str(logs),
        get_absolute_path=lambda key: str(tmp_path / key),
    )
    deployment = SimpleNamespace(config=config, plans=SimpleNamespace(resolve=lambda **kw: None),
                                 review_policy=SimpleNamespace())
    args = SimpleNamespace(
        user_id="default_user", browser_machines=2,
        host="127.0.0.1", port=8000, trusted_host=[], allowed_origin=[],
    )
    options = single_user_options(deployment, args)
    assert options["devices_dir"] == logs / "devices"
    assert options["browser_machines"] == 2

    # A server with accounts answers the same two things, out of its data directory.
    source = (tmp_path / "unused").parent  # keeps the import close to its use
    import inspect

    from web_chat import __main__ as entry

    accounts = inspect.getsource(entry.multi_user_options)
    assert '"devices_dir"' in accounts and '"browser_machines"' in accounts
    assert source.exists()


# -- folders, installers, binaries and tool packages --------------------------


@asynccontextmanager
async def served(app):
    """The app on a real port: the hub's device session belongs to this loop."""
    import httpx
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", ws="auto"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.02)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            yield client
    finally:
        server.should_exit = True
        await asyncio.gather(task, return_exceptions=True)


async def test_the_dirs_endpoint_lists_a_devices_folders_and_makes_one(tmp_path, fresh_hub):
    from tests.test_device_hub import FakeFiles

    files = FakeFiles()
    tasks, end = await connected(fresh_hub, files)
    try:
        app, _ = _app(tmp_path, fresh_hub)
        async with served(app) as client:
            listing = (await client.get("/api/devices/dev-1/dirs")).json()
            assert listing["device"] == "dev-1"
            # Nothing asked for: the node's own directory, and where it is.
            assert listing["path"] == "/home/alice" and listing["workdir"] == "/home/alice"
            assert [entry["path"] for entry in listing["dirs"]] == ["/home/alice/docs", "/home/alice/pics"]
            assert [entry["name"] for entry in listing["dirs"]] == ["docs", "pics"]
            assert listing["roots"] == ["/", "/home"]
            assert listing["bookmarks"] == ["/home/alice/work"]
            # Nothing is asked for: the node opens its own directory.
            assert files.calls[-1] == {}

            asked = (await client.get("/api/devices/dev-1/dirs", params={"path": "/home"})).json()
            assert asked["path"] == "/home" and files.calls[-1]["path"] == "/home"

            made = (await client.post(
                "/api/devices/dev-1/dirs", json={"path": "/home/alice", "mkdir": "/home/alice/new"}
            )).json()
            assert made["created"] == "/home/alice/new" and files.calls[-1]["mkdir"] == "/home/alice/new"
            assert "/home/alice/new" in [entry["path"] for entry in made["dirs"]]

            assert (await client.get("/api/devices/nobody/dirs")).status_code == 404

        # The server's own call holds no lease: an agent takes the device at once.
        await fresh_hub.call("alice", "dev-1", "fs_list_dirs", {}, holder="chat-a", allowed="look")
    finally:
        await end.close()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_a_device_without_fs_tools_still_says_where_it_works(tmp_path, fresh_hub):
    from tests.test_device_hub import WorkdirNode

    tasks, end = await connected(fresh_hub, FakeScreen(), node_class=WorkdirNode)
    try:
        app, _ = _app(tmp_path, fresh_hub)
        async with served(app) as client:
            listing = (await client.get("/api/devices/dev-1/dirs")).json()
            assert listing["path"] == "/home/alice/projects" and listing["workdir"] == "/home/alice/projects"
            assert listing["dirs"] == [] and "lists no directories" in listing["detail"]
    finally:
        await end.close()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_a_viewer_that_goes_away_gives_the_person_s_control_back(tmp_path):
    from core.devices.hub import Device

    class FakeTunnel:
        def __init__(self):
            self.sent = []

        async def send(self, channel, message):
            self.sent.append((channel, message))

        async def close(self, reason=""):
            pass

    hub = DeviceHub()
    # A device that is connected and watching, without a node behind it: this
    # test is about the view, not the tools (tests/test_device_hub.py has those).
    hub._devices["dev-1"] = Device(
        id="dev-1", user_id="alice", name="Laptop", kind="desktop", platform="linux-amd64",
        capabilities=[], view=True, tools={}, origin="paired", tunnel=FakeTunnel(), session=None,
        default_workdir="/home/alice",
    )
    app, _ = _app(tmp_path, hub)
    client = TestClient(app)

    with client.websocket_connect("/api/devices/dev-1/view"):
        # The page took control while its view is open.
        assert client.post("/api/devices/dev-1/control", json={"taken": True}).json()["person"] is True
        assert hub.get("alice", "dev-1").person is True
        assert hub.watchers("dev-1") == 1
    # The socket is closed: the person's hand goes with it, however the view ended.
    for _ in range(200):
        if not hub.get("alice", "dev-1").person:
            break
        time.sleep(0.01)
    assert hub.get("alice", "dev-1").person is False and hub.watchers("dev-1") == 0


def test_the_installers_name_this_server_and_pair_the_device(tmp_path):
    app, _ = _app(tmp_path, DeviceHub())
    client = TestClient(app)

    script = client.get("/node/install.sh").text
    assert "http://testserver" in script and 'URL="$SERVER/node/bin/$TARGET"' in script
    assert "grid-node-linux-amd64" in script and "grid-node-linux-arm64" in script
    # Termux is Android and gets its own build: it runs only PIE binaries.
    assert "grid-node-android-arm64" in script and "TERMUX_VERSION" in script
    assert "grid-node" in script and "install --server" in script and "--pair" in script
    assert "GRID_PAIR_CODE" in script

    with_code = client.get("/node/install.sh", params={"code": "ABCDE-FGHJK"}).text
    assert "ABCDE-FGHJK" in with_code
    # A code that is not one is never pasted into a script.
    evil = client.get("/node/install.sh", params={"code": 'x"; echo pwned; #'}).text
    assert "pwned" not in evil

    powershell = client.get("/node/install.ps1").text
    assert "http://testserver" in powershell and "grid-node-windows-amd64.exe" in powershell
    assert "install" in powershell and "--pair" in powershell
    assert "ABCDE-FGHJK" in client.get("/node/install.ps1", params={"code": "ABCDE-FGHJK"}).text


def test_a_compiled_node_binary_is_served_when_the_server_has_one(tmp_path, monkeypatch):
    from web_chat import devices_api

    monkeypatch.setattr(devices_api, "binaries_roots", lambda: [tmp_path / "dist" / "grid-node"])
    app, _ = _app(tmp_path, DeviceHub())
    client = TestClient(app)
    assert client.get("/node/bin/grid-node-linux-amd64").status_code == 404

    built = tmp_path / "dist" / "grid-node"
    built.mkdir(parents=True)
    (built / "grid-node-linux-amd64").write_bytes(b"\x7fELF a fake node")
    got = client.get("/node/bin/grid-node-linux-amd64")
    assert got.status_code == 200 and got.content == b"\x7fELF a fake node"
    assert client.get("/node/bin/grid-node-linux-arm64").status_code == 404
    # Only the names this server builds are handed out, never a path.
    assert client.get("/node/bin/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_a_tool_package_goes_to_a_paired_device_by_its_hash(tmp_path, monkeypatch):
    from web_chat import devices_api

    root = tmp_path / "repo"
    package = root / "examples" / "coder" / "tools" / "notes"
    package.mkdir(parents=True)
    (package / "tool.py").write_text("print('hi')\n")
    (package / "README.md").write_text("notes\n")
    monkeypatch.setattr(devices_api, "packages_root", lambda: root)

    app, _ = _app(tmp_path, DeviceHub())
    client = TestClient(app)
    assert devices_api.tool_package("notes") == package
    assert devices_api.tool_package("system") is None
    assert devices_api.tool_package("../etc") is None
    _, digest = devices_api.tool_package_archive(package)
    # The same tree always packs to the same bytes: the hash names the content.
    assert devices_api.tool_package_archive(package)[1] == digest

    url = f"/node/tools/notes/{digest}.tar.gz"
    assert client.get(url).status_code == 401  # a device's token is needed
    code = client.post("/api/devices/pairing").json()["code"]
    paired = client.post("/node/pair", json={"code": code, "name": "Laptop", "kind": "desktop"}).json()
    headers = {"Authorization": f"Bearer {paired['token']}"}

    answer = client.get(url, headers=headers)
    assert answer.status_code == 200 and answer.headers["x-package-sha256"] == digest
    archive = tarfile.open(fileobj=io.BytesIO(answer.content), mode="r:gz")
    assert archive.getnames() == ["notes/README.md", "notes/tool.py"]
    assert archive.extractfile("notes/tool.py").read() == b"print('hi')\n"

    assert client.get(f"/node/tools/notes/{digest[:12]}.tar.gz", headers=headers).status_code == 200
    assert client.get("/node/tools/notes/latest.tar.gz", headers=headers).status_code == 200
    assert client.get(f"/node/tools/notes/{'0' * 64}.tar.gz", headers=headers).status_code == 409
    assert client.get(f"/node/tools/missing/{digest}.tar.gz", headers=headers).status_code == 404


def test_a_paired_device_takes_a_runtime_and_its_sha(tmp_path, monkeypatch):
    """The runtime routes: a paired device downloads the archive and the sha
    it will be held against; no token, no runtime."""
    import hashlib

    monkeypatch.setenv("GRID_NODE_RUNTIME_DIR", str(tmp_path))
    archive = b"an interpreter"
    (tmp_path / "python-linux-amd64.tar.gz").write_bytes(archive)
    (tmp_path / "python-linux-amd64.tar.gz.sha256").write_text(
        hashlib.sha256(archive).hexdigest()
    )

    app, _ = _app(tmp_path / "store", DeviceHub())
    client = TestClient(app)
    code = client.post("/api/devices/pairing").json()["code"]
    token = client.post("/node/pair", json={"code": code, "name": "Laptop", "kind": "desktop"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    url = "/node/runtime/python-linux-amd64.tar.gz"

    assert client.get(url).status_code == 401
    assert client.get(url + ".sha256").status_code == 401
    assert client.get(url, headers=headers).content == archive
    assert client.get(url + ".sha256", headers=headers).text == hashlib.sha256(archive).hexdigest()
    assert client.get("/node/runtime/python-linux-arm64.tar.gz", headers=headers).status_code == 404
    assert client.get("/node/runtime/..%2Fescape.tar.gz", headers=headers).status_code == 404

"""End to end: a chat that works on a real computer, driven by the compiled grid-node.

Nothing here is a stand-in. The test starts a real Grid server (FastAPI, the
devices routes, a pairing store and a device hub), then runs the *compiled* Go
binary ``dist/grid-node/grid-node-linux-amd64`` in a folder of its own. The
binary pairs with a one-time code, keeps its token, dials the tunnel and serves
the MCP methods a device serves. From then on the test is the person at the
chat:

* the devices page's folder picker - ``GET`` and ``POST /api/devices/{id}/dirs``
  - lists the folder and creates one;
* the base tools keep their names and act on that computer: ``file_write``,
  ``file_edit``, ``file_append`` and ``file_read``; ``glob_tool`` and
  ``grep_tool`` search there; ``bash_tool`` runs a command there;
* a path that leaves the chosen working folder is refused by the node itself
  (``allow_external`` is not a tool's to claim), and the action policy routes
  the same path as an outside action before the tool ever runs;
* a tool package the device has never seen is fetched from
  ``/node/tools/{pkg}/{hash}.tar.gz`` the moment its function is called, and
  its function runs on the device.

The test needs the binary built for linux/amd64 (Wave 3); without it - another
platform, or a checkout that never ran the Go build - it is skipped, not failed.
"""

import asyncio
import json
import platform as host
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.action_policy import ActionGate
from core.action_routing import route_call
from core.devices import execution
from core.devices import hub as hub_module
from core.devices.hub import DeviceError, DeviceHub
from core.devices.pairing import PairingStore
from schemas.action_policy import ActionPolicyConfig
from tests.test_device_execution_target import Chat, bash_tool_module, file_tools, invoke, search_tools, working
from utils.tool_effects import write
from web_chat import devices_api

REPO = Path(__file__).resolve().parents[1]
BINARY = REPO / "dist" / "grid-node" / "grid-node-linux-amd64"

pytestmark = pytest.mark.skipif(
    not BINARY.is_file() or sys.platform != "linux" or host.machine() not in ("x86_64", "amd64"),
    reason="needs the compiled grid-node for linux/amd64 (dist/grid-node/grid-node-linux-amd64)",
)

#: How long the binary gets to pair and dial in: it is a real process, started cold.
CONNECT_SECONDS = 40.0

#: A tool package the device has never seen: it is fetched when its tool is called.
NOTES_PACKAGE = '''
from grid_tool import function_tool


@function_tool(read_only=True)
def first_line(path: str) -> dict:
    """Read the first line of a file of the workspace.

    Args:
        path: the file, relative to the workspace
    """
    with open(path, encoding="utf-8") as handle:
        line = handle.readline().rstrip("\\n")
    return {"line": line, "where": __import__("os").getcwd()}
'''


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _app(tmp_path: Path, hub: DeviceHub):
    """The server's devices routes: pairing, the tunnel and the downloads."""
    from fastapi import APIRouter, FastAPI

    app, api = FastAPI(), APIRouter()

    async def current_user():
        return SimpleNamespace(id="alice")

    store = PairingStore(tmp_path / "devices.db")
    devices_api.register_device_routes(app, api, current_user, hub=hub, machines=None, pairing=store)
    app.include_router(api)
    return app


async def _wait_for_device(hub: DeviceHub, process, log: Path, seconds: float = CONNECT_SECONDS) -> None:
    """Wait until the binary has paired and its tunnel is up; say why if it died."""
    for _ in range(int(seconds * 10)):
        if hub.devices("alice"):
            return
        if process.returncode is not None:
            break
        await asyncio.sleep(0.1)
    detail = log.read_text(encoding="utf-8", errors="replace")[-4000:] if log.exists() else "(no output)"
    raise AssertionError(f"grid-node did not connect in {seconds:.0f}s; its output:\n{detail}")


def _chat(device_id: str, workdir: Path) -> "Chat":
    """The conversation's factory: the chat chose *device_id* and its folder."""
    return Chat(execution.target_for(device_id), str(workdir), user="alice", conversation="ctx-1")


async def test_a_chat_works_end_to_end_on_a_real_grid_node(tmp_path, monkeypatch):
    import httpx
    import uvicorn

    workdir = tmp_path / "work"
    (workdir / "src").mkdir(parents=True)
    (workdir / "docs").mkdir()
    (workdir / "docs" / "readme.md").write_text("# Notes\nhello from the device\n", encoding="utf-8")
    # Outside the chosen folder: the node must not read it, the policy must ask.
    (tmp_path / "secret.txt").write_text("server secret\n", encoding="utf-8")

    packages = tmp_path / "packages"
    notes = packages / "tools" / "notes"
    notes.mkdir(parents=True)
    (notes / "notes.py").write_text(NOTES_PACKAGE, encoding="utf-8")

    hub = DeviceHub()
    monkeypatch.setattr(hub_module, "_hub", hub)
    monkeypatch.setattr(devices_api, "packages_root", lambda: packages)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(_app(tmp_path, hub), host="127.0.0.1", port=port, log_level="warning", ws="auto"))
    serving = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"
    log = tmp_path / "grid-node.log"
    process = None
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "the test server did not start"

        async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
            code = (await client.post("/api/devices/pairing")).json()["code"]

            # The real binary: it pairs with the code, keeps its token and dials in.
            with open(log, "wb") as output:
                process = await asyncio.create_subprocess_exec(
                    str(BINARY), "run",
                    "--server", base, "--pair", code,
                    "--workdir", str(workdir), "--state-dir", str(tmp_path / "state"),
                    "--driver", "fs,shell,tools",
                    stdout=output, stderr=asyncio.subprocess.STDOUT,
                )
            await _wait_for_device(hub, process, log)
            assert (tmp_path / "state" / "token.json").is_file(), "the binary kept no token"

            (device,) = hub.devices("alice")
            assert device.origin == "paired" and device.kind == "desktop"
            assert device.default_workdir == str(workdir) and "linux" in device.platform
            assert {"fs_read", "fs_write", "fs_edit", "fs_append", "fs_glob", "fs_grep",
                    "fs_list_dirs", "shell_exec", "tool_exec"} <= set(device.tools)

            # -- the folder picker the chat's workdir chip is built on ----------------
            listing = (await client.get(f"/api/devices/{device.id}/dirs", params={"path": str(workdir)})).json()
            assert listing["device"] == device.id and listing["workdir"] == str(workdir)
            assert {entry["name"] for entry in listing["dirs"]} == {"docs", "src"}
            assert listing["roots"], "the picker got no roots to jump to"
            assert listing["bookmarks"], "the picker got no bookmarks"

            made = (await client.post(f"/api/devices/{device.id}/dirs",
                                      json={"path": str(workdir), "mkdir": "reports"})).json()
            assert made["created"] == "reports" and made["device"] == device.id
            assert (workdir / "reports").is_dir(), "the folder was not created on the device"
            inside_it = (await client.get(f"/api/devices/{device.id}/dirs",
                                          params={"path": str(workdir / "reports")})).json()
            assert inside_it["dirs"] == [] and inside_it["path"] == str(workdir / "reports")

        factory = _chat(device.id, workdir)
        with working(factory):
            # -- the file tools, on the device ---------------------------------------
            written = await invoke(file_tools.file_write, filepath="src/app.py",
                                   content="import os\nprint('hello')\n", overwrite=True)
            assert written.startswith("📝") and "src/app.py" in written
            assert (workdir / "src" / "app.py").read_text(encoding="utf-8") == "import os\nprint('hello')\n"

            read_back = await invoke(file_tools.file_read, filepath="src/app.py")
            assert read_back.startswith("📄 src/app.py") and "print('hello')" in read_back

            appended = await invoke(file_tools.file_append, filepath="src/run.log", content="started\n")
            assert appended.startswith("✅") and "src/run.log" in appended
            assert (workdir / "src" / "run.log").read_text(encoding="utf-8") == "started\n"

            edited = await invoke(
                file_tools.file_edit, filepath="src/app.py",
                patch_content=(
                    "--- a/src/app.py\n"
                    "+++ b/src/app.py\n"
                    "@@ -1,2 +1,2 @@\n"
                    " import os\n"
                    "-print('hello')\n"
                    "+print('edited on the device')\n"
                ),
            )
            assert edited.startswith("✅") and "src/app.py" in edited
            assert "edited on the device" in (workdir / "src" / "app.py").read_text(encoding="utf-8")

            # -- search, on the device ----------------------------------------------
            found = await invoke(search_tools.glob_tool, pattern="**/*.py", directory=".")
            assert "src/app.py" in found
            matched = await invoke(search_tools.grep_tool, pattern="edited on the device", directory="src")
            assert "app.py" in matched and "edited on the device" in matched

            # -- a command, on the device -------------------------------------------
            ran = await invoke(bash_tool_module.bash_tool, command="pwd && cat src/run.log")
            assert str(workdir) in ran and "started" in ran

            # -- a package the device has never seen, fetched by its hash -----------
            from core.tool_packages import archive_digest, build_package_function_tool

            tool, _effect = build_package_function_tool(
                config_dir=packages, package_rel="tools/notes", tool_key="first_line"
            )
            assert tool is not None
            answer = await tool.on_invoke_tool(
                SimpleNamespace(context=SimpleNamespace(factory=factory)), json.dumps({"path": "src/app.py"})
            )
            # It ran on the device: its working directory is the chat's folder there.
            payload = json.loads(answer)
            assert payload["line"] == "import os"
            assert Path(payload["where"]).resolve() == workdir.resolve()
            assert (tmp_path / "state" / "tools" / archive_digest(notes) / ".grid-ready").is_file()

        # -- the folder's edge -------------------------------------------------------
        # The node itself keeps to the folder: a call that claims no freedom of path
        # is refused, so a path outside it never reaches the disk.
        with pytest.raises(DeviceError) as refused:
            outward = await hub.system_call(
                "alice", device.id, "fs_read",
                {"workdir": str(workdir), "filepath": "../secret.txt"}, holder="ctx-1",
            )
            execution.as_text(outward)
        assert "outside working directory" in str(refused.value)
        # Inside the folder the same call reads the file it names.
        inside_call = await hub.system_call(
            "alice", device.id, "fs_read",
            {"workdir": str(workdir), "filepath": "src/app.py"}, holder="ctx-1",
        )
        assert "edited on the device" in execution.as_text(inside_call)

        # The action policy judges the same paths by the device's folder.
        locate = ActionGate._locator(SimpleNamespace(factory=factory))
        assert locate("src/app.py") == "src/app.py"
        assert locate("../secret.txt") is None
        policy = ActionPolicyConfig(mode="enforce")
        _name, selected = policy.filter(None)
        inside = route_call(policy, selected, write("filepath"), {"filepath": "src/app.py"}, (), locate)
        outside = route_call(policy, selected, write("filepath"), {"filepath": "../secret.txt"}, (), locate)
        assert inside.effect == "write"
        assert outside.effect == "external" and "outside the working folder" in outside.reasons
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:  # pragma: no cover - a stuck process
                process.kill()
                await process.wait()
        for device in hub.devices("alice"):
            await device.tunnel.close("test over")
        server.should_exit = True
        await asyncio.gather(serving, return_exceptions=True)

"""The beads tools are coroutines on the server's loop: bd must not block it."""

import asyncio
import time

from core import tracker_gateway
from tools import beads_tools


async def test_a_slow_bd_command_leaves_the_event_loop_free(monkeypatch):
    def slow_bd(args, **kwargs):
        time.sleep(0.3)
        return {"success": True, "output": "", "error": "", "data": [], "exit_code": 0}

    monkeypatch.setattr(beads_tools, "_run_bd_command", slow_bd)
    ticks = 0

    async def other_users():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(other_users())
    try:
        await beads_tools._bd(["list"])
    finally:
        ticker.cancel()

    assert ticks >= 10


def test_bd_in_a_container_is_ended_there_too(monkeypatch, tmp_path):
    calls = []

    class Done:
        returncode = 0

        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.kwargs = kwargs

        def communicate(self, timeout=None):
            calls.append((self.cmd, timeout))
            return "[]", ""

    # The gateway starts bd with Popen (a timed-out command is stopped by its
    # process group, which run() cannot do); the probe is off here so exactly
    # one command is asked for.
    monkeypatch.setenv("GRID_TRACKER_GATE_DIR", str(tmp_path / "gate"))
    monkeypatch.setattr(beads_tools.subprocess, "Popen", Done)
    monkeypatch.setattr(tracker_gateway, "probe_version", lambda backend: None)
    monkeypatch.setattr(beads_tools, "_find_bd", lambda container_id: "bd")
    monkeypatch.setattr(beads_tools, "_map_path_to_container", lambda cwd, context: "/workspace")

    beads_tools._run_bd_command(["list"], cwd=str(tmp_path), container_id="c0ffee")

    [(cmd, timeout)] = calls
    assert cmd[:7] == ["docker", "exec", "-w", "/workspace", "-e", "BEADS_DAEMON=0", "c0ffee"]
    assert cmd[7:11] == ["timeout", "-k", "5", str(beads_tools.BD_TIMEOUT_SECONDS)]
    assert timeout > beads_tools.BD_TIMEOUT_SECONDS


def test_init_adds_only_the_tracker_to_the_users_project():
    # bd's defaults write AGENTS.md, CLAUDE.md, editor hooks and git hooks into
    # the project and stage its files in the user's git index.
    args = beads_tools._init_args("/home/me/geocrs")

    assert args[0] == "init"
    assert {"--skip-agents", "--skip-hooks", "--setup-exclude", "--init-if-missing"} <= set(args)
    # In a container bd would name the issues after the mount point, "workspace".
    assert args[args.index("--prefix") + 1] == "geocrs"

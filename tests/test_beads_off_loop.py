"""The beads tools are coroutines on the server's loop: bd must not block it."""

import asyncio
import time

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


def test_bd_in_a_container_is_ended_there_too(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs["timeout"]))
        return type("Done", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()

    monkeypatch.setattr(beads_tools.subprocess, "run", run)
    monkeypatch.setattr(beads_tools, "_find_bd", lambda container_id: "bd")
    monkeypatch.setattr(beads_tools, "_map_path_to_container", lambda cwd, context: "/workspace")

    beads_tools._run_bd_command(["list"], container_id="c0ffee")

    [(cmd, timeout)] = calls
    assert cmd[:7] == ["docker", "exec", "-w", "/workspace", "-e", "BEADS_DAEMON=0", "c0ffee"]
    assert cmd[7:11] == ["timeout", "-k", "5", str(beads_tools.BD_TIMEOUT_SECONDS)]
    assert timeout > beads_tools.BD_TIMEOUT_SECONDS

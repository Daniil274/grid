"""Commands on the host end with everything they started; containers share one cgroup."""

import os
import subprocess
import sys
import time

import pytest

from utils import process_limits
from utils.process_limits import run_host_command, sandbox_run_args

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="process groups of POSIX")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie is dead too: it waits only for its parent to read its status.
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().split(") ", 1)[1][0] != "Z"
    except OSError:
        return True


@posix_only
def test_a_timeout_kills_what_the_shell_started_not_only_the_shell(tmp_path):
    pidfile = tmp_path / "pid"
    # The busy loop is a grandchild: subprocess.run(shell=True) used to kill
    # the shell alone, and the loop kept a core busy for good.
    command = f"sh -c 'echo $$ > {pidfile}; while :; do :; done' & wait"

    with pytest.raises(subprocess.TimeoutExpired):
        run_host_command(command, shell=True, timeout=1)

    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


@posix_only
def test_a_command_runs_below_the_servers_priority():
    result = run_host_command(["sh", "-c", "cut -d' ' -f19 /proc/self/stat"], timeout=10)
    assert result.returncode == 0
    assert int(result.stdout.strip()) >= process_limits.HOST_NICENESS


def test_output_and_input_pass_through():
    result = run_host_command([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input="hi", timeout=30)
    assert result.stdout.strip() == "HI" and result.returncode == 0


def test_sandboxes_go_under_the_machines_cgroup_without_swap(monkeypatch):
    monkeypatch.setenv("GRID_CGROUP_PARENT", "grid-users.slice")
    args = sandbox_run_args("512m", 1)
    assert args[args.index("--memory-swap") + 1] == "512m"
    assert args[args.index("--cgroup-parent") + 1] == "grid-users.slice"
    assert "OMP_NUM_THREADS=1" in args

    monkeypatch.delenv("GRID_CGROUP_PARENT")
    assert "--cgroup-parent" not in sandbox_run_args("512m", 1)


@posix_only
def test_a_missing_program_is_reported_as_missing_not_as_an_exit_code():
    with pytest.raises(FileNotFoundError):
        run_host_command(["grid-no-such-program"], timeout=10)

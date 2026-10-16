"""How much of the machine the agents' processes may take.

On a server with accounts every user's commands run in that user's container.
Each container has limits of its own (isolation.memory, cpus, pids_limit),
but limits per container do not add up to a limit for the machine: three
users at 2 CPUs each take all six cores, and the server - every user's chat -
waits behind them. So every container Grid starts - a user's, a package test
sandbox, an evaluation sandbox - goes under one cgroup, named by
:data:`CGROUP_PARENT_ENV` (``grid-users.slice`` in deploy/): its limits are
the ceiling of all of them together, while the server stays outside it
(deploy/grid-server.slice gives it the larger CPU weight). Inside it, equal
CPU weights share the cores fairly among the users who need them.

A container may not swap - a swapping container slows the whole host - and
the kernel's OOM killer takes a container's process before the server's.

Without a container, :func:`run_host_command` runs a command on this machine:
in a process group of its own, so a timeout ends everything it started, not
only its shell; and at a lower priority than the server.
"""

from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import sys
from typing import Dict, List, Mapping, Optional, Sequence, Union

#: The cgroup every container of Grid is started in, e.g. "grid-users.slice"
#: (Docker's systemd cgroup driver takes a slice name, cgroupfs a path).
CGROUP_PARENT_ENV = "GRID_CGROUP_PARENT"

#: Above the server's 0: under memory pressure the kernel kills in containers first.
CONTAINER_OOM_SCORE_ADJ = 500

#: Niceness of commands run on the host: the server answers first.
HOST_NICENESS = 10

#: Variables that tell common runtimes how many threads to start. Inside a
#: container the programs still see every core of the host, start a thread for
#: each, and are throttled to the container's CPUs - slower than fewer threads.
_THREAD_VARIABLES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "RAYON_NUM_THREADS",
    "GOMAXPROCS",
)


def cgroup_parent(configured: Optional[str] = None) -> Optional[str]:
    """The cgroup for Grid's containers: the environment's, else *configured*."""
    value = os.environ.get(CGROUP_PARENT_ENV, "").strip() or (configured or "").strip()
    return value or None


def thread_environment(cpus: float) -> Dict[str, str]:
    """Thread counts for programs in a container of *cpus* CPUs."""
    threads = str(max(1, math.ceil(cpus)))
    env = {name: threads for name in _THREAD_VARIABLES}
    env["MAKEFLAGS"] = f"-j{threads}"
    return env


def sandbox_run_args(memory: str, cpus: float) -> List[str]:
    """``docker run`` options every throwaway sandbox gets besides its limits."""
    args = ["--memory-swap", memory, "--oom-score-adj", str(CONTAINER_OOM_SCORE_ADJ)]
    parent = cgroup_parent()
    if parent:
        args += ["--cgroup-parent", parent]
    for name, value in thread_environment(cpus).items():
        args += ["-e", f"{name}={value}"]
    return args


def _lowered(args: List[str]) -> List[str]:
    nice = shutil.which("nice")
    if not nice:
        return args
    # Under nice a missing program is nice's exit code 127; callers tell a
    # missing program by FileNotFoundError, as without it.
    if not (os.sep in args[0] or shutil.which(args[0])):
        raise FileNotFoundError(f"No such program: {args[0]}")
    return [nice, "-n", str(HOST_NICENESS), *args]


def _kill_group(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        # taskkill /T ends the whole tree of the command.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


def run_host_command(
    command: Union[str, Sequence[str]],
    *,
    timeout: float,
    cwd: Optional[str] = None,
    shell: bool = False,
    env: Optional[Mapping[str, str]] = None,
    input: Optional[str] = None,
    encoding: str = "utf-8",
) -> subprocess.CompletedProcess:
    """Run *command* on this machine and wait at most *timeout* seconds.

    Like ``subprocess.run(..., capture_output=True, text=True)``, except that
    on a timeout - or when the caller is interrupted - the whole process group
    is killed: ``subprocess.run`` kills only the shell, and whatever the shell
    started went on with the CPU. Raises ``subprocess.TimeoutExpired``.
    """
    if shell and sys.platform != "win32":
        args: Union[str, List[str]] = _lowered(["/bin/sh", "-c", str(command)])
        shell = False
    elif shell:
        args = str(command)
    else:
        args = _lowered(list(command)) if sys.platform != "win32" else list(command)

    options: Dict[str, object] = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        options["start_new_session"] = True

    process = subprocess.Popen(
        args,
        shell=shell,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding=encoding,
        errors="replace",
        **options,
    )
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        # What it wrote is not needed; a child that escaped the group could
        # keep the pipes open, so the wait for them is bounded too.
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise subprocess.TimeoutExpired(args, timeout) from None
    except BaseException:
        _kill_group(process)
        process.wait()
        raise
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)

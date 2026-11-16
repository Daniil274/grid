"""The tracker gateway: one physical database, one backend, one command at a time.

These tests are isolated: every one keeps its gate files and trackers in a
temporary directory, starts no docker and touches no real tracker.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext

from core import tracker_gateway as gateway
from tools import beads_tools

REPO = Path(gateway.__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_gate(monkeypatch, tmp_path):
    """Own gate directory, no version probing, no inherited compatibility state."""
    monkeypatch.setenv("GRID_TRACKER_GATE_DIR", str(tmp_path / "gate"))
    for name in (
        "GRID_TRACKER_STRICT_BACKEND",
        "GRID_TRACKER_ALLOW_BACKEND_MISMATCH",
        "BEADS_DIR",
        "BEADS_DOLT_DATA_DIR",
        "BEADS_DOLT_SERVER_MODE",
        "BEADS_DOLT_SHARED_SERVER",
        "BEADS_SHARED_SERVER_DIR",
        "BEADS_PROXIED_SERVER_ROOT_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gateway, "probe_version", lambda backend: None)
    gateway.forget_version()
    gateway._compat_warned.clear()
    yield
    gateway.forget_version()
    gateway._compat_warned.clear()


def tracker(directory: Path, metadata: dict = None) -> Path:
    """A directory that looks like an initialized tracker.

    ``.beads/metadata.json`` is what bd writes on ``init`` and what makes a
    ``.beads`` tree a workspace for bd itself (``hasBeadsProjectFiles``).
    """
    directory.mkdir(parents=True, exist_ok=True)
    beads = directory / ".beads"
    beads.mkdir(exist_ok=True)
    if metadata is None:
        metadata = {"database": "beads.db", "backend": "dolt"}
    (beads / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return directory


def run_gateway(argv, directory, **kwargs):
    return gateway.execute(
        argv,
        directory=str(directory),
        backend=gateway.Backend.host(sys.executable),
        **kwargs,
    )


# --------------------------------------------------------------------------
# Physical storage identity
# --------------------------------------------------------------------------


def test_a_symlinked_directory_is_the_same_tracker(tmp_path):
    real = tracker(tmp_path / "real")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    assert gateway.storage_identity(real).key == gateway.storage_identity(alias).key
    # The label still names the directory the caller asked about.
    assert gateway.storage_identity(alias).directory == real.resolve()


def test_a_symlinked_beads_tree_is_the_same_database(tmp_path):
    shared = tracker(tmp_path / "shared")
    first = tmp_path / "one"
    second = tmp_path / "two"
    for directory in (first, second):
        directory.mkdir()
        (directory / ".beads").symlink_to(shared / ".beads", target_is_directory=True)

    identities = [gateway.storage_identity(path) for path in (shared, first, second)]
    assert len({identity.key for identity in identities}) == 1


def test_two_trackers_are_two_locks(tmp_path):
    one = gateway.storage_identity(tracker(tmp_path / "one"))
    two = gateway.storage_identity(tracker(tmp_path / "two"))
    assert one.key != two.key
    assert gateway.lock_path(one) != gateway.lock_path(two)


def test_the_identity_of_a_directory_survives_bd_init(tmp_path):
    before = gateway.storage_identity(tmp_path)
    assert not before.tracker_exists
    tracker(tmp_path)
    after = gateway.storage_identity(tmp_path)
    assert after.tracker_exists and after.key == before.key


@pytest.mark.parametrize(
    "given",
    ["relative/dir", "", "   "],
)
def test_a_scope_that_cannot_bind_a_tracker_is_refused(tmp_path, given):
    with pytest.raises(gateway.TrackerError) as caught:
        gateway.storage_identity(given)
    assert caught.value.code == "tracker_scope"


def test_a_file_is_not_a_tracker_scope(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(gateway.TrackerError) as caught:
        gateway.storage_identity(path)
    assert caught.value.code == "tracker_scope"


# --------------------------------------------------------------------------
# Cross-process serialization
# --------------------------------------------------------------------------

_WORKER = """
import sys, time
from pathlib import Path
from core import tracker_gateway

database = Path(sys.argv[1])
log = Path(sys.argv[2])
hold_seconds = float(sys.argv[3])
start = Path(sys.argv[4])
# Both workers leave the gate together, so an ungoverned database would be
# opened by two processes at once instead of by a lucky scheduling order.
while not start.exists():
    time.sleep(0.01)
identity = tracker_gateway.storage_identity(database)
with log.open("a", encoding="utf-8") as stream:
    with tracker_gateway.hold(identity, timeout=20):
        stream.write("enter\\n")
        stream.flush()
        time.sleep(hold_seconds)
        stream.write("exit\\n")
        stream.flush()
"""


def _worker(database: Path, log: Path, start: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(database), str(log), "0.3", str(start)],
        cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": str(REPO)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_two_processes_take_turns_on_one_physical_database(tmp_path):
    database = tracker(tmp_path / "db")
    log = tmp_path / "turns.log"
    start = tmp_path / "go"

    workers = [_worker(database, log, start), _worker(database, log, start)]
    time.sleep(0.4)  # both are waiting at the gate
    start.write_text("go", encoding="utf-8")
    for worker in workers:
        _, error = worker.communicate(timeout=60)
        assert worker.returncode == 0, error

    # Only the interleaving enter/enter/... would be a shared database opened
    # twice; with the gate each process finishes before the next enters.
    assert log.read_text(encoding="utf-8").split() == ["enter", "exit", "enter", "exit"]


def test_two_trackers_do_not_wait_for_each_other(tmp_path):
    one = gateway.storage_identity(tracker(tmp_path / "one"))
    two = gateway.storage_identity(tracker(tmp_path / "two"))

    started = time.monotonic()
    with gateway.hold(one, timeout=5):
        with gateway.hold(two, timeout=5):
            pass
    assert time.monotonic() - started < 1.0


def test_a_second_caller_in_the_same_process_is_told_the_tracker_is_busy(tmp_path):
    identity = gateway.storage_identity(tracker(tmp_path / "db"))
    with gateway.hold(identity, timeout=5):
        with pytest.raises(gateway.TrackerError) as caught:
            with gateway.hold(identity, timeout=0.2):
                pass
    assert caught.value.code == "tracker_busy" and caught.value.retryable


def test_a_gate_file_is_never_removed(tmp_path):
    identity = gateway.storage_identity(tracker(tmp_path / "db"))
    with gateway.hold(identity, timeout=5):
        pass
    path = gateway.lock_path(identity)
    assert path.is_file()
    with gateway.hold(identity, timeout=5):
        pass
    assert path.is_file()


# --------------------------------------------------------------------------
# Backend, version and its record
# --------------------------------------------------------------------------


def test_the_host_and_the_image_running_one_bd_are_not_a_mismatch(monkeypatch, tmp_path):
    # The UI runs the host's bd, the agents the image's: same physical database,
    # same version, serialized here. That is the arrangement, not a problem.
    identity = gateway.storage_identity(tracker(tmp_path / "db"))
    host = gateway.Backend.host("/usr/local/bin/bd")
    in_image = gateway.Backend.container("c0ffee")
    gateway.check_backend_compatibility(identity, host, "1.3.0")
    monkeypatch.setenv("GRID_TRACKER_STRICT_BACKEND", "1")
    gateway.check_backend_compatibility(identity, in_image, "1.3.0")

    record = json.loads(
        (gateway.gate_directory() / f"{identity.key}.backend.json").read_text(encoding="utf-8")
    )
    assert record["backend"] == host.label  # the first one stays recorded


def test_a_different_bd_version_on_the_same_database_is_refused_by_default(tmp_path):
    # Two builds of one single-process embedded store taking turns is the
    # uncontrolled case; bd migrates the schema on a version change, so "warn
    # and go on" is not the safe default.
    identity = gateway.storage_identity(tracker(tmp_path / "db"))
    same_backend = gateway.Backend.host("/usr/local/bin/bd")
    gateway.check_backend_compatibility(identity, same_backend, "1.3.0")

    with pytest.raises(gateway.TrackerError) as caught:
        gateway.check_backend_compatibility(identity, same_backend, "1.2.0")
    assert caught.value.code == "tracker_backend"
    assert "1.3.0" in str(caught.value) and "1.2.0" in str(caught.value)
    # The refusal names the override instead of pretending a policy accident.
    assert "GRID_TRACKER_ALLOW_BACKEND_MISMATCH" in str(caught.value)


def test_the_operator_can_accept_a_mismatch_explicitly(monkeypatch, tmp_path):
    identity = gateway.storage_identity(tracker(tmp_path / "db"))
    same_backend = gateway.Backend.host("/usr/local/bin/bd")
    gateway.check_backend_compatibility(identity, same_backend, "1.3.0")

    monkeypatch.setenv("GRID_TRACKER_ALLOW_BACKEND_MISMATCH", "1")
    gateway.check_backend_compatibility(identity, same_backend, "1.2.0")  # no raise
    # ... and the opt-out never becomes an upgrade: the first version stays.
    record = json.loads(
        (gateway.gate_directory() / f"{identity.key}.backend.json").read_text(encoding="utf-8")
    )
    assert record["version"] == "1.3.0"


def test_a_version_that_cannot_be_read_is_reported_not_refused(monkeypatch, tmp_path):
    # The probe is best effort: a slow daemon must not take the tracker down,
    # and "unverifiable" must never be reported as "too old".
    identity = gateway.storage_identity(tracker(tmp_path / "db"))
    backend = gateway.Backend.host("/usr/local/bin/bd")
    gateway.check_backend_compatibility(identity, backend, "1.3.0")

    gateway.check_backend_compatibility(identity, backend, None)  # no raise
    assert gateway._compat_warned, "the unverifiable backend must still be reported"


def test_an_old_bd_is_refused_and_the_reason_is_the_version(tmp_path):
    identity = gateway.storage_identity(tracker(tmp_path / "db"))
    backend = gateway.Backend.host("/usr/local/bin/bd")
    with pytest.raises(gateway.TrackerError) as caught:
        gateway.check_backend_compatibility(identity, backend, "0.9.0")
    assert "older than the supported" in str(caught.value)


def test_the_same_version_spelled_differently_is_not_a_mismatch(tmp_path):
    identity = gateway.storage_identity(tracker(tmp_path / "db"))
    backend = gateway.Backend.host("/usr/local/bin/bd")
    gateway.check_backend_compatibility(identity, backend, "v1.3.0")
    gateway.check_backend_compatibility(identity, backend, "1.3.0+locally-built")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("bd version 1.3.0 (f45b249ce: HEAD@f45b249ce6b4)", (1, 3, 0)),
        ("1.3", None),
        ("", None),
        (None, None),
    ],
)
def test_versions_are_read_conservatively(text, expected):
    assert gateway.parse_version(text) == expected


def test_the_record_is_kept_beside_the_lock_not_in_the_tracker(tmp_path):
    root = tracker(tmp_path / "db")
    identity = gateway.storage_identity(root)
    gateway.check_backend_compatibility(identity, gateway.Backend.host("/x/bd"), "1.3.0")

    record = gateway.gate_directory() / f"{identity.key}.backend.json"
    assert json.loads(record.read_text(encoding="utf-8"))["version"] == "1.3.0"
    # Nothing of the gateway's own state is written into the user's tracker:
    # metadata.json is bd's own file, the record and the lock are elsewhere.
    assert sorted(path.name for path in (root / ".beads").iterdir()) == ["metadata.json"]


# --------------------------------------------------------------------------
# Execution: one command, addressable stop, nothing global
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="process groups and /proc are POSIX")
def test_a_hung_command_and_its_children_are_stopped_and_nothing_else(tmp_path):
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    try:
        result = run_gateway(["-c", script, str(pid_file)], tmp_path, timeout=0.5)
        assert result.timed_out and not result.success
        assert "timed out" in result.error

        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while not _dead(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _dead(child_pid), "the child of the timed-out command is still running"
        # A process outside the command's session is untouched: no pkill.
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait(timeout=5)


def _dead(pid: int) -> bool:
    """Whether *pid* is gone (a zombie counts as stopped)."""
    stat = Path(f"/proc/{pid}/stat")
    try:
        return stat.read_text(encoding="utf-8").split()[2] == "Z"
    except (OSError, IndexError):
        return not Path(f"/proc/{pid}").exists()


def test_nothing_in_the_gateway_or_the_tools_kills_by_pattern():
    # A gate that ends other people's processes is not a gate. Processes are
    # stopped by their own group/name (see tracker_gateway._stop_process_group).
    for source in (Path(gateway.__file__), REPO / "tools" / "beads_tools.py"):
        text = source.read_text(encoding="utf-8")
        assert "pkill" not in text and "killall" not in text


def test_a_temporary_container_gets_a_unique_name_and_is_killed_by_it(monkeypatch, tmp_path):
    command, _, name = gateway.build_command(
        ["list"],
        backend=gateway.Backend.docker("grid-agent:latest"),
        directory=str(tmp_path),
    )
    assert name and name.startswith("grid-bd-")
    assert command[command.index("--name") + 1] == name
    assert "--rm" in command

    killed = []
    monkeypatch.setattr(gateway, "_stop_container", killed.append)

    class Hung:
        pid = os.getpid()  # the stop guard must not signal this process
        stdout = None
        stderr = None

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(cmd="docker", timeout=timeout)
            return "", ""

        def send_signal(self, sig):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(gateway.subprocess, "Popen", lambda *a, **k: Hung())
    result = gateway._run_process(
        ["docker", "run", name], cwd=None, env=None, timeout=0.1, container_name=name
    )
    assert result.timed_out and killed == [name]


def test_a_temporary_container_writes_as_whoever_owns_the_project(monkeypatch, tmp_path):
    # A root server: the container writes as the project's owner, not as root.
    monkeypatch.setattr(gateway.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        gateway.os, "stat", lambda path: SimpleNamespace(st_uid=1234, st_gid=5678)
    )
    assert gateway._docker_user(str(tmp_path)) == "1234:5678"

    # A root-owned directory keeps Docker's default: never "run as root" by guess.
    monkeypatch.setattr(gateway.os, "stat", lambda path: SimpleNamespace(st_uid=0, st_gid=0))
    assert gateway._docker_user(str(tmp_path)) is None

    # A non-root server whose directory it owns: its own uid, so files stay its own.
    monkeypatch.setattr(gateway.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(gateway.os, "getegid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        gateway.os, "stat", lambda path: SimpleNamespace(st_uid=1000, st_gid=1000)
    )
    assert gateway._docker_user(str(tmp_path)) == "1000:1000"

    # ... but not a directory that belongs to somebody else.
    monkeypatch.setattr(
        gateway.os, "stat", lambda path: SimpleNamespace(st_uid=1234, st_gid=5678)
    )
    assert gateway._docker_user(str(tmp_path)) is None


# --------------------------------------------------------------------------
# The common path: agent tools and the UI go through the same gateway
# --------------------------------------------------------------------------


def _context(factory=None, container_id=None):
    return ToolContext(
        context=SimpleNamespace(factory=factory, container_id=container_id),
        tool_name="t", tool_call_id="c1", tool_arguments="{}",
    )


@pytest.mark.asyncio
async def test_the_agent_tool_and_the_ui_ask_the_gateway_for_the_same_database(
    monkeypatch, tmp_path
):
    root = tracker(tmp_path)
    seen = []

    def fake_execute(argv, **kwargs):
        seen.append((list(argv), kwargs))
        return gateway.ExecutionResult(True, "[]", "", 0)

    monkeypatch.setattr(beads_tools.tracker_gateway, "execute", fake_execute)
    monkeypatch.chdir(root)

    result = await beads_tools.beads_ready.on_invoke_tool(_context(), json.dumps({}))
    assert result == "[]"
    ui = await beads_tools.run_bd(["list"], cwd=str(root))
    assert ui["success"] and ui["data"] == []

    assert len(seen) == 2
    tool_call, ui_call = seen
    assert tool_call[0] == ["ready", "--json"]
    assert ui_call[0] == ["list", "--json"]
    assert tool_call[1]["directory"] == ui_call[1]["directory"] == str(root)
    assert tool_call[1]["backend"].kind == ui_call[1]["backend"].kind == "host"


def test_the_resolver_falls_back_to_the_bound_factory(monkeypatch, tmp_path):
    import utils.path_utils

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = tracker(tmp_path / "shared")
    factory = SimpleNamespace(
        container_id=None,
        tracker_directory=str(shared),
        config=SimpleNamespace(get_working_directory=lambda: str(workspace)),
    )
    # The context lost its factory (a dynamic agent built from init_tools).
    context = SimpleNamespace(context=SimpleNamespace(factory=None, container_id=None))

    with utils.path_utils.factory_path_context(factory):
        assert beads_tools._resolve_directory(context, ".") == str(shared)
        assert beads_tools._resolve_directory(context, "sub") == str(workspace / "sub")
        with pytest.raises(ValueError):
            beads_tools._resolve_directory(context, "../outside")


def test_a_directory_outside_the_run_is_refused_not_rebound(monkeypatch, tmp_path):
    root = tracker(tmp_path / "root")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    called = []

    def fake_execute(*args, **kwargs):  # pragma: no cover - must not run
        called.append(args)
        return gateway.ExecutionResult(True, "", "", 0)

    monkeypatch.setattr(beads_tools.tracker_gateway, "execute", fake_execute)
    factory = SimpleNamespace(
        container_id="c0ffee",
        config=SimpleNamespace(get_working_directory=lambda: str(root)),
    )
    context = SimpleNamespace(context=SimpleNamespace(factory=factory, container_id="c0ffee"))

    result = beads_tools._run_bd_command(
        ["list"], cwd=str(outside), container_id="c0ffee", context=context
    )

    assert result["success"] is False
    assert "workspace" in (result["error"] or "").lower()
    assert called == []


# --------------------------------------------------------------------------
# Identity: the tracker bd actually opens
# --------------------------------------------------------------------------


def test_a_subdirectory_of_a_project_is_the_same_tracker(tmp_path):
    root = tracker(tmp_path / "project")
    (root / "sub" / "deeper").mkdir(parents=True)
    assert gateway.storage_identity(root / "sub" / "deeper").key == gateway.storage_identity(
        root
    ).key


def test_a_beads_directory_without_workspace_files_does_not_stop_the_walk(tmp_path):
    # bd keeps walking up while a .beads holds nothing it recognises, so a
    # command in the child opens the ancestor's tracker - and must lock it.
    root = tracker(tmp_path / "project")
    child = root / "child"
    (child / ".beads").mkdir(parents=True)
    assert gateway.storage_identity(child).key == gateway.storage_identity(root).key


def test_a_redirect_is_the_target_tracker(tmp_path):
    shared = tracker(tmp_path / "shared")
    for name in ("one", "two"):
        source = tmp_path / name
        (source / ".beads").mkdir(parents=True)
        (source / ".beads" / "redirect").write_text("../shared/.beads\n", encoding="utf-8")
        assert gateway.storage_identity(source).key == gateway.storage_identity(shared).key


def test_a_redirect_to_nothing_is_ignored_the_way_bd_ignores_it(tmp_path):
    root = tracker(tmp_path / "project")
    before = gateway.storage_identity(root).key
    (root / ".beads" / "redirect").write_text("../nowhere\n", encoding="utf-8")
    assert gateway.storage_identity(root).key == before


def test_a_custom_dolt_data_dir_changes_the_identity(tmp_path):
    plain = gateway.storage_identity(tracker(tmp_path / "a")).key
    custom = gateway.storage_identity(
        tracker(tmp_path / "b", {"backend": "dolt", "dolt_data_dir": str(tmp_path / "data")})
    ).key
    assert plain != custom


def test_a_relative_dolt_data_dir_is_resolved_against_beads_as_bd_does(tmp_path):
    # bd joins a relative dolt_data_dir to the .beads directory (DatabasePath),
    # never to the process's working directory.
    root = tracker(tmp_path / "db", {"backend": "dolt", "dolt_data_dir": "../data"})
    expected = (root / ".beads" / ".." / "data")
    assert expected.resolve() in gateway.storage_identity(root).roots
    assert expected.resolve() == (root / "data").resolve()


def test_dolt_data_dir_in_config_yaml_is_not_a_bd_setting(tmp_path):
    # bd reads dolt_data_dir from metadata.json; config.yaml has no such key,
    # and a gateway that believed otherwise would key on a directory nobody opens.
    root = tracker(tmp_path / "db")
    (root / ".beads" / "config.yaml").write_text(
        "dolt_data_dir: /elsewhere\n", encoding="utf-8"
    )
    assert Path("/elsewhere") not in gateway.storage_identity(root).roots


def test_the_identity_is_stable_across_the_metadata_bd_writes(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    before = gateway.storage_identity(root)
    assert not before.tracker_exists
    tracker(root)
    after = gateway.storage_identity(root)
    assert after.tracker_exists and after.key == before.key


def test_a_symlinked_component_is_resolved_before_the_dotdot(tmp_path):
    real = tmp_path / "real" / "inner"
    real.mkdir(parents=True)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    # /tmp/x/link/.. is /tmp/x/real: collapsing ".." lexically first, as
    # os.path.abspath does, would name /tmp/x - a different directory.
    assert gateway._canonical_path(tmp_path / "link" / "..") == real.parent.resolve()


# --------------------------------------------------------------------------
# The environment must not move a command to another tracker
# --------------------------------------------------------------------------


def test_an_inherited_beads_dir_does_not_move_the_command(monkeypatch, tmp_path):
    root = tracker(tmp_path / "db")
    other = tracker(tmp_path / "other")
    monkeypatch.setenv("BEADS_DIR", str(other / ".beads"))
    result = run_gateway(
        ["-c", "import os, sys; sys.stdout.write(os.environ.get('BEADS_DIR', ''))"], root
    )
    assert result.success and result.output == ""


def test_an_explicit_beads_dir_for_another_tracker_is_refused(tmp_path):
    root = tracker(tmp_path / "db")
    other = tracker(tmp_path / "other")
    with pytest.raises(gateway.TrackerError) as caught:
        gateway.execute(
            ["-c", "pass"],
            directory=str(root),
            backend=gateway.Backend.host(sys.executable),
            env={"BEADS_DIR": str(other / ".beads")},
        )
    assert caught.value.code == "tracker_scope"


def test_an_explicit_beads_dir_for_the_same_tracker_is_kept(tmp_path):
    root = tracker(tmp_path / "db")
    beads = gateway.storage_identity(root).beads
    result = gateway.execute(
        ["-c", "import os, sys; sys.stdout.write(os.environ['BEADS_DIR'])"],
        directory=str(root),
        backend=gateway.Backend.host(sys.executable),
        env={"BEADS_DIR": str(beads)},
    )
    assert result.success and result.output == str(beads)


# --------------------------------------------------------------------------
# A timeout stops the command before the lock is released
# --------------------------------------------------------------------------

_FAKE_DOCKER = '''#!{python}
import os, sys, time

state = os.environ["FAKE_DOCKER_STATE"]
args = sys.argv[1:]
with open(os.path.join(state, "calls.log"), "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\\n")
command = args[0] if args else ""
pid_file = os.path.join(state, "container.pid")

if command == "run":
    # A container that outlives its client: the client hangs, the container
    # keeps running in its own session - exactly the hazard a killed client
    # leaves behind.
    if os.fork() == 0:
        os.setsid()
        with open(pid_file, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        while True:
            time.sleep(0.05)
    while True:
        time.sleep(0.05)
elif command == "kill":
    try:
        pid = int(open(pid_file, encoding="utf-8").read())
        with open(os.path.join(state, "killed.pid"), "w", encoding="utf-8") as handle:
            handle.write(str(pid))
        os.kill(pid, 9)
        os.remove(pid_file)
    except (OSError, ValueError):
        pass
elif command == "inspect":
    running = os.path.exists(pid_file)
    sys.stdout.write("true" if running else "false")
    sys.exit(0 if running else 1)
'''


def _fake_docker(monkeypatch, tmp_path) -> Path:
    state = tmp_path / "docker-state"
    state.mkdir()
    (state / "calls.log").write_text("", encoding="utf-8")
    script = tmp_path / "docker"
    script.write_text(_FAKE_DOCKER.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    return state


@pytest.mark.skipif(os.name == "nt", reason="POSIX processes")
def test_a_timed_out_container_is_gone_before_the_lock_is_released(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway, "KILL_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(gateway, "DOCKER_CLIENT_GRACE_SECONDS", 0.3)
    state = _fake_docker(monkeypatch, tmp_path)
    root = tracker(tmp_path / "db")

    result = gateway.execute(
        ["list", "--json"],
        directory=str(root),
        backend=gateway.Backend.docker("grid-agent:latest"),
        timeout=0.2,
    )

    assert result.timed_out and not result.success
    calls = (state / "calls.log").read_text(encoding="utf-8")
    assert "kill" in calls and "inspect" in calls
    container_pid = int((state / "killed.pid").read_text(encoding="utf-8"))
    assert _dead(container_pid), "the lock was released while bd was still in the container"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_descendant_holding_the_pipes_does_not_hold_the_command(tmp_path):
    pid_file = tmp_path / "escapee.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "    start_new_session=True,\n"
        ")\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()
    try:
        result = run_gateway(["-c", script, str(pid_file)], tmp_path, timeout=0.5)
        elapsed = time.monotonic() - started
    finally:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)

    assert result.timed_out and not result.success
    assert elapsed < 4.0, "a stranger's open pipe held the command (and its lock)"
    # The descendant left the command's process group: it is not ours to kill.
    escapee = int(pid_file.read_text(encoding="utf-8"))
    try:
        os.kill(escapee, 9)
    except OSError:
        pass


# --------------------------------------------------------------------------
# One lock per physical root: two workspaces may share a database
# --------------------------------------------------------------------------


def _shared_root(tmp_path: Path) -> Path:
    shared = tmp_path / "shared-data"
    shared.mkdir()
    return shared.resolve()


def test_two_workspaces_with_one_data_dir_have_different_keys_but_one_lock(tmp_path):
    # Different .beads trees, different identity hashes - and one directory both
    # of them open. A lock named after the *set* of roots would let them run at
    # once; a lock per root cannot.
    shared = _shared_root(tmp_path)
    one = gateway.storage_identity(
        tracker(tmp_path / "one", {"backend": "dolt", "dolt_data_dir": str(shared)})
    )
    two = gateway.storage_identity(
        tracker(tmp_path / "two", {"backend": "dolt", "dolt_data_dir": str(shared)})
    )

    assert one.key != two.key
    shared_lock = f"rt-{gateway._root_lock_key(shared)}.lock"
    assert shared_lock in gateway.lock_names(one)
    assert shared_lock in gateway.lock_names(two)
    assert set(gateway.lock_names(one)) & set(gateway.lock_names(two)) == {shared_lock}


def test_an_embedded_and_a_server_workspace_still_share_the_data_dir_lock(tmp_path):
    # The embedded engine gates embeddeddolt *and* the library path; the server
    # flavour gates only the library path. Both open the same directory, so the
    # intersection must be non-empty.
    shared = _shared_root(tmp_path)
    embedded = gateway.storage_identity(
        tracker(tmp_path / "embedded", {"backend": "dolt", "dolt_data_dir": str(shared)})
    )
    server = gateway.storage_identity(
        tracker(
            tmp_path / "server",
            {"backend": "dolt", "dolt_mode": "server", "dolt_data_dir": str(shared)},
        )
    )

    shared_lock = f"rt-{gateway._root_lock_key(shared)}.lock"
    assert shared_lock in gateway.lock_names(server)
    assert shared_lock in set(gateway.lock_names(embedded)) & set(gateway.lock_names(server))


def test_the_locks_of_one_identity_are_ordered_the_same_everywhere(tmp_path):
    # Two processes sharing only one root must queue in the same direction or
    # they deadlock; the order is the sorted lock names, so it is global.
    identity = gateway.storage_identity(tracker(tmp_path / "db"))
    names = gateway.lock_names(identity)
    assert list(names) == sorted(names)
    assert len(names) == len(set(names)) >= 2
    assert names == gateway.lock_names(gateway.storage_identity(tmp_path / "db"))


def test_holding_one_root_excludes_a_different_workspace_on_it(tmp_path):
    shared = _shared_root(tmp_path)
    one = gateway.storage_identity(
        tracker(tmp_path / "one", {"backend": "dolt", "dolt_data_dir": str(shared)})
    )
    two = gateway.storage_identity(
        tracker(tmp_path / "two", {"backend": "dolt", "dolt_data_dir": str(shared)})
    )

    with gateway.hold(one, timeout=5):
        with pytest.raises(gateway.TrackerError) as caught:
            with gateway.hold(two, timeout=0.2):
                pass
    assert caught.value.code == "tracker_busy"
    # ... and it is free again once the first one is done.
    with gateway.hold(two, timeout=5):
        pass


_SHARED_WORKER = """
import sys, time
from pathlib import Path
from core import tracker_gateway

database = Path(sys.argv[1])
log = Path(sys.argv[2])
start = Path(sys.argv[3])
while not start.exists():
    time.sleep(0.01)
identity = tracker_gateway.storage_identity(database)
with log.open("a", encoding="utf-8") as stream:
    with tracker_gateway.hold(identity, timeout=20):
        stream.write("enter\\n")
        stream.flush()
        time.sleep(0.3)
        stream.write("exit\\n")
        stream.flush()
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock between processes")
def test_two_processes_on_two_workspaces_sharing_one_data_dir_take_turns(tmp_path):
    shared = _shared_root(tmp_path)
    one = tracker(tmp_path / "one", {"backend": "dolt", "dolt_data_dir": str(shared)})
    two = tracker(tmp_path / "two", {"backend": "dolt", "dolt_data_dir": str(shared)})
    log = tmp_path / "turns.log"
    start = tmp_path / "go"

    workers = [
        subprocess.Popen(
            [sys.executable, "-c", _SHARED_WORKER, str(database), str(log), str(start)],
            cwd=str(REPO),
            env={**os.environ, "PYTHONPATH": str(REPO)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for database in (one, two)
    ]
    time.sleep(0.4)
    start.write_text("go", encoding="utf-8")
    for worker in workers:
        _, error = worker.communicate(timeout=60)
        assert worker.returncode == 0, error

    assert log.read_text(encoding="utf-8").split() == ["enter", "exit", "enter", "exit"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock between processes")
def test_a_killed_process_does_not_leave_the_database_locked(tmp_path):
    # A crash must not need a repair: the kernel drops the flock with the fd,
    # and the next command takes the gate without a stale-lock heuristic.
    identity = gateway.storage_identity(tracker(tmp_path / "db"))
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "from pathlib import Path\n"
            "from core import tracker_gateway\n"
            "identity = tracker_gateway.storage_identity(Path(sys.argv[1]))\n"
            "with tracker_gateway.hold(identity, timeout=10):\n"
            "    print('held', flush=True)\n"
            "    time.sleep(60)\n",
            str(identity.directory),
        ],
        cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": str(REPO)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        holder.kill()
        holder.wait(timeout=10)
        started = time.monotonic()
        with gateway.hold(identity, timeout=10):
            pass
        assert time.monotonic() - started < 5
    finally:
        if holder.poll() is None:  # pragma: no cover - defensive
            holder.kill()
            holder.wait(timeout=5)


# --------------------------------------------------------------------------
# A container that outlived its command quarantines the database
# --------------------------------------------------------------------------


def _timed_out_with_container(monkeypatch):
    def fake_run(cmd, *, cwd, env, timeout, container_name=None):
        if container_name:
            return gateway.ExecutionResult(False, "", "timed out", -1, timed_out=True)
        return gateway.ExecutionResult(True, "", "", 0)

    monkeypatch.setattr(gateway, "_run_process", fake_run)


def test_a_container_that_does_not_stop_quarantines_the_tracker(monkeypatch, tmp_path):
    root = tracker(tmp_path / "db")
    _timed_out_with_container(monkeypatch)
    monkeypatch.setattr(gateway, "container_stopped", lambda name, budget=0: False)

    first = gateway.execute(
        ["list"], directory=str(root), backend=gateway.Backend.docker("grid:test"), timeout=0.2
    )
    assert first.timed_out
    assert "quarantin" in first.error.lower()
    # The lock is gone, so the refusal must come from the quarantine, not luck.
    with pytest.raises(gateway.TrackerError) as caught:
        gateway.execute(
            ["list"], directory=str(root), backend=gateway.Backend.docker("grid:test")
        )
    assert caught.value.code == "tracker_quarantine" and caught.value.retryable
    assert "docker stop" in str(caught.value)


def test_the_quarantine_survives_a_restart_and_clears_when_the_container_is_gone(
    monkeypatch, tmp_path
):
    root = tracker(tmp_path / "db")
    _timed_out_with_container(monkeypatch)
    monkeypatch.setattr(gateway, "container_stopped", lambda name, budget=0: False)
    gateway.execute(
        ["list"], directory=str(root), backend=gateway.Backend.docker("grid:test"), timeout=0.2
    )
    identity = gateway.storage_identity(root)
    # It is a file, not process memory: a crash must not forget it.
    assert all(
        (gateway.gate_directory() / f"rt-{key}.quarantine.json").is_file()
        for key in identity.root_keys
    )

    # Another workspace opening the same data directory is refused as well.
    shared = _shared_root(tmp_path)
    other = tracker(tmp_path / "other", {"backend": "dolt", "dolt_data_dir": str(shared)})
    (gateway.gate_directory() / f"rt-{gateway._root_lock_key(shared)}.quarantine.json").write_text(
        json.dumps({"container": "stuck-one"}), encoding="utf-8"
    )
    with pytest.raises(gateway.TrackerError) as caught:
        gateway.execute(
            ["list"], directory=str(other), backend=gateway.Backend.host(sys.executable)
        )
    assert caught.value.code == "tracker_quarantine"

    # Once inspect confirms it is gone, the same call is allowed - no cleanup.
    monkeypatch.setattr(gateway, "container_stopped", lambda name, budget=0: True)
    result = gateway.execute(
        ["-c", "pass"], directory=str(root), backend=gateway.Backend.host(sys.executable)
    )
    assert result.success
    assert not any(
        (gateway.gate_directory() / f"rt-{key}.quarantine.json").exists()
        for key in identity.root_keys
    )


def test_a_container_confirmed_stopped_is_not_quarantined(monkeypatch, tmp_path):
    root = tracker(tmp_path / "db")
    _timed_out_with_container(monkeypatch)
    monkeypatch.setattr(gateway, "container_stopped", lambda name, budget=0: True)

    result = gateway.execute(
        ["list"], directory=str(root), backend=gateway.Backend.docker("grid:test"), timeout=0.2
    )
    assert result.timed_out
    identity = gateway.storage_identity(root)
    assert not any(
        (gateway.gate_directory() / f"rt-{key}.quarantine.json").exists()
        for key in identity.root_keys
    )


# --------------------------------------------------------------------------
# Stopping a command whose leader died, and bytes from a timeout
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_group_is_stopped_even_when_its_leader_is_already_gone(tmp_path):
    import signal as signal_module

    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(pid_file)],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        grandchild = int(pid_file.read_text(encoding="utf-8"))

        # The leader dies (and is reaped) while its child keeps the group alive.
        os.kill(proc.pid, signal_module.SIGKILL)
        proc.wait(timeout=10)

        gateway._stop_process_group(proc, own_session=True)

        deadline = time.monotonic() + 10
        while not _dead(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _dead(grandchild), "a child outlived the leader and its lock was released"
    finally:
        if proc.poll() is None:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=5)


def test_a_timeout_with_bytes_output_does_not_crash_the_gateway(tmp_path):
    class Hung:
        pid = os.getpid()
        stdout = None
        stderr = None

        def __init__(self):
            self.calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(
                    cmd="bd", timeout=timeout, output=b"partial", stderr=b"bad"
                )
            return "", ""

        def send_signal(self, sig):
            pass

        def wait(self, timeout=None):
            return -9

        def kill(self):
            pass

    import core.tracker_gateway as module

    original = module.subprocess.Popen
    module.subprocess.Popen = lambda *a, **k: Hung()
    try:
        result = module._run_process(["bd", "list"], cwd=None, env=None, timeout=0.1)
    finally:
        module.subprocess.Popen = original

    assert result.timed_out
    assert "partial" in result.output and "bad" in result.error


# --------------------------------------------------------------------------
# The actor crosses into the container; storage routing does not
# --------------------------------------------------------------------------


def test_the_actor_is_passed_into_a_container_and_nothing_else(tmp_path):
    command, _, _ = gateway.build_command(
        ["list"],
        backend=gateway.Backend.container("c0ffee"),
        directory=str(tmp_path),
        env={"BEADS_ACTOR": "grid:alice", "BEADS_DOLT_DATA_DIR": "/elsewhere"},
    )
    assert "BEADS_ACTOR=grid:alice" in command
    assert "BEADS_DOLT_DATA_DIR=/elsewhere" not in command
    assert command[command.index("BEADS_ACTOR=grid:alice") - 1] == "-e"

    run_command, _, name = gateway.build_command(
        ["list"],
        backend=gateway.Backend.docker("grid:test"),
        directory=str(tmp_path),
        env={"BEADS_ACTOR": "grid:alice"},
    )
    assert name and "BEADS_ACTOR=grid:alice" in run_command
    assert run_command.index("BEADS_ACTOR=grid:alice") < run_command.index("grid:test")


def test_execute_hands_the_actor_to_the_container_command(monkeypatch, tmp_path):
    root = tracker(tmp_path / "db")
    seen = {}

    def fake_run(cmd, *, cwd, env, timeout, container_name=None):
        seen["cmd"] = list(cmd)
        return gateway.ExecutionResult(True, "", "", 0)

    monkeypatch.setattr(gateway, "_run_process", fake_run)
    monkeypatch.setattr(gateway, "container_stopped", lambda name, budget=0: True)
    result = gateway.execute(
        ["list"],
        directory=str(root),
        backend=gateway.Backend.docker("grid:test"),
        env={"BEADS_ACTOR": "grid:alice"},
    )
    assert result.success
    assert "BEADS_ACTOR=grid:alice" in seen["cmd"]


def test_a_routing_value_for_another_tracker_is_refused_for_a_container(tmp_path):
    root = tracker(tmp_path / "db")
    other = tracker(tmp_path / "other")
    with pytest.raises(gateway.TrackerError) as caught:
        gateway.execute(
            ["list"],
            directory=str(root),
            backend=gateway.Backend.docker("grid:test"),
            env={"BEADS_DIR": str(other / ".beads")},
        )
    assert caught.value.code == "tracker_scope"


# --------------------------------------------------------------------------
# The gate directory must be a stable, trustworthy place
# --------------------------------------------------------------------------


def test_the_gate_directory_is_not_trusted_through_a_symlink(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("GRID_TRACKER_GATE_DIR", str(link))
    with pytest.raises(gateway.TrackerError) as caught:
        gateway.gate_directory()
    assert caught.value.code == "tracker_unsupported"


def test_the_gate_directory_is_owned_by_this_process(monkeypatch, tmp_path):
    monkeypatch.setenv("GRID_TRACKER_GATE_DIR", str(tmp_path / "gate"))
    monkeypatch.setattr(gateway.os, "geteuid", lambda: os.stat(tmp_path).st_uid + 1)
    with pytest.raises(gateway.TrackerError) as caught:
        gateway.gate_directory()
    assert caught.value.code == "tracker_unsupported"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_gate_lives_in_the_state_dir_not_only_in_the_temp_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("GRID_TRACKER_GATE_DIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    gate = gateway.gate_directory()
    assert gate == (tmp_path / "state" / "grid" / "tracker-gate").resolve()
    # A cleaner deleting it would break a live lock, so it must not be shared.
    assert oct(os.stat(gate).st_mode & 0o777) == "0o700"


def test_a_run_container_command_that_may_still_run_quarantines_the_tracker(
    monkeypatch, tmp_path
):
    # A run's own container must never be stopped or killed by the gateway, so
    # a timed-out `docker exec` is only safe when its exact command line is
    # gone from the container.
    root = tracker(tmp_path / "db")
    monkeypatch.setattr(
        gateway,
        "_run_process",
        lambda *a, **k: gateway.ExecutionResult(False, "", "timed out", -1, timed_out=True),
    )
    monkeypatch.setattr(gateway, "bd_process_in_container", lambda *a: True)
    backend = gateway.Backend.container("c0ffee")

    first = gateway.execute(["list"], directory=str(root), backend=backend, timeout=0.2)
    assert first.timed_out and "quarantin" in first.error.lower()

    with pytest.raises(gateway.TrackerError) as caught:
        gateway.execute(["list"], directory=str(root), backend=backend)
    assert caught.value.code == "tracker_quarantine"
    assert "docker top" in str(caught.value)

    # An unreadable process list is not a confirmation either.
    monkeypatch.setattr(gateway, "bd_process_in_container", lambda *a: None)
    with pytest.raises(gateway.TrackerError):
        gateway.execute(["list"], directory=str(root), backend=backend)

    # Once the command line is gone, the database is free again - no cleanup.
    monkeypatch.setattr(gateway, "bd_process_in_container", lambda *a: False)
    result = gateway.execute(["list"], directory=str(root), backend=backend)
    assert result.timed_out  # still reports the timeout it had
    identity = gateway.storage_identity(root)
    assert not any(
        (gateway.gate_directory() / f"rt-{key}.quarantine.json").exists()
        for key in identity.root_keys
    )


def test_the_container_probe_matches_the_exact_command_line(monkeypatch):
    seen = []
    top = "pid cmd\n1 timeout -k 5 30 /usr/local/bin/bd list --json\n"

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return SimpleNamespace(returncode=0, stdout=top, stderr="")

    monkeypatch.setattr(
        gateway.subprocess,
        "run",
        fake_run,
    )
    assert (
        gateway.bd_process_in_container("c0ffee", "/usr/local/bin/bd", ["list", "--json"]) is True
    )
    assert gateway.bd_process_in_container("c0ffee", "/usr/local/bin/bd", ["ready"]) is False
    assert seen[0] == ["docker", "top", "c0ffee"]


def test_docker_fallback_mounts_the_tracker_target_not_its_redirect_alias(tmp_path):
    shared = tracker(tmp_path / "shared")
    alias = tmp_path / "alias"
    (alias / ".beads").mkdir(parents=True)
    (alias / ".beads" / "redirect").write_text("../shared/.beads\n", encoding="utf-8")
    command, _, _ = gateway.build_command(
        ["list", "--json"], backend=gateway.Backend.docker("grid-agent:latest"),
        directory=str(alias),
    )
    assert command[command.index("-v") + 1] == f"{shared.resolve()}:/workspace"
    assert gateway.storage_identity(alias).key == gateway.storage_identity(shared).key


def test_docker_fallback_refuses_a_storage_root_outside_its_mount(tmp_path):
    directory = tracker(tmp_path / "project", {
        "backend": "dolt", "dolt_data_dir": str(tmp_path / "external-data"),
    })
    with pytest.raises(gateway.TrackerError) as refused:
        gateway.build_command(["create", "--", "Task"],
                              backend=gateway.Backend.docker("grid-agent:latest"),
                              directory=str(directory))
    assert refused.value.code == "tracker_scope"


@pytest.mark.parametrize("host_routing", ["metadata", "environment"])
def test_docker_fallback_refuses_host_only_routing_even_inside_mount(monkeypatch, tmp_path, host_routing):
    directory = tracker(tmp_path / "project")
    inside = directory / ".beads" / "custom"
    if host_routing == "metadata":
        (directory / ".beads" / "metadata.json").write_text(
            json.dumps({"backend": "dolt", "dolt_data_dir": str(inside)}), encoding="utf-8")
    else:
        monkeypatch.setenv("BEADS_DOLT_DATA_DIR", str(inside))
    with pytest.raises(gateway.TrackerError) as refused:
        gateway.build_command(["list"], backend=gateway.Backend.docker("grid-agent:latest"),
                              directory=str(directory))
    assert refused.value.code == "tracker_scope"

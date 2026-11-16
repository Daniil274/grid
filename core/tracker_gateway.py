"""The one execution gateway every Beads (bd) command goes through.

Beads keeps its tracker in an embedded, single-process-only engine: its own
``configfile`` package states that embedded Dolt must not be opened by two
Beads processes at once. Every GRID entry point - agent
tools, ``init_tools``, the web UI's tracker page, background workers - must
therefore run bd the same way and take turns on the *physical* database, not
merely on the directory path a caller happened to spell.

This module is that single path. It does four things and nothing else:

* **Physical storage identity.** :func:`storage_identity` asks bd's own
  questions: which ``.beads`` directory a command run in this directory finds
  (the ancestor walk of ``FindBeadsDir``, one level of ``.beads/redirect``),
  and which local directories the resolved mode opens (``.beads/embeddeddolt``
  for the embedded engine, ``DatabasePath`` - ``.beads/dolt`` or a custom
  ``dolt_data_dir``, relative to ``.beads`` - for the library path, the
  shared-server and proxied-server roots). Symlinks, ``..``, trailing slashes
  and a symlinked ``.beads`` all collapse to one identity. The key is stable
  before and after ``bd init``: a directory without a tracker maps to the
  roots an initialised embedded workspace resolves to.
* **Cross-process serialization.** :func:`hold` takes exclusive advisory
  flocks (``msvcrt.locking`` on Windows), outside the tracker directory: one
  per *physical root* - so two workspaces that point a ``dolt_data_dir`` at
  one directory exclude each other - plus one per-workspace gate that is the
  same before and after ``bd init``. All of them are taken in one sorted order
  with one budget, so workspaces sharing a root cannot deadlock. Lock files
  are created once and never deleted: on POSIX the lock follows the inode, so
  recreating a file would let a second process lock a fresh inode while the
  first still holds the old one (the same reasoning as bd's own
  ``internal/workspacegate``).
* **One backend, checked.** A :class:`Backend` says where bd runs: on the host,
  in the run's managed container (``docker exec``), or in a short-lived
  container (``docker run``). Its version is probed once, best effort, and the
  first version that opened a physical database is recorded beside its lock.
  A *different* bd version opening the same database later is refused:
  two builds of a single-process embedded store taking turns on one directory
  is exactly the uncontrolled case. The host and the image running the same
  version are the normal arrangement, already serialized here, and are not a
  mismatch. ``GRID_TRACKER_ALLOW_BACKEND_MISMATCH=1`` is the explicit opt-out
  (warn and keep working); a version older than the commands the tools use is
  refused the same way, and a version that merely cannot be read is *reported*
  as unverifiable, never as "too old".
* **Addressable termination.** A command that overruns its timeout is stopped
  together with what it started: the host process group, the ``docker`` client
  process group and - for a short-lived container - that container by the
  unique name it was given. Nothing is ever killed by pattern; other runs,
  other users and other trackers are outside the blast radius.

* **Fail closed when a stop cannot be confirmed.** If a short-lived container
  or a ``docker exec`` survives the stop attempt, a persistent quarantine is
  written beside the physical root's lock before that lock is released, and
  every later command that would open the root is refused (retryable) until
  ``docker inspect`` / ``docker top`` confirms the command is gone. A run's own
  container is never stopped or killed by the gateway; the quarantine is the
  only lever there.

Boundaries (deliberately not promises of this module):

* Coordination is between processes on **one host** that go through this
  gateway. A tracker on a network mount written by another machine, or a
  process that runs bd directly (bd's own ``.beads.gate.lock`` is a *shared*
  gate for normal commands, not an exclusive serializer), is not serialized.
  Cross-host or multi-writer access needs Dolt server mode, which is a
  separate, explicit decision.
* bd's own ``.beads.gate.lock`` is not taken here on purpose: bd takes it
  shared for a command, so holding it exclusively around a bd child would
  deadlock. Our lock is a different file, in the gateway's own directory.
* The lock directory is per effective uid (see :func:`gate_directory`); two
  server processes of different uids do not share it, so all bd children of a
  server must inherit that server's uid, as they do today.
* Processes this gateway did not start - a dolt daemon some other code left
  behind, a bd run outside the gateway - are not touched. Whoever starts a
  process stops it, by its own pid or container name; a pattern kill reaches
  other people's work and is never used here.
* Only the backend/version *record* lives next to the lock; the databases
  themselves are never opened, migrated, repaired or removed here.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import select
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

#: How long one bd command may run before it is stopped (seconds).
DEFAULT_TIMEOUT_SECONDS = 30

#: Extra seconds a stopped command gets to exit before SIGKILL.
KILL_GRACE_SECONDS = 5

#: How long the output a stopped command already produced is drained for. A
#: descendant that escaped the process group can hold the pipes open forever.
OUTPUT_DRAIN_SECONDS = 1.0

#: How long the ``docker`` client itself gets on top of a command's budget. It
#: has to outlive the inner ``timeout`` guard (see :func:`build_command`), so a
#: hung client is stopped only after bd inside the container had its chance.
DOCKER_CLIENT_GRACE_SECONDS = 5.0

#: How long a caller waits for another process to finish with the same
#: physical database. Longer than one command's own budget so a queue of
#: ordinary commands drains instead of failing.
DEFAULT_LOCK_TIMEOUT_SECONDS = 60

#: bd versions older than this predate the command shapes the tools use
#: (``--json`` everywhere, ``--claim``, ``--graph``, ``init --init-if-missing``).
MINIMUM_BD_VERSION = (1, 0, 0)

_GATE_DIR_ENV = "GRID_TRACKER_GATE_DIR"
_LOCK_TIMEOUT_ENV = "GRID_TRACKER_LOCK_TIMEOUT"
_STRICT_ENV = "GRID_TRACKER_STRICT_BACKEND"
_ALLOW_MISMATCH_ENV = "GRID_TRACKER_ALLOW_BACKEND_MISMATCH"
_PROBE_ENV = "GRID_TRACKER_PROBE_BD"


class TrackerError(Exception):
    """A refusal by the gateway, carrying a stable code for callers.

    ``retryable`` says whether the same call may succeed later unchanged:
    a busy database is retryable, a scope or backend refusal is not.
    """

    def __init__(self, message: str, *, code: str = "tracker_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# ---------------------------------------------------------------------------
# Physical storage identity
# ---------------------------------------------------------------------------


def _canonical_path(path: "str | Path") -> Path:
    """An absolute, symlink-free path; the one spelling of a directory.

    ``resolve`` walks the components and collapses ``..`` *after* the symlink
    it followed, which ``os.path.abspath`` gets wrong: ``/a/link/../b`` with
    ``link -> /x/y`` is ``/x/b``, not ``/a/b``. A path that cannot be resolved
    - a link loop - is refused instead of being passed on as a second spelling
    of one database. ``~`` is deliberately not expanded: bd does not expand it
    either, so a literal ``~`` in a config value must not silently name a
    directory bd will never open.
    """
    try:
        return Path(str(path)).resolve()
    except (OSError, RuntimeError) as exc:
        raise TrackerError(
            f"The tracker path {path!s} cannot be resolved: {exc}",
            code="tracker_scope",
        ) from exc


def _identity_text(path: Path) -> str:
    """The path as a comparison key: case and separators folded where the OS does."""
    return os.path.normcase(str(path))


#: Files and directories bd accepts as proof that a ``.beads`` tree is a
#: workspace (``internal/beads/beads.go:hasBeadsProjectFiles``).
_PROJECT_FILES = ("metadata.json", "config.yaml")
_PROJECT_DIRS = ("dolt", "embeddeddolt")

#: Server hosts that mean "this machine" (``configfile.HostImpliesServerMode``).
_LOCAL_HOSTS = ("", "127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0")


def _reads_as_workspace(beads: Path) -> bool:
    """Whether bd would take *beads* for a workspace directory."""
    try:
        if not beads.is_dir():
            return False
        if any((beads / name).exists() for name in _PROJECT_FILES):
            return True
        if any((beads / name).is_dir() for name in _PROJECT_DIRS):
            return True
        return any(
            name.endswith(".db") and ".backup" not in name and name != "vc.db"
            for name in (item.name for item in beads.glob("*.db"))
        )
    except OSError:
        return False


def _follow_redirect(beads: Path) -> Path:
    """``.beads/redirect``, followed the way bd follows it.

    ``internal/beads/beads.go:FollowRedirect``: the first line that is neither
    blank nor a comment; a relative target resolves from the project root (the
    parent of ``.beads``); the target is canonicalized; a target that is
    missing or holds no workspace files is ignored, not followed. One level
    only, as in bd.
    """
    try:
        text = (beads / "redirect").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return beads
    target = ""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            target = line
            break
    if not target:
        return beads
    candidate = Path(target) if os.path.isabs(target) else beads.parent / target
    try:
        resolved = _canonical_path(candidate)
    except TrackerError:
        logger.warning("Ignoring the tracker redirect in %s to %r", beads, target)
        return beads
    if not _reads_as_workspace(resolved):
        logger.warning(
            "Ignoring the tracker redirect in %s: %s is not a beads workspace",
            beads,
            resolved,
        )
        return beads
    return resolved


def _find_beads_dir(directory: Path) -> Path:
    """The tracker bd would open for a command run in *directory*.

    bd walks up from the working directory to the first ``.beads`` holding
    workspace files (``internal/beads/beads.go:FindBeadsDir``). A command in a
    subdirectory and a command in the project root therefore open one
    database and must share one lock; when no ancestor has a tracker yet, the
    path a normal ``bd init`` creates is returned.
    """
    for candidate in (directory, *directory.parents):
        beads = candidate / ".beads"
        if not beads.is_dir() and not beads.is_symlink():
            continue
        # bd follows the redirect first and asks the *target* whether it is a
        # workspace; a redirect is the reason the source holds no files.
        resolved = _follow_redirect(beads)
        if _reads_as_workspace(resolved):
            return resolved
    return directory / ".beads"


def _read_json(path: Path) -> dict:
    """A JSON object from *path*, or {} - settings are read, never written here."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_metadata(beads: Path) -> dict:
    """``.beads/metadata.json`` as bd writes it (the legacy ``config.json`` too).

    Read-only on purpose: bd migrates the legacy name with a write, and
    resolving an identity must never write into someone's tracker.
    """
    metadata = _read_json(beads / "metadata.json")
    if metadata:
        return metadata
    return _read_json(beads / "config.json")


def _read_yaml_config(beads: Path) -> dict:
    """``.beads/config.yaml`` when it is readable YAML, else {}."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - GRID ships PyYAML
        return {}
    try:
        data = yaml.safe_load(
            (beads / "config.yaml").read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _yaml_get(data: Mapping[str, object], key: str) -> Optional[str]:
    node: object = data
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return None if node is None else str(node)


def _dolt_mode(
    beads: Path, metadata: Mapping[str, object], yaml_config: Mapping[str, object]
) -> str:
    """The storage mode bd resolves for this workspace.

    ``configfile.IsDoltServerMode`` plus the shared-server and proxied-server
    flavours of ``internal/doltserver/physical_root.go``. The mode decides
    *which* directory a command opens, so identity cannot be guessed from
    ``.beads`` alone. ``other-backend`` is a tracker bd refuses to open.
    """
    backend = str(metadata.get("backend") or "").strip().lower()
    if backend and backend != "dolt":
        return "other-backend"
    if os.environ.get("BEADS_DOLT_SERVER_MODE") == "1":
        return "server"
    shared = os.environ.get("BEADS_DOLT_SHARED_SERVER", "").strip().lower()
    if shared in ("1", "true") or (
        (_yaml_get(yaml_config, "dolt.shared-server") or "").strip().lower() in ("1", "true")
    ):
        return "shared-server"
    explicit = str(metadata.get("dolt_mode") or "").strip().lower()
    if explicit == "proxied-server":
        return "proxied-server"
    host = str(metadata.get("dolt_server_host") or "").strip().lower()
    if host not in _LOCAL_HOSTS:
        return "remote"
    if explicit:
        return "server" if explicit == "server" else "embedded"
    if (_yaml_get(yaml_config, "dolt.mode") or "").strip().lower() == "server":
        return "server"
    return "embedded"


def _database_dir(beads: Path, metadata: Mapping[str, object]) -> Path:
    """``configfile.Config.DatabasePath``: the directory the library path opens.

    ``BEADS_DOLT_DATA_DIR`` or ``metadata.json``'s ``dolt_data_dir``, with a
    relative value resolved against the ``.beads`` directory exactly as bd
    resolves it; else ``.beads/dolt``.
    """
    custom = os.environ.get("BEADS_DOLT_DATA_DIR") or str(metadata.get("dolt_data_dir") or "")
    if custom:
        return _canonical_path(custom if os.path.isabs(custom) else beads / custom)
    database = str(metadata.get("database") or "")
    if os.path.isabs(database):
        return _canonical_path(database)
    return _canonical_path(beads / "dolt")


def _shared_server_dir() -> Path:
    """``doltserver.SharedDoltPath``: the one dolt directory of a shared server."""
    override = os.environ.get("BEADS_SHARED_SERVER_DIR")
    base = (
        Path(override)
        if override
        else Path(os.path.expanduser("~")) / ".beads" / "shared-server"
    )
    return _canonical_path(base) / "dolt"


def _proxied_root(beads: Path, metadata: Mapping[str, object]) -> Path:
    """``doltserver.ResolveProxiedServerRootPath``: env, sidecar, then default."""
    value = os.environ.get("BEADS_PROXIED_SERVER_ROOT_PATH")
    if value:
        return _canonical_path(value if os.path.isabs(value) else beads / value)
    sidecar = _read_json(beads / "proxied_server_client_info.json")
    root = str(sidecar.get("root_path") or "")
    if root:
        return _canonical_path(root if os.path.isabs(root) else beads / root)
    return _database_dir(beads, metadata)


def _physical_roots(beads: Path) -> "tuple[Path, ...]":
    """Every local directory a bd command may open for this workspace.

    Mirrors ``internal/doltserver/physical_root.go``. Two roots, not one,
    because one workspace is opened two ways: the CLI's embedded engine
    hardcodes ``.beads/embeddeddolt`` and ignores ``dolt_data_dir``, while the
    library open path (``internal/storage/dolt/open.go``) derives its directory
    from ``DatabasePath`` (``.beads/dolt`` or a custom ``dolt_data_dir``). bd
    gates the union of the two, and so does this key: over-serializing one
    workspace is harmless, two writers of one directory are not.
    """
    metadata = _read_metadata(beads)
    mode = _dolt_mode(beads, metadata, _read_yaml_config(beads))
    if mode == "other-backend":
        return (beads,)
    if mode == "shared-server":
        return (_shared_server_dir(),)
    if mode == "proxied-server":
        return (_proxied_root(beads, metadata),)
    roots = [_database_dir(beads, metadata)]
    if mode == "embedded":
        roots.append(_canonical_path(beads / "embeddeddolt"))
    return tuple(dict.fromkeys(roots))


def _identity_key(roots: "Sequence[Path]") -> str:
    """One hash for one set of physical roots; sorted, case folded where the OS does."""
    text = "\n".join(sorted(_identity_text(root) for root in roots))
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _root_lock_key(root: Path) -> str:
    """The gate name of one *physical directory* two workspaces may share.

    Hashing a workspace's whole root *set* would give two workspaces that point
    their ``dolt_data_dir`` at one directory different keys - and let both open
    it at once. One directory, one lock is the only safe unit.
    """
    text = "root\n" + _identity_text(root)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _workspace_lock_key(beads: Path) -> str:
    """The gate name of one *workspace*, stable before and after ``bd init``."""
    text = "workspace\n" + _identity_text(beads)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:32]


@dataclass(frozen=True)
class StorageIdentity:
    """One physical tracker, however its directory was spelled."""

    directory: Path
    """The canonical project directory a command runs in."""

    beads: Path
    """The ``.beads`` directory bd would open for :attr:`directory`."""

    roots: "tuple[Path, ...]"
    """The physical directories that must not be opened by two bd processes."""

    key: str
    """Stable hash of :attr:`roots`, used for the lock file name."""

    tracker_exists: bool
    """Whether a ``.beads`` tree exists yet (init may still create it)."""

    workspace_key: str
    """Stable hash of the ``.beads`` path: the per-workspace init gate."""

    root_keys: "tuple[str, ...]"
    """One lock key per physical root, aligned with :attr:`roots`."""

    @property
    def root(self) -> Path:
        """The primary physical directory: the one the library path opens."""
        return self.roots[0]

    @property
    def label(self) -> str:
        return str(self.directory)


def storage_identity(directory: "str | Path") -> StorageIdentity:
    """The physical identity of the tracker in *directory*; fail closed.

    A relative path is refused rather than resolved against the server's
    working directory: a caller that loses the run's scope must not
    silently pick another tracker. The tracker itself is found the way bd
    finds it - walking up from the directory, following a ``.beads/redirect``
    - so a command in a subdirectory locks the database it actually opens,
    not a second key beside it.
    """
    if directory is None or str(directory).strip() == "":
        raise TrackerError("No tracker directory was given.", code="tracker_scope")
    raw = str(directory)
    if not os.path.isabs(raw):
        raise TrackerError(
            f"Tracker directory must be an absolute path, not {raw!r}.",
            code="tracker_scope",
        )
    canonical = _canonical_path(raw)
    if not canonical.exists():
        raise TrackerError(
            f"Tracker directory does not exist: {canonical}", code="tracker_scope"
        )
    if not canonical.is_dir():
        raise TrackerError(
            f"Tracker directory is not a directory: {canonical}", code="tracker_scope"
        )

    beads = _find_beads_dir(canonical)
    exists = beads.is_dir()
    if exists:
        roots = _physical_roots(beads)
    else:
        # The names the normal child will get once `bd init` makes it: the
        # embedded engine's own root and the library path's, which is what an
        # initialised embedded workspace resolves to, so the key does not move
        # when the tracker appears.
        roots = (
            _canonical_path(beads / "embeddeddolt"),
            _canonical_path(beads / "dolt"),
        )

    return StorageIdentity(
        directory=canonical,
        beads=beads,
        roots=roots,
        key=_identity_key(roots),
        tracker_exists=exists,
        workspace_key=_workspace_lock_key(beads),
        root_keys=tuple(dict.fromkeys(_root_lock_key(root) for root in roots)),
    )


# ---------------------------------------------------------------------------
# The cross-process gate
# ---------------------------------------------------------------------------


def gate_directory() -> Path:
    """The directory holding bd gate and backend-record files.

    ``GRID_TRACKER_GATE_DIR`` when set (an unusable explicit choice is an
    error, not a silent move); else ``$XDG_RUNTIME_DIR/grid-tracker-gate``;
    else ``$XDG_STATE_HOME/grid/tracker-gate`` (``~/.local/state``); else a
    per-uid directory under the temp dir. The per-uid fallback is the safe
    default: a world-writable shared directory would let one user's unreadable
    lock file block - or be mistaken for - another's, and the uid is what
    separates two servers that must not coordinate through it.

    The state directory is preferred over the temp dir because a cleaner may
    delete a temp directory another process still holds a lock in: the lock
    would then sit on an inode nobody can find again, and the next command
    would lock a fresh file beside a writer that never stopped. Whichever
    directory is used must be a plain directory owned by us and not
    group/world writable - a gate another user can replace is not a gate.
    """
    configured = os.environ.get(_GATE_DIR_ENV)
    if configured:
        candidates = [Path(os.path.expanduser(configured))]
        explicit = True
    else:
        explicit = False
        candidates = []
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            candidates.append(Path(runtime) / "grid-tracker-gate")
        else:
            state_home = os.environ.get("XDG_STATE_HOME")
            home = state_home or os.path.expanduser("~")
            if home and home != "~":
                base = Path(home)
                candidates.append(
                    base / "grid" / "tracker-gate"
                    if state_home
                    else base / ".local" / "state" / "grid" / "tracker-gate"
                )
            uid = os.getuid() if hasattr(os, "getuid") else "user"
            candidates.append(Path(tempfile.gettempdir()) / f"grid-tracker-gate-{uid}")
    failure: Optional[OSError] = None
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True, mode=0o700)
            # The umask must not leave a gate another user could write into.
            os.chmod(base, 0o700)
        except OSError as exc:
            failure = exc
            if explicit:
                break
            continue
        try:
            _validate_gate_directory(base)
        except TrackerError:
            if explicit:
                raise
            continue
        return base
    raise TrackerError(
        f"The tracker gate directory {candidates[-1]} cannot be used: {failure}",
        code="tracker_unsupported",
    ) from failure


def _validate_gate_directory(base: Path) -> None:
    """Refuse a gate directory that is a symlink, foreign, or too open.

    The lock is only as good as the directory holding it: a symlinked or
    foreign-writable directory could be swapped while a lock is held, and the
    next command would lock a new inode and treat the database as free.
    """
    try:
        info = base.lstat()
    except OSError as exc:
        raise TrackerError(
            f"The tracker gate directory {base} cannot be inspected: {exc}",
            code="tracker_unsupported",
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TrackerError(
            f"The tracker gate directory {base} is not a plain directory; "
            f"refusing to trust a gate that can be replaced under a held lock.",
            code="tracker_unsupported",
        )
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise TrackerError(
            f"The tracker gate directory {base} belongs to uid {info.st_uid}, "
            f"not to this process ({os.geteuid()}).",
            code="tracker_unsupported",
        )
    if info.st_mode & 0o022:
        raise TrackerError(
            f"The tracker gate directory {base} is group/world writable; "
            f"another user could replace the lock files in it.",
            code="tracker_unsupported",
        )


def lock_timeout() -> float:
    return _env_seconds(_LOCK_TIMEOUT_ENV, DEFAULT_LOCK_TIMEOUT_SECONDS)


_thread_locks: "dict[str, threading.Lock]" = {}
_thread_locks_guard = threading.Lock()


def _thread_lock(key: str) -> threading.Lock:
    """One lock object per identity in this process.

    flock conflicts between two open descriptors even in one process, but a
    thread that waits must not spin on the file descriptor; the thread lock
    makes same-process waiting orderly and gives Windows the same behaviour.
    """
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.Lock())


def _lock_primitive() -> str:
    if os.name == "nt":
        try:
            import msvcrt  # noqa: F401

            return "msvcrt"
        except ImportError:  # pragma: no cover - CPython on Windows always has it
            pass
    else:
        try:
            import fcntl  # noqa: F401

            return "fcntl"
        except ImportError:  # pragma: no cover - CPython on POSIX always has it
            pass
    raise TrackerError(
        "This platform has no advisory file locking; refusing to open a "
        "single-process bd database without serialization.",
        code="tracker_unsupported",
    )


def _try_lock(fd: int) -> bool:
    if _lock_primitive() == "msvcrt":  # pragma: no cover - exercised on Windows
        import msvcrt

        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    except OSError as exc:
        if exc.errno in (errno.ENOLCK, errno.EOPNOTSUPP, errno.ENOSYS):
            # A filesystem without advisory locks (some network mounts) must
            # not silently degrade into two writers of one embedded database.
            raise TrackerError(
                f"Advisory locking is unavailable here ({exc}); refusing to "
                f"open a single-process bd database without serialization.",
                code="tracker_unsupported",
            ) from exc
        return False


def _unlock(fd: int) -> None:
    if _lock_primitive() == "msvcrt":  # pragma: no cover - exercised on Windows
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def lock_names(identity: StorageIdentity) -> "tuple[str, ...]":
    """Every gate file one command on *identity* must hold, globally ordered.

    One lock per *physical root* - two workspaces may point their
    ``dolt_data_dir`` at one directory and must then exclude each other - plus
    one per-workspace gate that stays the same before and after ``bd init``.
    The names are sorted, and every process builds the same order from the
    same names, so two commands that share only one of several roots always
    queue for it in the same direction and cannot deadlock.
    """
    names = {f"rt-{key}.lock" for key in identity.root_keys}
    names.add(f"ws-{identity.workspace_key}.lock")
    return tuple(sorted(names))


def lock_paths(identity: StorageIdentity) -> "tuple[Path, ...]":
    """The gate files of *identity*, in acquisition order."""
    directory = gate_directory()
    return tuple(directory / name for name in lock_names(identity))


def lock_path(identity: StorageIdentity) -> Path:
    """The first gate file of *identity* (the workspace-independent root lock)."""
    return lock_paths(identity)[0]


@contextmanager
def hold(
    identity: StorageIdentity,
    *,
    timeout: Optional[float] = None,
    poll: float = 0.05,
) -> Iterator[Path]:
    """Hold every physical root of *identity*; TrackerError when busy.

    All gates are taken in one fixed order with one shared budget: a command
    holds everything it may open before it opens any of it, and releases it
    only after its process is gone. The lock files are never deleted - not on
    release, not on error: another process may be waiting on that inode.
    """
    budget = lock_timeout() if timeout is None else max(0.0, float(timeout))
    names = lock_names(identity)
    paths = lock_paths(identity)
    deadline = time.monotonic() + budget
    thread_locks = [_thread_lock(name) for name in names]
    held_threads: list = []
    try:
        for name, thread_lock in zip(names, thread_locks):
            left = deadline - time.monotonic()
            if not thread_lock.acquire(timeout=max(0.0, left)):
                raise TrackerError(
                    f"The tracker for {identity.label} is busy in this process; "
                    f"waited {budget:.0f}s for the previous command.",
                    code="tracker_busy",
                    retryable=True,
                )
            held_threads.append(thread_lock)
        fds: "list[int]" = []
        try:
            for path in paths:
                try:
                    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                except OSError as exc:
                    raise TrackerError(
                        f"The tracker gate {path} cannot be opened: {exc}",
                        code="tracker_unsupported",
                    ) from exc
                fds.append(fd)
                while True:
                    if _try_lock(fd):
                        break
                    if time.monotonic() >= deadline:
                        raise TrackerError(
                            f"The tracker for {identity.label} is busy: another "
                            f"bd command holds {path.name} (waited {budget:.0f}s). "
                            f"Try again when it finishes.",
                            code="tracker_busy",
                            retryable=True,
                        )
                    time.sleep(poll)
            yield paths[0]
        finally:
            for fd in reversed(fds):
                try:
                    _unlock(fd)
                except OSError:  # closing releases it as well
                    logger.debug("Could not unlock fd %s; closing releases it", fd, exc_info=True)
                try:
                    os.close(fd)
                except OSError:  # pragma: no cover - defensive
                    pass
    finally:
        for thread_lock in reversed(held_threads):
            thread_lock.release()


# ---------------------------------------------------------------------------
# Backend and its version
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Backend:
    """Where bd runs, spelled so two ways of running it never look alike."""

    kind: str  # "host" | "container" | "docker"
    name: str  # bd binary path, container id, or image
    bd_path: str = ""

    @classmethod
    def host(cls, bd_path: str) -> "Backend":
        return cls("host", bd_path)

    @classmethod
    def container(cls, container_id: str, bd_path: str = "/usr/local/bin/bd") -> "Backend":
        return cls("container", container_id, bd_path)

    @classmethod
    def docker(cls, image: str, bd_path: str = "/usr/local/bin/bd") -> "Backend":
        return cls("docker", image, bd_path)

    @property
    def cache_key(self) -> str:
        return f"{self.kind}:{self.name}:{self.bd_path}"

    @property
    def label(self) -> str:
        if self.kind == "host":
            return f"host bd ({self.name})"
        if self.kind == "container":
            return f"container {self.name} bd ({self.bd_path})"
        return f"docker image {self.name} bd ({self.bd_path})"


_version_cache: "dict[str, Optional[str]]" = {}
_version_cache_guard = threading.Lock()

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(text: Optional[str]) -> Optional[tuple]:
    """``bd version 1.3.0 (...)`` -> ``(1, 3, 0)``; None when not there."""
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _same_version(first: str, second: str) -> bool:
    """Whether two version strings name one build.

    Numeric parts decide when both can be read (``1.3.0`` and ``v1.3.0+abc``
    are one bd); the raw strings are the fallback when either cannot be.
    """
    left, right = parse_version(first), parse_version(second)
    if left is not None and right is not None:
        return left == right
    return str(first).strip() == str(second).strip()


def _version_command(backend: Backend) -> list:
    if backend.kind == "host":
        return [backend.name, "version"]
    if backend.kind == "container":
        return ["docker", "exec", backend.name, backend.bd_path, "version"]
    return ["docker", "run", "--rm", backend.name, backend.bd_path, "version"]


def _probe_enabled() -> bool:
    return _env_flag(_PROBE_ENV, default=True)


def forget_version(backend: Optional[Backend] = None) -> None:
    """Drop the cached version (a rebuilt image, a new bd); tests use it too."""
    with _version_cache_guard:
        if backend is None:
            _version_cache.clear()
        else:
            _version_cache.pop(backend.cache_key, None)


def probe_version(backend: Backend) -> Optional[str]:
    """``bd version`` of the backend, once per process; None when unknown.

    Best effort by design: a missing bd, a stopped container or a slow daemon
    must not turn a tracker call into a failure. The caller's actual command
    reports its own error.
    """
    if not _probe_enabled():
        return None
    with _version_cache_guard:
        if backend.cache_key in _version_cache:
            return _version_cache[backend.cache_key]
    if backend.kind == "host" and not os.path.isfile(backend.name):
        version = None
    else:
        try:
            completed = subprocess.run(
                _version_command(backend),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            text = (completed.stdout or "") + (completed.stderr or "")
            match = _VERSION_RE.search(text)
            version = match.group(0) if match else None
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("Cannot read bd version of %s: %s", backend.label, exc)
            version = None
    with _version_cache_guard:
        _version_cache[backend.cache_key] = version
    return version


# ---------------------------------------------------------------------------
# Backend/version compatibility record
# ---------------------------------------------------------------------------


def _record_path(identity: StorageIdentity) -> Path:
    return gate_directory() / f"{identity.key}.backend.json"


def _read_record(identity: StorageIdentity) -> Optional[dict]:
    try:
        data = json.loads(_record_path(identity).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_record(identity: StorageIdentity, record: Mapping[str, object]) -> None:
    """Replace the record atomically; never fail the command over it."""
    path = _record_path(identity)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temp.write_text(json.dumps(dict(record), ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        logger.debug("Could not record the bd backend of %s", identity.label, exc_info=True)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


_compat_warned: set = set()
_compat_warned_guard = threading.Lock()


# ---------------------------------------------------------------------------
# Quarantine: a stopped command that could not be confirmed stopped
# ---------------------------------------------------------------------------


def _quarantine_path(root_key: str) -> Path:
    return gate_directory() / f"rt-{root_key}.quarantine.json"


def _read_quarantine(root_key: str) -> Optional[dict]:
    try:
        data = json.loads(_quarantine_path(root_key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_quarantine(root_key: str, record: Mapping[str, object]) -> None:
    """Persist the quarantine atomically; it must survive a crash of this process."""
    path = _quarantine_path(root_key)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temp.write_text(json.dumps(dict(record), ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        logger.error(
            "Could not record the tracker quarantine %s; a database may still "
            "be open in a container that would not stop",
            path,
            exc_info=True,
        )
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _clear_quarantine(root_key: str) -> None:
    try:
        _quarantine_path(root_key).unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not clear the tracker quarantine of %s", root_key, exc_info=True)


def _invocation_key(identity: StorageIdentity, backend: Backend, argv: Sequence[str]) -> str:
    """The exact call that left a container behind: argv, backend and directory."""
    text = "\n".join(
        [str(identity.directory), backend.cache_key, *(str(part) for part in argv)]
    )
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def quarantine_tracker(
    identity: StorageIdentity, backend: Backend, argv: Sequence[str], container: str
) -> None:
    """Record that *container* may still hold the database of every root of *identity*.

    Written while the command's locks are still held, so the next command
    cannot slip in between the failed stop and the record. It is a file, not
    memory: a crash of this process must not forget an open database.
    """
    record = {
        "container": container,
        "kind": backend.kind,
        "bd_path": backend.bd_path,
        "backend": backend.label,
        "directory": str(identity.directory),
        "invocation": _invocation_key(identity, backend, argv),
        "argv": [str(part) for part in argv][:32],
        "roots": [str(root) for root in identity.roots],
        "created_at": int(time.time()),
    }
    for root_key in identity.root_keys:
        _write_quarantine(root_key, record)


def bd_process_in_container(
    container: str, bd_path: str, argv: Sequence[str]
) -> Optional[bool]:
    """Whether the exact bd command still runs in a long-lived container.

    A run's container must never be stopped or killed by the gateway - other
    work lives there - so the only confirmation available is the container's
    own process list. ``docker top`` is read-only and needs no shell inside the
    image. ``True``: the command line is still there; ``False``: it is not, or
    the container itself is gone; ``None``: the question could not be answered,
    which is treated as "may still be running".
    """
    try:
        done = subprocess.run(
            ["docker", "top", container],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=KILL_GRACE_SECONDS + 5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        message = (done.stderr or "") + (done.stdout or "")
        if "no such" in message.lower():
            return False
        return None
    needle = " ".join([str(bd_path), *(str(part) for part in argv)])
    return needle in (done.stdout or "")


def _refuse_if_quarantined(identity: StorageIdentity) -> None:
    """Fail closed while a container that outlived its command may hold a root.

    The check is self-healing and never waits indefinitely and never kills
    another run's container: as soon as ``docker inspect`` confirms the
    container is gone or stopped, the quarantine is dropped. Until then every
    call that would open one of its physical roots is refused, because two
    bd processes on one embedded database is exactly what must not happen.
    """
    for root_key in identity.root_keys:
        record = _read_quarantine(root_key)
        if not record:
            continue
        container = str(record.get("container") or "")
        if not container:
            _clear_quarantine(root_key)
            continue
        if record.get("kind") == "container":
            # A run's own container is not ours to stop: it is confirmed only
            # when the recorded command line no longer exists in it.
            alive = bd_process_in_container(
                container,
                str(record.get("bd_path") or ""),
                [str(part) for part in record.get("argv") or []],
            )
        else:
            alive = False if container_stopped(container) else True
        if alive is False:
            _clear_quarantine(root_key)
            continue
        if record.get("kind") == "container":
            hint = (
                f"Check `docker top {container}`; bd is still running there or its "
                f"state cannot be read."
            )
        else:
            hint = f"Stop it (`docker stop {container}`)."
        raise TrackerError(
            f"The tracker for {identity.label} is quarantined: the bd container "
            f"{container} did not stop after its command timed out and may still "
            f"hold the database ({', '.join(str(root) for root in identity.roots)}). "
            f"{hint} Nothing else is allowed to open this database until then.",
            code="tracker_quarantine",
            retryable=True,
        )


def _warn_once(key: object, message: str) -> None:
    with _compat_warned_guard:
        if key in _compat_warned:
            return
        _compat_warned.add(key)
    logger.warning("%s", message)


def check_backend_compatibility(
    identity: StorageIdentity, backend: Backend, version: Optional[str]
) -> None:
    """Refuse a different bd on one physical database; warn on an unverifiable one.

    The record is the first bd **version** that opened this physical database
    in this gate directory; the backend it came through is kept with it only
    to name the culprit. Host and container bd of the same version are the
    normal GRID arrangement - the UI runs the host's, the agents the image's -
    and are already serialized here, so they are not a mismatch. A different
    version is: two builds of a single-process embedded store taking turns on
    one directory is the uncontrolled case the analysis names, and bd itself
    migrates the schema on a version change, so "warn and go on" is not a safe
    default. ``GRID_TRACKER_ALLOW_BACKEND_MISMATCH=1`` opts out explicitly
    (warn and keep working); ``GRID_TRACKER_STRICT_BACKEND=1`` forces the
    refusal even with the opt-out set.

    A version that cannot be read is not compared: the probe is best effort,
    and a slow daemon must not take the tracker down. It is still reported,
    and it never becomes a "too old" claim - the minimum-version problem is
    only stated for a version that was actually parsed.
    """
    record = _read_record(identity) or {}
    recorded_version = record.get("version") or None
    recorded_backend = record.get("backend")
    problems = []
    recorded_parsed = parse_version(recorded_version)
    if (
        recorded_version
        and version
        and recorded_parsed is not None
        and not _same_version(recorded_version, version)
    ):
        problems.append(
            f"it was first opened with bd {recorded_version}"
            + (f" through {recorded_backend}" if recorded_backend else "")
        )
    elif recorded_parsed is not None and not version:
        problems.append(
            f"it was first opened with bd {recorded_version} and the bd version "
            f"of this call could not be read, so the same binary cannot be confirmed"
        )
    parsed = parse_version(version)
    if parsed is not None and parsed < MINIMUM_BD_VERSION:
        minimum = ".".join(str(part) for part in MINIMUM_BD_VERSION)
        problems.append(f"bd {version} is older than the supported {minimum}")

    if problems:
        message = (
            f"bd mismatch for the tracker in {identity.label}: now running bd "
            f"through {backend.label}"
            + (f" (bd {version})" if version else "")
            + "; "
            + "; ".join(problems)
            + "."
        )
        # An unreadable version is reported, not refused: the probe is best
        # effort and a transient failure must not make the tracker unusable.
        refusable = bool(version)
        refuse = refusable and not _env_flag(_ALLOW_MISMATCH_ENV)
        if refuse or _env_flag(_STRICT_ENV):
            raise TrackerError(
                message
                + " Refusing: one physical database, one bd binary"
                + " (set GRID_TRACKER_ALLOW_BACKEND_MISMATCH=1 to override).",
                code="tracker_backend",
            )
        _warn_once((identity.key, tuple(problems)), message)

    # The record is the *first* version: a mismatch is not swallowed by
    # overwriting it, so every later command still sees it.
    # Only a version that could not be read at first is filled in.
    if not record or (not recorded_version and version):
        _write_record(
            identity,
            {
                "backend": backend.label,
                "version": version,
                "roots": [str(root) for root in identity.roots],
                "recorded_at": int(time.time()),
            },
        )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class ExecutionResult:
    """What one bd process did; shaped like the dict the tools already return."""

    success: bool
    output: str
    error: str
    exit_code: int
    timed_out: bool = False
    missing_executable: bool = False

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "exit_code": self.exit_code,
        }


def _container_mount(directory: str) -> str:
    # Docker on Windows accepts forward slashes in volume specs.
    return os.path.abspath(directory).replace("\\", "/")


def _docker_user(directory: str) -> Optional[str]:
    """The uid/gid a temporary container should write as, when it can be known.

    bd creates files inside the mounted tracker. A container running as root
    leaves them root-owned and the user's next command (or git) fails, so the
    container runs as whoever owns the project directory:

    * this process is root: the directory's owner, unless the directory is
      root's too - then there is no better guess than Docker's default;
    * this process is not root: its own uid, but only when it owns the
      directory (running as a stranger's uid would only trade one wrong owner
      for another, and the write would fail anyway).
    """
    try:
        info = os.stat(directory)
    except OSError:
        return None
    if hasattr(os, "geteuid"):
        if os.geteuid() == 0:
            return None if info.st_uid == 0 else f"{info.st_uid}:{info.st_gid}"
        if info.st_uid == os.geteuid():
            return f"{os.geteuid()}:{os.getegid()}"
    return None


def _container_workdir(container_workdir: Optional[str]) -> str:
    from utils.path_utils import CONTAINER_WORKDIR

    return container_workdir or CONTAINER_WORKDIR


def _docker_env_flags(env: Optional[Mapping[str, str]]) -> list:
    """The one variable that crosses the container boundary: bd's audit actor.

    ``BEADS_ACTOR`` is bd's own setting, so a write made in a container is
    attributed the same way as one made on the host. Everything else - above
    all the storage routing variables (``BEADS_DIR``, ``BEADS_DOLT_DATA_DIR``,
    ``BEADS_DOLT_SERVER_MODE``, the shared/proxied server roots) - is *not*
    passed: the host's routing must not silently move the container to another
    physical database than the one this gateway holds the locks for.
    """
    if not env:
        return []
    actor = env.get("BEADS_ACTOR")
    if not actor:
        return []
    text = str(actor)
    if len(text) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise TrackerError(
            "BEADS_ACTOR contains control characters; refusing to pass it into "
            "a bd container.",
            code="tracker_scope",
        )
    return ["-e", f"BEADS_ACTOR={text}"]


def build_command(
    argv: Sequence[str],
    *,
    backend: Backend,
    directory: str,
    container_workdir: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    env: Optional[Mapping[str, str]] = None,
) -> "tuple[list, Optional[str], Optional[str]]":
    """The process to run for *argv*: (command, cwd, temp container name)."""
    if backend.kind == "host":
        return [backend.name, *argv], directory, None
    env_flags = _docker_env_flags(env)
    if backend.kind == "container":
        cmd = [
            "docker", "exec",
            "-w", _container_workdir(container_workdir),
            "-e", "BEADS_DAEMON=0",
            *env_flags,
            backend.name,
            # `timeout` ends bd inside the container: stopping the docker
            # client alone would leave bd there, holding its database.
            "timeout", "-k", str(KILL_GRACE_SECONDS), str(max(1, int(round(timeout)))),
            backend.bd_path, *argv,
        ]
        return cmd, None, None
    name = f"grid-bd-{uuid.uuid4().hex[:12]}"
    # A redirect or symlinked .beads may live outside the caller's directory.
    # Mount the workspace that actually owns the locked tracker, not its alias.
    identity = storage_identity(directory)
    mounted_directory = identity.beads.parent
    metadata = _read_metadata(identity.beads) if identity.tracker_exists else {}
    routing_env = ("BEADS_DOLT_DATA_DIR", "BEADS_DOLT_SERVER_MODE",
                   "BEADS_DOLT_SHARED_SERVER", "BEADS_SHARED_SERVER_DIR",
                   "BEADS_PROXIED_SERVER_ROOT_PATH")
    if (any(os.environ.get(key) for key in routing_env)
            or any(os.path.isabs(str(metadata.get(key) or ""))
                   for key in ("dolt_data_dir", "database"))
            or (identity.tracker_exists and _dolt_mode(
                identity.beads, metadata, _read_yaml_config(identity.beads)) != "embedded")):
        raise TrackerError(
            "The temporary bd container cannot safely translate host storage "
            "routing. Use the configured host backend. Nothing was started.",
            code="tracker_scope",
        )
    for root in identity.roots:
        if not root.is_relative_to(mounted_directory):
            raise TrackerError(
                "The temporary bd container cannot access an external storage "
                "root through its workspace mount; use the configured host "
                "backend instead. Nothing was started.",
                code="tracker_scope",
            )
    cmd = [
        "docker", "run", "--rm", "--name", name, "-i", "-e", "BEADS_DAEMON=0",
        *env_flags,
    ]
    user = _docker_user(str(mounted_directory))
    if user:
        cmd += ["--user", user]
    cmd += [
        "-v", f"{_container_mount(str(mounted_directory))}:{_container_workdir(container_workdir)}",
        "-w", _container_workdir(container_workdir),
        backend.name, backend.bd_path, *argv,
    ]
    return cmd, None, name


def _stop_process_group(proc: subprocess.Popen, *, own_session: bool = True) -> None:
    """Stop *proc* and only what it started, by process group.

    *own_session* means the command was started with ``start_new_session``, so
    its pid *is* the process group it leads even after the leader itself died:
    children that outlived it are still in that group and may still hold the
    database. Asking ``getpgid`` alone would find nothing there and leave them
    running behind a released lock.
    """
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=KILL_GRACE_SECONDS + 5,
            )
        except (OSError, subprocess.SubprocessError):
            proc.kill()
        return
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid if own_session else None
    # Never the group this server itself runs in: only the command's own tree.
    own_group = os.getpgid(0)
    target_group = pgid if pgid is not None and pgid != own_group else None
    if target_group is not None:
        try:
            os.killpg(target_group, 0)  # does that group still exist at all?
        except ProcessLookupError:
            return
        except OSError:
            pass

    def send(sig: int) -> None:
        if target_group is not None:
            os.killpg(target_group, sig)
        else:
            proc.send_signal(sig)

    try:
        send(signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass
    # The leader may be gone while a child it started still holds the pipes or
    # the database: the group gets SIGKILL whether or not the wait timed out.
    try:
        send(signal.SIGKILL)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable process
        logger.warning("bd process %s did not stop after SIGKILL", proc.pid)


def _stop_container(name: str) -> None:
    """Stop the short-lived container *name* - ours, never a pattern."""
    try:
        subprocess.run(
            ["docker", "kill", name], capture_output=True, timeout=KILL_GRACE_SECONDS + 5
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("Could not stop the temporary bd container %s", name, exc_info=True)


def container_stopped(name: str, budget: float = KILL_GRACE_SECONDS) -> bool:
    """Whether the container *name* is confirmed to be gone or stopped.

    A killed ``docker`` client does not mean the container it started is gone:
    bd inside it can still hold the database. Confirmed with ``docker inspect``
    (a container that no longer exists counts as stopped); never by pattern.
    """
    deadline = time.monotonic() + budget
    while True:
        try:
            done = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=budget,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode != 0 or (done.stdout or "").strip().lower() == "false":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _read_available(stream, seconds: float) -> str:
    """Whatever *stream* already holds, waiting at most *seconds* for more.

    Used after a kill: the leader may be gone while a descendant still holds
    the write end, and blocking on that pipe would keep the tracker lock for
    as long as the stranger lives.
    """
    if stream is None or os.name == "nt":  # select() takes sockets on Windows
        return ""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return ""
    chunks = []
    deadline = time.monotonic() + seconds
    try:
        # A buffered text wrapper can block past select()'s promise, so the
        # descriptor is read raw and non-blocking: whatever is already in the
        # pipe is taken and nothing is waited for beyond *seconds*.
        os.set_blocking(fd, False)
    except (AttributeError, OSError):
        pass
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                break
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:  # pragma: no cover - raced with select
                continue
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    except (OSError, ValueError):
        pass
    return b"".join(chunks).decode("utf-8", "replace")


def _as_text(value) -> str:
    """Captured output as text.

    ``subprocess.TimeoutExpired.output`` carries raw bytes on several Python
    versions even for a ``text=True`` child, so concatenating it with ``str``
    raises ``TypeError`` exactly on the timeout path - the one path that must
    never fail.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _close_pipes(proc: subprocess.Popen) -> None:
    """Close the command's pipes without waiting for a stranger to let go."""
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except (OSError, ValueError):
            pass


def _run_process(
    cmd: Sequence[str],
    *,
    cwd: Optional[str],
    env: Optional[Mapping[str, str]],
    timeout: float,
    container_name: Optional[str] = None,
) -> ExecutionResult:
    try:
        proc = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            # Its own session/process group: a timeout stops this command's
            # tree by id and never anything else on the host.
            start_new_session=os.name != "nt",
        )
    except FileNotFoundError as exc:
        return ExecutionResult(False, "", f"Executable not found: {exc}", -1, missing_executable=True)
    except OSError as exc:
        return ExecutionResult(False, "", str(exc), -1)

    try:
        output, error = proc.communicate(timeout=timeout)
        return ExecutionResult(
            proc.returncode == 0,
            _as_text(output).strip(),
            _as_text(error).strip(),
            proc.returncode,
        )
    except subprocess.TimeoutExpired as expired:
        if container_name:
            _stop_container(container_name)
        # start_new_session above: the command's pid is its own process group
        # even if the leader has already died.
        _stop_process_group(proc, own_session=os.name != "nt")
        # Everything the command said before it was stopped, plus what the
        # pipes still hold. A descendant that escaped the group can hold the
        # write end forever; the drain is bounded and the pipes are closed, so
        # that stranger cannot keep this command - and the lock behind it - busy.
        output = _as_text(expired.output)
        error = _as_text(expired.stderr)
        output += _read_available(proc.stdout, OUTPUT_DRAIN_SECONDS)
        error += _read_available(proc.stderr, OUTPUT_DRAIN_SECONDS)
        _close_pipes(proc)
        try:
            proc.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable process
            logger.warning("bd process %s did not stop after SIGKILL", proc.pid)
        note = f"Command timed out after {timeout:g}s and was stopped"
        if container_name:
            note += f" (container {container_name})"
        return ExecutionResult(
            False, (output or "").strip(), note + (f"\n{error.strip()}" if error else ""), -1, timed_out=True
        )


def _require_same_tracker(identity: StorageIdentity, beads_dir: str) -> None:
    """Refuse an explicit ``BEADS_DIR`` that names another physical tracker.

    bd prefers ``BEADS_DIR`` over its own ancestor walk, so a value that points
    elsewhere would have bd open one database while this command holds the lock
    of another - the one arrangement this gateway exists to prevent.
    """
    try:
        given = _canonical_path(
            beads_dir if os.path.isabs(beads_dir) else identity.directory / beads_dir
        )
    except TrackerError as exc:
        raise TrackerError(
            f"BEADS_DIR {beads_dir!r} cannot be resolved: {exc}", code="tracker_scope"
        ) from exc
    if _reads_as_workspace(given):
        given = _follow_redirect(given)
    if _identity_text(given) != _identity_text(identity.beads):
        raise TrackerError(
            f"BEADS_DIR {beads_dir!r} is not the tracker of {identity.label} "
            f"({identity.beads}): one command cannot be serialized on one "
            f"database and open another.",
            code="tracker_scope",
        )


def execute(
    argv: Sequence[str],
    *,
    directory: "str | Path",
    backend: Backend,
    container_workdir: Optional[str] = None,
    timeout: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
    lock_wait: Optional[float] = None,
) -> ExecutionResult:
    """Run one bd command against the physical tracker in *directory*.

    Serialized against every other command on the same physical database
    (whichever path or backend reached it), checked against the recorded
    backend, and stopped addressably on timeout. Raises :class:`TrackerError`
    for refusals - a busy database, a scope outside any tracker, a strict
    backend mismatch, a platform without advisory locks - so a caller can
    report the reason instead of a vague failure.
    """
    identity = storage_identity(directory)
    budget = DEFAULT_TIMEOUT_SECONDS if timeout is None else max(0.1, float(timeout))
    version = probe_version(backend)

    with hold(identity, timeout=lock_wait):
        check_backend_compatibility(identity, backend, version)
        # A container that outlived an earlier command may still hold this
        # database. Fail closed here, under the lock, before opening anything.
        _refuse_if_quarantined(identity)
        forwarded = env
        if backend.kind != "host" and env:
            # Only bd's own audit actor crosses into a container. Storage
            # routing (BEADS_DIR, the dolt data dir, server modes) is the
            # host's business: passing it would point the container at a
            # database this gateway has no lock for.
            if env.get("BEADS_DIR"):
                _require_same_tracker(identity, env["BEADS_DIR"])
            forwarded = {
                name: value for name, value in env.items() if name == "BEADS_ACTOR"
            }
        cmd, cwd, container_name = build_command(
            argv, backend=backend, directory=str(identity.directory),
            container_workdir=container_workdir, timeout=budget, env=forwarded,
        )
        run_env = None
        if backend.kind == "host":
            run_env = os.environ.copy()
            run_env["BEADS_DAEMON"] = "0"
            # bd prefers BEADS_DIR over its own ancestor walk. An ambient value
            # from the server's environment would move the command to another
            # tracker while this lock is held for *this* one; the directory
            # argument is the authority. An explicitly passed BEADS_DIR must
            # name the same physical tracker or the call is refused.
            run_env.pop("BEADS_DIR", None)
            if env:
                if env.get("BEADS_DIR"):
                    _require_same_tracker(identity, env["BEADS_DIR"])
                run_env.update(env)
        # The docker client gets extra time to report what the inner timeout
        # did; its own process group is stopped if it does not.
        outer = (
            budget
            if backend.kind == "host"
            else budget + KILL_GRACE_SECONDS + DOCKER_CLIENT_GRACE_SECONDS
        )
        result = _run_process(
            cmd, cwd=cwd, env=run_env, timeout=outer, container_name=container_name
        )
        if result.timed_out and container_name:
            # The lock goes the moment this returns. A killed docker client is
            # not a stopped container: wait for the container itself to be down
            # before anyone else can be let in on the same database.
            if not container_stopped(container_name):
                # Still holding the locks: record the quarantine *now*, so the
                # next command cannot pass between the failed stop and the
                # record. It is persistent, so a crash of this process does not
                # forget a database that may still be open.
                quarantine_tracker(identity, backend, argv, container_name)
                logger.error(
                    "The bd container %s is still running after a timeout; its "
                    "database is quarantined until it is confirmed stopped",
                    container_name,
                )
                result.error = (
                    (result.error or "")
                    + f"\nThe container {container_name} may still be running "
                    f"and holding the database; the tracker is quarantined until "
                    f"it is confirmed stopped (`docker stop {container_name}`)."
                )
        elif result.timed_out and backend.kind == "container":
            # The same reasoning for a run's own container: killing the
            # `docker exec` client does not end bd inside it. It is never
            # stopped or killed - other work lives there - so the only proof is
            # that the exact command line is gone.
            alive = bd_process_in_container(backend.name, backend.bd_path, argv)
            if alive is not False:
                quarantine_tracker(identity, backend, argv, backend.name)
                logger.error(
                    "The bd command in container %s may still be running after a "
                    "timeout; its database is quarantined",
                    backend.name,
                )
                result.error = (
                    (result.error or "")
                    + f"\nThe bd command in container {backend.name} may still be "
                    f"running and holding the database; the tracker is quarantined "
                    f"until `docker top {backend.name}` no longer shows it."
                )
        return result

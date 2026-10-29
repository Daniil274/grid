"""Versioned revision manifests for conversations.

A stored conversation is only meaningful to a runtime that reads its history the
same way, runs under a version of the code that still speaks its contracts, and
serves the resolved system configuration it was written with. This module makes
that statement explicit and checkable: a conversation is *pinned* to a manifest
the first time a turn writes into it, and every later turn judges the manifest
before any external operation happens (a model call, a tool, a sub-agent).

The manifest is content addressed (``digest`` over everything else) so a hand
edited file is refused rather than obeyed. Admission is *graded*: only a real
incompatibility is a refusal - a manifest this runtime cannot read (unknown
format version, unsupported contract version, malformed), one altered after
pinning, a contract changed at the same version, a manifest of another
conversation, or an unresolved system. A Grid version, Python runtime or
resolved system configuration that moved on, and a conversation stored before
pinning existed at all (*legacy*), are not incompatibilities: the stored history
is intact and readable, so the conversation continues, is re-pinned to the
current revision, and the previous manifest is appended to an append-only
history in the conversation (``ContextManager.repin_revision``). Each re-pin
comes with a typed notice (code plus the sentence a person reads) that the
transport shows without treating it as an error.

A first pin stays immutable: only :func:`admit_turn` re-pins, only along this
graded path, and only by appending to that history - a pin is never rewritten
in place.

The four contracts are versioned apart because their compatibility questions
differ: ``history`` (how messages and executions are serialized), ``checkpoint``
(what a workspace snapshot resumes), ``runtime`` (the interpreter the turn ran
on) and ``tools`` (which tool semantics the stored calls were made with).
Permissions are *not* part of the manifest: the action policy stays the single
authority on what may run, for every turn, pinned or not.

The system configuration is described twice: a digest over the whole resolved
configuration - prompts included, so changing one fails closed - and a small
secret-free projection (agent/model/tool keys and resolved limits) kept for
people reading the manifest. Raw prompts, credentials and environment values are
never stored: they only ever reach the hash.

Nothing here claims a turn can be replayed or run exactly once. The manifest
describes compatibility of a stored conversation, no more.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from core import __version__ as GRID_VERSION
from utils.exceptions import AgentError

#: Version of the manifest document itself. A manifest from another version is
#: refused, not guessed at.
MANIFEST_VERSION = 1
SUPPORTED_MANIFEST_VERSIONS = (1,)

#: Contracts a conversation depends on, each versioned on its own.
HISTORY_CONTRACT = {
    "name": "grid.context.history",
    "version": 1,
    "fields": [
        "conversation_history",
        "execution_history",
        "metadata",
        "created_at",
        "updated_at",
    ],
}
CHECKPOINT_CONTRACT = {
    "name": "grid.context.checkpoint",
    "version": 1,
    "unit": "workspace-before-turn",
    "resumes": "workspace-files-only",
}
RUNTIME_CONTRACT = {
    "name": "grid.agent.runtime",
    "version": 1,
    "serialization": "json",
}
TOOLS_CONTRACT = {
    "name": "grid.tools.contract",
    "version": 1,
    # The action policy, not the manifest, decides what a tool may do.
    "authority": "action_policy",
}

CONTRACTS: Dict[str, Dict[str, Any]] = {
    "history": HISTORY_CONTRACT,
    "checkpoint": CHECKPOINT_CONTRACT,
    "runtime": RUNTIME_CONTRACT,
    "tools": TOOLS_CONTRACT,
}
SUPPORTED_CONTRACT_VERSIONS: Dict[str, tuple] = {
    name: (section["version"],) for name, section in CONTRACTS.items()
}

# -- machine-readable reasons -------------------------------------------------

OK = "revision.compatible"
LEGACY = "revision.legacy_context"
TAMPERED = "revision.manifest_tampered"
UNKNOWN_MANIFEST = "revision.manifest_version_unsupported"
GRID_VERSION_CHANGED = "revision.grid_version_changed"
CONTRACT_VERSION_UNSUPPORTED = "revision.contract_version_unsupported"
CONTRACT_CHANGED = "revision.contract_changed"
RUNTIME_CHANGED = "revision.runtime_changed"
SYSTEM_CONFIG_CHANGED = "revision.system_config_changed"
SYSTEM_UNRESOLVED = "revision.system_unresolved"
NOT_IMMUTABLE = "revision.manifest_immutable"
NO_CONTEXT = "revision.context_unknown"
MANIFEST_MALFORMED = "revision.manifest_malformed"
CONTEXT_MISMATCH = "revision.context_mismatch"

#: Differences a conversation *survives*: its stored history is intact and
#: readable, so the turn continues on this runtime and the conversation is
#: re-pinned to the current revision. Every other difference is a refusal.
CONTINUABLE_CODES = frozenset(
    {LEGACY, GRID_VERSION_CHANGED, RUNTIME_CHANGED, SYSTEM_CONFIG_CHANGED}
)

#: The graded outcomes of :func:`review`.
REFUSE = "refuse"
CONTINUE = "continue"
PIN = "pin"

#: The human sentence of a re-pin notice, one per continuable difference.
_NOTICE_TEXT = {
    LEGACY: (
        "This conversation was stored before revision pinning. Its history is intact, "
        "so it continues and has been pinned to the current revision."
    ),
    GRID_VERSION_CHANGED: (
        "This conversation was pinned by another Grid version. Its history is intact, "
        "so it continues on {current} and has been re-pinned to this revision."
    ),
    SYSTEM_CONFIG_CHANGED: (
        "The system configuration changed since this conversation was pinned. Its history "
        "is intact, so it continues on the current configuration and has been re-pinned."
    ),
    RUNTIME_CHANGED: (
        "The Python runtime changed since this conversation was pinned. Its history is "
        "intact, so it continues and has been re-pinned to this revision."
    ),
}

#: Keys whose *values* are redacted before hashing: rotating a
#: credential must not invalidate a conversation, changing a prompt must.
_SENSITIVE_KEY_RE = re.compile(
    r"(api[-_]?key|apikey|secret|token|passw(or)?d|credential|authori[sz]ation"
    r"|bearer|private[-_]?key|access[-_]?key|session[-_]?key|headers|env)$",
    re.IGNORECASE,
)
#: A value that looks like a live credential must never be written, even under a
#: key this module does not know about.
_SECRET_VALUE_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})")
REDACTED = "<redacted>"


class ContextCompatibilityError(AgentError):
    """A conversation cannot be continued here: pinned to another revision.

    ``code`` is the machine-readable reason (``revision.*``) and ``details``
    carries the context id and what differed. The message is what the existing
    error plumbing shows a person; both are safe to log - no secrets enter
    either.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        context_id: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message, {"code": code, "context_id": context_id, **dict(details or {})})
        self.code = code
        self.context_id = context_id
        self.details = dict(self.details or {})


# -- canonical form and digests ------------------------------------------------


def canonical_json(value: Any) -> str:
    """*value* in one canonical JSON spelling, so a digest is stable."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest_of(value: Any) -> str:
    """A ``sha256:`` digest of *value* in canonical form."""
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def contract_section(name: str) -> Dict[str, Any]:
    """The manifest section of one contract: its name, version and definition digest."""
    definition = CONTRACTS[name]
    return {
        "name": definition["name"],
        "version": definition["version"],
        "digest": digest_of(definition),
    }


def redacted(value: Any) -> Any:
    """*value* with credential-looking keys and values replaced.

    Used only to build digest material, never to build what is stored, but a
    digest of a value that looks like a key must not be inferable either.
    """
    if isinstance(value, Mapping):
        clean: Dict[str, Any] = {}
        for key, item in value.items():
            if _SENSITIVE_KEY_RE.search(str(key)):
                clean[str(key)] = REDACTED
            else:
                clean[str(key)] = redacted(item)
        return clean
    if isinstance(value, (list, tuple)):
        return [redacted(item) for item in value]
    if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        return REDACTED
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def runtime_info() -> Dict[str, Any]:
    """The interpreter a turn runs on, as far as history compatibility cares."""
    info = sys.version_info
    return {
        "python": f"{info.major}.{info.minor}.{info.micro}",
        "python_series": f"{info.major}.{info.minor}",
        "implementation": platform.python_implementation(),
        "system": platform.system(),
    }


def _projection(grid_config: Any) -> Dict[str, Any]:
    """The secret-free, resolved view of a system configuration.

    Keys and resolved limits only: no prompt text, no credential, no environment
    value, no absolute path. Enough for a person to see what a conversation is
    pinned to, not enough to leak how the system is configured.
    """
    settings = getattr(grid_config, "settings", None)
    agents: Dict[str, Any] = {}
    for key, agent in (getattr(grid_config, "agents", None) or {}).items():
        model = getattr(agent, "model", None)
        turns = getattr(agent, "max_turns", None)
        timeout = getattr(agent, "timeout", None)
        agents[str(key)] = {
            "model": model if isinstance(model, str) else list(model or []),
            "tools": sorted(str(tool) for tool in (getattr(agent, "tools", None) or [])),
            "routable": bool(getattr(agent, "routable", True)),
            "mcp_enabled": bool(getattr(agent, "mcp_enabled", False)),
            "max_turns": turns if turns is not None else getattr(settings, "max_turns", None),
            "timeout": timeout if timeout is not None else getattr(settings, "agent_timeout", None),
        }
    models = {
        str(key): {
            "provider": getattr(model, "provider", None),
            "context_window": getattr(model, "context_window", None),
        }
        for key, model in (getattr(grid_config, "models", None) or {}).items()
    }
    tools = {
        str(key): str(getattr(tool, "type", "") or "")
        for key, tool in (getattr(grid_config, "tools", None) or {}).items()
    }
    policy = getattr(settings, "action_policy", None)
    policy_file = getattr(policy, "policy_file", None)
    return {
        "default_agent": getattr(settings, "default_agent", None),
        "max_history": getattr(settings, "max_history", None),
        "max_turns": getattr(settings, "max_turns", None),
        "agent_timeout": getattr(settings, "agent_timeout", None),
        # The policy stays the authority for permissions; only its mode is pinned.
        "action_policy": {
            "mode": getattr(policy, "mode", None),
            "policy_file": Path(str(policy_file)).name if policy_file else None,
        },
        "agents": agents,
        "models": models,
        "tools": tools,
    }


def system_snapshot(config: Any) -> Dict[str, Any]:
    """What the resolved system configuration is, as far as a conversation needs.

    ``config`` is a :class:`core.config.Config`; without one the system is
    *unresolved* and a turn fails closed rather than pin a conversation to
    nothing.

    The digest covers the resolved configuration *and* its external sources:
    the instruction files of the system skills and the source of local tool
    packages. A config only names a skill or a package; editing that file
    changes what an agent does, so it must change the pinned digest too. Only
    source-like files are read and only their digests are kept - credentials,
    environment files and anything that looks like key material are never
    opened for the digest and never stored.
    """
    grid_config = getattr(config, "config", None)
    if grid_config is None:
        return {"status": "unresolved", "source": None, "digest": None, "defaults": {}}
    dump = getattr(grid_config, "model_dump", None)
    material = dump() if callable(dump) else grid_config
    source = getattr(config, "config_path", None)
    base_dir = Path(str(source)).parent if source else None
    return {
        "status": "resolved",
        "source": Path(str(source)).name if source else None,
        "digest": digest_of(
            {
                "config": redacted(material),
                "external": _external_material(grid_config, base_dir),
            }
        ),
        "defaults": _projection(grid_config),
    }


#: A source file whose content changes what an agent does or can call.
_SOURCE_SUFFIXES = frozenset(
    {".py", ".md", ".txt", ".yaml", ".yml", ".toml", ".json", ".cfg", ".ini", ".sh"}
)
#: Names that hold, or may hold, credentials: never read for a digest.
_SECRET_FILE_RE = re.compile(
    r"(^\.env(\.|$)|\.env$|secret|credential|token|passwd|password|\.pem$|\.key$"
    r"|\.p12$|\.pfx$|id_rsa|id_ed25519|known_hosts|\.netrc$)",
    re.IGNORECASE,
)
#: Directories that are generated, installed or foreign to the system.
_SKIP_DIRS = frozenset(
    {"__pycache__", ".git", ".hg", ".svn", ".venv", "venv", "node_modules", ".mypy_cache", ".ruff_cache"}
)


def _source_digest(path: Optional[Path], base_dir: Optional[Path]) -> Dict[str, Any]:
    """A content digest of one source file, or why there is none.

    Never returns content and never follows a name that looks like a
    credential store.
    """
    if path is None:
        return {"status": "missing"}
    if _SECRET_FILE_RE.search(path.name) or path.suffix.lower() not in _SOURCE_SUFFIXES:
        return {"status": "not-source"}
    try:
        fingerprint = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(65536), b""):
                fingerprint.update(block)
    except OSError:
        return {"status": "unreadable"}
    label = str(path.relative_to(base_dir)) if base_dir is not None and _is_relative(path, base_dir) else path.name
    return {"status": "hashed", "file": label, "sha256": fingerprint.hexdigest()}


def _is_relative(path: Path, base_dir: Path) -> bool:
    try:
        path.relative_to(base_dir)
        return True
    except ValueError:
        return False


def _tree_digest(directory: Path, base_dir: Optional[Path]) -> Dict[str, Any]:
    """A digest over the source files of a directory tree, by relative path."""
    try:
        if not directory.is_dir():
            return {"status": "missing"}
    except OSError:
        return {"status": "unreadable"}
    files: Dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(directory).parts):
            continue
        allowed = _source_digest(path, base_dir)
        if allowed.get("status") == "hashed":
            try:
                files[str(path.relative_to(directory))] = allowed["sha256"]
            except ValueError:
                continue
    return {"status": "hashed", "files": files}


def _external_material(grid_config: Any, base_dir: Optional[Path]) -> Dict[str, Any]:
    """What a config points at: skill instructions and local tool sources."""
    if base_dir is None:
        return {"status": "unresolved", "skills": {}, "tool_sources": {}}
    skills: Dict[str, Any] = {}
    for agent_key, agent in (getattr(grid_config, "agents", None) or {}).items():
        for name in getattr(agent, "system_skills", None) or []:
            skill = None
            for extension in (".md", ".txt"):
                candidate = base_dir / "skills" / f"{name}{extension}"
                if candidate.is_file():
                    skill = candidate
                    break
            skills[f"{agent_key}/{name}"] = _source_digest(skill, base_dir)
    tool_sources: Dict[str, Any] = {}
    for key, tool in (getattr(grid_config, "tools", None) or {}).items():
        package = getattr(tool, "tool_package", None)
        if package:
            tool_sources[str(key)] = _tree_digest(base_dir / str(package), base_dir)
    project_tools = getattr(getattr(grid_config, "settings", None), "project_tools", None)
    if project_tools is not None and getattr(project_tools, "enabled", False):
        directory = getattr(project_tools, "tools_directory", None) or "./tools"
        tool_sources["project_tools"] = _tree_digest(base_dir / str(directory), base_dir)
    return {"status": "resolved", "skills": skills, "tool_sources": tool_sources}


def environment_of(config: Any = None) -> Dict[str, Any]:
    """The revision a turn started now would pin a conversation to."""
    return {
        "grid_version": GRID_VERSION,
        "platform": runtime_info(),
        "contracts": {name: contract_section(name) for name in CONTRACTS},
        "system": system_snapshot(config),
    }


# -- the manifest --------------------------------------------------------------


def build_manifest(
    context_id: str,
    environment: Mapping[str, Any],
    *,
    pinned_at: Optional[str] = None,
) -> Dict[str, Any]:
    """The manifest a conversation is pinned to. Content addressed by ``digest``."""
    manifest: Dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "context_id": context_id,
        "pinned_at": pinned_at or datetime.now().isoformat(),
        "grid_version": environment["grid_version"],
        "platform": dict(environment["platform"]),
        "contracts": {name: dict(section) for name, section in environment["contracts"].items()},
        "system": {
            "status": environment["system"]["status"],
            "source": environment["system"]["source"],
            "digest": environment["system"]["digest"],
            "defaults": environment["system"]["defaults"],
        },
    }
    manifest["digest"] = digest_of(manifest)
    return manifest


def manifest_digest_ok(manifest: Mapping[str, Any]) -> bool:
    """Whether the manifest still matches its own digest: unchanged since pinning."""
    if not isinstance(manifest, Mapping) or "digest" not in manifest:
        return False
    body = {key: value for key, value in manifest.items() if key != "digest"}
    return manifest.get("digest") == digest_of(body)


def _decision(ok: bool, code: str, reason: str) -> Dict[str, Any]:
    return {"ok": ok, "code": code, "reason": reason}


def verify_integrity(manifest: Any) -> Dict[str, Any]:
    """Whether a stored manifest is intact and readable by this runtime.

    No system configuration is involved, so this can run before routing: it
    catches a tampered, malformed or future manifest and a changed history
    contract. Every shape is checked - a manifest is untrusted input.
    """
    if not isinstance(manifest, Mapping):
        return _decision(False, MANIFEST_MALFORMED, "its revision record is not an object")
    if not isinstance(manifest.get("digest"), str):
        return _decision(False, MANIFEST_MALFORMED, "its revision manifest records no digest")
    if not manifest_digest_ok(manifest):
        return _decision(False, TAMPERED, "its revision manifest was altered after it was pinned")

    version = manifest.get("manifest_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return _decision(
            False, MANIFEST_MALFORMED, f"its revision manifest version is {version!r}, not a number"
        )
    if version not in SUPPORTED_MANIFEST_VERSIONS:
        return _decision(
            False,
            UNKNOWN_MANIFEST,
            f"its revision manifest version {version!r} is not one this runtime reads "
            f"(supported: {list(SUPPORTED_MANIFEST_VERSIONS)})",
        )

    pinned_grid = manifest.get("grid_version")
    if not isinstance(pinned_grid, str):
        return _decision(False, MANIFEST_MALFORMED, "its revision manifest records no Grid version")

    pinned_contracts = manifest.get("contracts")
    if not isinstance(pinned_contracts, Mapping):
        return _decision(False, MANIFEST_MALFORMED, "its revision manifest records no contracts")
    for name, current in ((name, contract_section(name)) for name in CONTRACTS):
        pinned = pinned_contracts.get(name)
        if not isinstance(pinned, Mapping):
            return _decision(
                False, MANIFEST_MALFORMED, f"its {name} contract is not an object"
            )
        if pinned.get("version") not in SUPPORTED_CONTRACT_VERSIONS[name]:
            return _decision(
                False,
                CONTRACT_VERSION_UNSUPPORTED,
                f"its {name} contract is version {pinned.get('version')!r}, "
                f"this runtime reads {list(SUPPORTED_CONTRACT_VERSIONS[name])}",
            )
        if pinned.get("digest") != current.get("digest"):
            return _decision(
                False,
                CONTRACT_CHANGED,
                f"its {name} contract differs from this runtime's at the same version",
            )

    pinned_platform = manifest.get("platform")
    if not isinstance(pinned_platform, Mapping):
        return _decision(False, MANIFEST_MALFORMED, "its revision manifest records no runtime")
    series = pinned_platform.get("python_series")
    if not isinstance(series, str):
        return _decision(False, MANIFEST_MALFORMED, "its revision manifest records no Python version")

    # A version difference is checked only after every *shape* and contract
    # question is answered: a Grid upgrade must not hide a changed contract
    # behind its own, harmless difference. The difference itself is returned as
    # a decision code; whether it is a refusal or a re-pin is decided by
    # :func:`review`, which is the only place that knows the graded policy.
    if pinned_grid != GRID_VERSION:
        return _decision(
            False,
            GRID_VERSION_CHANGED,
            f"it was pinned by Grid {pinned_grid!r} and this runtime is {GRID_VERSION!r}",
        )
    current_series = runtime_info()["python_series"]
    if series != current_series:
        return _decision(
            False,
            RUNTIME_CHANGED,
            f"it was pinned on Python {series!r} and this runtime is {current_series!r}",
        )
    return _decision(True, OK, "its pinned revision is intact and this runtime reads it")


def _system_verdict(manifest: Mapping[str, Any], environment: Mapping[str, Any]) -> Dict[str, Any]:
    """Whether the system *manifest* was pinned to is the one *environment* resolves now."""
    pinned_system = manifest.get("system")
    if not isinstance(pinned_system, Mapping):
        return _decision(False, MANIFEST_MALFORMED, "its revision manifest records no system")
    current_system = (environment or {}).get("system")
    if not isinstance(current_system, Mapping):
        return _decision(False, SYSTEM_UNRESOLVED, "this runtime has no resolved system configuration")
    if pinned_system.get("status") != "resolved":
        return _decision(False, SYSTEM_UNRESOLVED, "it was pinned without a resolved system configuration")
    if current_system.get("status") != "resolved":
        return _decision(False, SYSTEM_UNRESOLVED, "this runtime has no resolved system configuration")
    pinned_digest = pinned_system.get("digest")
    if not isinstance(pinned_digest, str):
        return _decision(False, MANIFEST_MALFORMED, "its revision manifest records no system digest")
    if pinned_digest != current_system.get("digest"):
        return _decision(
            False,
            SYSTEM_CONFIG_CHANGED,
            "the resolved system configuration changed since it was pinned",
        )
    return _decision(True, OK, "its pinned revision matches this runtime")


def verify_manifest(manifest: Mapping[str, Any], environment: Mapping[str, Any]) -> Dict[str, Any]:
    """Whether a pinned manifest is compatible with *environment* now.

    Returns ``{"ok": bool, "code": str, "reason": str}`` - the reason is written
    for a person and ends with the machine code. Malformed input (any level,
    any type) is a decision, never an exception.

    This is the *strict* verdict: any difference at all is ``ok`` False. A turn
    is admitted by :func:`review`, which grades these codes into a refusal and a
    difference the conversation survives (it is re-pinned). Inspectors and the
    migration planner keep using this one, so "compatible" stays exact.
    """
    integrity = verify_integrity(manifest)
    if not integrity["ok"]:
        return integrity
    return _system_verdict(manifest, environment)


def _refuse(decision: Mapping[str, Any], context_id: str, *, status: str) -> None:
    raise ContextCompatibilityError(
        str(decision["code"]),
        f"Conversation {context_id} cannot be continued: {decision['reason']}. "
        f"Start a new conversation. ({decision['code']})",
        context_id=context_id,
        details={"revision_status": status},
    )


def context_manifest(context_manager: Any, context_id: str) -> Optional[Mapping[str, Any]]:
    """The manifest of *context_id*, None when it is new and unpinned.

    Unknown conversations and managers without a revision API fail closed: a
    turn must not run on a conversation this process cannot see or judge.
    ``revision_trusted_ephemeral = True`` is the one explicit opt-out, for a
    caller that keeps no conversations at all (never a generic fallback).
    """
    revision_manifest = getattr(context_manager, "revision_manifest", None)
    has_context = getattr(context_manager, "has_context", None)
    if not callable(revision_manifest) or not callable(has_context):
        if getattr(context_manager, "revision_trusted_ephemeral", False) is True:
            return None
        raise ContextCompatibilityError(
            NO_CONTEXT,
            f"Conversation {context_id} cannot be judged: this context manager keeps no revision "
            f"records. ({NO_CONTEXT})",
            context_id=context_id,
            details={"revision_status": "unknown"},
        )
    if not has_context(context_id):
        raise ContextCompatibilityError(
            NO_CONTEXT,
            f"Conversation {context_id} does not exist here. ({NO_CONTEXT})",
            context_id=context_id,
            details={"revision_status": "unknown"},
        )
    manifest = revision_manifest(context_id)
    if manifest is None:
        return None
    if not isinstance(manifest, Mapping):
        raise ContextCompatibilityError(
            MANIFEST_MALFORMED,
            f"Conversation {context_id} carries a revision record that is not an object. "
            f"({MANIFEST_MALFORMED})",
            context_id=context_id,
            details={"revision_status": "tampered"},
        )
    return manifest


def _bound(manifest: Mapping[str, Any], context_id: str) -> Mapping[str, Any]:
    """A manifest bound to *context_id*; one copied from another conversation is refused."""
    bound_to = manifest.get("context_id")
    if bound_to != context_id:
        raise ContextCompatibilityError(
            CONTEXT_MISMATCH,
            f"Conversation {context_id} carries a revision manifest of {bound_to!r}: "
            f"it does not belong to this conversation. ({CONTEXT_MISMATCH})",
            context_id=context_id,
            details={"revision_status": "tampered", "manifest_context_id": bound_to},
        )
    return manifest


def review(
    manifest: Mapping[str, Any],
    environment: Mapping[str, Any],
    *,
    context_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The graded verdict on a pinned manifest: refuse, or continue with drift.

    A conversation is refused only for a *real* incompatibility - a manifest
    this runtime cannot read (unsupported or malformed), an altered manifest, a
    changed contract at the same version, or an unresolved system. A Grid
    version, Python runtime or resolved system configuration that moved on is
    not an incompatibility: the stored history is readable, so the conversation
    continues and :func:`admit_turn` re-pins it.

    Returns ``{"ok", "action", "code", "reason", "changes"}`` where ``action``
    is :data:`REFUSE` or :data:`CONTINUE` and ``changes`` lists every graded
    difference (``[{"code", "reason"}]``). Integrity is checked before the
    version differences are graded, so an upgrade cannot hide a changed
    contract (see :func:`verify_integrity`).
    """
    integrity = verify_integrity(manifest)
    changes: List[Dict[str, str]] = []
    if not integrity["ok"]:
        if integrity["code"] not in CONTINUABLE_CODES:
            return {
                "ok": False,
                "action": REFUSE,
                "code": integrity["code"],
                "reason": integrity["reason"],
                "changes": [],
            }
        changes.append({"code": integrity["code"], "reason": integrity["reason"]})

    system = _system_verdict(manifest, environment)
    if not system["ok"]:
        if system["code"] not in CONTINUABLE_CODES:
            return {
                "ok": False,
                "action": REFUSE,
                "code": system["code"],
                "reason": system["reason"],
                "changes": changes,
            }
        changes.append({"code": system["code"], "reason": system["reason"]})

    if not changes:
        return {
            "ok": True,
            "action": CONTINUE,
            "code": OK,
            "reason": "its pinned revision matches this runtime",
            "changes": [],
        }
    return {
        "ok": True,
        "action": CONTINUE,
        "code": changes[0]["code"],
        "reason": " and ".join(change["reason"] for change in changes),
        "changes": changes,
    }


def _notice(
    code: str,
    previous: Optional[Mapping[str, Any]],
    environment: Mapping[str, Any],
    context_id: str,
    changes: List[str],
) -> Dict[str, Any]:
    """A typed re-pin notice: a machine code and the sentence a person reads.

    This is the domain object; the transport spells it out as a non-error
    event (``web_chat.session.revision_notice_event``). No credential, prompt
    or environment value enters it.
    """
    text = _NOTICE_TEXT.get(code, "This conversation was re-pinned to the current revision.")
    text = text.format(current=environment.get("grid_version"))
    return {
        "code": code,
        "reason": text,
        "context_id": context_id,
        "details": {
            "revision_status": "legacy" if previous is None else "pinned",
            "changes": list(changes),
            "previous_digest": (previous or {}).get("digest"),
            "previous_grid_version": (previous or {}).get("grid_version"),
            "grid_version": environment.get("grid_version"),
            "system_digest": (environment.get("system") or {}).get("digest"),
        },
    }


def context_state(context_manager: Any, context_id: str) -> Dict[str, Any]:
    """Where a conversation stands before a turn: new, legacy, pinned, ephemeral.

    ``{"status": ..., "manifest": ...}``. Raises with a machine-readable reason
    for an unknown conversation, a manifest of another conversation, or a
    record this runtime cannot read at all (tampered, malformed, future format,
    changed contract). A *version* difference is not decided here: the
    environment is not known yet, and :func:`review` grades it.
    """
    if getattr(context_manager, "revision_trusted_ephemeral", False) is True and not callable(
        getattr(context_manager, "revision_manifest", None)
    ):
        # The explicit opt-out: a caller with no conversations at all.
        return {"status": "ephemeral", "manifest": None}
    manifest = context_manifest(context_manager, context_id)
    if manifest is None:
        is_new = getattr(context_manager, "is_new_context", None)
        if not callable(is_new):
            raise ContextCompatibilityError(
                NO_CONTEXT,
                f"Conversation {context_id} cannot be judged: this context manager cannot say "
                f"whether the conversation was created here. ({NO_CONTEXT})",
                context_id=context_id,
                details={"revision_status": "unknown"},
            )
        return {"status": "new" if is_new(context_id) else "legacy", "manifest": None}
    _bound(manifest, context_id)
    integrity = verify_integrity(manifest)
    if not integrity["ok"] and integrity["code"] not in CONTINUABLE_CODES:
        _refuse(
            integrity,
            context_id,
            status="tampered" if integrity["code"] == TAMPERED else "pinned",
        )
    return {"status": "pinned", "manifest": manifest}


def assess(context_manager: Any, context_id: str, config: Any) -> Dict[str, Any]:
    """The graded admission of one turn, changing nothing.

    ``{"status", "manifest", "environment", "action", "notice"}``. The action is
    :data:`PIN` for a chat created here, or :data:`CONTINUE` - with a notice
    when the conversation is legacy or the environment moved on. Raises with a
    machine-readable reason only for a real incompatibility (and for a turn
    with no resolved system, which cannot be pinned or judged). The transport
    uses this to tell the client before the turn starts; :func:`admit_turn`
    makes the same decision and then writes it.
    """
    state = context_state(context_manager, context_id)
    if state["status"] == "ephemeral":
        return {
            "status": "ephemeral",
            "manifest": None,
            "environment": None,
            "action": CONTINUE,
            "notice": None,
        }
    environment = environment_of(config)
    if environment["system"].get("status") != "resolved":
        raise ContextCompatibilityError(
            SYSTEM_UNRESOLVED,
            f"Conversation {context_id} cannot be judged: this turn has no resolved system "
            f"configuration to pin or verify. ({SYSTEM_UNRESOLVED})",
            context_id=context_id,
            details={"revision_status": "unresolved"},
        )
    if state["status"] == "new":
        return {
            "status": "new",
            "manifest": None,
            "environment": environment,
            "action": PIN,
            "notice": None,
        }
    if state["status"] == "legacy":
        return {
            "status": "legacy",
            "manifest": None,
            "environment": environment,
            "action": CONTINUE,
            "notice": _notice(LEGACY, None, environment, context_id, [LEGACY]),
        }
    decision = review(state["manifest"], environment, context_id=context_id)
    if decision["action"] == REFUSE:
        _refuse(decision, context_id, status="pinned")
    notice = (
        _notice(
            decision["code"],
            state["manifest"],
            environment,
            context_id,
            [change["code"] for change in decision["changes"]],
        )
        if decision["changes"]
        else None
    )
    return {
        "status": "pinned",
        "manifest": state["manifest"],
        "environment": environment,
        "action": CONTINUE,
        "notice": notice,
    }


def preflight(context_manager: Any, context_id: str) -> Optional[Mapping[str, Any]]:
    """The checks that need no system configuration, before routing and warm-up.

    Returns the pinned manifest, or None for a conversation that has none - a
    new chat *or* a legacy one. Use :func:`context_state` when the two must be
    told apart. Raises with a machine-readable reason for an unknown
    conversation, a manifest of another conversation, or a record this runtime
    cannot read (tampered, malformed, future format, changed contract).
    """
    return context_state(context_manager, context_id)["manifest"]


def compatible(manifest: Mapping[str, Any], config: Any, context_id: str) -> Mapping[str, Any]:
    """Refuse a pinned conversation only for a *real* incompatibility.

    A Grid version, runtime or resolved-system difference is admitted here: the
    turn's own gate (:func:`admit_turn`) re-pins the conversation and reports
    it. A transport uses this before warming anything; it never writes.
    """
    manifest = _bound(manifest, context_id)
    decision = review(manifest, environment_of(config), context_id=context_id)
    if decision["action"] == REFUSE:
        _refuse(decision, context_id, status="pinned")
    return manifest


def _revision_store(context_manager: Any, context_id: str, *, status: str) -> Any:
    """``revision_manifest`` of a manager that can keep revisions, or a refusal."""
    pin = getattr(context_manager, "pin_revision", None)
    revision_manifest = getattr(context_manager, "revision_manifest", None)
    if not callable(pin) or not callable(revision_manifest):
        raise ContextCompatibilityError(
            NO_CONTEXT,
            f"Conversation {context_id} cannot be pinned: this context manager has no revision "
            f"store. ({NO_CONTEXT})",
            context_id=context_id,
            details={"revision_status": status},
        )
    return revision_manifest


def admit_turn(
    context_manager: Any,
    context_id: str,
    config: Any,
    *,
    notices: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Gate one turn on the conversation's revision, before any external operation.

    A conversation created in this process is pinned here - once, with the
    environment this turn actually runs in. A conversation whose revision is
    only *older* - pinned by another Grid version, under another system
    configuration or Python runtime, or stored before pinning existed at all -
    continues: it is re-pinned to the current revision and the previous
    manifest is appended to the conversation's revision history. Only a real
    incompatibility is refused, with a machine-readable reason.

    The environment is validated *before* pinning: an unresolved system is
    never written into a manifest. Every write is persisted strictly and read
    back; the stored manifest is what every later turn is judged by.

    ``notices``, when given, collects the typed notice of each re-pin (see
    :func:`_notice`), so a transport can tell the client the turn continues.

    Returns the manifest the turn runs under (None only for an explicitly
    ephemeral manager that keeps no conversations).
    """
    state = assess(context_manager, context_id, config)
    if state["status"] == "ephemeral":
        return None
    environment = state["environment"]
    revision_manifest = _revision_store(
        context_manager, context_id, status="new" if state["status"] == "new" else "pinned"
    )

    if state["action"] == PIN:
        getattr(context_manager, "pin_revision")(context_id, build_manifest(context_id, environment))
        # Another turn of the same conversation may have pinned it in between,
        # and the pin must actually be stored: judge what is stored, never a
        # manifest this call only holds in memory.
        installed = revision_manifest(context_id)
        if not isinstance(installed, Mapping):
            raise ContextCompatibilityError(
                NO_CONTEXT,
                f"Conversation {context_id} could not be pinned: no revision manifest was stored. "
                f"({NO_CONTEXT})",
                context_id=context_id,
                details={"revision_status": "new"},
            )
        return installed

    notice = state["notice"]
    if notice is None:
        # Pinned and matching this runtime: nothing to write.
        return state["manifest"]

    repin = getattr(context_manager, "repin_revision", None)
    if not callable(repin):
        raise ContextCompatibilityError(
            NO_CONTEXT,
            f"Conversation {context_id} cannot be re-pinned: this context manager keeps no "
            f"revision history. ({NO_CONTEXT})",
            context_id=context_id,
            details={"revision_status": state["status"]},
        )
    previous = state["manifest"]
    expected = previous.get("digest") if isinstance(previous, Mapping) else None
    repin(
        context_id,
        build_manifest(context_id, environment),
        code=str(notice["code"]),
        expected_digest=expected if isinstance(expected, str) else None,
    )
    installed = revision_manifest(context_id)
    if not isinstance(installed, Mapping):
        raise ContextCompatibilityError(
            NO_CONTEXT,
            f"Conversation {context_id} could not be re-pinned: no revision manifest was stored. "
            f"({NO_CONTEXT})",
            context_id=context_id,
            details={"revision_status": state["status"]},
        )
    # Judge what is stored: a concurrent turn may have re-pinned it already, or
    # to another configuration; this turn runs only on a manifest that matches.
    settled = review(installed, environment, context_id=context_id)
    if settled["action"] == REFUSE:
        _refuse(settled, context_id, status="pinned")
    if notices is not None:
        notices.append(notice)
    return installed


def manifest_summary(manifest: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """A small read-only view of a manifest for inspectors and tests."""
    if not manifest:
        return {"status": "none"}
    if not isinstance(manifest, Mapping):
        return {"status": "malformed"}
    contracts = manifest.get("contracts")
    contracts = contracts if isinstance(contracts, Mapping) else {}
    system = manifest.get("system")
    system = system if isinstance(system, Mapping) else {}
    return {
        "status": "pinned" if manifest_digest_ok(manifest) else "tampered",
        "manifest_version": manifest.get("manifest_version"),
        "grid_version": manifest.get("grid_version"),
        "pinned_at": manifest.get("pinned_at"),
        "digest": manifest.get("digest"),
        "contracts": {
            name: section.get("version") if isinstance(section, Mapping) else None
            for name, section in contracts.items()
        },
        "system": {
            "status": system.get("status"),
            "digest": system.get("digest"),
            "source": system.get("source"),
        },
    }

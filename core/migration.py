"""Reference-only bootstrap for conversations whose revision cannot continue.

A conversation stored before revision pinning - *legacy* - has unknown
provenance: its history was written by a runtime whose contracts are not
recorded, so it is readable but never continued (``core.revisions``). A
conversation pinned to another Grid version, runtime or resolved system is in
the same position: its revision is intact, but this runtime must not execute
its history.

This module offers the one safe way forward: a **new, empty conversation**
pinned to this environment, plus an explicit, machine-readable record tying it
to the old one as *reference data*. Nothing is adopted, replayed or executed:

* the source is never written, re-pinned, edited or deleted;
* the target starts with no messages and no executions - its history is empty
  and the first turn is a new turn, not a continuation;
* agent SDK sessions, pending messages, a pending run, checkpoints and
  workspace files are *not* imported; the report names them and their counts;
* history and artifacts are only hashed and referenced (the source keeps
  them), never loaded as executable state;
* the target's revision manifest is pinned and stored **before** the reference
  record is written, so a conversation that cannot be pinned is never left
  behind as a half-migrated one.

The action is dry-run first. :func:`plan` writes nothing and returns a report
with a confirmation token bound to the exact source state (content digest,
revision manifest and metadata) and to this action. :func:`apply` performs the
bootstrap only when it is called with that token **and** ``confirm=True``; it
recomputes the plan first, so a source that changed since the preview is
refused rather than migrated from a stale description. Repeating an apply that
already succeeded is a no-op that returns the existing target, which makes the
action idempotent.

Authorization is not decided here: a caller must already be allowed to act on
the conversation (the web chat passes the owner's space). The token is a
confirmation binding, not a capability.
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from core import __version__ as GRID_VERSION
from core import revisions

#: The action a client calls and a target records; stable and machine-readable.
ACTION = "context.reference_bootstrap"
#: Version of this action's request/report shape.
VERSION = 1
#: The conversation metadata key holding the record of one bootstrap.
RECORD_KEY = "migration"

# -- machine-readable statuses and reasons ------------------------------------

READY = "migration.ready"
APPLIED = "migration.applied"
COMPLETED = "migration.completed"

SOURCE_UNKNOWN = "migration.source_unknown"
SOURCE_EMPTY = "migration.source_empty"
SOURCE_UNPINNED = "migration.source_unpinned"
SOURCE_TAMPERED = "migration.source_tampered"
SOURCE_MALFORMED = "migration.source_malformed"
SOURCE_FUTURE = "migration.source_future"
SOURCE_MISMATCH = "migration.source_mismatch"
SOURCE_COMPATIBLE = "migration.source_compatible"
SOURCE_INTEGRITY_UNKNOWN = "migration.source_integrity_unknown"
TARGET_INVALID = "migration.target_invalid"
TARGET_INCOMPATIBLE = "migration.target_incompatible"
SYSTEM_UNRESOLVED = "migration.system_unresolved"
PERSISTENCE_UNAVAILABLE = "migration.persistence_unavailable"
CONFIRMATION_REQUIRED = "migration.confirmation_required"
CONFIRMATION_STALE = "migration.confirmation_stale"
PIN_FAILED = "migration.pin_failed"

#: Integrity refusals that make even the reference untrustworthy: the source
#: is not migrated, it is reported. A *different* Grid version, runtime or
#: contract definition is intact and may be referenced - it is a compatibility
#: refusal, not corruption.
_FUTURE_INTEGRITY = {
    revisions.UNKNOWN_MANIFEST,
    revisions.CONTRACT_VERSION_UNSUPPORTED,
}
_MALFORMED_INTEGRITY = {
    revisions.MANIFEST_MALFORMED,
}
_TAMPERED_INTEGRITY = {
    revisions.TAMPERED,
}

#: Integrity refusals that are only *compatibility* differences: the record is
#: intact and can be read, so the history may be referenced. Everything else
#: that is not one of the refusals above is an unknown verdict and fails
#: closed - a code this module does not know is never taken as "merely
#: incompatible".
_COMPATIBILITY_INTEGRITY = {
    revisions.GRID_VERSION_CHANGED,
    revisions.CONTRACT_CHANGED,
    revisions.RUNTIME_CHANGED,
}

#: The title a target starts with: a fresh chat, never the source's title.
TARGET_TITLE = "Reference-only chat"

#: Serializes applies in this process, so two confirmations cannot both create
#: a target for one source. The re-plan under the lock is what makes it safe.
_APPLY_LOCK = threading.Lock()


def _report(
    status: str,
    code: str,
    reason: str,
    *,
    source: Optional[Mapping[str, Any]] = None,
    target: Optional[Mapping[str, Any]] = None,
    reference: Optional[Mapping[str, Any]] = None,
    confirmation: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """One report shape for both the dry run and the applied action."""
    return {
        "action": ACTION,
        "version": VERSION,
        "status": status,
        "code": code,
        "reason": reason,
        "grid_version": GRID_VERSION,
        "source": dict(source or {}),
        "target": dict(target or {"kind": "new-context", "context_id": None}),
        "reference": dict(reference or {}),
        "confirmation": dict(confirmation or {"required": False, "token": None}),
        # What this action will never do, stated for machines and people alike.
        "effects": {
            "external": [],
            "model_calls": 0,
            "tool_calls": 0,
            "replays": 0,
            "local_writes": [],
        },
        "limitations": {
            # Provenance is only ever *claimed* when the source explicitly
            # carries it: a report with no source section (or one that never
            # established a pin) must not read as recorded provenance.
            "provenance": (
                "recorded" if (source or {}).get("provenance") in {"pinned", "recorded"} else "unknown"
            ),
            "executable_state": "none",
            "history_imported": False,
            "original_preserved": True,
        },
    }


def _blocked(
    code: str,
    reason: str,
    *,
    source: Optional[Mapping[str, Any]] = None,
    target: Optional[Mapping[str, Any]] = None,
    reference: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    return _report("blocked", code, reason, source=source, target=target, reference=reference)


def _selection(selection: Optional[Mapping[str, Any]]) -> Optional[Dict[str, str]]:
    """The target's explicit ``system_key``/``agent_key``, or None.

    A bootstrap target is a new chat pinned to one resolved configuration, so
    the caller names that system and agent - the same choice a new chat makes
    - instead of inheriting the source's routing. Anything else is refused
    rather than written as an unusable selection.
    """
    if selection is None:
        return None
    if not isinstance(selection, Mapping):
        raise ValueError("selection must be a mapping")
    chosen = {}
    for key in ("system_key", "agent_key"):
        value = selection.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"selection.{key} must be a non-empty string")
        chosen[key] = value.strip()
    return chosen


def _as_dict(item: Any) -> Dict[str, Any]:
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # a broken message must not break the report
            pass
    if isinstance(item, Mapping):
        return dict(item)
    return {}


def _message_material(view: Mapping[str, Any]) -> Dict[str, Any]:
    """The source's content, in one canonical shape.

    Only used to compute digests and counts: no content is copied into the
    target or returned in the report.
    """
    return {
        "context_id": view.get("id"),
        "messages": [_as_dict(message) for message in view.get("messages") or []],
        "executions": [_as_dict(execution) for execution in view.get("executions") or []],
    }


#: Metadata that changes on its own or describes presentation only: it does not
#: identify the history a confirmation is about. Everything else is included.
_VOLATILE_METADATA = frozenset({"last_context_assembly", "title", "active_branch"})


def _fingerprint(view: Mapping[str, Any], manifest: Optional[Mapping[str, Any]]) -> str:
    """A digest of everything a confirmation for this source is bound to."""
    metadata = {
        str(key): value
        for key, value in (view.get("metadata") or {}).items()
        if str(key) not in _VOLATILE_METADATA
    }
    material = {
        "action": ACTION,
        "version": VERSION,
        "grid_version": GRID_VERSION,
        "source": view.get("id"),
        "manifest": dict(manifest) if isinstance(manifest, Mapping) else None,
        "content": _message_material(view),
        "metadata": metadata,
    }
    return revisions.digest_of(revisions.redacted(material))


def _artifacts(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    """The source's non-history artifacts, named but never imported."""
    checkpoints = metadata.get("checkpoints")
    turns = sorted(str(key) for key in checkpoints) if isinstance(checkpoints, Mapping) else []
    epochs = metadata.get("session_epochs")
    sessions = (
        {str(agent): int(epoch) for agent, epoch in epochs.items() if isinstance(epoch, int)}
        if isinstance(epochs, Mapping)
        else {}
    )
    return {
        "checkpoints": turns,
        "agent_sessions": sessions,
        "workspace": "referenced-not-copied" if metadata.get("workspace") else "none",
    }


def _excluded(metadata: Mapping[str, Any], executions: int) -> Dict[str, Any]:
    """What is deliberately not carried over, by name and count only."""
    pending = metadata.get("pending_agent_run")
    pending_status = pending.get("status") if isinstance(pending, Mapping) else None
    queue = metadata.get("pending_messages")
    return {
        "executions": executions,
        "pending_messages": len(queue) if isinstance(queue, list) else 0,
        "pending_agent_run": str(pending_status) if pending_status else None,
        "agent_sessions": "not-imported",
        "checkpoints": "not-restored",
        "workspace_files": "not-copied",
        "runtime_state": "not-imported",
        "replay": False,
    }


def _reference(view: Mapping[str, Any], fingerprint: str) -> Dict[str, Any]:
    """The reference view of the source: hashes and names, never content."""
    metadata = view.get("metadata") or {}
    messages = view.get("messages") or []
    executions = view.get("executions") or []
    first = getattr(messages[0], "timestamp", None) if messages else None
    last = getattr(messages[-1], "timestamp", None) if messages else None
    return {
        "executable": False,
        "replay": False,
        "history": {
            "selected": True,
            "messages": len(messages),
            "executions": len(executions),
            "digest": fingerprint,
            "first_at": first,
            "last_at": last,
            "kept_in": view.get("id"),
        },
        "artifacts": {**_artifacts(metadata), "copied": False},
        "excluded": _excluded(metadata, len(executions)),
    }


def _source_state(
    manager: Any, context_id: str, view: Mapping[str, Any], config: Any = None
) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """(source description, refusal) for one conversation.

    The refusal is a report, or None when the source may be referenced. The
    order is fail-closed: an unknown, malformed, tampered or future record is
    refused before anything about compatibility is considered.
    """
    manifest = manager.revision_manifest(context_id)
    status = manager.revision_status(context_id)
    messages = list(view.get("messages") or [])
    executions = list(view.get("executions") or [])
    source: Dict[str, Any] = {
        "context_id": context_id,
        "revision_status": status,
        "provenance": "unknown",
        "messages": len(messages),
        "executions": len(executions),
        "manifest": revisions.manifest_summary(manifest) if manifest is not None else {"status": "none"},
    }

    if manifest is None:
        if manager.is_new_context(context_id):
            return source, _blocked(
                SOURCE_UNPINNED,
                "Conversation was created in this process and has not been pinned: it is not "
                "legacy history and has nothing whose provenance is unknown. "
                f"({SOURCE_UNPINNED})",
                source=source,
            )
        if not messages and not executions:
            return source, _blocked(
                SOURCE_EMPTY,
                "Conversation holds no history; start a new conversation instead of referencing "
                f"an empty one. ({SOURCE_EMPTY})",
                source=source,
            )
        return source, None

    bound_to = manifest.get("context_id")
    if bound_to is not None and bound_to != context_id:
        return source, _blocked(
            SOURCE_MISMATCH,
            "Conversation carries a revision manifest of another conversation; it is not "
            f"trusted as a reference source. ({SOURCE_MISMATCH})",
            source=source,
        )

    integrity = revisions.verify_integrity(manifest)
    source["integrity"] = {
        "ok": bool(integrity["ok"]),
        "code": integrity["code"],
    }
    if not integrity["ok"]:
        code = str(integrity["code"])
        if code in _TAMPERED_INTEGRITY:
            return source, _blocked(
                SOURCE_TAMPERED,
                f"Conversation's revision manifest was altered after pinning; refusing to use it "
                f"even as a reference. ({SOURCE_TAMPERED})",
                source=source,
            )
        if code in _FUTURE_INTEGRITY:
            return source, _blocked(
                SOURCE_FUTURE,
                f"Conversation's revision record is from a newer format this runtime cannot read; "
                f"refusing to guess at it. ({SOURCE_FUTURE})",
                source=source,
            )
        if code in _MALFORMED_INTEGRITY:
            return source, _blocked(
                SOURCE_MALFORMED,
                f"Conversation's revision record is malformed; it cannot be used as a reference. "
                f"({SOURCE_MALFORMED})",
                source=source,
            )
        if code not in _COMPATIBILITY_INTEGRITY:
            # An unknown or unhandled verdict is never read as "only
            # incompatible": the source is not referenced.
            return source, _blocked(
                SOURCE_INTEGRITY_UNKNOWN,
                f"Conversation's revision record could not be verified ({code}); refusing to use "
                f"it even as a reference. ({SOURCE_INTEGRITY_UNKNOWN})",
                source=source,
            )

    # Only a record whose integrity was accepted is presented as pinned: a
    # compatibility difference in a known dimension keeps intact provenance,
    # while a tampered, malformed, future or unknown verdict stays "unknown".
    source["provenance"] = "pinned"

    if integrity["ok"]:
        decision = revisions.verify_manifest(manifest, revisions.environment_of(config))
        source["compatibility"] = {"ok": bool(decision["ok"]), "code": decision["code"]}
        if decision["ok"]:
            return source, _blocked(
                SOURCE_COMPATIBLE,
                "Conversation is compatible with this runtime and can be continued directly; a "
                f"reference-only copy would add nothing. ({SOURCE_COMPATIBLE})",
                source=source,
            )
    return source, None


def _existing_target(manager: Any, source_id: str) -> tuple[Optional[str], Optional[Mapping[str, Any]]]:
    """The conversation already bootstrapped from *source_id*, if any."""
    for context_id in manager.list_context_ids():
        if context_id == source_id:
            continue
        metadata = manager.get_context_metadata(context_id)
        record = metadata.get(RECORD_KEY)
        if not isinstance(record, Mapping):
            continue
        if record.get("action") != ACTION or record.get("source_context_id") != source_id:
            continue
        return context_id, record
    return None, None


def _target_valid(manager: Any, target_id: str, source_id: str) -> bool:
    """Whether an existing target is an intact pin of this reference bootstrap."""
    if manager.revision_status(target_id) != "pinned":
        return False
    manifest = manager.revision_manifest(target_id)
    if not isinstance(manifest, Mapping):
        return False
    if manifest.get("context_id") != target_id:
        return False
    if not revisions.manifest_digest_ok(manifest):
        return False
    view = manager.conversation_view(target_id)
    if view is None:
        return False
    metadata = view.get("metadata") or {}
    record = metadata.get(RECORD_KEY)
    return (
        isinstance(record, Mapping)
        and record.get("action") == ACTION
        and record.get("source_context_id") == source_id
        and record.get("executable_state") == "none"
    )


def _target_refusal(
    manager: Any,
    target_id: str,
    source_id: str,
    config: Any,
) -> Optional[Dict[str, Any]]:
    """Why an existing target cannot be used *now*, or None when it can.

    The target's pin is never replaced. A target that this environment no
    longer resolves - the configuration it was pinned to has changed - is
    reported as
    ``migration.target_incompatible`` with the safe next step: reference the
    previous target in a new bootstrap.
    """
    target = {"kind": "new-context", "context_id": target_id, "reference_only": True}
    manifest = manager.revision_manifest(target_id)
    decision = revisions.verify_integrity(manifest)
    if not decision["ok"]:
        code = decision["code"]
    else:
        decision = revisions.verify_manifest(manifest, revisions.environment_of(config))
        code = decision["code"]
    if decision["ok"]:
        return None
    return _blocked(
        TARGET_INCOMPATIBLE,
        f"The existing reference-only conversation {target_id} is pinned to a configuration this "
        f"environment no longer resolves ({code}): it cannot be opened as a working chat. It is "
        f"not re-pinned, and nothing was created. Start the reference-only bootstrap from "
        f"{target_id} itself to carry it forward. ({TARGET_INCOMPATIBLE})",
        source={"context_id": source_id, "provenance": "unknown"},
        target=target,
    )


def _confirmation_material(
    source: Mapping[str, Any],
    fingerprint: str,
    reference: Mapping[str, Any],
    selection: Optional[Mapping[str, Any]] = None,
    environment: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """What the confirmation token is bound to: this request, exactly.

    The target environment is part of the binding: the same source content and
    selection pinned to another resolved configuration is a *different*
    request, so a token from a preview cannot silently apply elsewhere. Only a
    digest of the environment is used - never its content.
    """
    return {
        "action": ACTION,
        "version": VERSION,
        "grid_version": GRID_VERSION,
        "source_context_id": source.get("context_id"),
        "source_revision_status": source.get("revision_status"),
        "source_provenance": source.get("provenance"),
        "source_fingerprint": fingerprint,
        "target": {
            "kind": "new-context",
            "reference_only": True,
            "system_key": (selection or {}).get("system_key"),
            "agent_key": (selection or {}).get("agent_key"),
        },
        "target_environment": _environment_digest(environment),
        "selection": {
            "history": "reference",
            "artifacts": "reference",
            "executable": False,
            "replay": False,
        },
        "reference_digest": revisions.digest_of(reference),
    }


def _environment_digest(environment: Optional[Mapping[str, Any]]) -> Optional[str]:
    """A secret-safe digest of the environment a target would be pinned to."""
    if environment is None:
        return None
    return revisions.digest_of(revisions.redacted(dict(environment)))


def plan(
    manager: Any,
    context_id: str,
    config: Any = None,
    *,
    selection: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Dry run: what a reference-only bootstrap would do, and the token that confirms it.

    Writes nothing. A source that cannot be referenced - unknown, empty,
    malformed, tampered, from a newer format, or already compatible - is a
    ``blocked`` report with a machine-readable code. A source already
    bootstrapped is a ``completed`` report naming its target, so a repeated
    request is safe.
    """
    try:
        chosen = _selection(selection)
    except ValueError as exc:
        return _blocked(
            TARGET_INVALID,
            f"The target's system and agent must be named explicitly: {exc}. ({TARGET_INVALID})",
            source={"context_id": context_id, "provenance": "unknown"},
        )

    view = manager.conversation_view(context_id)
    if view is None:
        return _blocked(
            SOURCE_UNKNOWN,
            f"Conversation {context_id} does not exist here. ({SOURCE_UNKNOWN})",
            source={"context_id": context_id, "provenance": "unknown"},
        )

    if getattr(manager, "read_only", False) or (
        getattr(manager, "persist_path", None) is None and getattr(manager, "store", None) is None
    ):
        return _blocked(
            PERSISTENCE_UNAVAILABLE,
            "This conversation manager cannot store a new conversation durably, and a bootstrap "
            f"that a restart would lose is not offered. ({PERSISTENCE_UNAVAILABLE})",
            source={"context_id": context_id},
        )

    existing_id, record = _existing_target(manager, context_id)
    if existing_id is not None:
        if not _target_valid(manager, existing_id, context_id):
            return _blocked(
                TARGET_INVALID,
                f"A previous reference copy of this conversation ({existing_id}) exists but its "
                f"pinned revision is damaged; refusing to create another. ({TARGET_INVALID})",
                source={"context_id": context_id, "provenance": "unknown"},
            )
        refusal = _target_refusal(manager, existing_id, context_id, config)
        if refusal is not None:
            return refusal
        return _report(
            "completed",
            COMPLETED,
            f"This conversation was already bootstrapped as the reference-only conversation "
            f"{existing_id}; nothing is repeated. ({COMPLETED})",
            # The record says what the source's provenance was; a legacy source
            # stays "unknown" here, never presented as recorded provenance.
            source={
                "context_id": context_id,
                "provenance": record.get("source_provenance") or "unknown",
            },
            target={
                "kind": "new-context",
                "context_id": existing_id,
                "reference_only": True,
            },
            reference=dict(record.get("reference") or {}),
        )

    environment = revisions.environment_of(config)
    if environment["system"].get("status") != "resolved":
        return _blocked(
            SYSTEM_UNRESOLVED,
            "There is no resolved system configuration to pin the new conversation to. "
            f"({SYSTEM_UNRESOLVED})",
            source={"context_id": context_id},
        )

    source, refusal = _source_state(manager, context_id, view, config)
    if refusal is not None:
        return refusal

    manifest = manager.revision_manifest(context_id)
    fingerprint = _fingerprint(view, manifest)
    source["fingerprint"] = fingerprint
    reference = _reference(view, fingerprint)
    environment_digest = _environment_digest(environment)
    token = revisions.digest_of(
        _confirmation_material(
            source, fingerprint, reference, chosen, environment=environment
        )
    )
    target = {
        "kind": "new-context",
        "context_id": None,
        # A fresh chat: the source's title is not copied.
        "title": TARGET_TITLE,
        "reference_only": True,
        "history": "empty",
        # The resolved configuration the target would be pinned to.
        "environment_digest": environment_digest,
        "system_key": (chosen or {}).get("system_key"),
        "agent_key": (chosen or {}).get("agent_key"),
    }
    report = _report(
        "ready",
        READY,
        "A new empty conversation can be pinned to this environment and the old history kept "
        f"as reference data only. ({READY})",
        source=source,
        target=target,
        reference=reference,
        confirmation={
            "required": True,
            "token": token,
            "binding": "source content, revision and metadata; target environment",
            "selects": ["history", "artifacts"],
            "imports_executable_state": False,
        },
    )
    return report


def apply(
    manager: Any,
    context_id: str,
    config: Any = None,
    *,
    confirmation: Optional[str] = None,
    confirm: bool = False,
    selection: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Bootstrap the reference-only conversation after an explicit confirmation.

    ``confirmation`` must be the token from :func:`plan` and ``confirm`` must be
    true. The plan is recomputed under a process-wide lock, so a source that
    changed since the preview yields ``migration.confirmation_stale`` and no
    target is created. On success the new conversation is pinned first, then
    the reference record is written; if pinning fails the empty conversation is
    removed, so no half-migrated target remains. Repeating a successful apply
    returns ``migration.completed`` for the same target.
    """
    with _APPLY_LOCK:
        report = plan(manager, context_id, config, selection=selection)
        if report["status"] == "completed":
            report["idempotent"] = True
            return report
        if report["status"] != "ready":
            return report

        expected = (report.get("confirmation") or {}).get("token")
        if not confirm or not isinstance(confirmation, str) or not confirmation:
            report["status"] = "blocked"
            report["code"] = CONFIRMATION_REQUIRED
            report["reason"] = (
                "This action changes the user's conversations: it needs an explicit confirmation "
                f"(confirm=true) and the token from the dry run. ({CONFIRMATION_REQUIRED})"
            )
            report["confirmation"] = {**report.get("confirmation", {}), "required": True, "accepted": False}
            return report
        if confirmation != expected:
            report["status"] = "blocked"
            report["code"] = CONFIRMATION_STALE
            report["reason"] = (
                "The confirmation token does not match this conversation as it is now (it changed "
                "since the dry run, or the token is for another request); nothing was created. "
                f"({CONFIRMATION_STALE})"
            )
            report["confirmation"] = {**report.get("confirmation", {}), "required": True, "accepted": False}
            return report

        source = report["source"]
        reference = report["reference"]
        environment = revisions.environment_of(config)
        target_id = manager.create_context()
        try:
            manifest = revisions.build_manifest(target_id, environment)
            if not manager.pin_revision(target_id, manifest):
                raise RuntimeError("the new conversation could not be pinned")
            installed = manager.revision_manifest(target_id)
            if (
                not isinstance(installed, Mapping)
                or not revisions.manifest_digest_ok(installed)
                or installed.get("context_id") != target_id
            ):
                raise RuntimeError("the stored revision manifest does not match the new conversation")
            # The stored manifest is what every later turn is judged by.
            revisions.admit_turn(manager, target_id, config)
            record = {
                "action": ACTION,
                "version": VERSION,
                "source_context_id": context_id,
                "source_provenance": source.get("provenance"),
                "target_selection": {
                    key: report["target"].get(key)
                    for key in ("system_key", "agent_key")
                    if report["target"].get(key)
                },
                "source_revision": {
                    "status": source.get("revision_status"),
                    "grid_version": (source.get("manifest") or {}).get("grid_version")
                    if isinstance(source.get("manifest"), Mapping)
                    else None,
                    "digest": (source.get("manifest") or {}).get("digest")
                    if isinstance(source.get("manifest"), Mapping)
                    else None,
                },
                "source_fingerprint": source.get("fingerprint"),
                "reference": copy.deepcopy(reference),
                "executable_state": "none",
                "replay": False,
                "confirmation_digest": revisions.digest_of(confirmation),
                "created_at": datetime.now().isoformat(),
            }
            target_metadata: Dict[str, Any] = {
                "created_by_web": True,
                "title": report["target"]["title"],
                RECORD_KEY: record,
            }
            for key in ("system_key", "agent_key"):
                if report["target"].get(key):
                    target_metadata[key] = report["target"][key]
            manager.update_context_metadata(target_id, target_metadata)
            stored_record = manager.get_context_metadata(target_id).get(RECORD_KEY)
            if not isinstance(stored_record, Mapping) or stored_record.get("action") != ACTION:
                raise RuntimeError("the migration record was not stored with the new conversation")
            # Persist the whole target strictly: a conversation that a restart
            # would not find with its record is not a completed bootstrap.
            manager.save(strict=True)
        except Exception:
            # No half-migrated target: the conversation is only a target once
            # its pin and its record are both stored.
            try:
                manager.delete_context(target_id)
            except Exception:  # the original error is the one worth raising
                pass
            raise

        return _report(
            "applied",
            APPLIED,
            "A new empty conversation was pinned to this environment; the old history stays "
            f"where it is, as reference data only. ({APPLIED})",
            source=source,
            target={
                "kind": "new-context",
                "context_id": target_id,
                "title": report["target"]["title"],
                "reference_only": True,
                "history": "empty",
                "environment_digest": report["target"].get("environment_digest"),
                "system_key": report["target"].get("system_key"),
                "agent_key": report["target"].get("agent_key"),
                "manifest": revisions.manifest_summary(manager.revision_manifest(target_id)),
            },
            reference=reference,
            confirmation={
                "required": True,
                "accepted": True,
                "imports_executable_state": False,
            },
        ) | {
            "effects": {
                "external": [],
                "model_calls": 0,
                "tool_calls": 0,
                "replays": 0,
                "local_writes": ["new-conversation", "revision-pin", "migration-record"],
            }
        }

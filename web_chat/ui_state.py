"""Where a presented UI lives after the turn, and what its state means.

A UI block is stored on the assistant message that presented it - beside the
answer, not inside a tool payload that compaction may drop. Its values are part
of the same record, with a \`\`state_revision\`\` that only grows, so a reload, a
second tab and a late save all read one honest number.

Values are checked against the document before they are kept; a save carrying a
revision other than the stored one is refused with the current values instead of
overwriting a newer choice. A repeated \`\`request_id\`\` is the same request, not a
second one: the save answers with what it already holds and the submit does not
start another turn.

Nothing here invents a prompt from the browser. The sentence a ``submit_to_agent``
button sends is composed from the stored, validated document and the validated
values only.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

from web_chat.ui_document import UiDocumentError, action_of, validate_document, validate_values, value_labels

#: Metadata key on the assistant message that carries its UI blocks.
UI_KEY = "ui"
#: Metadata key on the conversation: submit request ids already honoured.
SUBMITS_KEY = "ui_submits"
#: Metadata key on a user message: where the request came from.
ORIGIN_KEY = "ui_origin"
#: How many blocks one answer may carry, and how many submit ids a chat keeps.
MAX_BLOCKS = 3
MAX_SUBMITS = 50


class UiStateError(ValueError):
    """A refused UI operation, with a code the client can branch on."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


class UiSink:
    """The blocks one turn presented, in order, validated once at the door.

    The observer owns one and hands the same one to a sub-agent's view, so a
    child that presents an interface adds it to the turn rather than to a
    report nobody sees.
    """

    def __init__(self) -> None:
        self.blocks: list[dict[str, Any]] = []

    def present(self, raw: Any) -> dict[str, Any]:
        document = validate_document(raw)
        if len(self.blocks) >= MAX_BLOCKS:
            raise UiStateError("ui.limit", f"One answer may show at most {MAX_BLOCKS} interfaces.")
        block = new_block(document, self.blocks)
        self.blocks.append(block)
        return block


def mint_block_id(document: dict[str, Any], existing: list[dict[str, Any]]) -> str:
    """A stable id for a presented document, unique within its answer."""
    taken = {block.get("id") for block in existing if isinstance(block, dict)}
    seed = json.dumps([document.get("id"), document.get("revision")], sort_keys=True)
    for salt in range(64):
        digest = hashlib.sha1(f"{seed}|{salt}".encode("utf-8")).hexdigest()[:10]
        candidate = f"ui-{digest}"
        if candidate not in taken:
            return candidate
    raise UiStateError("ui.limit", "This answer already shows too many interfaces.")


def new_block(document: dict[str, Any], existing: list[dict[str, Any]]) -> dict[str, Any]:
    """A block as it is stored and sent: the document plus its first values."""
    return {
        "id": mint_block_id(document, existing),
        "revision": document["revision"],
        "document": document,
        "values": dict(document["initial_state"]),
        "state_revision": 0,
        "updated_at": _now(),
    }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def blocks_of(message: Any) -> list[dict[str, Any]]:
    """The UI blocks a stored message carries, as data; [] when it carries none."""
    metadata = getattr(message, "metadata", None) or {}
    blocks = metadata.get(UI_KEY)
    return [block for block in blocks if isinstance(block, dict)] if isinstance(blocks, list) else []


def find_block(
    manager: Any, context_id: str, message_id: str, ui_id: str
) -> tuple[Any, dict[str, Any]]:
    """The stored message and block, or a refusal.

    Membership is decided by the conversation the caller already authenticated
    for: a message id from another chat, or of another user, is simply not in
    this conversation.
    """
    view = manager.conversation_view(context_id)
    if view is None:
        raise UiStateError("ui.not_found", "Conversation not found")
    for message in view.get("messages") or []:
        metadata = getattr(message, "metadata", None) or {}
        if metadata.get("message_id") != message_id:
            continue
        for block in blocks_of(message):
            if block.get("id") == ui_id:
                return message, block
    raise UiStateError("ui.not_found", "This interface is not part of this conversation")


def _document(block: dict[str, Any]) -> dict[str, Any]:
    document = block.get("document")
    if not isinstance(document, dict):
        raise UiStateError("ui.not_found", "This interface has no document")
    return document


def state_of(block: dict[str, Any]) -> dict[str, Any]:
    """What a client needs to restore a block: values and their revision."""
    return {
        "values": dict(block.get("values") or {}),
        "state_revision": int(block.get("state_revision") or 0),
    }


def save_values(
    manager: Any,
    context_id: str,
    message_id: str,
    ui_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Keep the user's local choices; an idempotent, revision-checked write.

    ``payload`` carries ``values``, ``state_revision`` (the revision the client
    is updating) and ``request_id``. The answer is the state as stored now.

    The revision check and the write happen in one atomic step: the manager
    holds its lock while :func:`_mutate_block` reads the stored revision and
    decides, so two saves from two tabs (this call runs in a thread pool) can
    never both pass the check and then overwrite each other.
    """
    _, block = find_block(manager, context_id, message_id, ui_id)
    document = _document(block)
    try:
        values = validate_values(document, payload.get("values"))
    except UiDocumentError as exc:
        raise UiStateError("ui.schema", exc.message) from None
    expected = payload.get("state_revision")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise UiStateError("ui.schema", "state_revision must be a non-negative integer")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not 0 < len(request_id) <= 64:
        raise UiStateError("ui.schema", "request_id is required")
    answer: dict[str, Any] = {}

    def mutate(metadata: dict[str, Any]) -> dict[str, Any] | None:
        stored = _block_in(metadata, ui_id)
        current = state_of(stored)
        if stored.get("last_request_id") == request_id:
            # The same save delivered twice: it already happened.
            answer.update({**current, "saved": True, "duplicate": True})
            return None
        if expected != current["state_revision"]:
            raise UiStateError(
                "ui.stale",
                "This interface was changed elsewhere; reload its values.",
                **current,
                saved=False,
            )
        updated = {
            **stored,
            "values": values,
            "state_revision": current["state_revision"] + 1,
            "last_request_id": request_id,
            "updated_at": _now(),
        }
        answer.update({**state_of(updated), "saved": True, "duplicate": False})
        blocks = [
            updated if isinstance(item, dict) and item.get("id") == ui_id else item
            for item in metadata.get(UI_KEY) or []
        ]
        return {**metadata, UI_KEY: blocks}

    mutator = getattr(manager, "mutate_message_metadata_by_id", None)
    if mutator is None:  # pragma: no cover - a manager without it cannot store UI
        raise UiStateError("ui.unavailable", "This server cannot store interface state")
    if mutator(context_id, message_id, mutate) is None:
        raise UiStateError("ui.not_found", "This interface is not part of this conversation")
    return answer


def _block_in(metadata: dict[str, Any], ui_id: str) -> dict[str, Any]:
    """The block with *ui_id* in one message's metadata, or a refusal."""
    blocks = metadata.get(UI_KEY)
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and block.get("id") == ui_id:
                return block
    raise UiStateError("ui.not_found", "This interface is not part of this conversation")


def compose_submission(document: dict[str, Any], action_id: str, values: dict[str, Any]) -> str:
    """The sentence a ``submit_to_agent`` button sends, composed on the server.

    The action's own prompt (chosen with the document, checked with it) comes
    first; the chosen values follow, one line each, labelled as the document
    labelled them. Nothing the browser sent is echoed as a prompt.
    """
    action = action_of(document, action_id)
    prompt = (action.get("prompt") or "").strip()
    lines = value_labels(document, values)
    if not lines:
        return prompt or action["label"]
    body = "\n".join(lines)
    return f"{prompt}\n\n{body}" if prompt else body


def origin_of(
    message_id: str, ui_id: str, action_id: str, document: dict[str, Any], values: dict[str, Any], state_revision: int
) -> dict[str, Any]:
    """The provenance stored on the user message a UI action creates."""
    return {
        "source": "ui",
        "message_id": message_id,
        "ui_id": ui_id,
        "action_id": action_id,
        "action_label": document["actions"][action_id]["label"],
        "document_revision": document["revision"],
        "state_revision": state_revision,
        "values": values,
    }


def claim_submit(manager: Any, context_id: str, request_id: str, record: dict[str, Any]) -> bool:
    """Record *request_id* as honoured; False when it already was.

    The claim is what makes a repeated frame (a retry after a dropped socket)
    harmless: the first one starts the turn, the rest only repeat its answer.
    """
    kept = dict(manager.get_context_metadata(context_id).get(SUBMITS_KEY) or {})
    if request_id in kept:
        return False
    kept[request_id] = {**record, "at": _now()}
    for stale in list(kept)[:-MAX_SUBMITS]:
        kept.pop(stale, None)
    manager.update_context_metadata(context_id, {SUBMITS_KEY: kept})
    return True


def known_submit(manager: Any, context_id: str, request_id: str) -> Optional[dict[str, Any]]:
    """The record of a submit already honoured, or None."""
    kept = manager.get_context_metadata(context_id).get(SUBMITS_KEY) or {}
    record = kept.get(request_id)
    return record if isinstance(record, dict) else None

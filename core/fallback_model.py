"""Ordered model fallback for OpenAI Agents SDK model calls.

A request that carries media - images, audio, video, PDF documents
(core.modalities) - goes only to the models that accept them, in the agent's
order: what a model accepts comes from its config or the model catalog
(core.model_catalog), and a provider that refuses a kind of media once is
taken at its word for the rest of the process. When none of the agent's
models accepts a kind, the media of that kind are replaced by a note telling
the agent so: it learns it lacks the modality instead of a chat failing or a
model guessing at a file it never saw. When the models that accept it all
fail the request, the others get it with the same kind of note.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, FrozenSet, Iterator, Optional, Sequence

from agents.models.interface import Model
import httpx
from openai import APIConnectionError, APIStatusError

from core.credentials import CredentialError, credential_error_in
from core.modalities import IMAGE, MEDIA, NAMES, PDF, replace_modalities, request_modalities


logger = logging.getLogger("grid.models.fallback")

# Lifecycle events a stream always opens with; buffered but never "content".
_LIFECYCLE_EVENT_TYPES = frozenset({"response.created", "response.in_progress"})
# A provider can report failure as an in-stream event instead of raising; before
# any real content has been yielded, that must still fall back to the next
# candidate rather than being forwarded as committed content.
_FAILURE_EVENT_TYPES = frozenset({"response.failed", "response.incomplete"})

#: What stands in for an image in a request to a model that cannot see.
IMAGE_NOTE = "[An image is attached here, but this model cannot see images.]"
#: A provider's refusal of a kind of input, in the words providers use.
_REFUSAL = re.compile(
    r"(?:not|n't) support\w* (image|vision|audio|video|pdf|file|document)"
    r"|(image|audio|video|file|document)s? input\w* (?:is|are) not supported",
    re.I,
)
_REFUSED_WORDS = {"vision": IMAGE, "file": PDF, "document": PDF}
#: Model keys whose provider refused images in this process.
_BLIND: set[str] = set()
#: Model keys whose provider refused each modality in this process.
_REFUSED: Dict[str, set] = {IMAGE: _BLIND, **{m: set() for m in MEDIA if m != IMAGE}}


def has_images(items: Any) -> bool:
    """Whether a model request's input holds an image anywhere."""
    return IMAGE in request_modalities(items)


def strip_images(items: Any) -> Any:
    """*items* with every image replaced by :data:`IMAGE_NOTE`; the rest as it was."""
    return replace_modalities(items, frozenset({IMAGE}), lambda modality: IMAGE_NOTE)


def refused_modality(exc: BaseException) -> Optional[str]:
    """The modality a provider's error says the model does not take, or None."""
    match = _REFUSAL.search(str(exc))
    if match is None:
        return None
    word = (match.group(1) or match.group(2)).lower()
    return _REFUSED_WORDS.get(word, word)


def is_image_refusal(exc: BaseException) -> bool:
    return refused_modality(exc) == IMAGE


def lacking_note(modality: str, models: Sequence[str]) -> str:
    """What the agent reads in place of media none of its models accepts."""
    name = NAMES.get(modality, modality)
    return (
        f"[Error: this agent cannot take this {name}. None of its models "
        f"({', '.join(models)}) accepts {modality} input, so it was not sent to the model. "
        f"Do not guess its content: say so, or hand the task to an agent whose model accepts {modality}.]"
    )


def failed_note(modality: str, model: str, able: Sequence[str]) -> str:
    """What the agent reads in place of media when the models that accept them failed."""
    name = NAMES.get(modality, modality)
    return (
        f"[This {name} was not sent: the models that accept {modality} input "
        f"({', '.join(able)}) failed this request, and {model}, which answers it, does not "
        f"accept {modality}. Do not guess its content.]"
    )


#: A status a backend reports in words rather than in a typed error.
_STATUS_IN_TEXT = re.compile(r"\b(?:408|409|425|429|5\d\d)\b")

#: A typed status that says this route cannot serve the request now: the key or
#: the account behind this model is refused (401-403), the model it names is not
#: there (404), or the backend is out of reach or out of quota (408-5xx). Another
#: model of another provider may still answer, so the chain moves on.
_ROUTE_STATUSES = frozenset({401, 402, 403, 404, 408, 409, 425, 429, 500, 502, 503, 504})

#: A provider's own words for a refusal of the request itself, on any transport.
#: These stay terminal: a denied request must not be replayed on another model.
_POLICY_MARKERS = (
    "policy_deny", "policy deny", "policy_filter",
    "action declined", "declined by the user", "declined by user",
    "content_filter", "content filter", "content_policy", "content policy",
    "safety", "refusal",
)

#: Words that may be either a policy refusal or a refused route ("not
#: authorized" for this key). A typed HTTP status already told the two apart, so
#: they only decide for backends that report their failure in words alone.
_AUTHORITY_MARKERS = ("permission denied", "not authorized", "not allowed")


def _provider_words(exc: BaseException) -> str:
    """The exception's own message plus the provider's response body, lowered.

    A typed HTTP error carries its reason in the body; the SDK only sometimes
    repeats it in the message, and a 403 that names a content policy must still
    read as a refusal rather than as a refused route.
    """
    parts = [str(exc)]
    body = getattr(exc, "body", None)
    if body is not None:
        parts.append(str(body))
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            parts.append(response.text[:2000])
        except (RuntimeError, ValueError):  # never read (a stream) or not text
            logger.debug("fallback: the provider's error body was not read")
    return " ".join(parts).lower()


def can_fallback(exc: BaseException) -> bool:
    """Only technical unavailability, never a policy refusal or a bad request.

    CLI backends report quota errors as text; API backends have typed errors.
    A typed 401/402/403/404 is the route's refusal, not the request's: the key,
    the account or the model is what cannot serve it, so the next model is
    tried. A policy or content refusal is terminal whatever carried it.
    Unknown failures fail closed rather than moving a denied request elsewhere.
    """
    text = _provider_words(exc) if isinstance(exc, APIStatusError) else str(exc).lower()
    if any(marker in text for marker in _POLICY_MARKERS):
        return False
    if refused_modality(exc) is not None:
        return True
    if credential_error_in(exc) is not None or isinstance(exc, CredentialError):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in _ROUTE_STATUSES
    if any(marker in text for marker in _AUTHORITY_MARKERS):
        return False
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException,
                        httpx.NetworkError, httpx.RemoteProtocolError, APIConnectionError)):
        return True
    # A backend that reports its refusal in words names the status there too: a
    # 5xx, 429 or 408 in the text is an outage of that model, whichever
    # exception type carried it. Policy refusals were already turned away above.
    if _STATUS_IN_TEXT.search(text):
        return True
    return any(marker in text for marker in (
        "rate limit", "rate_limit", "usage limit", "usage_limit",
        "session limit", "quota exceeded", "quota exhausted", "insufficient_quota",
        "you've hit your limit", "you have hit your limit",
        "connection error", "connection reset", "timed out", "timeout",
        "temporarily unavailable", "service unavailable", "primary unavailable",
        "upstream_unavailable", "upstream unavailable", "server_error",
        "server error", "bad gateway", "overloaded",
        # The Claude Code CLI as a process: a crash or a binary the updater
        # removed is an outage of that backend, not a refusal.
        "cli exited with status", "was not found on path",
    ))


def _stream_failure(event: Any) -> BaseException:
    """Retain the provider's reason, and say whether the next model may answer.

    A ``response.failed`` that names no reason at all is the backend dropping a
    request it never explained: with nothing to call a refusal, the reserve gets
    it. A ``response.incomplete`` is this model's own cut-off (a token or
    content limit), which another model must not silently repeat.
    """
    kind = getattr(event, "type", "response.failed")
    response = getattr(event, "response", None)
    detail = getattr(response, "error", None) or getattr(response, "incomplete_details", None)
    code = getattr(detail, "code", None) or getattr(detail, "reason", None)
    message = getattr(detail, "message", None)
    unnamed = kind == "response.failed" and not code and not message
    return _StreamFailure(f"{kind}: {code or ''} {message or ''}", fallback=unnamed)


class _StreamFailure(Exception):
    """An in-stream provider failure event, raised before anything committed.

    ``fallback``: whether the chain may try the next model. Only a failure event
    that carries no reason at all sets it; everything else is judged by
    :func:`can_fallback` like any other exception.
    """

    def __init__(self, message: str, *, fallback: bool = False) -> None:
        super().__init__(message)
        self.fallback = fallback


class AllModelsFailedError(RuntimeError):
    """Raised after every configured model failed the same request.

    ``cause``: the one failure behind it when the chain held a single live model
    and was shortened by skipped ones. That model's own verdict still decides
    whether the run is retried (core.factory.failures.is_retriable), while the
    message names the models that were never tried.
    """

    def __init__(self, message: str, cause: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.cause = cause


class ModelSelection:
    """A transient, shared record of the candidate a live stream runs on.

    The web chat wants to show the model that actually answers, including a
    candidate reached only after a fallback. That fact must not travel in the
    response text or in anything stored, so it lives here for the duration of
    one turn: the consumer creates a selection, binds it with
    :func:`model_selection` before starting the run, and reads
    :func:`selected_model_key` while it consumes the stream.

    It is a mutable object rather than a ContextVar value because the Agents
    SDK runs the model stream in a background task: a value set inside that
    task would not be visible to the consumer's task. The task inherits the
    binding (and this object) by reference, so what it records here is what
    the consumer reads.
    """

    __slots__ = ("_key",)

    def __init__(self) -> None:
        self._key: Optional[str] = None

    @property
    def key(self) -> Optional[str]:
        """The candidate of the latest live stream, or None before one commits."""
        return self._key

    def selected(self, key: str) -> None:
        """Record that *key* now produces the live stream."""
        self._key = key


#: The selection of the turn in progress; a consumer binds one per turn.
_SELECTION: ContextVar[Optional[ModelSelection]] = ContextVar(
    "grid_model_selection", default=None
)


@contextmanager
def model_selection(selection: ModelSelection) -> Iterator[ModelSelection]:
    """Report the live stream's model into *selection* inside this block.

    Bind it in the task that will read the answer, before the run starts: the
    Agents SDK starts the model stream in a child task, which inherits this
    binding and shares *selection*. The binding is reset on exit, so a later
    turn (or a concurrent one) never inherits a stale key.
    """
    token = _SELECTION.set(selection)
    try:
        yield selection
    finally:
        _SELECTION.reset(token)


def selected_model_key() -> Optional[str]:
    """The model key of the live stream in this context, or None.

    This is the read side for a stream observer: call it while handling a
    stream event of the turn bound with :func:`model_selection`.
    """
    selection = _SELECTION.get()
    return selection.key if selection is not None else None


def _report_model_key(key: str) -> None:
    """Record *key* for the bound selection, creating an implicit one if none.

    An unbound call (a direct caller, a test) still gets a record in its own
    context instead of silently doing nothing.
    """
    selection = _SELECTION.get()
    if selection is None:
        selection = ModelSelection()
        _SELECTION.set(selection)
    selection.selected(key)


@dataclass(frozen=True)
class ModelCandidate:
    """One model and the settings that belong to it."""

    key: str
    model: Model
    settings: Any
    #: Whether the model reads images; None is unknown, taken as yes until refused.
    vision: Optional[bool] = None
    #: What the model accepts as input (core.model_catalog); None is unknown.
    modalities: Optional[FrozenSet[str]] = None

    def accepts(self, modality: str) -> Optional[bool]:
        """Whether the model takes *modality*: True, False, or None when unknown.

        Unknown is tried for images only, until the provider refuses them;
        audio, video and PDF go only to a model known to take them.
        """
        if self.key in _REFUSED.get(modality, ()):
            return False
        if self.modalities is not None:
            return modality in self.modalities
        if modality == IMAGE:
            return self.vision
        return False

    @property
    def sees(self) -> bool:
        return self.accepts(IMAGE) is not False


class FallbackModel(Model):
    """Try configured models from left to right for every model request.

    Stream live once a candidate produces content. Only initial lifecycle
    events are buffered; a failure after content escapes is propagated so a
    second model cannot silently duplicate text or tool calls.
    """

    def __init__(self, candidates: Sequence[ModelCandidate], skipped: Sequence[tuple[str, str]] = ()) -> None:
        if not candidates:
            raise ValueError("FallbackModel requires at least one candidate")
        self.candidates = tuple(candidates)
        #: (model key, why it is not a candidate): models the config named but
        #: the plan or a missing credential kept out of the chain. They are
        #: named in the final error, so "no model left" never reads as "the
        #: only model failed" when the truth is "the good ones were skipped".
        self.skipped = tuple(skipped)

    @staticmethod
    def _with_settings(
        args: tuple[Any, ...], kwargs: dict[str, Any], settings: Any
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Replace the SDK's model_settings argument in either call style."""
        call_args = list(args)
        call_kwargs = dict(kwargs)
        if len(call_args) >= 3:
            call_args[2] = settings
            call_kwargs.pop("model_settings", None)
        else:
            call_kwargs["model_settings"] = settings
        return tuple(call_args), call_kwargs

    @staticmethod
    def _with_input(args: tuple[Any, ...], kwargs: dict[str, Any], items: Any) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Replace the SDK's input argument in either call style."""
        if len(args) >= 2:
            return (args[0], items, *args[2:]), kwargs
        return args, {**kwargs, "input": items}

    @staticmethod
    def _input_of(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        return args[1] if len(args) >= 2 else kwargs.get("input")

    def _missing(self, candidate: ModelCandidate, needed: FrozenSet[str]) -> FrozenSet[str]:
        return frozenset(m for m in needed if candidate.accepts(m) is False)

    def _attempts(self, items: Any) -> deque:
        """(candidate, the modalities its request leaves out) in the order to try.

        The models that may take everything the request carries go first, in
        the agent's order; then the others, those missing least first, each
        with what it cannot take replaced by a note.
        """
        needed = request_modalities(items)
        if not needed:
            return deque((candidate, frozenset()) for candidate in self.candidates)
        plans = [(c, self._missing(c, needed)) for c in self.candidates]
        able = [plan for plan in plans if not plan[1]]
        unable = sorted((plan for plan in plans if plan[1]), key=lambda plan: len(plan[1]))
        return deque(able + unable)

    def _note(self, candidate: ModelCandidate, modality: str) -> str:
        able = [c.key for c in self.candidates if c.accepts(modality) is not False]
        if not able:
            return lacking_note(modality, [c.key for c in self.candidates])
        return failed_note(modality, candidate.key, able)

    def _call(self, candidate: ModelCandidate, leave_out: FrozenSet[str], args: tuple, kwargs: dict) -> tuple[tuple, dict]:
        call_args, call_kwargs = self._with_settings(args, kwargs, candidate.settings)
        if leave_out:
            items = self._input_of(call_args, call_kwargs)
            logger.info(
                "Model '%s' does not take %s: sending a note in its place",
                candidate.key, ", ".join(sorted(leave_out)),
            )
            call_args, call_kwargs = self._with_input(
                call_args, call_kwargs,
                replace_modalities(items, leave_out, lambda modality: self._note(candidate, modality)),
            )
        return call_args, call_kwargs

    def _refused(self, candidate: ModelCandidate, leave_out: FrozenSet[str], exc: BaseException, attempts: deque, items: Any) -> bool:
        """A provider that refused a kind of media the request sent it: remember
        it and try the model again without that kind, after the models that may
        still take it."""
        modality = refused_modality(exc)
        if modality is None or modality in leave_out or modality not in request_modalities(items):
            return False
        _REFUSED.setdefault(modality, set()).add(candidate.key)
        logger.warning("Model '%s' refused %s input; it gets a note in its place from now on", candidate.key, modality)
        attempts.append((candidate, leave_out | {modality}))
        return True

    def _give_up(self, errors: list[str], originals: list[BaseException]) -> BaseException:
        # One model, one failure: its own error, not a wrapper around it - unless
        # the chain was already shortened, which the reader has to see too.
        if len(originals) == 1 and not self.skipped:
            return originals[0]
        message = "All configured models failed: " + " | ".join(errors or ["no model was tried"])
        if self.skipped:
            message += ". Not tried: " + "; ".join(f"{key} ({why})" for key, why in self.skipped)
        # The single failure behind a shortened chain keeps its own verdict on
        # retrying: a transient outage is still worth another attempt, and the
        # message above carries the models that were never tried.
        cause = originals[0] if len(originals) == 1 else None
        return AllModelsFailedError(message, cause=cause)

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        errors: list[str] = []
        originals: list[BaseException] = []
        items = self._input_of(args, kwargs)
        attempts = self._attempts(items)
        while attempts:
            candidate, leave_out = attempts.popleft()
            try:
                call_args, call_kwargs = self._call(candidate, leave_out, args, kwargs)
                return await candidate.model.get_response(*call_args, **call_kwargs)
            except Exception as exc:
                if not can_fallback(exc):
                    raise
                if self._refused(candidate, leave_out, exc, attempts, items):
                    continue
                # A credential refused while signing the request, not a network failure.
                exc = credential_error_in(exc) or exc
                errors.append(f"{candidate.key}: {type(exc).__name__}: {exc}")
                originals.append(exc)
                logger.warning(
                    "Model '%s' failed%s: %s",
                    candidate.key,
                    "; trying next fallback" if attempts else "",
                    exc,
                )
        raise self._give_up(errors, originals)

    async def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        errors: list[str] = []
        originals: list[BaseException] = []
        items = self._input_of(args, kwargs)
        attempts = self._attempts(items)
        while attempts:
            candidate, leave_out = attempts.popleft()
            events: list[Any] = []
            committed = False
            try:
                call_args, call_kwargs = self._call(candidate, leave_out, args, kwargs)
                async for event in candidate.model.stream_response(
                    *call_args, **call_kwargs
                ):
                    if not committed:
                        event_type = getattr(event, "type", None)
                        if event_type in _FAILURE_EVENT_TYPES:
                            raise _stream_failure(event)
                        events.append(event)
                        if event_type in _LIFECYCLE_EVENT_TYPES:
                            continue
                        committed = True
                        # This candidate now produces the live stream: publish
                        # it before its first event reaches the consumer, so an
                        # observer attributes every event of the turn correctly.
                        _report_model_key(candidate.key)
                        for initial in events:
                            yield initial
                        events.clear()
                    else:
                        yield event
            except _StreamFailure as failure:
                if not failure.fallback and not can_fallback(failure):
                    raise
                errors.append(f"{candidate.key}: {failure}")
                originals.append(failure)
                logger.warning(
                    "Model '%s' reported %s%s",
                    candidate.key,
                    failure,
                    "; trying next fallback" if attempts else "",
                )
                continue
            except Exception as exc:
                if committed:
                    raise
                if not can_fallback(exc):
                    raise
                if self._refused(candidate, leave_out, exc, attempts, items):
                    continue
                exc = credential_error_in(exc) or exc
                errors.append(f"{candidate.key}: {type(exc).__name__}: {exc}")
                originals.append(exc)
                logger.warning(
                    "Model '%s' stream failed%s: %s",
                    candidate.key,
                    "; trying next fallback" if attempts else "",
                    exc,
                )
                continue

            for event in events:
                yield event
            return

        raise self._give_up(errors, originals)

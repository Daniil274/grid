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

import logging
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, FrozenSet, Optional, Sequence

from agents.models.interface import Model

from core.credentials import credential_error_in
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


class _StreamFailure(Exception):
    """An in-stream provider failure event, raised before anything committed."""


class AllModelsFailedError(RuntimeError):
    """Raised after every configured model failed the same request."""


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

    def __init__(self, candidates: Sequence[ModelCandidate]) -> None:
        if not candidates:
            raise ValueError("FallbackModel requires at least one candidate")
        self.candidates = tuple(candidates)

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

    @staticmethod
    def _give_up(errors: list[str], originals: list[BaseException]) -> BaseException:
        # One model, one failure: its own error, not a wrapper around it.
        if len(originals) == 1:
            return originals[0]
        return AllModelsFailedError("All configured models failed: " + " | ".join(errors))

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
                            raise _StreamFailure(event_type)
                        events.append(event)
                        if event_type in _LIFECYCLE_EVENT_TYPES:
                            continue
                        committed = True
                        for initial in events:
                            yield initial
                        events.clear()
                    else:
                        yield event
            except _StreamFailure as failure:
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

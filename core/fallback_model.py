"""Ordered model fallback for OpenAI Agents SDK model calls.

A request that carries images goes to the models that see first. A model that
cannot - ``vision: false`` in its config, or one whose provider refused images
once in this process - gets the request with each image replaced by a note, so
an image in the conversation never stops a chat whose models cannot read it.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional, Sequence

from agents.models.interface import Model


logger = logging.getLogger("grid.models.fallback")

# Lifecycle events a stream always opens with; buffered but never "content".
_LIFECYCLE_EVENT_TYPES = frozenset({"response.created", "response.in_progress"})
# A provider can report failure as an in-stream event instead of raising; before
# any real content has been yielded, that must still fall back to the next
# candidate rather than being forwarded as committed content.
_FAILURE_EVENT_TYPES = frozenset({"response.failed", "response.incomplete"})


#: Content part types that carry an image (core.agent_input).
_IMAGE_PART_TYPES = frozenset({"input_image", "image_url"})
#: What stands in for an image in a request to a model that cannot see.
IMAGE_NOTE = "[An image is attached here, but this model cannot see images.]"
#: A provider's refusal of image input, in the words providers use.
_IMAGE_REFUSAL = re.compile(r"(not|n't) support\w* (image|vision)|image input\w* (is|are) not supported", re.I)
#: Model keys whose provider refused images in this process.
_BLIND: set[str] = set()


def _is_image(part: Any) -> bool:
    return isinstance(part, dict) and part.get("type") in _IMAGE_PART_TYPES


def has_images(items: Any) -> bool:
    """Whether a model request's input holds an image anywhere."""
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        if _is_image(item):
            return True
        for key in ("content", "output"):
            parts = item.get(key)
            if isinstance(parts, list) and any(_is_image(part) for part in parts):
                return True
    return False


def strip_images(items: Any) -> Any:
    """*items* with every image replaced by :data:`IMAGE_NOTE`; the rest as it was."""
    if not isinstance(items, list):
        return items
    note = {"type": "input_text", "text": IMAGE_NOTE}
    stripped = []
    for item in items:
        if not isinstance(item, dict):
            stripped.append(item)
        elif _is_image(item):
            stripped.append(dict(note))
        else:
            copy = dict(item)
            for key in ("content", "output"):
                parts = copy.get(key)
                if isinstance(parts, list):
                    copy[key] = [dict(note) if _is_image(part) else part for part in parts]
            stripped.append(copy)
    return stripped


def is_image_refusal(exc: BaseException) -> bool:
    return bool(_IMAGE_REFUSAL.search(str(exc)))


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

    @property
    def sees(self) -> bool:
        return self.vision is not False and self.key not in _BLIND


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

    def _attempts(self, items: Any) -> deque:
        """(candidate, whether its request keeps the images) in the order to try."""
        if not has_images(items):
            return deque((candidate, True) for candidate in self.candidates)
        seeing = [(c, True) for c in self.candidates if c.sees]
        blind = [(c, False) for c in self.candidates if not c.sees]
        return deque(seeing + blind)

    def _call(self, candidate: ModelCandidate, images: bool, args: tuple, kwargs: dict) -> tuple[tuple, dict]:
        call_args, call_kwargs = self._with_settings(args, kwargs, candidate.settings)
        if not images:
            items = self._input_of(call_args, call_kwargs)
            logger.info("Model '%s' cannot see images: sending them as a note", candidate.key)
            call_args, call_kwargs = self._with_input(call_args, call_kwargs, strip_images(items))
        return call_args, call_kwargs

    def _refused_images(self, candidate: ModelCandidate, images: bool, exc: BaseException, attempts: deque, items: Any) -> bool:
        """A provider that refused the request's images: remember it and try the
        model again without them, after the models that may still see."""
        if not images or not has_images(items) or not is_image_refusal(exc):
            return False
        _BLIND.add(candidate.key)
        logger.warning("Model '%s' refused images; it gets them as a note from now on", candidate.key)
        attempts.append((candidate, False))
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
            candidate, images = attempts.popleft()
            try:
                call_args, call_kwargs = self._call(candidate, images, args, kwargs)
                return await candidate.model.get_response(*call_args, **call_kwargs)
            except Exception as exc:
                if self._refused_images(candidate, images, exc, attempts, items):
                    continue
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
            candidate, images = attempts.popleft()
            events: list[Any] = []
            committed = False
            try:
                call_args, call_kwargs = self._call(candidate, images, args, kwargs)
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
                if self._refused_images(candidate, images, exc, attempts, items):
                    continue
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

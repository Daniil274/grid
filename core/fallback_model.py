"""Ordered model fallback for OpenAI Agents SDK model calls."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Sequence

from agents.models.interface import Model


logger = logging.getLogger("grid.models.fallback")


class AllModelsFailedError(RuntimeError):
    """Raised after every configured model failed the same request."""


@dataclass(frozen=True)
class ModelCandidate:
    """One model and the settings that belong to it."""

    key: str
    model: Model
    settings: Any


class FallbackModel(Model):
    """Try configured models from left to right for every model request.

    Streaming events are buffered until one candidate finishes successfully.
    This lets a later candidate replace a failed partial stream without leaking
    duplicate text or tool calls to the runner.
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

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        errors: list[str] = []
        for index, candidate in enumerate(self.candidates):
            try:
                call_args, call_kwargs = self._with_settings(
                    args, kwargs, candidate.settings
                )
                return await candidate.model.get_response(*call_args, **call_kwargs)
            except Exception as exc:
                errors.append(f"{candidate.key}: {type(exc).__name__}: {exc}")
                logger.warning(
                    "Model '%s' failed%s: %s",
                    candidate.key,
                    "; trying next fallback" if index + 1 < len(self.candidates) else "",
                    exc,
                )
        raise AllModelsFailedError("All configured models failed: " + " | ".join(errors))

    async def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        errors: list[str] = []
        for index, candidate in enumerate(self.candidates):
            events: list[Any] = []
            try:
                call_args, call_kwargs = self._with_settings(
                    args, kwargs, candidate.settings
                )
                async for event in candidate.model.stream_response(
                    *call_args, **call_kwargs
                ):
                    events.append(event)
            except Exception as exc:
                errors.append(f"{candidate.key}: {type(exc).__name__}: {exc}")
                logger.warning(
                    "Model '%s' stream failed%s: %s",
                    candidate.key,
                    "; trying next fallback" if index + 1 < len(self.candidates) else "",
                    exc,
                )
                continue

            for event in events:
                yield event
            return

        raise AllModelsFailedError("All configured models failed: " + " | ".join(errors))

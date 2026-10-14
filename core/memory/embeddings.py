"""Text embeddings for semantic memory retrieval, over an OpenAI-compatible API.

The host builds the client (core.factory.memory), so in the web chat the calls
use the user's own credentials and are metered like every other model request.
Queries and documents are embedded differently: instruction-tuned models such
as Qwen3-Embedding expect the task described in front of the query only.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

#: The query instruction the default model was calibrated with (see MemoryEmbeddingsConfig).
DEFAULT_QUERY_INSTRUCTION = (
    "Instruct: Given a coding agent's task request, retrieve stored memory notes "
    "that are useful for doing this task\nQuery: "
)


class Embedder:
    """Embeds texts with *model* through *client* (``AsyncOpenAI`` or compatible)."""

    def __init__(self, client: Any, model: str, *, query_instruction: str = "",
                 batch_size: int = 32, max_chars: int = 8000) -> None:
        if not model:
            raise ValueError("An embedding model is required")
        self.client = client
        self.model = model
        self.query_instruction = query_instruction
        self.batch_size = max(1, batch_size)
        self.max_chars = max_chars

    async def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = [text[: self.max_chars] or " " for text in texts[start:start + self.batch_size]]
            response = await self.client.embeddings.create(model=self.model, input=batch, encoding_format="float")
            data = sorted(response.data, key=lambda item: item.index)
            if len(data) != len(batch):
                raise RuntimeError(f"{self.model} returned {len(data)} embeddings for {len(batch)} texts")
            vectors.extend(list(item.embedding) for item in data)
        return vectors

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(list(texts)) if texts else []

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([self.query_instruction + text]))[0]

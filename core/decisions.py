"""Shared Decisions API transport using Grid's model/provider registry."""

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(frozen=True)
class DecisionsModel:
    url: str
    api_key: str = field(repr=False)
    model_name: str
    timeout: float = 30
    proxy: str | None = None

    @classmethod
    def from_config(cls, config, model_key: str):
        model = config.get_model(model_key)
        provider = config.get_provider(model.provider)
        base_url = provider.base_url.rstrip("/").removesuffix("/v1")
        return cls(
            url=f"{base_url}/alpha/decisions",
            api_key=config.get_api_key(model.provider) or "",
            model_name=model.name,
            timeout=float(provider.timeout),
            proxy=config.get_proxy_for_provider(model.provider),
        )

    def http_client(self, *, transport=None, timeout=None):
        return httpx.AsyncClient(
            timeout=self.timeout if timeout is None else timeout,
            proxy=self.proxy,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    async def evaluate(
        self, http: httpx.AsyncClient, state: Any, questions: dict
    ) -> dict:
        response = await http.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model_name, "state": state, "questions": questions},
        )
        response.raise_for_status()
        return response.json()["answers"]

"""Synchronous MemoryCore v3 gateway adapter.

The gateway's resource routes are host-configurable: deployments must match them
and the documented JSON contract to their sidecar version. No tenant identity is
accepted from records, queries or tool arguments. Client ownership is local.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from threading import RLock
from typing import Any, Self

import httpx

from .models import (
    MemoryBackend,
    MemoryContextPack,
    MemoryLayer,
    MemoryQuery,
    MemoryRecord,
    MemorySearchHit,
    StaleStatus,
    build_context_pack,
    list_records_via_search,
    reciprocal_rank_fusion,
    record_from_dict,
    record_to_dict,
    stats_via_search,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TencentGatewayConfig:
    team_id: str
    user_id: str
    agent_id: str
    service_id: str
    base_url: str = "http://127.0.0.1:8000"
    bearer_token: str | None = None
    timeout: float = 5.0
    conversation_route: str = "/v3/conversations"
    memory_route: str = "/v3/memories"
    scenario_route: str = "/v3/scenarios"
    profile_route: str = "/v3/profile"
    health_route: str = "/v3/health"

    def __post_init__(self) -> None:
        if not all((self.team_id, self.user_id, self.agent_id, self.service_id)):
            raise ValueError("All host tenant identifiers are required")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        for route in (self.conversation_route, self.memory_route, self.scenario_route,
                      self.profile_route, self.health_route):
            if not route.startswith("/v3/") or "?" in route or "#" in route:
                raise ValueError("Gateway routes must be /v3/ paths")


class GatewayError(RuntimeError):
    """HTTP, envelope or response-contract failure (without leaking secrets)."""


class TencentMemoryBackend:
    name = "tencent"

    def __init__(self, config: TencentGatewayConfig, *,
                 transport: httpx.BaseTransport | None = None,
                 fallback: MemoryBackend | None = None, mirror: bool = False) -> None:
        self.config = config
        self.fallback = fallback
        self.mirror = mirror
        self._lock = RLock()
        self._closed = False
        headers = {"x-tdai-service-id": config.service_id}
        if config.bearer_token:
            headers["Authorization"] = f"Bearer {config.bearer_token}"
        self._client = httpx.Client(base_url=config.base_url, headers=headers,
                                    timeout=config.timeout, transport=transport,
                                    follow_redirects=False, trust_env=False)

    def _tenant(self) -> dict[str, str]:
        return {"team_id": self.config.team_id, "user_id": self.config.user_id,
                "agent_id": self.config.agent_id}

    def _route(self, layer: MemoryLayer) -> str:
        return {MemoryLayer.L0: self.config.conversation_route,
                MemoryLayer.L1: self.config.memory_route,
                MemoryLayer.L2: self.config.scenario_route,
                MemoryLayer.L3: self.config.profile_route}[layer]

    def _request(self, method: str, route: str, payload: Mapping[str, Any] | None = None) -> Any:
        # Tenant fields overwrite, never merge underneath, untrusted input.
        body = {**(payload or {}), **self._tenant()}
        try:
            with self._lock:
                if self._closed:
                    raise GatewayError("Gateway client closed")
                response = self._client.request(method, route,
                    params=body if method == "GET" else None,
                    json=body if method != "GET" else None)
            response.raise_for_status()
            envelope = response.json()
            if not isinstance(envelope, dict):
                raise GatewayError("Expected JSON envelope")
            if envelope.get("code", 0) not in (0, "0", "OK", "Success"):
                raise GatewayError("Gateway returned unsuccessful envelope")
            if "data" not in envelope:
                raise GatewayError("Gateway envelope missing data")
            return envelope["data"]
        except (httpx.HTTPError, ValueError) as exc:
            raise GatewayError("Memory gateway request failed") from exc

    @staticmethod
    def _records(data: Any, layer: MemoryLayer) -> list[MemoryRecord]:
        if data is None:
            return []
        if isinstance(data, dict):
            if "records" in data or "items" in data or "memories" in data:
                data = data.get("records", data.get("items", data.get("memories")))
            elif "record" in data or "profile" in data:
                data = [data.get("record", data.get("profile"))]
            elif "content" in data:
                data = [data]
        if not isinstance(data, list):
            raise GatewayError("Expected records in gateway data")
        records: list[MemoryRecord] = []
        try:
            for item in data:
                if not isinstance(item, dict):
                    raise GatewayError("Invalid memory item")
                item = item.get("record", item)
                records.append(record_from_dict({**item, "layer": layer}))
        except (ValueError, TypeError, KeyError) as exc:
            raise GatewayError("Invalid gateway memory record") from exc
        return records

    def remember(self, record: MemoryRecord, *, supersedes: str | None = None) -> MemoryRecord:
        local = record
        if self.mirror and self.fallback:
            local = self.fallback.remember(record, supersedes=supersedes)
        try:
            method = "PUT" if record.layer in (MemoryLayer.L2, MemoryLayer.L3) else "POST"
            data = self._request(method, self._route(record.layer),
                                 {"record": record_to_dict(local), "supersedes": supersedes})
            records = self._records(data, record.layer)
            return records[0] if records else local
        except GatewayError:
            logger.warning("Memory gateway unavailable; using local memory if configured")
            if self.fallback:
                return local if self.mirror else self.fallback.remember(record, supersedes=supersedes)
            # Explicitly signal non-persistence; the agent can continue without lying
            # about durability. Factory defaults provide a local fallback.
            return replace(record, stale_status=StaleStatus.UNVERIFIED,
                           metadata={**record.metadata, "persisted": False, "backend_error": "gateway_unavailable"})

    def search(self, query: MemoryQuery) -> list[MemorySearchHit]:
        rankings: list[list[str]] = []
        records: dict[str, MemoryRecord] = {}
        try:
            for layer in query.layers:
                if layer in (MemoryLayer.L2, MemoryLayer.L3):
                    data = self._request("GET", self._route(layer), {"query": query.text, "limit": query.limit})
                else:
                    data = self._request("POST", self._route(layer) + "/search",
                                         {"query": query.text, "limit": query.limit})
                found = self._records(data, layer)
                selected = [r for r in found if r.scope in query.scopes
                            and (query.include_stale or r.stale_status != StaleStatus.STALE)
                            and (query.include_superseded or not r.superseded_by)]
                rankings.append([r.id for r in selected])
                records.update({r.id: r for r in selected})
        except GatewayError:
            logger.warning("Memory gateway search unavailable; using local memory if configured")
            if self.fallback:
                return self.fallback.search(query)
        if self.mirror and self.fallback:
            local = self.fallback.search(query)
            rankings.append([hit.record.id for hit in local])
            for hit in local:
                records.setdefault(hit.record.id, hit.record)
        return [MemorySearchHit(records[rid], score, rrf_score=score)
                for rid, score in list(reciprocal_rank_fusion(rankings).items())[:query.limit]]

    def get(self, record_id: str) -> MemoryRecord | None:
        # The portable gateway contract has no cross-layer lookup route.
        if self.fallback:
            cached = self.fallback.get(record_id)
            if cached:
                return cached
        return next((hit.record for hit in self.search(MemoryQuery(limit=1000))
                     if hit.record.id == record_id), None)

    def forget(self, record_id: str) -> bool:
        """Delete remotely first; never claim deletion during a gateway outage."""
        record = self.get(record_id)
        if record is None:
            return False
        self._request("DELETE", self._route(record.layer), {"record_id": record_id})
        if self.fallback:
            self.fallback.forget(record_id)
        return True

    def append_conversation(self, content: str, **fields: Any) -> MemoryRecord:
        return self.remember(MemoryRecord(content=content, layer=MemoryLayer.L0, **fields))

    def search_conversation(self, text: str, limit: int = 20) -> list[MemorySearchHit]:
        return self.search(MemoryQuery(text=text, layers=(MemoryLayer.L0,), limit=limit))

    def remember_atomic(self, record: MemoryRecord) -> MemoryRecord:
        return self.remember(replace(record, layer=MemoryLayer.L1))

    def search_atomic(self, text: str, limit: int = 20) -> list[MemorySearchHit]:
        return self.search(MemoryQuery(text=text, layers=(MemoryLayer.L1,), limit=limit))

    def upsert_scenario(self, record: MemoryRecord) -> MemoryRecord:
        return self.remember(replace(record, layer=MemoryLayer.L2))

    def list_scenarios(self) -> tuple[MemoryRecord, ...]:
        return tuple(h.record for h in self.search(MemoryQuery(layers=(MemoryLayer.L2,))))

    def get_profile(self) -> tuple[MemoryRecord, ...]:
        return tuple(h.record for h in self.search(MemoryQuery(layers=(MemoryLayer.L3,))))

    def update_profile(self, record: MemoryRecord) -> MemoryRecord:
        return self.remember(replace(record, layer=MemoryLayer.L3))

    def build_context(self, query: MemoryQuery) -> MemoryContextPack:
        return build_context_pack(self, query)

    def list_records(self, **options: Any) -> list[MemoryRecord]:
        """Browse over the gateway's search (local fallback on an outage)."""
        return list_records_via_search(self, **options)

    def stats(self) -> dict[str, Any]:
        return stats_via_search(self)

    def healthcheck(self) -> bool:
        try:
            self._request("GET", self.config.health_route)
            return True
        except GatewayError:
            return False

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._client.close()
                self._closed = True
        if self.fallback:
            self.fallback.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

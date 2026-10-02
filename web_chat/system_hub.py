"""The systems page (``/systems``): make, test, follow and publish systems.

Two kinds of new systems meet here:

- **created systems** (core.system_store): admins make them, as a copy of an
  existing system they then edit, in ``routing.systems_dir`` of the catalog -
  a directory of the repository. A draft is offered to admins in the route
  picker, to test by hand; publishing hands it to the router for everyone.
- **user systems** (web_chat.user_systems): a user groups agents derived from
  the operator's templates. The owner tests it by hand, lets the router use it
  for them (``active``), and may *submit* it: a snapshot goes to the admins,
  who can import it as a created system - a draft they test and publish.

For every system the page shows its agents, the health check the router runs
(config problems and what this machine lacks), a routing probe - which system
the router picks for sample messages, the new one among them - and the activity
recorded for it (web_chat.system_activity).

    GET    /api/systems                                what the user may see here
    GET    /api/systems/{kind}/{key}                   one system: catalog, created, mine, submission
    POST   /api/systems/probe                          {kind, key, messages} -> the router's picks

    POST   /api/systems/created                        {key, name, description, source, agents}   admins
    PATCH  /api/systems/created/{key}                  {name, description, requires}              admins
    PUT    /api/systems/created/{key}/config           {yaml}                                     admins
    POST   /api/systems/created/{key}/status           {status: draft|published|archived}         admins
    DELETE /api/systems/created/{key}                                                             admins

    POST   /api/systems/mine                           a user system spec
    PUT    /api/systems/mine/{key}                     the same
    DELETE /api/systems/mine/{key}
    POST   /api/systems/mine/{key}/active              {active}
    POST   /api/systems/mine/{key}/submit              {note}
    DELETE /api/systems/mine/{key}/submit              withdraw a pending submission

    POST   /api/systems/submissions/{id}/import        {key, name}                                admins
    POST   /api/systems/submissions/{id}/decline       {note}                                     admins
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.config import Config
from core.managers.project_tools_loader import get_project_loader, set_project_loader
from core.system_quality import AcceptanceSuite, load_evidence, quality_summary, require_quality, save_evidence
from core.system_store import (
    StoreError,
    inspect_system,
    SystemManifest,
    SystemOrigin,
    SystemStore,
    check_key,
    relocate_config,
)
from web_chat.identity import User
from web_chat.space import UserSpace
from web_chat.system_activity import SystemActivity
from web_chat.user_systems import (
    Submission,
    UserSystem,
    UserSystemError,
    UserSystemNotFound,
    UserSystemSpec,
    build_agents,
    check_spec,
    materialize,
    member_key,
    spec_of,
)

logger = logging.getLogger("grid.web_chat.system_hub")

MAX_PROBE_MESSAGES = 10
MAX_PROBE_CHARS = 2000


# -- requests -----------------------------------------------------------------------------
class CreateSystemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    #: The system copied as the starting point; None starts from a blank config.
    source: Optional[str] = None
    #: The source's agents to keep; None keeps them all.
    agents: Optional[List[str]] = None


class UpdateSystemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    description: Optional[str] = Field(default=None, max_length=2000)
    requires: Optional[List[str]] = None


class ConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    yaml: str = Field(max_length=1_000_000)


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["draft", "published", "archived"]


class ActiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool


class NoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=2000)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["catalog", "created", "mine", "built"]
    key: str
    messages: List[str] = Field(min_length=1, max_length=MAX_PROBE_MESSAGES)


# -- submissions --------------------------------------------------------------------------
class SubmissionRecord(BaseModel):
    """A user system offered to everyone, frozen as it was submitted."""

    id: str
    user_id: str
    username: str = ""
    system: UserSystem
    note: str = ""
    at: float
    state: Literal["pending", "imported", "declined", "withdrawn"] = "pending"
    decided_at: Optional[float] = None
    decided_by: str = ""
    decision_note: str = ""
    #: The created system it became.
    system_key: Optional[str] = None


class SubmissionStore:
    """One JSON file per submission, replaced atomically."""

    def __init__(self, root: Optional[Path]) -> None:
        self.root = Path(root) if root is not None else None

    def _path(self, submission_id: str) -> Path:
        if self.root is None or not submission_id.isalnum():
            raise KeyError(submission_id)
        return self.root / f"{submission_id}.json"

    def list(self) -> List[SubmissionRecord]:
        if self.root is None or not self.root.is_dir():
            return []
        records = []
        for path in sorted(self.root.glob("*.json")):
            try:
                records.append(SubmissionRecord(**json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError) as exc:
                logger.error("Submission %s is unreadable: %s", path, exc)
        return sorted(records, key=lambda record: record.at)

    def get(self, submission_id: str) -> SubmissionRecord:
        try:
            path = self._path(submission_id)
            return SubmissionRecord(**json.loads(path.read_text(encoding="utf-8")))
        except (KeyError, OSError, ValueError):
            raise HTTPException(status_code=404, detail="No such submission") from None

    def save(self, record: SubmissionRecord) -> None:
        if self.root is None:
            raise HTTPException(status_code=400, detail="This server takes no submissions")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(record.id)
        partial = path.with_name(path.name + ".tmp")
        partial.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        os.replace(partial, path)


# -- what a system is made of -------------------------------------------------------------
def _loading(load: Callable[[], Config]) -> Config:
    """Load a config without leaving its project tools loader behind.

    Loading a config replaces the process-wide loader (core.routing does the same).
    """
    previous = get_project_loader()
    try:
        return load()
    finally:
        set_project_loader(previous)


def _round_trip():
    from ruamel.yaml import YAML

    loader = YAML()
    loader.preserve_quotes = True
    loader.width = 4096
    loader.indent(mapping=2, sequence=4, offset=2)
    return loader


def _dump(document: Any) -> str:
    buffer = io.StringIO()
    _round_trip().dump(document, buffer)
    return buffer.getvalue()


def _validate(text: str) -> None:
    from schemas import GridConfig

    document = yaml.safe_load(text) or {}
    if not isinstance(document, dict):
        raise StoreError("A config is a YAML mapping.")
    GridConfig(**document)


def copy_config(source_path: Path, target_dir: Path, agents: Optional[List[str]]) -> str:
    """The source config's text, moved to *target_dir*, with only *agents* kept.

    An agent tool whose target is dropped goes too, from the declarations and
    from the kept agents' lists, so the copy does not start broken.
    """
    document = _round_trip().load(source_path.read_text(encoding="utf-8"))
    if agents is not None:
        declared = document.get("agents") or {}
        unknown = set(agents) - set(declared)
        if unknown:
            raise StoreError(f"The source has no agents {', '.join(sorted(unknown))}.")
        if not agents:
            raise StoreError("Keep at least one agent.")
        for key in [key for key in declared if key not in agents]:
            del declared[key]
        tools = document.get("tools") or {}
        dropped = [
            name
            for name, tool in tools.items()
            if isinstance(tool, dict) and tool.get("type") == "agent" and tool.get("target_agent") not in agents
        ]
        for name in dropped:
            del tools[name]
        for agent in declared.values():
            if isinstance(agent, dict) and isinstance(agent.get("tools"), list):
                kept = [tool for tool in agent["tools"] if tool not in dropped]
                agent["tools"].clear()
                agent["tools"].extend(kept)
        settings = document.setdefault("settings", {})
        if settings.get("default_agent") not in agents:
            settings["default_agent"] = agents[0]
    relocate_config(document, source_path.parent, target_dir)
    return _dump(document)


def blank_config(base: Config) -> str:
    """A connected coordinator, worker and reviewer using the base model."""
    raw = yaml.safe_load(Path(base.config_path).read_text(encoding="utf-8")) or {}
    models = raw.get("models") or {}
    model = base.config.agents[base.get_default_agent()].primary_model
    used = models[model]
    document = {
        "settings": {"default_agent": "assistant", "working_directory": ".", "allow_path_override": True, "max_turns": 100},
        "providers": {used["provider"]: raw["providers"][used["provider"]]},
        "models": {model: used},
        "tools": {
            "call_worker": {"type": "agent", "target_agent": "worker", "context_strategy": "minimal",
                            "description": "Delegate the task with inputs and acceptance criteria."},
            "call_reviewer": {"type": "agent", "target_agent": "reviewer", "context_strategy": "minimal",
                              "description": "Independently check the result against the original criteria."},
        },
        "agents": {
            "assistant": {
                "name": "Coordinator",
                "model": model,
                "description": "Coordinate execution and independent review of the user's task.",
                "tools": ["call_worker", "call_reviewer"],
                "custom_prompt": "Delegate execution to call_worker with inputs and acceptance criteria. "
                                 "Send the result and original criteria to call_reviewer. Return findings to "
                                 "the worker for correction, at most three cycles. Report the result, verification "
                                 "and any unresolved limitations. Do not simulate the other agents' roles.\n",
            },
            "worker": {
                "name": "Worker", "model": model, "routable": False, "tools": [],
                "custom_prompt": "Execute the delegated task. Return the concrete result and evidence. "
                                 "If required inputs or tools are unavailable, report the blocker explicitly.\n",
            },
            "reviewer": {
                "name": "Reviewer", "model": model, "routable": False, "tools": [],
                "custom_prompt": "Independently check the result against the original inputs and acceptance "
                                 "criteria. Report specific defects or verified findings. Distinguish unverified "
                                 "claims from evidence; never approve work you cannot check.\n",
            },
        },
    }
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def export_user_system(system: UserSystemSpec, base: Config, target_dir: Path) -> str:
    """A user system as a config file of its own in *target_dir*.

    The base system's file, its agents made unroutable, the members and their
    ask tools added, moved to the new directory.
    """
    base_path = Path(base.config_path)
    document = _round_trip().load(base_path.read_text(encoding="utf-8"))
    agents, tools = build_agents(system, base)
    declared = document.setdefault("agents", {})
    for agent in declared.values():
        if isinstance(agent, dict):
            agent["routable"] = False
    for key, agent in agents.items():
        declared[key] = agent.model_dump(mode="json", exclude_defaults=True) | {
            "name": agent.name,
            "model": agent.model,
            "tools": list(agent.tools),
            "routable": agent.routable,
        }
    declared_tools = document.setdefault("tools", {})
    for name, tool in tools.items():
        declared_tools[name] = tool.model_dump(mode="json", exclude_none=True, exclude_defaults=True) | {
            "type": "agent"
        }
    document.setdefault("settings", {})["default_agent"] = member_key(system.entry)
    relocate_config(document, base_path.parent, target_dir)
    return _dump(document)


# -- the hub ------------------------------------------------------------------------------
class SystemHub:
    """What the systems page does, for the routes below."""

    def __init__(
        self,
        deployment: Any,
        spaces: Any,
        activity: SystemActivity,
        submissions: SubmissionStore,
        names: Callable[[], Dict[str, str]],
    ) -> None:
        self.deployment = deployment
        self.spaces = spaces
        self.activity = activity
        self.submissions = submissions
        self.names = names

    # -- helpers -------------------------------------------------------------------
    @property
    def store(self) -> SystemStore:
        store = self.deployment.created_store
        if store is None:
            raise HTTPException(
                status_code=400,
                detail="The catalog names no systems_dir: created systems are off on this server.",
            )
        return store

    async def configs_changed(self) -> None:
        """Every space picks up the created systems as they are now (drafts, the router)."""
        self.spaces.invalidate()
        await self.spaces.sweep()

    def _catalog_keys(self) -> List[str]:
        catalog = self.deployment.catalog
        return list(catalog.config.routing.systems) if catalog is not None else []

    def _admins_only(self, key: str) -> bool:
        system = self.deployment.catalog.config.routing.systems.get(key) if self.deployment.catalog else None
        return bool(system and system.admins_only)

    def _builder_key(self) -> Optional[str]:
        """The catalog system whose agents have the builder's tools."""
        for key in self._catalog_keys():
            try:
                raw = yaml.safe_load(self.deployment.system_config_path(key).read_text(encoding="utf-8")) or {}
            except (OSError, KeyError, ValueError):
                continue
            if "builder_create" in (raw.get("tools") or {}):
                return key
        return None

    def _requires(self, key: str) -> List[str]:
        catalog = self.deployment.catalog
        system = catalog.config.routing.systems.get(key) if catalog is not None else None
        if system is not None:
            return list(system.requires)
        store = self.deployment.created_store
        if store is not None and store.exists(key):
            return list(store.get(key).requires)
        return []

    @staticmethod
    def _who(user: User) -> str:
        return user.username or user.id

    # -- overview ------------------------------------------------------------------
    def overview(self, user: User, space: UserSpace) -> Dict[str, Any]:
        counts = self.activity.counts()
        items: List[Dict[str, Any]] = []

        def item(kind: str, key: str, name: str, description: str, status_: str, **extra: Any) -> None:
            usage = counts.get(extra.get("chat_key", key), {})
            items.append(
                {
                    "kind": kind,
                    "key": key,
                    "name": name,
                    "description": " ".join((description or "").split()),
                    "status": status_,
                    "turns": usage.get("turns", 0),
                    "errors": usage.get("errors", 0),
                    "last_at": usage.get("last_at"),
                    **extra,
                }
            )

        store = self.deployment.created_store
        if user.is_admin:
            catalog = self.deployment.catalog
            for key, system in (catalog.config.routing.systems.items() if catalog is not None else []):
                item("catalog", key, key.replace("_", " ").replace("-", " ").title(), system.description, "catalog")
            for manifest in store.list() if store is not None else []:
                item(
                    "created",
                    manifest.key,
                    manifest.name,
                    manifest.description,
                    manifest.status,
                    origin=manifest.origin.model_dump(),
                    updated_at=manifest.updated_at,
                )
            for record in self.submissions.list():
                if record.state == "pending":
                    item(
                        "submission",
                        record.id,
                        record.system.name,
                        record.system.description,
                        "submitted",
                        by=record.username or record.user_id,
                        at=record.at,
                    )
        mine = space.user_systems
        if mine is not None:
            for system in mine.list():
                item(
                    "mine",
                    system.key,
                    system.name,
                    system.description,
                    "active" if system.active else "draft",
                    submission=self._submission_view(system),
                    updated_at=system.updated_at,
                )
        built = getattr(space, "built_systems", None)
        if built is not None:
            access = space._builder_access()
            for manifest in built.list():
                item("built", manifest.key, manifest.name, manifest.description,
                     "active" if manifest.status == "published" else manifest.status,
                     chat_key=access.system_key(manifest.key), updated_at=manifest.updated_at)
        builder = self._builder_key()
        return {
            "admin": user.is_admin,
            "created_enabled": store is not None,
            "user_systems": self._user_offer(space),
            "sources": [key for key in self._catalog_keys() if not self._admins_only(key) and key != builder]
            + [m.key for m in (store.published() if store else [])],
            "builder": builder if builder in space.registry.keys() and (store is not None or built is not None) else None,
            "items": items,
        }

    def _user_offer(self, space: UserSpace) -> Dict[str, Any]:
        """What a user may build systems from: bases, their templates, models, limits."""
        policy = self.deployment.personal_agents_policy
        mine = space.user_systems
        enabled = mine is not None and mine.enabled
        bases = []
        for base, templates in (policy.templates.items() if enabled else []):
            try:
                config = _loading(lambda base=base: space.fresh_config(base))
            except Exception as exc:
                logger.warning("Base system %s is not available: %s", base, exc)
                continue
            models = config.config.models
            offered = []
            for key in templates:
                agent = config.config.agents.get(key)
                if agent is None:
                    continue
                offered.append(
                    {
                        "agent": key,
                        "name": agent.name or key,
                        "description": agent.description or "",
                        "model": agent.primary_model,
                        "models": [
                            {"key": model, "name": getattr(models.get(model), "name", model)}
                            for model in dict.fromkeys([agent.primary_model, *policy.models])
                            if model in models
                        ],
                        "tools": [
                            {"key": tool, "description": getattr(config.config.tools.get(tool), "description", "") or ""}
                            for tool in agent.tools
                        ],
                    }
                )
            if offered:
                bases.append({"system": base, "name": base.replace("_", " ").title(), "templates": offered})
        return {
            "enabled": enabled and bool(bases),
            "bases": bases,
            "limits": {
                "max_systems": policy.max_systems,
                "max_members": policy.max_system_members,
                "max_instructions_chars": policy.max_instructions_chars,
            },
        }

    def _submission_view(self, system: UserSystem) -> Optional[Dict[str, Any]]:
        if system.submission is None:
            return None
        try:
            record = self.submissions.get(system.submission.id)
        except HTTPException:
            return {"id": system.submission.id, "at": system.submission.at, "state": "unknown"}
        return {
            "id": record.id,
            "at": record.at,
            "state": record.state,
            "note": record.decision_note,
            "system_key": record.system_key,
        }

    # -- one system ----------------------------------------------------------------
    def detail(self, user: User, space: UserSpace, kind: str, key: str) -> Dict[str, Any]:
        confined = space.require_isolation
        names = self.names()
        if kind == "built":
            store = self._built(space)
            manifest = self._manifest(store, key)
            access = space._builder_access()
            chat_key = access.system_key(key)
            return {
                "kind": kind, **manifest.model_dump(),
                "status": "active" if manifest.status == "published" else manifest.status,
                "active": manifest.status == "published",
                "chat_key": chat_key,
                "config_yaml": store.config_text(key),
                "quality": quality_summary(store.directory(key)),
                "acceptance": load_evidence(store.directory(key), "acceptance"),
                "inspection": inspect_system(lambda: access.load(key, str(space.workspace_path)),
                                             requires=manifest.requires, confined=confined),
                "activity": self.activity.summary(chat_key, names=names),
            }
        if kind == "catalog":
            self._admin(user)
            if key not in self._catalog_keys():
                raise HTTPException(status_code=404, detail="No such catalog system")
            system = self.deployment.catalog.config.routing.systems[key]
            return {
                "kind": kind,
                "key": key,
                "name": key.replace("_", " ").replace("-", " ").title(),
                "description": system.description,
                "status": "catalog",
                "requires": list(system.requires),
                "inspection": inspect_system(lambda: space.fresh_config(key), requires=system.requires, confined=confined),
                "activity": self.activity.summary(key, names=names),
            }
        if kind == "created":
            self._admin(user)
            store = self.store
            manifest = self._manifest(store, key)
            return {
                "kind": kind,
                **manifest.model_dump(),
                "config_yaml": store.config_text(key),
                "quality": quality_summary(store.directory(key)),
                "acceptance": load_evidence(store.directory(key), "acceptance"),
                "config_path": str(store.config_path(key)),
                "inspection": inspect_system(
                    lambda: space.fresh_config(key), requires=manifest.requires, confined=confined
                ),
                "activity": self.activity.summary(key, names=names),
            }
        if kind == "mine":
            mine = self._mine(space)
            system = self._user_system(mine, key)
            return {
                "kind": kind,
                **system.model_dump(),
                "status": "active" if system.active else "draft",
                "submission": self._submission_view(system),
                "inspection": inspect_system(lambda: mine.build(key), confined=confined),
                "activity": self.activity.summary(key, names=names),
            }
        if kind == "submission":
            self._admin(user)
            record = self.submissions.get(key)
            snapshot = record.system

            def load() -> Config:
                config = space.fresh_config(snapshot.base)
                check_spec(snapshot, config, self.deployment.personal_agents_policy)
                return materialize(snapshot, config)

            return {
                "kind": kind,
                "key": record.id,
                "name": snapshot.name,
                "description": snapshot.description,
                "status": record.state,
                "submission": record.model_dump(),
                "spec": spec_of(snapshot).model_dump(),
                "inspection": inspect_system(load, confined=confined),
                "activity": self.activity.summary(snapshot.key, names=names),
            }
        raise HTTPException(status_code=404, detail="No such kind of system")

    @staticmethod
    def _admin(user: User) -> None:
        if not user.is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins only")

    @staticmethod
    def _manifest(store: SystemStore, key: str) -> SystemManifest:
        try:
            return store.get(key)
        except StoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @staticmethod
    def _mine(space: UserSpace):
        if space.user_systems is None:
            raise HTTPException(status_code=400, detail="Systems of your own need a server with accounts.")
        return space.user_systems

    @staticmethod
    def _built(space: UserSpace) -> SystemStore:
        store = getattr(space, "built_systems", None)
        if store is None:
            raise HTTPException(status_code=404, detail="No private builder store.")
        return store

    def edit_built(self, space: UserSpace, key: str, *, changes=None, config_yaml=None, active=None, delete=False):
        store = self._built(space)
        manifest = self._manifest(store, key)
        access = space._builder_access()
        try:
            if manifest.status == "published" and (config_yaml is not None or (changes and set(changes) - {"name"})):
                raise StoreError("Prepare a new draft with builder_fork, or deactivate this system before changing its behavior.")
            if config_yaml is not None:
                access.validate(config_yaml, key)
                store.write_config(key, config_yaml)
            if changes is not None:
                store.update(key, **changes)
            if active is not None:
                if active:
                    require_quality(store.directory(key))
                    inspection = inspect_system(lambda: access.load(key, str(space.workspace_path)), confined=True)
                    if not inspection["loaded"] or inspection["config"]:
                        raise StoreError("Fix the system's config before activating it.")
                store.set_status(key, "published" if active else "draft")
            if delete:
                store.set_status(key, "draft")
                store.delete(key)
        except (StoreError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        access.done(key, "deleted" if delete else "edited")
        return {"ok": True, "key": key}

    @staticmethod
    def _user_system(mine, key: str) -> UserSystem:
        try:
            return mine.get(key)
        except UserSystemNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    # -- the routing probe ---------------------------------------------------------
    async def probe(self, user: User, space: UserSpace, body: ProbeRequest) -> Dict[str, Any]:
        """Which system the router picks for each message, with the tested one among the candidates."""
        registry = space.registry
        if not registry.can_route or not registry.has_catalog:
            raise HTTPException(status_code=400, detail="This server does not route between systems.")
        tested_key = body.key
        if body.kind == "mine":
            system = self._user_system(self._mine(space), body.key)
            description = system.description
        elif body.kind == "built":
            description = self._manifest(self._built(space), body.key).description
            tested_key = space._builder_access().system_key(body.key)
        elif body.kind == "created":
            self._admin(user)
            description = self._manifest(self.store, body.key).description
        else:
            self._admin(user)
            if body.key not in self._catalog_keys():
                raise HTTPException(status_code=404, detail="No such catalog system")
            description = self.deployment.catalog.config.routing.systems[body.key].description
        if not description.strip():
            raise HTTPException(status_code=400, detail="Give the system a description first: the router picks by it.")
        candidates = {**registry.route_candidates(), tested_key: description}
        results = []
        for message in body.messages:
            text = message.strip()[:MAX_PROBE_CHARS]
            if not text:
                continue
            chosen = await registry.probe(text, candidates)
            results.append({"message": text, "chosen": chosen, "hit": chosen == tested_key})
        return {
            "candidates": [{"key": key, "description": text} for key, text in candidates.items()],
            "results": results,
            "hits": sum(result["hit"] for result in results),
        }

    # -- created systems -----------------------------------------------------------
    async def create(self, user: User, body: CreateSystemRequest) -> Dict[str, Any]:
        store = self.store
        key = self._new_key(body.key)
        if body.source:
            try:
                source_path = self.deployment.system_config_path(body.source)
            except KeyError:
                raise HTTPException(status_code=400, detail=f"No system '{body.source}' to start from.") from None
        else:
            source_path = None
        try:
            if source_path is not None:
                text = copy_config(source_path, store.directory(key), body.agents)
            else:
                text = blank_config(self.deployment.config)
            _validate(text)
            manifest = store.create(
                SystemManifest(
                    key=key,
                    name=body.name,
                    description=body.description,
                    requires=self._requires(body.source) if body.source else [],
                    origin=SystemOrigin(kind="admin", user_id=user.id, username=user.username, source=body.source or ""),
                ),
                text,
                skills_from=source_path.parent / "skills" if source_path is not None else None,
            )
        except (StoreError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        self.activity.record_event(key, "created", by=self._who(user), note=f"copy of {body.source}" if body.source else "blank")
        await self.configs_changed()
        return manifest.model_dump()

    def _new_key(self, key: str) -> str:
        try:
            check_key(key)
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if key in self._catalog_keys() or self.store.exists(key) or key.startswith("us_"):
            raise HTTPException(status_code=400, detail=f"The key '{key}' is taken.")
        return key

    async def update(self, user: User, key: str, body: UpdateSystemRequest) -> Dict[str, Any]:
        changes = body.model_dump(exclude_none=True)
        current = self._manifest(self.store, key)
        if current.status == "published" and set(changes) - {"name"}:
            raise HTTPException(status_code=400, detail="Withdraw or fork the system before changing routing or requirements.")
        try:
            manifest = self.store.update(key, **changes)
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        self.activity.record_event(key, "described", by=self._who(user), note=", ".join(sorted(changes)))
        await self.configs_changed()
        return manifest.model_dump()

    async def save_config(self, user: User, key: str, body: ConfigRequest) -> Dict[str, Any]:
        store = self.store
        manifest = self._manifest(store, key)
        if manifest.status == "published":
            raise HTTPException(status_code=400, detail="Prepare a new draft with builder_fork, or withdraw this system before editing it.")
        try:
            _validate(body.yaml)
            manifest = store.write_config(key, body.yaml)
        except (StoreError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        self.activity.record_event(
            key, "edited", by=self._who(user), note="while published" if manifest.status == "published" else ""
        )
        await self.configs_changed()
        return manifest.model_dump()

    def _fresh(self, key: str) -> Config:
        """A system's config as its file says, for checks that need no user's workspace."""
        return Config(str(self.deployment.system_config_path(key)))

    async def set_status(self, user: User, key: str, body: StatusRequest) -> Dict[str, Any]:
        store = self.store
        manifest = self._manifest(store, key)
        if body.status == manifest.status:
            return manifest.model_dump()
        if body.status == "published":
            try:
                require_quality(store.directory(key))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            if not manifest.description.strip():
                raise HTTPException(status_code=400, detail="Describe the system first: the router picks by its description.")
            inspection = inspect_system(lambda: Config(str(store.config_path(key))), requires=manifest.requires)
            if not inspection["loaded"]:
                raise HTTPException(status_code=400, detail=f"The config does not load: {inspection['error']}")
            if inspection["config"]:
                raise HTTPException(
                    status_code=400,
                    detail="Fix the config first: " + "; ".join(inspection["config"][:3]),
                )
        event = {
            ("draft", "published"): "published",
            ("archived", "published"): "published",
            ("published", "draft"): "withdrawn",
            ("archived", "draft"): "restored",
        }.get((manifest.status, body.status), body.status)
        manifest = store.set_status(key, body.status)
        self.activity.record_event(key, event, by=self._who(user))
        await self.configs_changed()
        return manifest.model_dump()

    async def delete(self, user: User, key: str) -> None:
        try:
            self.store.delete(key)
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        self.activity.record_event(key, "deleted", by=self._who(user))
        await self.configs_changed()

    # -- user systems --------------------------------------------------------------
    def create_mine(self, user: User, space: UserSpace, spec: UserSystemSpec) -> Dict[str, Any]:
        built = getattr(space, "built_systems", None)
        if built is not None and len(built.list()) + len(self._mine(space).list()) >= self.deployment.personal_agents_policy.max_systems:
            raise HTTPException(status_code=400, detail="Your system limit includes systems made with the builder.")
        system = self._change_mine(space, lambda mine: mine.create(spec))
        self.activity.record_event(system.key, "created", by=self._who(user))
        return system.model_dump()

    def update_mine(self, user: User, space: UserSpace, key: str, spec: UserSystemSpec) -> Dict[str, Any]:
        system = self._change_mine(space, lambda mine: mine.update(key, spec))
        self.activity.record_event(key, "edited", by=self._who(user))
        return system.model_dump()

    def delete_mine(self, user: User, space: UserSpace, key: str) -> None:
        self._change_mine(space, lambda mine: mine.delete(key))
        self.activity.record_event(key, "deleted", by=self._who(user))

    def activate_mine(self, user: User, space: UserSpace, key: str, active: bool) -> Dict[str, Any]:
        if active:
            system = self._user_system(self._mine(space), key)
            if not system.description.strip():
                raise HTTPException(status_code=400, detail="Describe the system first: the router picks by its description.")
            self._must_build(space, key)
        system = self._change_mine(space, lambda mine: mine.set_active(key, active))
        self.activity.record_event(key, "activated" if active else "deactivated", by=self._who(user))
        return system.model_dump()

    def submit_mine(self, user: User, space: UserSpace, key: str, note: str) -> Dict[str, Any]:
        mine = self._mine(space)
        system = self._user_system(mine, key)
        current = self._submission_view(system)
        if current and current["state"] == "pending":
            raise HTTPException(status_code=400, detail="This system is already waiting for the admins.")
        self._must_build(space, key)
        record = SubmissionRecord(
            id=uuid.uuid4().hex[:12],
            user_id=user.id,
            username=user.username,
            system=system,
            note=note,
            at=time.time(),
        )
        self.submissions.save(record)
        system = self._change_mine(space, lambda mine: mine.record_submission(key, Submission(id=record.id, at=record.at)))
        self.activity.record_event(key, "submitted", by=self._who(user), note=note)
        return system.model_dump()

    def withdraw_submission(self, user: User, space: UserSpace, key: str) -> Dict[str, Any]:
        system = self._user_system(self._mine(space), key)
        if system.submission is None:
            raise HTTPException(status_code=400, detail="The system was not submitted.")
        record = self.submissions.get(system.submission.id)
        if record.state != "pending":
            raise HTTPException(status_code=400, detail="The admins have already decided.")
        self.submissions.save(record.model_copy(update={"state": "withdrawn", "decided_at": time.time()}))
        self.activity.record_event(key, "submission withdrawn", by=self._who(user))
        return self._change_mine(space, lambda mine: mine.record_submission(key, None)).model_dump()

    def _change_mine(self, space: UserSpace, edit):
        try:
            return space.change_user_systems(edit)
        except UserSystemNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except UserSystemError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    def _must_build(self, space: UserSpace, key: str) -> None:
        inspection = inspect_system(lambda: self._mine(space).build(key))
        if not inspection["loaded"]:
            raise HTTPException(status_code=400, detail=f"The system does not build: {inspection['error']}")
        if inspection["config"]:
            raise HTTPException(status_code=400, detail="Fix the system first: " + "; ".join(inspection["config"][:3]))

    # -- submissions ---------------------------------------------------------------
    async def import_submission(self, user: User, submission_id: str, body: ImportRequest) -> Dict[str, Any]:
        record = self.submissions.get(submission_id)
        if record.state != "pending":
            raise HTTPException(status_code=400, detail=f"The submission is {record.state}.")
        store = self.store
        key = self._new_key(body.key)
        snapshot = record.system
        try:
            base = _loading(lambda: self._fresh(snapshot.base))
            check_spec(snapshot, base, self.deployment.personal_agents_policy)
            text = export_user_system(snapshot, base, store.directory(key))
            _validate(text)
            manifest = store.create(
                SystemManifest(
                    key=key,
                    name=body.name or snapshot.name,
                    description=snapshot.description,
                    requires=self._requires(snapshot.base),
                    origin=SystemOrigin(
                        kind="user", user_id=record.user_id, username=record.username, source=snapshot.key
                    ),
                ),
                text,
                skills_from=Path(base.config_path).parent / "skills",
            )
        except (StoreError, UserSystemError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        self.submissions.save(
            record.model_copy(
                update={"state": "imported", "decided_at": time.time(), "decided_by": self._who(user), "system_key": key}
            )
        )
        self.activity.record_event(key, "imported", by=self._who(user), note=f"from {record.username or record.user_id}'s {snapshot.name}")
        self.activity.record_event(snapshot.key, "imported", by=self._who(user), note=f"as {key}")
        await self.configs_changed()
        return manifest.model_dump()

    def decline_submission(self, user: User, submission_id: str, note: str) -> Dict[str, Any]:
        record = self.submissions.get(submission_id)
        if record.state != "pending":
            raise HTTPException(status_code=400, detail=f"The submission is {record.state}.")
        record = record.model_copy(
            update={"state": "declined", "decided_at": time.time(), "decided_by": self._who(user), "decision_note": note}
        )
        self.submissions.save(record)
        self.activity.record_event(record.system.key, "declined", by=self._who(user), note=note)
        return record.model_dump()


def register_system_routes(
    api: APIRouter,
    hub: SystemHub,
    current_user: Callable[..., Any],
    current_space: Callable[..., Any],
    admin: Callable[..., Any],
) -> None:
    """The routes of the module docs. A change to a created system takes no lease on
    the admin's space: it retires the spaces, and one held by the request could not go."""

    @api.put("/api/systems/{kind}/{key}/acceptance")
    async def save_acceptance(
        kind: str, key: str, body: ConfigRequest,
        user: User = Depends(current_user), space: UserSpace = Depends(current_space),
    ) -> JSONResponse:
        if kind == "created":
            hub._admin(user)
            store = hub.store
        elif kind == "built":
            store = hub._built(space)
        else:
            raise HTTPException(status_code=404, detail="Acceptance cases apply to created systems")
        hub._manifest(store, key)
        if len(body.yaml.encode()) > 512 * 1024:
            raise HTTPException(status_code=400, detail="Acceptance suite exceeds 512 KiB")
        try:
            suite = AcceptanceSuite.model_validate(yaml.safe_load(body.yaml))
        except (ValueError, yaml.YAMLError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        save_evidence(store.directory(key), "acceptance", suite.model_dump())
        save_evidence(store.directory(key), "contract-required", {"required": True})
        hub.activity.record_event(key, "acceptance updated", by=hub._who(user))
        return JSONResponse({"ok": True})

    @api.get("/api/systems")
    async def overview(user: User = Depends(current_user), space: UserSpace = Depends(current_space)) -> JSONResponse:
        return JSONResponse(hub.overview(user, space))

    @api.post("/api/systems/probe")
    async def probe(
        body: ProbeRequest, user: User = Depends(current_user), space: UserSpace = Depends(current_space)
    ) -> JSONResponse:
        return JSONResponse(await hub.probe(user, space, body))

    @api.post("/api/systems/created")
    async def create(body: CreateSystemRequest, user: User = Depends(admin)) -> JSONResponse:
        return JSONResponse(await hub.create(user, body), status_code=status.HTTP_201_CREATED)

    @api.patch("/api/systems/built/{key}")
    async def update_built(key: str, body: UpdateSystemRequest, space: UserSpace = Depends(current_space)) -> JSONResponse:
        return JSONResponse(hub.edit_built(space, key, changes=body.model_dump(exclude_none=True)))

    @api.put("/api/systems/built/{key}/config")
    async def config_built(key: str, body: ConfigRequest, space: UserSpace = Depends(current_space)) -> JSONResponse:
        return JSONResponse(hub.edit_built(space, key, config_yaml=body.yaml))

    @api.post("/api/systems/built/{key}/active")
    async def active_built(key: str, body: ActiveRequest, space: UserSpace = Depends(current_space)) -> JSONResponse:
        return JSONResponse(hub.edit_built(space, key, active=body.active))

    @api.delete("/api/systems/built/{key}")
    async def delete_built(key: str, space: UserSpace = Depends(current_space)) -> JSONResponse:
        return JSONResponse(hub.edit_built(space, key, delete=True))

    @api.patch("/api/systems/created/{key}")
    async def update(key: str, body: UpdateSystemRequest, user: User = Depends(admin)) -> JSONResponse:
        return JSONResponse(await hub.update(user, key, body))

    @api.put("/api/systems/created/{key}/config")
    async def save_config(key: str, body: ConfigRequest, user: User = Depends(admin)) -> JSONResponse:
        return JSONResponse(await hub.save_config(user, key, body))

    @api.post("/api/systems/created/{key}/status")
    async def set_status(key: str, body: StatusRequest, user: User = Depends(admin)) -> JSONResponse:
        return JSONResponse(await hub.set_status(user, key, body))

    @api.delete("/api/systems/created/{key}")
    async def delete(key: str, user: User = Depends(admin)) -> JSONResponse:
        await hub.delete(user, key)
        return JSONResponse({"ok": True, "key": key})

    @api.post("/api/systems/mine")
    async def create_mine(
        spec: UserSystemSpec, user: User = Depends(current_user), space: UserSpace = Depends(current_space)
    ) -> JSONResponse:
        return JSONResponse(hub.create_mine(user, space, spec), status_code=status.HTTP_201_CREATED)

    @api.put("/api/systems/mine/{key}")
    async def update_mine(
        key: str, spec: UserSystemSpec, user: User = Depends(current_user), space: UserSpace = Depends(current_space)
    ) -> JSONResponse:
        return JSONResponse(hub.update_mine(user, space, key, spec))

    @api.delete("/api/systems/mine/{key}")
    async def delete_mine(
        key: str, user: User = Depends(current_user), space: UserSpace = Depends(current_space)
    ) -> JSONResponse:
        hub.delete_mine(user, space, key)
        return JSONResponse({"ok": True, "key": key})

    @api.post("/api/systems/mine/{key}/active")
    async def activate_mine(
        key: str, body: ActiveRequest, user: User = Depends(current_user), space: UserSpace = Depends(current_space)
    ) -> JSONResponse:
        return JSONResponse(hub.activate_mine(user, space, key, body.active))

    @api.post("/api/systems/mine/{key}/submit")
    async def submit_mine(
        key: str, body: NoteRequest, user: User = Depends(current_user), space: UserSpace = Depends(current_space)
    ) -> JSONResponse:
        return JSONResponse(hub.submit_mine(user, space, key, body.note))

    @api.delete("/api/systems/mine/{key}/submit")
    async def withdraw_submission(
        key: str, user: User = Depends(current_user), space: UserSpace = Depends(current_space)
    ) -> JSONResponse:
        return JSONResponse(hub.withdraw_submission(user, space, key))

    @api.post("/api/systems/submissions/{submission_id}/import")
    async def import_submission(submission_id: str, body: ImportRequest, user: User = Depends(admin)) -> JSONResponse:
        return JSONResponse(await hub.import_submission(user, submission_id, body), status_code=status.HTTP_201_CREATED)

    @api.post("/api/systems/submissions/{submission_id}/decline")
    async def decline_submission(submission_id: str, body: NoteRequest, user: User = Depends(admin)) -> JSONResponse:
        return JSONResponse(hub.decline_submission(user, submission_id, body.note))

    # Last: "/api/systems/{kind}/{key}" would otherwise catch the routes above.
    @api.get("/api/systems/{kind}/{key}")
    async def detail(
        kind: str, key: str, user: User = Depends(current_user), space: UserSpace = Depends(current_space)
    ) -> JSONResponse:
        return JSONResponse(hub.detail(user, space, kind, key))

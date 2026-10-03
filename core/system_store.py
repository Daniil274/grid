"""Systems created from the web chat: one directory each, beside the hand-written ones.

The catalogs (``routing.yaml``, ``deploy/routing.yaml``) are written by the
operator and list the systems they curate. A system created from the web chat
lives instead in the catalog's ``routing.systems_dir``::

    systems/<key>/
      config.yaml    the system's Grid config
      system.yaml    its manifest: name, router description, status, origin
      skills/        the skills its agents use

and goes through a lifecycle recorded in the manifest:

    draft      only admins see it, and only when they pick it by hand - to test it
    published  the router offers it to everyone, like a catalog system
    archived   nobody sees it; its files stay for the record

So publishing changes one file and no catalog: the router reads the published
systems of ``systems_dir`` next to the catalog's own (core.routing.AutoRouter).
The directory is in the repository, so every created system is versioned.
The same store format also holds private user systems outside the workspace,
under users/<id>/built_systems/. There, published means active for that user's
router only; the shared catalog never reads that store.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger("grid.system_store")

#: A system key: what the router, the picker and the directory are named by.
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
CONFIG_FILE = "config.yaml"
MANIFEST_FILE = "system.yaml"
SKILLS_DIR = "skills"

Status = Literal["draft", "published", "archived"]

#: What the system builder is told about the layout of a created system.
SYSTEMS_README_HINT = (
    "A created system is <store>/<key>/: config.yaml, skills/<name>.md (an agent lists them in "
    "system_skills, without .md), tools/<package>/ for tool packages (an MCP tool with tool_package: "
    "tools/<package>), README.md. Relative paths in config.yaml start from that directory."
)


class StoreError(ValueError):
    """A request the store refuses; the message is safe to show."""


class SystemOrigin(BaseModel):
    """Who made a system and from what."""

    model_config = ConfigDict(extra="forbid")

    #: "admin": written by an admin; "user": imported from a user's own system.
    kind: Literal["admin", "user"] = "admin"
    user_id: str = ""
    username: str = ""
    #: The system it was copied from, or the user system it was imported from.
    source: str = ""


class SystemManifest(BaseModel):
    """What the router and the systems page know about a created system."""

    model_config = ConfigDict(extra="forbid")

    key: str
    name: str = Field(min_length=1, max_length=80)
    #: What the router picks the system by.
    description: str = Field(default="", max_length=2000)
    requires: List[str] = Field(default_factory=list)
    status: Status = "draft"
    origin: SystemOrigin = Field(default_factory=SystemOrigin)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    published_at: Optional[float] = None


def store_for_catalog(catalog: Any) -> Optional["SystemStore"]:
    """The store a catalog config names in ``routing.systems_dir``, else None.

    The directory is relative to the catalog file.
    """
    directory = catalog.config.routing.systems_dir
    if not directory:
        return None
    path = Path(directory)
    if not path.is_absolute():
        path = Path(catalog.config_path).resolve().parent / path
    return SystemStore(path.resolve())


def check_key(key: str) -> str:
    if not KEY_PATTERN.fullmatch(key or ""):
        raise StoreError(
            "A system key is 2-40 characters: lowercase letters, digits, '-' and '_', starting with a letter."
        )
    return key


class SystemStore:
    """The created systems under one directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- reading -----------------------------------------------------------------------
    def directory(self, key: str) -> Path:
        return self.root / check_key(key)

    def config_path(self, key: str) -> Path:
        return self.directory(key) / CONFIG_FILE

    def list(self) -> List[SystemManifest]:
        """Every readable system, oldest first; an unreadable one is logged and skipped."""
        if not self.root.is_dir():
            return []
        manifests = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or not KEY_PATTERN.fullmatch(entry.name):
                continue
            try:
                manifests.append(self._read(entry.name))
            except (OSError, ValueError, ValidationError) as exc:
                logger.error("Created system %s is unreadable: %s", entry.name, exc)
        return sorted(manifests, key=lambda manifest: manifest.created_at)

    def published(self) -> List[SystemManifest]:
        return [manifest for manifest in self.list() if manifest.status == "published"]

    def get(self, key: str) -> SystemManifest:
        if not (self.directory(key) / MANIFEST_FILE).is_file():
            raise StoreError(f"No created system '{key}'.")
        return self._read(key)

    def exists(self, key: str) -> bool:
        return self.directory(key).exists()

    def config_text(self, key: str) -> str:
        return self.config_path(key).read_text(encoding="utf-8")

    def _read(self, key: str) -> SystemManifest:
        document = yaml.safe_load((self.root / key / MANIFEST_FILE).read_text(encoding="utf-8")) or {}
        manifest = SystemManifest(**document)
        if manifest.key != key:
            raise ValueError(f"manifest names '{manifest.key}', directory is '{key}'")
        return manifest

    # -- writing -----------------------------------------------------------------------
    def create(
        self,
        manifest: SystemManifest,
        config_text: str,
        *,
        skills_from: Optional[Path] = None,
    ) -> SystemManifest:
        """Write a new system; *skills_from* is a skills directory to copy.

        The directory is made exclusively, so two requests for one key cannot
        both win.
        """
        directory = self.directory(manifest.key)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir()
        except FileExistsError:
            raise StoreError(f"A created system '{manifest.key}' already exists.") from None
        try:
            if skills_from is not None and skills_from.is_dir():
                shutil.copytree(skills_from, directory / SKILLS_DIR)
            _write(directory / CONFIG_FILE, config_text)
            now = time.time()
            manifest = manifest.model_copy(update={"created_at": now, "updated_at": now})
            self._write_manifest(manifest)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return manifest

    def write_config(self, key: str, config_text: str) -> SystemManifest:
        self.get(key)
        _write(self.config_path(key), config_text)
        return self.touch(key)

    def update(self, key: str, **changes: Any) -> SystemManifest:
        """Change manifest fields other than the key, status and dates."""
        allowed = {"name", "description", "requires"}
        if not set(changes) <= allowed:
            raise StoreError(f"Only {', '.join(sorted(allowed))} can be changed here.")
        manifest = self.get(key).model_copy(update={**changes, "updated_at": time.time()})
        self._write_manifest(SystemManifest(**manifest.model_dump()))
        return manifest

    def set_status(self, key: str, status: Status) -> SystemManifest:
        manifest = self.get(key)
        now = time.time()
        changes: Dict[str, Any] = {"status": status, "updated_at": now}
        if status == "published":
            changes["published_at"] = now
        manifest = manifest.model_copy(update=changes)
        self._write_manifest(manifest)
        return manifest

    def touch(self, key: str) -> SystemManifest:
        manifest = self.get(key).model_copy(update={"updated_at": time.time()})
        self._write_manifest(manifest)
        return manifest

    def delete(self, key: str) -> None:
        """Remove a system for good; a published one must be withdrawn first."""
        if self.get(key).status == "published":
            raise StoreError("Withdraw the system before deleting it.")
        shutil.rmtree(self.directory(key))

    def _write_manifest(self, manifest: SystemManifest) -> None:
        text = yaml.safe_dump(manifest.model_dump(), allow_unicode=True, sort_keys=False)
        _write(self.directory(manifest.key) / MANIFEST_FILE, text)


def _write(path: Path, text: str) -> None:
    """Replace *path* atomically: a reader sees the old file or the new one."""
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, path)


# -- configs moved to another directory -------------------------------------------------
def relocate_config(document: Dict[str, Any], source_dir: Path, target_dir: Path) -> Dict[str, Any]:
    """A config *document* read from *source_dir*, rewritten to work from *target_dir*.

    Relative paths in a config resolve against its own directory. A copy keeps
    reaching what the original reached: the project tools, the config directory,
    the logs, the action policy files and the files an MCP command names. The
    skills are copied beside the new config instead (:meth:`SystemStore.create`),
    since a system reads them from there and nowhere else. The working
    directory is left as it is: a system works in its own directory or, in the
    web chat, in the user's workspace.
    """
    source_dir, target_dir = source_dir.resolve(), target_dir.resolve()

    def moved(value: Any) -> Any:
        if not isinstance(value, str) or not value or os.path.isabs(value):
            return value
        return Path(os.path.relpath(source_dir / value, target_dir)).as_posix()

    settings = document.get("settings")
    if isinstance(settings, dict):
        for field in ("config_directory", "logs_directory"):
            if settings.get(field) not in (None, "."):
                settings[field] = moved(settings[field])
        tools = settings.get("project_tools")
        if isinstance(tools, dict) and tools.get("enabled", True):
            tools["tools_directory"] = moved(tools.get("tools_directory") or "./tools")
        policy = settings.get("action_policy")
        if isinstance(policy, str):
            settings["action_policy"] = moved(policy)
        elif isinstance(policy, dict):
            for field in ("policy_file", "system"):
                if isinstance(policy.get(field), str):
                    policy[field] = moved(policy[field])
    for tool in (document.get("tools") or {}).values():
        command = tool.get("server_command") if isinstance(tool, dict) else None
        if isinstance(command, list):
            tool["server_command"] = [
                moved(part) if isinstance(part, str) and (source_dir / part).exists() else part
                for part in command
            ]
    return document


# -- a system's health, for the systems page and the builder -----------------------------
def inspect_system(load: Callable[[], Any], *, requires=(), confined: bool = False) -> Dict[str, Any]:
    """Agents and health of a system; a config that does not load is reported, never raised.

    Loading a config replaces the process-wide project tools loader: it is
    put back afterwards.
    """
    from core.managers.project_tools_loader import get_project_loader, set_project_loader
    from core.tool_check import CONFIG, diagnose, summarize

    previous = get_project_loader()
    try:
        config = load()
        issues = diagnose(config, requires=requires, confined=confined)
    except Exception as exc:
        return {"loaded": False, "error": str(exc), "agents": [], "config": [], "environment": [], "packages": []}
    finally:
        set_project_loader(previous)
    agents = [
        {
            "key": key,
            "name": agent.name or key,
            "description": agent.description or "",
            "model": agent.primary_model,
            "tools": list(agent.tools),
            "routable": bool(agent.routable),
            "default": key == config.get_default_agent(),
        }
        for key, agent in config.config.agents.items()
    ]
    return {
        "loaded": True,
        "error": "",
        "agents": agents,
        "config": [issue.text() for issue in issues if issue.kind == CONFIG],
        "environment": summarize([issue for issue in issues if issue.kind != CONFIG]),
        "packages": packages_of(config),
    }


def packages_of(config: Any) -> List[Dict[str, Any]]:
    """The tool packages a config declares, each read without running it (core.tool_packages)."""
    from core.tool_packages import PackageError, analyze, package_dir

    found = []
    for name, tool in (config.config.tools or {}).items():
        relative = getattr(tool, "tool_package", None)
        if not relative:
            continue
        try:
            info = analyze(package_dir(Path(config.config_path).parent, relative)).to_dict()
        except PackageError as exc:
            info = {"path": relative, "files": [], "tools": [], "requirements": [], "tests": 0, "issues": [str(exc)]}
        found.append({"tool": name, "package": relative, **info})
    return found


# -- the system builder's access ----------------------------------------------------------
class BuilderAccess:
    """What the system builder's tools (tools/system_builder_tools.py) may act on.

    Admins act on the shared store; other users act on their own store outside
    the workspace. It also validates private configs before any host loading.
    """

    def __init__(
        self,
        *,
        store: SystemStore,
        catalog: Any,
        base_config: Any,
        user_id: str,
        image: str,
        record: Callable[[str, str, str], None],
        changed: Callable[[], None],
        shared: bool = True,
        max_systems: Optional[int] = None,
        count_other: Optional[Callable[[], int]] = None,
        model_access: Optional[Any] = None,
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.base_config = base_config
        self.user_id = user_id
        self.image = image
        self._record = record
        self._changed = changed
        self.shared = shared
        self.max_systems = max_systems
        self.count_other = count_other or (lambda: 0)
        #: The owner's credentials, model filter and spend meter (core.model_access):
        #: what evaluations run on, so they spend the owner's plan and no one else's.
        self.model_access = model_access

    def system_key(self, key: str) -> str:
        if self.shared:
            return key
        import hashlib

        owner = hashlib.sha256(self.user_id.encode()).hexdigest()[:12]
        return f"ub_{owner}_{key}"

    def examples(self) -> Dict[str, Path]:
        found = {}
        for key, path in self.catalog_systems().items():
            if self.catalog.config.routing.systems[key].admins_only:
                continue
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if "builder_create" not in (document.get("tools") or {}):
                found[key] = path
        return found

    def providers(self) -> Dict[str, Any]:
        document = yaml.safe_load(Path(self.base_config.config_path).read_text(encoding="utf-8")) or {}
        return self.redact_providers(document.get("providers") or {})

    @staticmethod
    def redact_providers(providers: Dict[str, Any]) -> Dict[str, Any]:
        import copy

        providers = copy.deepcopy(providers)
        for provider in providers.values():
            if provider.get("api_key"):
                provider["api_key"] = "<hidden: use api_key_env>"
            for name in provider.get("default_headers", {}):
                if name.lower() in {"authorization", "x-api-key", "api-key", "cookie"}:
                    provider["default_headers"][name] = "<hidden>"
        return providers

    def validate(self, text: str, key: str) -> None:
        """Private systems cannot import uploaded Python into the server or redirect its credentials."""
        from schemas import GridConfig

        document = yaml.safe_load(text) or {}
        if not isinstance(document, dict):
            raise StoreError("A config is a YAML mapping.")
        config = GridConfig(**document)
        if self.shared:
            return
        settings = config.settings
        if settings.project_tools is not None and settings.project_tools.enabled:
            raise StoreError("Private systems use shared tools or tool packages; project_tools imports are not allowed.")
        if not settings.allow_path_override or settings.config_directory != "." or settings.logs_directory is not None:
            raise StoreError("Private systems keep allow_path_override: true, config_directory: . and the server's logs.")
        if settings.action_policy.policy_file or settings.action_policy.system or document.get("routing"):
            raise StoreError("Private systems inherit the server's policy and routing; do not reference other config files.")
        allowed_providers = self.providers()
        for name, provider in (document.get("providers") or {}).items():
            if name not in allowed_providers or provider != allowed_providers[name]:
                raise StoreError("Copy providers unchanged from builder_catalog; private systems use the server's providers.")
        for name, model in config.models.items():
            trusted = self.base_config.config.models.get(name)
            if trusted is None or (model.name, model.provider) != (trusted.name, trusted.provider):
                raise StoreError("Private systems use model keys and model names from builder_catalog.")
            if model.price != trusted.price:
                # A price here would decide what the owner's calls cost the operator.
                raise StoreError("Private systems use the model prices of the server; do not set price.")
        for agent in config.agents.values():
            for skill in agent.system_skills or []:
                if "/" in skill or "\\" in skill or skill in {".", ".."}:
                    raise StoreError("A system skill is a name inside the system's skills/ directory.")
        root = self.store.directory(key).resolve()
        for tool in config.tools.values():
            if tool.type != "mcp":
                continue
            if not tool.tool_package or tool.server_command:
                raise StoreError("Private MCP tools use tool_package, with no server_command.")
            package = (root / tool.tool_package).resolve()
            if root not in package.parents:
                raise StoreError("A tool package stays inside the private system's directory.")

    def load(self, key: str, working_directory: Optional[str] = None) -> Any:
        from core.config import Config

        self.validate(self.store.config_text(key), key)
        config = Config(str(self.store.config_path(key)), working_directory)
        if not self.shared:
            # Secrets stay in the trusted config, never in the user's files.
            config.config.providers = {name: self.base_config.config.providers[name].model_copy(deep=True)
                                       for name in config.config.providers}
        return config

    def catalog_systems(self) -> Dict[str, Path]:
        """The catalog's systems by key, with their config files."""
        if self.catalog is None:
            return {}
        base = Path(self.catalog.config_path).resolve().parent
        found = {}
        for key, system in self.catalog.config.routing.systems.items():
            path = Path(system.config)
            found[key] = (path if path.is_absolute() else base / path).resolve()
        return found

    def done(self, key: str, event: str, note: str = "") -> None:
        """Record what the builder did to *key* and let every space pick it up."""
        try:
            self._record(key, event, note)
        finally:
            self._changed()

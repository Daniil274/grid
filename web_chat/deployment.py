"""What every user of one web chat server shares: the systems and their configs.

A server runs one system (``--config``) or routes across the catalog of systems
(``routing.yaml``). Which config files that means, the catalog itself, the
voice settings and the operator's edits to those files belong to the server
and are the same for every user. What each user owns - conversations, agent
sessions, a workspace - is a space (web_chat.space).

Started without an explicit config, the server loads the catalog and every
message is routed across the systems in it, just like a CLI session started
without ``--agent``/``--config``. Given a config, it runs that one system, which
can still route between its own agents.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import yaml

from core.config import Config
from core.system_store import SystemStore
from schemas.schemas import PersonalAgentsPolicy, ReviewPolicy, UploadsPolicy, UserLimitsPolicy

logger = logging.getLogger("grid.web_chat.deployment")
PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CONFIG = "config.yaml"
DEFAULT_ROUTING = "routing.yaml"
#: The target that names the routing catalog among the editable config files.
CATALOG_TARGET = "__catalog__"


@dataclass(frozen=True)
class ConfigFile:
    """One file the configuration page edits: the catalog or a system's config."""

    key: str
    name: str
    kind: str  # "catalog" or "system"
    path: Path


def resolve_path(path_value: str) -> Path:
    """Resolve against the project root first, then the working directory."""
    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path.resolve(strict=False)

    project_path = PROJECT_ROOT / path
    if project_path.exists():
        return project_path.resolve(strict=False)

    cwd_path = path.resolve(strict=False)
    return cwd_path if cwd_path.exists() else project_path.resolve(strict=False)


def isolation_enabled(config: Config) -> bool:
    """Whether *config* asks for its agents' tools to run in a container."""
    isolation_cfg = getattr(config.config, "isolation", None)
    if not isolation_cfg:
        return False
    if isinstance(isolation_cfg, dict):
        return bool(isolation_cfg.get("enabled", False))
    return bool(getattr(isolation_cfg, "enabled", False))


class Deployment:
    """The system configs a server runs, read once and re-read after an edit."""

    def __init__(
        self,
        *,
        config_path: Optional[str] = None,
        routing_path: Optional[str] = DEFAULT_ROUTING,
        working_directory: Optional[str] = None,
    ) -> None:
        """``working_directory`` is the ``--path`` override of a single-user
        server; spaces with a workspace of their own ignore it."""
        self.requested_config_path = config_path
        self.routing_path = resolve_path(routing_path) if routing_path else None
        self.working_directory = working_directory

        #: The catalog of systems; None when one system was requested explicitly.
        self.catalog: Optional[Config]
        #: The system whose settings (workspace, logs, isolation) spaces start from.
        self.config_path: Path
        #: That system's config, for model keys and settings.
        self.config: Config
        self.load()

    def load(self) -> None:
        """(Re)read the catalog and the base system config from disk."""
        self.catalog = self._catalog_config()
        self.config_path = self._base_config_path(self.catalog)
        self.config = Config(str(self.config_path), self.working_directory)

    def _catalog_config(self) -> Optional[Config]:
        if self.requested_config_path:
            return None
        if self.routing_path is None:
            raise ValueError("A routing catalog is required when --config is not specified")
        if not self.routing_path.is_file():
            raise FileNotFoundError(
                f"Routing catalog not found: {self.routing_path}. "
                "Pass --routing or use --config for one system."
            )
        return Config(str(self.routing_path))

    def _base_config_path(self, catalog: Optional[Config]) -> Path:
        if self.requested_config_path:
            return resolve_path(self.requested_config_path)
        if catalog is not None:
            from core.routing import AutoRouter

            router = AutoRouter.from_config(catalog)
            if router is not None:
                return router.system_config_path(router.default_system())
        return resolve_path(DEFAULT_CONFIG)

    @property
    def personal_agents_policy(self) -> PersonalAgentsPolicy:
        """What users may build their own agents from: the catalog's
        ``personal_agents``, or the single system's."""
        return (self.catalog or self.config).config.personal_agents

    @property
    def user_limits(self) -> UserLimitsPolicy:
        """How much one user may run: the catalog's ``user_limits``, or the single system's."""
        return (self.catalog or self.config).config.user_limits

    @property
    def uploads_policy(self) -> UploadsPolicy:
        """What users may upload: the catalog's ``uploads``, or the single system's."""
        return (self.catalog or self.config).config.uploads

    @property
    def review_policy(self) -> ReviewPolicy:
        """Who may file and open reviews: the catalog's ``review``, or the single system's."""
        return (self.catalog or self.config).config.review

    @property
    def policy_config(self) -> Optional[Config]:
        """The operator-owned action policy for every routed system.

        The catalog wins, matching the CLI runtime; a system's own policy is the
        fallback when no catalog policy is enabled.
        """
        return self.catalog

    # -- voice ---------------------------------------------------------------
    def voice_source(self) -> Tuple[Path, Config]:
        """The file that holds the voice settings, and its Config for model keys.

        Routing across systems, voice belongs to the whole chat rather than to
        the default system, so its settings - ``voice`` and the Decisions model
        that controls the conversation - come from the routing catalog. A single
        system (``--config``) keeps them in its own config.
        """
        if self.catalog is not None and self.routing_path is not None:
            return self.routing_path, self.catalog
        return self.config_path, self.config

    def voice_config_dict(self) -> Dict[str, Any]:
        """The whole file of :meth:`voice_source`, read fresh."""
        path, _ = self.voice_source()
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    def voice_enabled(self) -> bool:
        """Whether the chat offers voice at all: ``voice.enabled``, off by default.
        Off, the voice routes refuse (403) and the page shows no voice controls."""
        voice = self.voice_config_dict().get("voice") or {}
        return isinstance(voice, dict) and voice.get("enabled", False) is True

    # -- the config files an admin edits ----------------------------------------
    def config_files(self) -> List[ConfigFile]:
        """Every file the configuration page edits: the catalog, then each system.

        A system whose path the catalog names twice is listed once, under its
        first key.
        """
        files: List[ConfigFile] = []
        if self.catalog is not None and self.routing_path is not None:
            files.append(ConfigFile(CATALOG_TARGET, "Routing", "catalog", self.routing_path))
        seen: set[Path] = set()
        for key, path in self._system_paths().items():
            if path not in seen:
                seen.add(path)
                files.append(ConfigFile(key, key.replace("_", " ").replace("-", " ").title(), "system", path))
        return files

    def _system_paths(self) -> Dict[str, Path]:
        """The config file of each system, by key; one system without a catalog."""
        systems = self.catalog.config.routing.systems if self.catalog is not None else {}
        if not systems:
            return {self.config_path.parent.name or "system": self.config_path}
        base = self.catalog.config_path.resolve().parent
        paths: Dict[str, Path] = {}
        for key, system in systems.items():
            path = Path(system.config)
            paths[key] = (path if path.is_absolute() else base / path).resolve()
        return paths

    # -- systems created from the web chat (core.system_store) --------------------
    @property
    def created_store(self) -> Optional[SystemStore]:
        """The catalog's ``routing.systems_dir``, or None when it names none."""
        from core.system_store import store_for_catalog

        return store_for_catalog(self.catalog) if self.catalog is not None else None

    def system_config_path(self, key: str) -> Path:
        """The config file of a catalog system or of a created one."""
        paths = self._system_paths()
        if key in paths:
            return paths[key]
        store = self.created_store
        if store is not None and store.exists(key):
            return store.config_path(key).resolve()
        raise KeyError(f"No system '{key}'")

    def config_file(self, target: Optional[str] = None) -> ConfigFile:
        """The file *target* names; None is the default system's, the base config."""
        files = self.config_files()
        if target is None:
            base = self.config_path.resolve()
            return next((file for file in files if file.path.resolve() == base), files[0])
        for file in files:
            if file.key == target:
                return file
        raise KeyError(f"No config file '{target}'")

    def config_dict(self, target: Optional[str] = None) -> Dict[str, Any]:
        return yaml.safe_load(self.config_file(target).path.read_text(encoding="utf-8")) or {}

    def config_yaml(self, target: Optional[str] = None) -> str:
        return self.config_file(target).path.read_text(encoding="utf-8")

    def save_structured_config(self, payload: Dict[str, Any], target: Optional[str] = None) -> None:
        """Validate and write a config; the caller rebuilds the spaces.

        The edit is merged into the file as it stands, so its comments, key
        order and the layout of what did not change survive.
        """
        from schemas import GridConfig

        GridConfig(**payload)
        path = self.config_file(target).path
        path.write_text(merge_yaml(path.read_text(encoding="utf-8"), payload), encoding="utf-8")
        self.load()

    def save_yaml_config(self, yaml_content: str, target: Optional[str] = None) -> None:
        """Validate and write a config as given; the caller rebuilds the spaces."""
        from schemas import GridConfig

        GridConfig(**(yaml.safe_load(yaml_content) or {}))
        self.config_file(target).path.write_text(yaml_content, encoding="utf-8")
        self.load()


def _round_trip() -> Any:
    from ruamel.yaml import YAML

    loader = YAML()
    loader.preserve_quotes = True
    loader.width = 4096
    loader.indent(mapping=2, sequence=4, offset=2)
    return loader


def _merge(node: Any, value: Any) -> Any:
    """*value* written over *node*, keeping *node*'s objects where nothing changed.

    Kept objects carry their comments and scalar styles (a folded description
    stays folded); only what the edit changed is new.
    """
    if isinstance(node, dict) and isinstance(value, dict):
        for key in [key for key in node if key not in value]:
            del node[key]
        for key, item in value.items():
            node[key] = _merge(node[key], item) if key in node else item
        return node
    if isinstance(node, list) and isinstance(value, list) and len(node) == len(value):
        for index, item in enumerate(value):
            node[index] = _merge(node[index], item)
        return node
    # True == 1 in Python, but not in the file.
    if node == value and isinstance(node, bool) == isinstance(value, bool):
        return node
    return value


def merge_yaml(text: str, payload: Dict[str, Any]) -> str:
    """The YAML *text* changed to hold *payload*, its comments kept."""
    loader = _round_trip()
    document = loader.load(text)
    if not isinstance(document, dict):
        document = payload
    else:
        _merge(document, payload)
    buffer = io.StringIO()
    loader.dump(document, buffer)
    return buffer.getvalue()

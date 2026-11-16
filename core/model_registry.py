"""Shared provider and model registries for Grid configuration files.

A deployment has one set of providers (where requests go, which environment
variable holds the key, and how usage is reported) and one catalog of models
(which model id is sent, which provider serves it, its window, modalities and
price). Systems used to copy both blocks into every ``config.yaml`` and slowly
drifted apart.

Here a config may name ``providers_file`` and ``models_file`` (or use the
repository's ``catalog/providers.yaml`` and ``catalog/models.yaml``) and select
or refine entries under ``providers:`` and ``models:``::

    providers_file: ../../catalog/providers.yaml
    models_file: ../../catalog/models.yaml

    providers:
      use: [kimi-code, opencode, openrouter, chatgpt]

    models:
      use:
        - kimi-k3.1-pro
        - deepseek-flash-latest
      refine:
        deepseek-flash-latest:
          temperature: 0.2

Expansion happens in :meth:`core.config.Config._load_config` before
:class:`schemas.GridConfig` validates the document, so every consumer continues
to see plain ``Dict[str, ProviderConfig]`` and ``Dict[str, ModelConfig]``.
Inline declarations keep working unchanged and win over registry entries.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import yaml

from utils.exceptions import ConfigError

PROVIDERS_ENV = "GRID_PROVIDERS_FILE"
MODELS_ENV = "GRID_MODELS_FILE"
DEFAULT_PROVIDERS_REL = Path("catalog") / "providers.yaml"
DEFAULT_MODELS_REL = Path("catalog") / "models.yaml"
RESERVED_SECTION_KEYS = frozenset({"use", "refine"})
ROUTE_LOCKED_MODEL_FIELDS = frozenset({"name", "provider", "price"})
MAX_REGISTRY_BYTES = 2 * 1024 * 1024

#: Cache of parsed registry files: resolved path -> (mtime_ns, size, mapping).
_FILE_CACHE: Dict[Path, Tuple[int, int, Dict[str, Dict[str, Any]]]] = {}


@dataclass(frozen=True)
class ModelRegistry:
    """Loaded provider and model definitions from one or more YAML files."""

    providers: Dict[str, Dict[str, Any]]
    models: Dict[str, Dict[str, Any]]
    provider_files: Tuple[Path, ...] = ()
    model_files: Tuple[Path, ...] = ()


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _as_path_list(reference: Union[str, Sequence[str], None]) -> List[str]:
    if reference is None:
        return []
    if isinstance(reference, str):
        value = reference.strip()
        if not value:
            raise ConfigError("Registry file path must not be empty")
        return [value]
    if isinstance(reference, Sequence) and not isinstance(reference, (bytes, bytearray)):
        paths: List[str] = []
        for item in reference:
            if not isinstance(item, str) or not item.strip():
                raise ConfigError("Every registry file path must be a non-empty string")
            paths.append(item.strip())
        return paths
    raise ConfigError("Registry file reference must be a path string or a list of path strings")


def _resolve_reference_path(path_str: str, base_dir: Optional[Path]) -> Path:
    raw = Path(path_str).expanduser()
    if raw.is_absolute():
        return raw.resolve(strict=False)
    anchor = (base_dir or Path.cwd()).resolve(strict=False)
    return (anchor / raw).resolve(strict=False)


def _discover_default_file(kind: str, base_dir: Optional[Path]) -> Optional[Path]:
    env_name = PROVIDERS_ENV if kind == "providers" else MODELS_ENV
    env_val = os.environ.get(env_name, "").strip()
    if env_val:
        parts = [part.strip() for part in env_val.split(os.pathsep) if part.strip()]
        if parts:
            return _resolve_reference_path(parts[0], base_dir)
    rel = DEFAULT_PROVIDERS_REL if kind == "providers" else DEFAULT_MODELS_REL
    start = (base_dir or Path.cwd()).resolve(strict=False)
    for directory in (start, *start.parents):
        candidate = directory / rel
        if candidate.is_file():
            return candidate.resolve(strict=False)
    return None


def _env_files(kind: str, base_dir: Optional[Path]) -> List[Path]:
    env_name = PROVIDERS_ENV if kind == "providers" else MODELS_ENV
    env_val = os.environ.get(env_name, "").strip()
    if not env_val:
        return []
    return [
        _resolve_reference_path(part.strip(), base_dir)
        for part in env_val.split(os.pathsep)
        if part.strip()
    ]


def _uses_directives(section: Any) -> bool:
    return isinstance(section, Mapping) and any(key in section for key in RESERVED_SECTION_KEYS)


def _select_registry_files(
    kind: str,
    explicit_ref: Any,
    section: Any,
    base_dir: Optional[Path],
) -> List[Path]:
    if explicit_ref is not None:
        return [_resolve_reference_path(item, base_dir) for item in _as_path_list(explicit_ref)]
    if not _uses_directives(section):
        return []
    from_env = _env_files(kind, base_dir)
    if from_env:
        return from_env
    discovered = _discover_default_file(kind, base_dir)
    if discovered is not None:
        return [discovered]
    raise ConfigError(
        f"'{kind}' uses 'use'/'refine', but no {kind}_file was specified and no default "
        f"catalog/{kind}.yaml could be found from {base_dir or Path.cwd()}"
    )


def _read_registry_file(path: Path, kind: str) -> Dict[str, Dict[str, Any]]:
    if not path.is_file():
        raise ConfigError(f"{kind.capitalize()} registry file {path} not found")
    stat = path.stat()
    if stat.st_size > MAX_REGISTRY_BYTES:
        raise ConfigError(f"{kind.capitalize()} registry file {path} exceeds {MAX_REGISTRY_BYTES} bytes")
    cached = _FILE_CACHE.get(path)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return copy.deepcopy(cached[2])

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {kind} registry {path}: {exc}") from exc

    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{kind.capitalize()} registry {path} must contain a YAML mapping")

    other = "models" if kind == "providers" else "providers"
    if other in loaded and kind not in loaded:
        raise ConfigError(
            f"{kind.capitalize()} registry {path} contains top-level '{other}', expected '{kind}'"
        )

    if kind in loaded and isinstance(loaded[kind], dict):
        allowed_meta = {"version", kind}
        extra_top = set(loaded.keys()) - allowed_meta
        if extra_top:
            raise ConfigError(
                f"{kind.capitalize()} registry {path} has unexpected top-level keys: "
                f"{', '.join(sorted(str(k) for k in extra_top))}"
            )
        entries_raw = loaded[kind]
    else:
        entries_raw = {k: v for k, v in loaded.items() if k != "version"}

    entries: Dict[str, Dict[str, Any]] = {}
    for raw_key, spec in entries_raw.items():
        key = str(raw_key)
        if key in RESERVED_SECTION_KEYS:
            raise ConfigError(
                f"{kind.capitalize()} registry {path} cannot use reserved key '{key}'"
            )
        if not isinstance(spec, dict):
            raise ConfigError(
                f"{kind.capitalize()} '{key}' in {path} must be a YAML mapping"
            )
        normalized = copy.deepcopy(spec)
        if kind == "providers":
            normalized.setdefault("name", key)
        entries[key] = normalized

    _FILE_CACHE[path] = (stat.st_mtime_ns, stat.st_size, copy.deepcopy(entries))
    return entries


def load_registry_entries(paths: Iterable[Path], kind: str) -> Dict[str, Dict[str, Any]]:
    """Load and layer registry entries from *paths* (later files override earlier ones)."""
    merged: Dict[str, Dict[str, Any]] = {}
    for path in paths:
        current = _read_registry_file(path, kind)
        for key, spec in current.items():
            if key in merged:
                merged[key] = _deep_merge(merged[key], spec)
            else:
                merged[key] = spec
            if kind == "providers":
                merged[key].setdefault("name", key)
    return merged


def default_registry(start_dir: Optional[Union[str, Path]] = None) -> ModelRegistry:
    """Load the default provider and model registries visible from *start_dir*, if any."""
    anchor = Path(start_dir).resolve(strict=False) if start_dir is not None else Path.cwd()
    if anchor.is_file():
        anchor = anchor.parent
    provider_files = _env_files("providers", anchor)
    if not provider_files:
        found = _discover_default_file("providers", anchor)
        provider_files = [found] if found is not None else []
    model_files = _env_files("models", anchor)
    if not model_files:
        found = _discover_default_file("models", anchor)
        model_files = [found] if found is not None else []
    return ModelRegistry(
        providers=load_registry_entries(provider_files, "providers") if provider_files else {},
        models=load_registry_entries(model_files, "models") if model_files else {},
        provider_files=tuple(provider_files),
        model_files=tuple(model_files),
    )


def _parse_use_list(
    raw_use: Any,
    available: Mapping[str, Dict[str, Any]],
    kind: str,
) -> List[str]:
    if raw_use in ("*", "all"):
        return list(available.keys())
    if not isinstance(raw_use, list) or not all(isinstance(item, str) and item.strip() for item in raw_use):
        raise ConfigError(f"'{kind}.use' must be a list of {kind[:-1]} keys or '*'")
    selected: List[str] = []
    unknown: List[str] = []
    for item in raw_use:
        key = item.strip()
        if key not in available:
            unknown.append(key)
        elif key not in selected:
            selected.append(key)
    if unknown:
        known_list = ", ".join(sorted(available.keys())) or "<none>"
        raise ConfigError(
            f"Unknown {kind[:-1]} key(s) in '{kind}.use': {', '.join(unknown)}. "
            f"Available in registry: {known_list}"
        )
    return selected


def _resolve_section(
    kind: str,
    raw_section: Any,
    registry_entries: Mapping[str, Dict[str, Any]],
    *,
    has_explicit_file: bool,
    allow_route_override: bool = True,
) -> Dict[str, Dict[str, Any]]:
    if raw_section is None:
        raw_section = {}
    if not isinstance(raw_section, Mapping):
        raise ConfigError(f"'{kind}' section must be a YAML mapping")

    section = dict(raw_section)
    has_use = "use" in section
    has_refine = "refine" in section
    raw_use = section.pop("use", None)
    raw_refine = section.pop("refine", None)

    resolved: Dict[str, Dict[str, Any]] = {}
    if has_use:
        for key in _parse_use_list(raw_use, registry_entries, kind):
            resolved[key] = copy.deepcopy(registry_entries[key])
    elif has_explicit_file and not section and not has_refine:
        # When a config names providers_file/models_file and leaves the section empty,
        # all entries from that file become available unless restricted by use:.
        for key, spec in registry_entries.items():
            resolved[key] = copy.deepcopy(spec)
    elif has_refine:
        # refine Keys implicitly pull from the registry when use: is omitted.
        pass

    if raw_refine is not None:
        if not isinstance(raw_refine, Mapping):
            raise ConfigError(f"'{kind}.refine' must be a YAML mapping of {kind[:-1]} keys to overrides")
        for raw_key, overrides in raw_refine.items():
            key = str(raw_key)
            if not isinstance(overrides, Mapping):
                raise ConfigError(f"'{kind}.refine.{key}' must be a YAML mapping")
            if key not in resolved:
                if key not in registry_entries:
                    known_list = ", ".join(sorted(registry_entries.keys())) or "<none>"
                    raise ConfigError(
                        f"Cannot refine unknown {kind[:-1]} '{key}'. "
                        f"Available in registry: {known_list}"
                    )
                resolved[key] = copy.deepcopy(registry_entries[key])
            if kind == "models" and not allow_route_override:
                forbidden = set(overrides.keys()) & ROUTE_LOCKED_MODEL_FIELDS
                if forbidden:
                    raise ConfigError(
                        f"Cannot override {', '.join(sorted(forbidden))} in 'models.refine.{key}'"
                    )
            resolved[key] = _deep_merge(resolved[key], overrides)

    for raw_key, inline_spec in section.items():
        key = str(raw_key)
        if not isinstance(inline_spec, Mapping):
            raise ConfigError(f"{kind[:-1].capitalize()} '{key}' must be a YAML mapping")
        base_spec = resolved.get(key) or registry_entries.get(key)
        if base_spec is not None and ("base_url" not in inline_spec if kind == "providers" else ("name" not in inline_spec or "provider" not in inline_spec)):
            merged_spec = _deep_merge(base_spec, inline_spec)
        else:
            merged_spec = copy.deepcopy(dict(inline_spec))
        if kind == "providers":
            merged_spec.setdefault("name", key)
        resolved[key] = merged_spec

    for key, spec in resolved.items():
        if kind == "providers":
            spec.setdefault("name", key)
    return resolved


def resolve_document(
    document: Mapping[str, Any],
    config_path: Optional[Union[str, Path]] = None,
    *,
    allow_route_override: bool = True,
    fallback_registry: Optional[ModelRegistry] = None,
    registry_overrides: Optional[Mapping[Path, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return a copy of *document* with ``providers`` and ``models`` expanded."""
    if not isinstance(document, Mapping):
        raise ConfigError("Configuration root must be a YAML mapping")

    resolved = copy.deepcopy(dict(document))

    def entries_for(paths: Iterable[Path], kind: str) -> Dict[str, Any]:
        entries: Dict[str, Any] = {}
        for path in paths:
            override = (registry_overrides or {}).get(path.resolve())
            layer = copy.deepcopy(dict(override)) if override is not None else load_registry_entries([path], kind)
            for key, spec in layer.items():
                entries[key] = _deep_merge(entries.get(key, {}), spec)
        return entries

    base_dir: Optional[Path] = None
    if config_path is not None:
        cp = Path(config_path)
        base_dir = (cp if cp.is_dir() else cp.parent).resolve(strict=False)

    providers_ref = resolved.get("providers_file")
    models_ref = resolved.get("models_file")
    providers_section = resolved.get("providers")
    models_section = resolved.get("models")

    need_providers = providers_ref is not None or _uses_directives(providers_section)
    need_models = models_ref is not None or _uses_directives(models_section)

    provider_entries: Dict[str, Dict[str, Any]] = {}
    if need_providers:
        try:
            p_files = _select_registry_files("providers", providers_ref, providers_section, base_dir)
            provider_entries = entries_for(p_files, "providers")
        except ConfigError:
            if fallback_registry is not None and providers_ref is None and fallback_registry.providers:
                provider_entries = copy.deepcopy(fallback_registry.providers)
            else:
                raise

    model_entries: Dict[str, Dict[str, Any]] = {}
    if need_models:
        try:
            m_files = _select_registry_files("models", models_ref, models_section, base_dir)
            model_entries = entries_for(m_files, "models")
        except ConfigError:
            if fallback_registry is not None and models_ref is None and fallback_registry.models:
                model_entries = copy.deepcopy(fallback_registry.models)
            else:
                raise

    if need_providers or isinstance(providers_section, Mapping):
        resolved["providers"] = _resolve_section(
            "providers",
            providers_section,
            provider_entries,
            has_explicit_file=providers_ref is not None,
            allow_route_override=allow_route_override,
        )

    if need_models or isinstance(models_section, Mapping):
        resolved["models"] = _resolve_section(
            "models",
            models_section,
            model_entries,
            has_explicit_file=models_ref is not None,
            allow_route_override=allow_route_override,
        )

    # If models were pulled from a registry and reference providers from the
    # provider registry that the config did not repeat explicitly, attach those
    # providers automatically when a provider registry is active.
    if (need_models or need_providers) and isinstance(resolved.get("models"), dict):
        if not provider_entries:
            if fallback_registry is not None and fallback_registry.providers:
                provider_entries = copy.deepcopy(fallback_registry.providers)
            else:
                discovered = _discover_default_file("providers", base_dir)
                if discovered is not None:
                    provider_entries = entries_for([discovered], "providers")
        providers_map = resolved.setdefault("providers", {})
        if isinstance(providers_map, dict) and provider_entries:
            for model_spec in resolved["models"].values():
                if not isinstance(model_spec, Mapping):
                    continue
                provider_key = model_spec.get("provider")
                if isinstance(provider_key, str) and provider_key and provider_key not in providers_map:
                    if provider_key in provider_entries:
                        providers_map[provider_key] = copy.deepcopy(provider_entries[provider_key])

    return resolved


def materialize_document(
    document: Mapping[str, Any],
    config_path: Optional[Union[str, Path]] = None,
    *,
    fallback_registry: Optional[ModelRegistry] = None,
) -> Dict[str, Any]:
    """Return a standalone config dict with expanded ``providers``/``models`` and no file references."""
    expanded = resolve_document(
        document,
        config_path,
        fallback_registry=fallback_registry,
    )
    expanded.pop("providers_file", None)
    expanded.pop("models_file", None)
    return expanded

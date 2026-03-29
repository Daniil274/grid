"""Atomic apply/revert helpers for config diffs."""

from __future__ import annotations

import shutil
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Dict, List

import yaml

from schemas.improvement import ImprovementConfigDiff

try:
    from ruamel.yaml import YAML
except ImportError:  # pragma: no cover - fallback only used when dependency is missing
    YAML = None


def _create_roundtrip_yaml() -> Any:
    if YAML is None:
        return None
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.width = 4096
    yaml_rt.indent(mapping=2, sequence=4, offset=2)

    def _represent_none(representer: Any, _data: Any) -> Any:
        return representer.represent_scalar("tag:yaml.org,2002:null", "null")

    yaml_rt.representer.add_representer(type(None), _represent_none)
    return yaml_rt


def _load_yaml(path: Path) -> Dict[str, Any]:
    if YAML is not None:
        yaml_rt = _create_roundtrip_yaml()
        with path.open("r", encoding="utf-8") as fh:
            return yaml_rt.load(fh) or {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _write_yaml_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = path.with_suffix(path.suffix + ".bak")
    if path.exists():
        shutil.copy2(path, backup_path)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    if YAML is not None:
        yaml_rt = _create_roundtrip_yaml()
        with temp_path.open("w", encoding="utf-8", newline="\n") as fh:
            yaml_rt.dump(payload, fh)
    else:
        with temp_path.open("w", encoding="utf-8", newline="\n") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
    temp_path.replace(path)


def _new_mapping_like(container: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    try:
        return type(container)()
    except TypeError:
        return {}


def _walk_to_parent(document: MutableMapping[str, Any], dotted_path: str) -> tuple[MutableMapping[str, Any], str]:
    parts = dotted_path.split(".")
    current: MutableMapping[str, Any] = document
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, MutableMapping):
            next_value = _new_mapping_like(current)
            current[part] = next_value
        current = next_value
    return current, parts[-1]


def read_config_value(document: Dict[str, Any], dotted_path: str) -> Any:
    current: Any = document
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _coerce_diff_entry(entry: ImprovementConfigDiff | Dict[str, Any]) -> ImprovementConfigDiff:
    if isinstance(entry, ImprovementConfigDiff):
        return entry
    return ImprovementConfigDiff(**entry)


def apply_config_diff(config_path: str, diff: List[ImprovementConfigDiff | Dict[str, Any]]) -> Dict[str, Any]:
    path = Path(config_path)
    document = _load_yaml(path)
    applied: List[str] = []
    for raw_entry in diff:
        entry = _coerce_diff_entry(raw_entry)
        parent, leaf = _walk_to_parent(document, entry.path)
        parent[leaf] = entry.new
        applied.append(entry.path)
    _write_yaml_atomic(path, document)
    return {"success": True, "applied_paths": applied, "config_path": str(path)}


def revert_config_diff(config_path: str, diff: List[ImprovementConfigDiff | Dict[str, Any]]) -> Dict[str, Any]:
    path = Path(config_path)
    document = _load_yaml(path)
    reverted: List[str] = []
    for raw_entry in diff:
        entry = _coerce_diff_entry(raw_entry)
        parent, leaf = _walk_to_parent(document, entry.path)
        parent[leaf] = entry.old
        reverted.append(entry.path)
    _write_yaml_atomic(path, document)
    return {"success": True, "reverted_paths": reverted, "config_path": str(path)}

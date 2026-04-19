"""Atomic apply/revert helpers for config diffs."""

from __future__ import annotations

import re
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


def _serialize_inline_yaml_value(value: Any) -> str:
    dumped = yaml.safe_dump(
        value,
        allow_unicode=True,
        default_flow_style=True,
        sort_keys=False,
    ).strip()
    if dumped.endswith("\n..."):
        dumped = dumped[:-4].rstrip()
    return dumped


def _replace_existing_yaml_scalar(text: str, dotted_path: str, value: Any) -> str | None:
    lines = text.splitlines()
    parts = dotted_path.split(".")
    parent_indent = -1
    search_start = 0

    def _match_key(line: str, key: str, *, exact_indent: int | None) -> re.Match[str] | None:
        pattern = r"^(?P<indent>\s*)" + re.escape(key) + r":(?P<suffix>.*)$"
        match = re.match(pattern, line)
        if not match:
            return None
        if exact_indent is not None and len(match.group("indent")) != exact_indent:
            return None
        return match

    for part in parts[:-1]:
        found = False
        for index in range(search_start, len(lines)):
            line = lines[index]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            current_indent = len(line) - len(line.lstrip(" "))
            if parent_indent >= 0 and current_indent <= parent_indent:
                return None
            match = _match_key(line, part, exact_indent=None if parent_indent < 0 else parent_indent + 2)
            if match:
                parent_indent = len(match.group("indent"))
                search_start = index + 1
                found = True
                break
        if not found:
            return None

    leaf = parts[-1]
    replacement = _serialize_inline_yaml_value(value)
    for index in range(search_start, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        current_indent = len(line) - len(line.lstrip(" "))
        if parent_indent >= 0 and current_indent <= parent_indent:
            break
        expected_indent = None if parent_indent < 0 else parent_indent + 2
        match = _match_key(line, leaf, exact_indent=expected_indent)
        if match:
            lines[index] = f"{match.group('indent')}{leaf}: {replacement}"
            trailing_newline = "\n" if text.endswith("\n") else ""
            return "\n".join(lines) + trailing_newline
    return None


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
    if YAML is None and path.exists():
        original_text = path.read_text(encoding="utf-8")
        updated_text = original_text
        applied: List[str] = []
        for raw_entry in diff:
            entry = _coerce_diff_entry(raw_entry)
            replaced = _replace_existing_yaml_scalar(updated_text, entry.path, entry.new)
            if replaced is None:
                break
            updated_text = replaced
            applied.append(entry.path)
        else:
            path.write_text(updated_text, encoding="utf-8", newline="\n")
            return {"success": True, "applied_paths": applied, "config_path": str(path)}

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
    if YAML is None and path.exists():
        original_text = path.read_text(encoding="utf-8")
        updated_text = original_text
        reverted: List[str] = []
        for raw_entry in diff:
            entry = _coerce_diff_entry(raw_entry)
            replaced = _replace_existing_yaml_scalar(updated_text, entry.path, entry.old)
            if replaced is None:
                break
            updated_text = replaced
            reverted.append(entry.path)
        else:
            path.write_text(updated_text, encoding="utf-8", newline="\n")
            return {"success": True, "reverted_paths": reverted, "config_path": str(path)}

    document = _load_yaml(path)
    reverted: List[str] = []
    for raw_entry in diff:
        entry = _coerce_diff_entry(raw_entry)
        parent, leaf = _walk_to_parent(document, entry.path)
        parent[leaf] = entry.old
        reverted.append(entry.path)
    _write_yaml_atomic(path, document)
    return {"success": True, "reverted_paths": reverted, "config_path": str(path)}

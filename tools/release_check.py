#!/usr/bin/env python3
"""Validate the machine-readable release manifest against the code.

Offline and side-effect free: reads ``releases/manifest.json``, compares it with
``core.revisions`` (the contract definitions the runtime enforces) and reports
whether a release can be described by that manifest.

    python tools/release_check.py            # human summary, exit 1 on failure
    python tools/release_check.py --json     # one JSON object
    python tools/release_check.py --manifest path/to/manifest.json

Exit code 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "grid.release.manifest"
SUPPORTED_SCHEMA_VERSIONS = (1,)
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_SECRET_KEY_RE = re.compile(
    r"(api[-_]?key|secret|token|passw(or)?d|credential|private[-_]?key)", re.IGNORECASE
)
_SECRET_VALUE_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})")


class Report:
    """The checks of one run: a JSON-friendly list with an overall verdict."""

    def __init__(self) -> None:
        self.checks: List[Dict[str, Any]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return ok

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.checks)

    def payload(self, manifest_path: Path) -> Dict[str, Any]:
        return {
            "schema": "grid.release.check",
            "schema_version": 1,
            "manifest": str(manifest_path),
            "ok": self.ok,
            "failed": [item["check"] for item in self.checks if not item["ok"]],
            "checks": self.checks,
        }


def _version_tuple(text: str) -> tuple:
    return tuple(int(part) for part in str(text).split("."))


def _valid_version(value: Any) -> bool:
    """A release version is a plain ``MAJOR.MINOR.PATCH`` string."""
    return isinstance(value, str) and bool(_VERSION_RE.match(value))


def _secret_paths(value: Any, path: str = "") -> List[str]:
    """Where a manifest holds something that looks like a credential."""
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if _SECRET_KEY_RE.search(str(key)):
                found.append(here)
            else:
                found.extend(_secret_paths(item, here))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_secret_paths(item, f"{path}[{index}]"))
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        found.append(path or "<value>")
    return found


def validate(manifest_path: Path) -> Report:
    from core import __version__ as grid_version
    from core import revisions

    report = Report()

    if not report.check("manifest.exists", manifest_path.is_file(), str(manifest_path)):
        return report
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.check("manifest.json", False, str(exc))
        return report
    if not report.check("manifest.json", isinstance(document, dict), "a JSON object"):
        # A list or scalar at the top level: nothing below can be read.
        return report

    report.check("schema", document.get("schema") == SCHEMA, f"expected {SCHEMA}")
    report.check(
        "schema_version",
        document.get("schema_version") in SUPPORTED_SCHEMA_VERSIONS,
        f"supported {list(SUPPORTED_SCHEMA_VERSIONS)}",
    )

    secrets = _secret_paths(document)
    report.check("no-secrets", not secrets, ", ".join(secrets) or "no credential-like entries")

    releases = document.get("releases")
    if not report.check("releases", isinstance(releases, list) and bool(releases), "a non-empty list"):
        return report

    entries = [entry for entry in releases if isinstance(entry, dict)]
    report.check("releases.entries", len(entries) == len(releases), "every release is an object")
    # Normalise once: a version that is not a string (a list, an object, a
    # number) becomes None so no later comparison or sort can raise.
    versions = [entry.get("version") if isinstance(entry.get("version"), str) else None for entry in entries]
    report.check(
        "releases.versions",
        bool(versions) and all(_valid_version(version) for version in versions),
        str(versions),
    )
    report.check("releases.unique", len(versions) == len({str(version) for version in versions}), str(versions))

    current = document.get("current")
    report.check("current.version", isinstance(current, str) and current in versions, f"{current!r} among {versions}")
    newest = max(versions, key=_version_tuple) if versions and all(_valid_version(v) for v in versions) else None
    report.check(
        "current.is-newest",
        newest is not None and current == newest,
        f"{current!r} is the newest release" if newest is not None else "no valid versions to compare",
    )
    report.check(
        "current.matches-code",
        current == grid_version,
        f"manifest {current!r}, core.__version__ {grid_version!r}",
    )

    for entry in releases:
        if not isinstance(entry, dict):
            continue
        version = str(entry.get("version"))
        prefix = f"release[{version}]"
        report.check(f"{prefix}.date", bool(_DATE_RE.match(str(entry.get("date", "")))), str(entry.get("date")))

        revision = entry.get("context_revision") or {}
        if not report.check(f"{prefix}.context_revision", bool(revision), "a context revision block"):
            continue
        report.check(
            f"{prefix}.manifest_version",
            revision.get("manifest_version") in revisions.SUPPORTED_MANIFEST_VERSIONS,
            f"code {list(revisions.SUPPORTED_MANIFEST_VERSIONS)}",
        )
        report.check(
            f"{prefix}.supported_manifest_versions",
            sorted(revision.get("supported_manifest_versions") or []) == sorted(revisions.SUPPORTED_MANIFEST_VERSIONS),
            f"code {list(revisions.SUPPORTED_MANIFEST_VERSIONS)}",
        )

        contracts = revision.get("contracts") or {}
        for name in revisions.CONTRACTS:
            expected = revisions.contract_section(name)
            described = contracts.get(name) or {}
            report.check(
                f"{prefix}.contract.{name}",
                described == expected,
                f"code {expected}",
            )
        report.check(
            f"{prefix}.contracts.complete",
            set(contracts) == set(revisions.CONTRACTS),
            f"code {sorted(revisions.CONTRACTS)}",
        )

        compatibility = revision.get("compatibility") or {}
        required_compatibility = {
            "grid_version": "exact",
            "manifest_version": "supported-only",
            "contract_version": "supported-only",
            "contract_definition": "exact-digest",
            "runtime_python": "exact-minor",
            "system_config": "exact-digest",
            "system_defaults": "projection-only",
        }
        report.check(
            f"{prefix}.compatibility.enforced",
            all(compatibility.get(key) == value for key, value in required_compatibility.items()),
            "declared compatibility must match the runtime gates",
        )
        report.check(
            f"{prefix}.compatibility.permissions",
            compatibility.get("permissions_authority") == "action_policy",
            "the action policy remains the authority for permissions",
        )
        report.check(
            f"{prefix}.compatibility.legacy",
            compatibility.get("legacy_conversations") == "read-only",
            "legacy conversations are read-only",
        )
        report.check(
            f"{prefix}.compatibility.repin",
            compatibility.get("repin") == "never",
            "a pinned conversation is never re-pinned",
        )
        claims = revision.get("claims") or {}
        report.check(
            f"{prefix}.claims",
            claims.get("replay") == "none" and claims.get("exactly_once") == "none",
            "no replay or exactly-once claims",
        )

        tests = (entry.get("validation") or {}).get("tests") or []
        report.check(
            f"{prefix}.validation.tests",
            bool(tests) and all((ROOT / path).exists() for path in tests),
            ", ".join(tests),
        )

    return report


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--manifest",
        default=str(ROOT / "releases" / "manifest.json"),
        help="release manifest to validate (default: releases/manifest.json)",
    )
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    parser.add_argument("--quiet", action="store_true", help="print nothing on success")
    args = parser.parse_args(argv)

    path = Path(args.manifest)
    try:
        report = validate(path)
    except Exception as exc:  # a validator bug still answers in the requested shape
        report = Report()
        report.check("validator.complete", False, f"{type(exc).__name__}: {exc}")
    if args.json:
        print(json.dumps(report.payload(path), indent=2, ensure_ascii=False))
    elif not report.ok:
        print(f"release manifest INVALID: {path}")
        for item in report.checks:
            if not item["ok"]:
                print(f"  FAIL {item['check']}: {item['detail']}")
    elif not args.quiet:
        print(f"release manifest OK: {path} ({len(report.checks)} checks)")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

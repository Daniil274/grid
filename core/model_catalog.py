"""What each model accepts as input, from a models.dev snapshot.

A model's config may say what it accepts (``input_modalities``, or ``vision``
for images alone); most do not. This catalog answers for the rest: models.dev
lists the input modalities of most providers' models - text, image, audio,
video, pdf - and a snapshot of the providers Grid uses ships with the code
(``core/model_modalities.json``), so routing by modality works without
any setup. A model the catalog does not know is unknown, not text-only
(core.fallback_model decides what that means for each modality).

``python -m core.model_catalog refresh`` rewrites the snapshot; a running
server notices the new file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from core.modalities import MEDIA, TEXT

logger = logging.getLogger("grid.model_catalog")

SNAPSHOT = Path(__file__).with_name("model_modalities.json")
SOURCE = "https://models.dev/api.json"
DEFAULT_PROVIDERS = (
    "openrouter", "opencode", "opencode-go", "openai", "anthropic", "google",
    "zai", "deepseek", "xai", "mistral", "moonshotai",
)
_KNOWN = frozenset((TEXT, *MEDIA))


@dataclass
class ModalityCatalog:
    """Input modalities by provider and model.

    ``providers`` maps a models.dev provider id to ``{model id: modalities}``;
    ``endpoints`` maps its API base URL to that id, so a Grid provider is found
    by where it points, whatever its key is called in a system's config.
    """

    providers: Dict[str, Dict[str, FrozenSet[str]]] = field(default_factory=dict)
    endpoints: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_snapshot(cls, document: Mapping[str, Any]) -> "ModalityCatalog":
        catalog = cls()
        for provider_id, provider in (document.get("providers") or {}).items():
            if not isinstance(provider, Mapping):
                continue
            models = {
                str(model_id): frozenset(m for m in modalities if m in _KNOWN) | {TEXT}
                for model_id, modalities in (provider.get("models") or {}).items()
                if isinstance(modalities, list)
            }
            catalog.providers[str(provider_id)] = models
            api = provider.get("api")
            if isinstance(api, str) and api:
                catalog.endpoints[api.rstrip("/")] = str(provider_id)
        return catalog

    @classmethod
    def load(cls, path: Path = SNAPSHOT) -> "ModalityCatalog":
        """The snapshot at *path*; an empty catalog when it is missing or unreadable."""
        if not path.is_file():
            return cls()
        try:
            return cls.from_snapshot(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            logger.exception("Could not read the model catalog %s; nothing is known from it", path)
            return cls()

    def lookup(self, base_url: str, model: str, provider_id: Optional[str] = None) -> Optional[FrozenSet[str]]:
        """What *model* at the provider serving *base_url* accepts; None when unknown.

        The provider is the one *provider_id* names (a Grid provider's
        ``price_source``), else the one at *base_url*, else the ones at its host
        (a gateway with several plans), else every provider listing the model
        id. Where several listings answer, only what all of them say is taken:
        a modality is never assumed on one listing's word. For gateways whose
        ids read ``vendor/model`` (OpenRouter) the vendor's own listing answers
        too.
        """
        base = (base_url or "").rstrip("/")
        for candidates in (
            [provider_id] if provider_id in self.providers else [],
            [self.endpoints[base]] if base in self.endpoints else [],
            [pid for api, pid in self.endpoints.items() if base and _host(api) == _host(base)],
            list(self.providers),
        ):
            found = [self.providers[pid][model] for pid in candidates if model in self.providers.get(pid, {})]
            if found:
                return frozenset.intersection(*found)
        if "/" in model:
            vendor, _, rest = model.partition("/")
            return self.providers.get(vendor, {}).get(rest)
        return None

    def models_known(self) -> int:
        return sum(len(models) for models in self.providers.values())


def snapshot_of(document: Mapping[str, Any], wanted: Iterable[str]) -> Dict[str, Any]:
    """The part of a models.dev document the catalog keeps: *wanted* providers, input modalities only."""
    keep = set(wanted)
    providers: Dict[str, Any] = {}
    for provider_id, provider in document.items():
        if provider_id not in keep or not isinstance(provider, Mapping):
            continue
        models = {}
        for model_id, model in (provider.get("models") or {}).items():
            modalities = (model.get("modalities") or {}).get("input") if isinstance(model, Mapping) else None
            if isinstance(modalities, list):
                models[model_id] = sorted(m for m in modalities if m in _KNOWN)
        providers[provider_id] = {"api": provider.get("api"), "models": models}
    return {"source": SOURCE, "providers": providers}


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


_lock = threading.Lock()
_loaded: Dict[Path, tuple] = {}


def catalog(path: Path = SNAPSHOT) -> ModalityCatalog:
    """The catalog of *path*, read again when the file changes."""
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = None
    with _lock:
        cached = _loaded.get(path)
        if cached is None or cached[0] != stamp:
            cached = _loaded[path] = (stamp, ModalityCatalog.load(path))
        return cached[1]


def fetch() -> dict:
    import httpx

    response = httpx.get(SOURCE, timeout=60, follow_redirects=True)
    response.raise_for_status()
    return response.json()


def refresh(path: Path, providers: Sequence[str], source: Callable[[], dict] = fetch) -> ModalityCatalog:
    """Write the snapshot of *providers* to *path* (atomically) and return what it holds."""
    snapshot = snapshot_of(source(), providers)
    missing = [name for name in providers if name not in snapshot["providers"]]
    if missing:
        raise ValueError(f"models.dev has no provider named: {', '.join(missing)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(snapshot, out, separators=(",", ":"), sort_keys=True)
            out.write("\n")
        os.chmod(temporary, 0o644)  # read by the server and the image build, not only its writer
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return ModalityCatalog.from_snapshot(snapshot)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.model_catalog", description="What models accept as input")
    commands = parser.add_subparsers(dest="command", required=True)
    update = commands.add_parser("refresh", help="Download the input modalities of models from models.dev")
    update.add_argument("--out", type=Path, default=SNAPSHOT, help="Where to write the snapshot")
    update.add_argument(
        "--providers", default=",".join(DEFAULT_PROVIDERS), help="models.dev provider ids to keep (comma separated)"
    )
    args = parser.parse_args(argv)
    try:
        found = refresh(args.out, [name for name in args.providers.split(",") if name])
    except Exception as exc:  # network, HTTP, a provider models.dev lacks
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {args.out}: {found.models_known()} models of {len(found.providers)} providers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

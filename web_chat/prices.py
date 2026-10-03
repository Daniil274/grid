"""Keep the model price snapshot current: ``python -m web_chat.prices refresh``.

Prices come from models.dev, which lists dollars per million tokens for the
models of most providers, the OpenCode Go plan and OpenRouter among them. The
snapshot keeps only prices, for the providers asked for, and is what
``pricing.prices_file`` names (core.pricing.PriceBook). Run it again when
providers change their prices; a running server notices the new file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional, Sequence

import httpx

from core.pricing import PriceBook, models_dev_subset

SOURCE = "https://models.dev/api.json"
DEFAULT_PROVIDERS = ("openrouter", "opencode-go", "opencode", "openai", "anthropic")


def fetch() -> dict:
    response = httpx.get(SOURCE, timeout=60, follow_redirects=True)
    response.raise_for_status()
    return response.json()


def refresh(path: Path, providers: Sequence[str], source: Callable[[], dict] = fetch) -> PriceBook:
    """Write the snapshot of *providers* to *path* (atomically) and return what it holds."""
    subset = models_dev_subset(source(), providers)
    missing = [name for name in providers if name not in subset]
    if missing:
        raise ValueError(f"models.dev has no provider named: {', '.join(missing)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(subset, out, separators=(",", ":"))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return PriceBook.from_models_dev(subset)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m web_chat.prices", description="Model prices for cost accounting")
    commands = parser.add_subparsers(dest="command", required=True)
    update = commands.add_parser("refresh", help="Download the price snapshot from models.dev")
    update.add_argument("--out", type=Path, required=True, help="The file pricing.prices_file names")
    update.add_argument(
        "--providers", default=",".join(DEFAULT_PROVIDERS), help="models.dev provider ids to keep (comma separated)"
    )
    args = parser.parse_args(argv)
    try:
        book = refresh(args.out, [name for name in args.providers.split(",") if name])
    except (httpx.HTTPError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {args.out}: {book.models_priced()} priced models of {len(book.providers)} providers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

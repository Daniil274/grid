"""Evaluate the running voice server on the generated corpus."""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import httpx


HERE = Path(__file__).resolve().parent


def normalize(text: str) -> str:
    return " ".join(re.findall(r"[а-яёa-z0-9]+", text.lower()))


def distance(reference: list[str], hypothesis: list[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for i, left in enumerate(reference, 1):
        current = [i]
        for j, right in enumerate(hypothesis, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def score(reference: str, hypothesis: str) -> tuple[int, int, int, int]:
    ref = normalize(reference)
    hyp = normalize(hypothesis)
    ref_words, hyp_words = ref.split(), hyp.split()
    return distance(ref_words, hyp_words), max(1, len(ref_words)), distance(list(ref), list(hyp)), max(1, len(ref))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--profile", action="append", help="profile(s) to include")
    parser.add_argument("--engine", action="append", help="engine(s) to include")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    manifest = [json.loads(line) for line in (HERE / "audio/manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    if args.profile:
        manifest = [row for row in manifest if row["profile"]["id"] in args.profile]
    if args.engine:
        manifest = [row for row in manifest if row["engine"] in args.engine]
    if args.limit:
        manifest = manifest[: args.limit]
    totals = defaultdict(lambda: [0, 0, 0, 0, 0])
    output = HERE / "audio" / "stt-results.jsonl"
    results = []
    with httpx.Client(timeout=args.timeout) as client:
        for index, row in enumerate(manifest, 1):
            audio = HERE / row["audio"]
            response = client.post(f"{args.url.rstrip('/')}/api/voice/transcribe",
                                   content=audio.read_bytes(), headers={"content-type": "audio/wav"})
            response.raise_for_status()
            data = response.json()
            errors = score(row["text"], data.get("text", ""))
            result = {**row, "hypothesis": data.get("text", ""), "elapsed_ms": data.get("elapsed_ms"),
                      "word_errors": errors[0], "reference_words": errors[1],
                      "char_errors": errors[2], "reference_chars": errors[3]}
            results.append(result)
            keys = ("all", f"profile:{row['profile']['id']}", f"set:{row['set']}", f"engine:{row['engine']}")
            for key in keys:
                bucket = totals[key]
                for pos, value in enumerate(errors):
                    bucket[pos] += value
                bucket[4] += 1
            print(f"[{index}/{len(manifest)}] {row['audio']}: {data.get('text', '')}")
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results), encoding="utf-8")
    print("\nmetric                  files      WER      CER")
    for key, (we, rw, ce, rc, count) in sorted(totals.items()):
        print(f"{key:23} {count:5d}  {we/rw:7.2%}  {ce/rc:7.2%}")
    print(f"\nDetailed results: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

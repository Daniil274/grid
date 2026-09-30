"""Lightweight documentation analysis tools (Python stdlib only)."""
import os
import re


def read_text(path: str) -> str:
    if not os.path.isfile(path):
        raise ValueError(f"File not found: {path}")
    try:
        return open(path, encoding="utf-8").read()
    except UnicodeDecodeError:
        raise ValueError(f"Not a UTF-8 text file: {path}")


def md_headings(markdown_text: str):
    out = []
    in_code = False
    for line in markdown_text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            title = re.sub(r"\s*#+\s*$", "", m.group(2)).strip()
            out.append({"level": len(m.group(1)), "title": title})
    return out


def slugify(title: str) -> str:
    s = title.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    return re.sub(r"\s+", "-", s)

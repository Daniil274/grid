"""Shared helpers of the doc_tools package (stdlib only)."""
import os
import re

FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.*?)[ \t]*$")
TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".rst")


def safe_path(path: str) -> str:
    """Resolve a path and make sure it stays inside the workspace (current directory)."""
    if not path or not str(path).strip():
        raise ValueError("Path is empty")
    root = os.path.realpath(os.getcwd())
    full = os.path.realpath(os.path.join(root, path))
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"Path is outside the workspace: {path}")
    return full


def read_text(path: str) -> str:
    full = safe_path(path)
    if not os.path.exists(full):
        raise FileNotFoundError(f"File not found: {path}")
    if not os.path.isfile(full):
        raise ValueError(f"Not a file: {path}")
    try:
        with open(full, encoding="utf-8") as handle:
            return handle.read()
    except UnicodeDecodeError:
        raise ValueError(f"File is not UTF-8 text: {path}")


def write_new(path: str, content: str, overwrite: bool = False) -> str:
    """Write a text file inside the workspace; refuse to replace an existing file unless overwrite."""
    full = safe_path(path)
    if os.path.exists(full) and not overwrite:
        raise FileExistsError(f"File already exists: {path} (pass overwrite=true to replace it)")
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    return path


def scan(text: str):
    """Yield (line_number, line, in_code) for each line; fence lines themselves count as code."""
    fence = None
    result = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)[0], len(match.group(1))
                result.append((number, line, True))
            else:
                result.append((number, line, False))
        else:
            result.append((number, line, True))
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= fence[1] and not match.group(2).strip():
                fence = None
    return result


def strip_inline(text: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`+([^`]*)`+", r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?<!\w)[*_]([^*_]+)[*_](?!\w)", r"\1", text)
    return text.strip()


def slugify(title: str) -> str:
    """GitHub-style anchor slug."""
    text = strip_inline(title).lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s", "-", text.strip())


def md_headings(text: str) -> list:
    """ATX headings outside code blocks: [{level, title, line}]."""
    result = []
    for number, line, in_code in scan(text):
        if in_code:
            continue
        match = HEADING_RE.match(line)
        if match:
            title = re.sub(r"[ \t]+#+$", "", match.group(2)).strip()
            result.append({"level": len(match.group(1)), "title": title, "line": number})
    return result


def unique_slugs(headings: list) -> list:
    """Slugs for headings with GitHub-style -1, -2 suffixes for duplicates."""
    seen = {}
    slugs = []
    for heading in headings:
        base = slugify(heading["title"])
        count = seen.get(base, 0)
        slugs.append(base if count == 0 else f"{base}-{count}")
        seen[base] = count + 1
    return slugs


def prose_lines(text: str) -> list:
    """Lines outside fenced code blocks, as (line_number, line)."""
    return [(n, line) for n, line, in_code in scan(text) if not in_code]


def remove_inline_code(line: str) -> str:
    return re.sub(r"`+[^`]*`+", lambda m: " " * len(m.group(0)), line)

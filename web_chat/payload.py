"""Bounded, additive tool previews. Unknown tools use the same safe fallback.

Preserve native JSON and MCP content blocks before legacy trace summarization.
Only explicit formats or verified argument/output shapes select special views.
"""
from __future__ import annotations

import json
import math
from pathlib import PurePath
from typing import Any

MAX_TEXT = 32_000
MAX_PARTS = 40
MAX_IMAGES = 512_000
_CONTENT_TYPES = {"text", "input_text", "output_text", "image", "image_url",
                  "input_image", "resource", "resource_link", "audio"}
_LANGUAGES = {".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
              ".jsx": "jsx", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
              ".sh": "bash", ".css": "css", ".html": "html", ".sql": "sql",
              ".rs": "rust", ".go": "go", ".java": "java", ".md": "markdown"}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def _reject_constant(value: str) -> None:
    raise ValueError(value)


def _parse(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_constant)


def _blocks(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(block, dict) and isinstance(block.get("type"), str)
        and block["type"] in _CONTENT_TYPES for block in value
    )


def normalize_payload(value: Any, *, role: str = "result", tool: str = "",
                      markdown: bool = False) -> dict[str, Any]:
    """Serialize a bounded preview without allowing failures to break a run.

    Null and empty values are explicit results. Oversized JSON becomes plain
    text, never an invalid JSON fragment labelled JSON. Binary previews are
    bounded separately and are never duplicated in the copy/source text.
    """
    truncated = False
    nodes = 0

    def safe(v: Any, depth: int = 0) -> Any:
        nonlocal truncated, nodes
        nodes += 1
        if depth > 20 or nodes > 4000:
            truncated = True
            return "[preview limit]"
        if hasattr(v, "model_dump"):
            v = v.model_dump(mode="json", by_alias=True)
        if v is None or isinstance(v, (str, bool, int)):
            return v
        if isinstance(v, float):
            return v if math.isfinite(v) else str(v)
        if isinstance(v, dict):
            out = {}
            for index, (key, item) in enumerate(v.items()):
                if index >= 1000 or nodes > 4000:
                    truncated = True
                    out["[preview limit]"] = "More fields omitted"
                    break
                out[str(key)] = safe(item, depth + 1)
            return out
        if isinstance(v, (list, tuple)):
            out = []
            for index, item in enumerate(v):
                if index >= 1000 or nodes > 4000:
                    truncated = True
                    out.append("[more items omitted]")
                    break
                out.append(safe(item, depth + 1))
            return out
        return str(v)

    try:
        value = safe(value)
        original = value if isinstance(value, str) else _dump(value)
        # A pruned SDK/object tree has no exact original wire size.
        original_size = None if truncated else len(original.encode("utf-8"))
        if isinstance(value, str):
            try:
                value = safe(_parse(value))
            except (ValueError, RecursionError):
                pass
    except Exception:  # noqa: BLE001 - third-party serializers must not break a run
        value = original = "[Unable to serialize tool value]"
        original_size = None
        truncated = True

    parts: list[dict[str, Any]] = []
    budget = MAX_TEXT
    image_budget = MAX_IMAGES
    multimodal = False

    def add(kind: str, media: str, data: Any, **meta: Any) -> None:
        nonlocal budget, truncated
        if len(parts) >= MAX_PARTS or budget <= 0:
            truncated = True
            return
        text = data if isinstance(data, str) else _dump(data)
        if len(text) > budget:
            truncated = True
            data = text[:budget]
            if kind == "json":
                kind, media = "text", "text/plain"
        budget -= min(len(text), budget)
        # Names and URIs are untrusted too; keep them bounded.
        parts.append({"kind": kind, "media_type": str(media)[:128], "data": data,
                      **{k: str(v)[:1000] for k, v in meta.items() if v is not None}})

    def text_part(text: Any, mime: str = "", name: str | None = None) -> None:
        if not isinstance(text, str):
            add("json", "application/json", text, name=name)
            return
        if mime in {"", "application/json"}:
            try:
                add("json", "application/json", safe(_parse(text)), name=name)
                return
            except (ValueError, RecursionError):
                pass
        if mime == "text/markdown" or (not mime and markdown):
            add("markdown", "text/markdown", text, name=name)
        elif mime in {"text/x-diff", "text/x-patch"}:
            add("diff", mime, text, name=name)
        elif text.startswith(("diff --git ", "--- ")) and "\n+++ " in text:
            add("diff", "text/x-diff", text, name=name)
        else:
            add("text", "text/plain", text, name=name)

    def content(v: Any) -> None:
        nonlocal truncated, image_budget, multimodal
        if isinstance(v, dict) and isinstance(v.get("content"), list) and (
            _blocks(v["content"]) or "structuredContent" in v or "isError" in v
            or "structured_content" in v
        ):
            structured = v.get("structuredContent", v.get("structured_content"))
            if structured is not None:
                add("json", "application/json", structured, name="Structured content")
            content(v["content"])
            extras = {k: x for k, x in v.items() if k not in
                      {"content", "structuredContent", "structured_content"}}
            if extras:
                add("json", "application/json", extras, name="Metadata")
            return
        if _blocks(v):
            multimodal = True
            for block in v:
                typ = block["type"]
                mime = str(block.get("mimeType", block.get("mime_type", "")) or "")
                if typ in {"text", "input_text", "output_text"}:
                    text_part(block.get("text", ""), mime)
                elif typ in {"image", "image_url", "input_image"}:
                    url = block.get("url", block.get("image_url", ""))
                    if isinstance(url, dict):
                        url = url.get("url", "")
                    encoded = block.get("data")
                    if isinstance(encoded, str) and mime in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                        url = f"data:{mime};base64,{encoded}"
                    if isinstance(url, str) and url and len(url) <= image_budget and len(parts) < MAX_PARTS:
                        parts.append({"kind": "image", "media_type": mime or "image/*", "data": url})
                        image_budget -= len(url)
                    else:
                        truncated = True
                        add("text", "text/plain", "Image preview unavailable", name="Image")
                elif typ in {"resource", "resource_link"}:
                    resource = block.get("resource", block)
                    if not isinstance(resource, dict):
                        add("json", "application/json", block)
                        continue
                    uri = resource.get("uri", "")
                    rmime = str(resource.get("mimeType", mime) or "application/octet-stream")
                    add("resource", rmime, "", uri=uri, name=block.get("name") or uri)
                    if "text" in resource:
                        text_part(resource["text"], rmime)
                    elif "blob" in resource:
                        add("text", "text/plain", "Binary resource (not embedded in trace)")
                else:
                    add("json", "application/json", {k: x for k, x in block.items() if k != "data"})
            return
        if isinstance(v, dict) and any(key in v for key in ("stdout", "stderr")):
            add("json", "application/json", {k: x for k, x in v.items() if k not in {"stdout", "stderr"}}, name="Execution")
            for key in ("stdout", "stderr"):
                if key in v:
                    add("code", "text/plain", v[key], name=key)
            return
        if not isinstance(v, str):
            add("json", "application/json", v)
        elif tool in {"read_file", "file_read", "read"} and v.startswith("📄 File content ") and "\n\n" in v:
            heading, text = v.split("\n\n", 1)
            path = heading.removeprefix("📄 File content ").removesuffix(":")
            language = _LANGUAGES.get(PurePath(path).suffix.lower())
            add("markdown" if language == "markdown" else "code", "text/markdown" if language == "markdown" else "text/plain", text,
                name=path, language=language)
        else:
            text_part(v)

    try:
        # Input parameters must never be mistaken for MCP result blocks.
        if role == "input" and isinstance(value, dict):
            fields = dict(value)
            extracted = []
            path = str(fields.get("filepath", fields.get("file_path", fields.get("path", ""))))
            for key in ("command", "patch_content", "old_text", "new_text", "new_source"):
                if isinstance(fields.get(key), str):
                    extracted.append((key, fields.pop(key)))
            # Only verified file-write schemas pull their payload out of Parameters.
            if tool in {"write_file", "append_file", "file_write", "file_append"} and isinstance(fields.get("content"), str):
                extracted.append(("content", fields.pop("content")))
            task = next((key for key in ("input", "task") if isinstance(fields.get(key), str)), None)
            if task and (markdown or tool in {"Agent", "WebSpider", "orchestrate"}):
                extracted.append((task, fields.pop(task)))
            add("json", "application/json", fields, name="Parameters")
            for key, text in extracted:
                language = "bash" if key == "command" else _LANGUAGES.get(PurePath(path).suffix.lower())
                if key in {"input", "task"}:
                    add("markdown", "text/markdown", text, name="Task")
                    continue
                add("diff" if key == "patch_content" else "code", "text/x-diff" if key == "patch_content" else "text/plain",
                    text, name=key, language=language)
        else:
            content(value)
    except Exception:  # noqa: BLE001 - malformed tool results still get a preview
        parts.clear()
        budget = MAX_TEXT
        truncated = True
        add("text", "text/plain", original)

    if multimodal:
        sources = []
        for part in parts:
            if part["kind"] == "image":
                sources.append("[Image]" if str(part["data"]).startswith("data:") else str(part["data"]))
            elif part["kind"] == "resource":
                sources.append(part.get("uri", ""))
            else:
                sources.append(part["data"] if isinstance(part["data"], str) else _dump(part["data"]))
        source = "\n\n".join(sources)
    else:
        source = original
    truncated = truncated or len(source) > MAX_TEXT
    return {"version": 1, "parts": parts, "raw_text": source[:MAX_TEXT],
            "truncated": truncated, "original_size": original_size,
            "is_error": isinstance(value, dict) and value.get("isError") is True}

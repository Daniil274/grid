import html
import os
import re
from html.parser import HTMLParser

from grid_tool import tool

from _common import read_text, md_headings, slugify


@tool(read_only=True)
def doc_stats(path: str) -> dict:
    """Statistics of a text document: words, sentences, lines, reading time, headings and skipped heading levels.

    Use for Markdown or plain text files to gauge size, readability and heading structure before editing.

    Args:
        path: the file, relative to the workspace
    """
    text = read_text(path)
    words = re.findall(r"\w+", text, flags=re.UNICODE)
    sentences = [s for s in re.split(r"[.!?…]+(?:\s|$)", text) if s.strip()]
    headings = md_headings(text)
    skipped = []
    prev = 0
    for h in headings:
        if prev and h["level"] > prev + 1:
            skipped.append({"after": prev, "level": h["level"], "title": h["title"]})
        prev = h["level"]
    return {
        "words": len(words),
        "sentences": len(sentences),
        "lines": text.count("\n") + 1,
        "reading_time_minutes": round(len(words) / 200.0, 1),
        "headings": len(headings),
        "h1_count": sum(1 for h in headings if h["level"] == 1),
        "skipped_levels": skipped,
    }


@tool(read_only=True)
def doc_outline(path: str, with_slugs: bool = True) -> dict:
    """Heading outline of a Markdown file, with optional GitHub-style anchor slugs and a ready Markdown table of contents.

    Use before building or fixing a table of contents.

    Args:
        path: the Markdown file, relative to the workspace
        with_slugs: include anchor slugs for each heading
    """
    headings = md_headings(read_text(path))
    toc_lines = []
    items = []
    for h in headings:
        item = {"level": h["level"], "title": h["title"]}
        if with_slugs:
            item["slug"] = slugify(h["title"])
        items.append(item)
        toc_lines.append(f"{'  ' * (h['level'] - 1)}- [{h['title']}](#{slugify(h['title'])})")
    return {"headings": items, "toc_markdown": "\n".join(toc_lines)}


@tool(read_only=True)
def check_links(path: str, check_local: bool = True) -> dict:
    """Check links in a Markdown file: broken relative file paths, external URLs, anchor targets.

    Use before publishing a doc to find dead relative links. Anchors are matched against heading slugs of the same file.

    Args:
        path: the Markdown file, relative to the workspace
        check_local: also verify relative file paths against the workspace
    """
    text = read_text(path)
    headings = [slugify(h["title"]) for h in md_headings(text)]
    out = []
    for target in re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", text):
        if target.startswith(("http://", "https://", "mailto:")):
            out.append({"link": target, "status": "external"})
            continue
        if target.startswith("data:"):
            continue
        if "#" in target:
            file_part, _, anchor = target.partition("#")
        else:
            file_part, anchor = target, ""
        if file_part:
            resolved = file_part if os.path.isabs(file_part) else os.path.join(os.path.dirname(path) or ".", file_part)
            if check_local and not os.path.exists(resolved):
                out.append({"link": target, "status": "broken_file"})
            else:
                out.append({"link": target, "status": "local"})
        elif anchor:
            if anchor not in headings:
                out.append({"link": target, "status": "broken_anchor"})
            else:
                out.append({"link": target, "status": "local"})
    return {"links": out, "broken": sum(1 for l in out if l["status"].startswith("broken"))}


@tool(read_only=True)
def md_to_html(path: str, with_css: bool = False) -> str:
    """Convert a Markdown file to a standalone HTML file (<name>.html next to the source).

    Supports headings, paragraphs, fenced code blocks, list items, bold and italic, inline code and links. Use to produce a readable HTML copy of a doc without heavy tools.

    Args:
        path: the Markdown file, relative to the workspace
        with_css: include a minimal embedded stylesheet
    """
    text = read_text(path)
    in_code = False
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            lines.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            lines.append(html.escape(line))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            lines.append(f"<h{level} id=\"{slugify(title)}\">" + _inline(title) + f"</h{level}>")
            continue
        if re.match(r"^[-*+]\s+", line):
            lines.append("<li>" + _inline(re.sub(r"^[-*+]\s+", "", line)) + "</li>")
            continue
        m = re.match(r"^\d+\.\s+(.*)$", line)
        if m:
            lines.append("<li>" + _inline(m.group(1)) + "</li>")
            continue
        if not line.strip():
            lines.append("")
            continue
        lines.append("<p>" + _inline(line) + "</p>")
    css = ""
    if with_css:
        css = "<style>body{max-width:760px;margin:2em auto;font:16px/1.5 sans-serif}pre{background:#f5f5f5;padding:.7em;overflow:auto}</style>"
    body = "\n".join(lines).replace("<li>", "<ul><li>", 1)
    body = re.sub(r"<li>((?:.|\n)*?)</li>", lambda m: m.group(0), body) + "</ul>" if "<li>" in body else body
    out_path = os.path.splitext(path)[0] + ".html"
    open(out_path, "w", encoding="utf-8").write(
        f"<!DOCTYPE html>\n<html><head><meta charset='utf-8'>{css}</head><body>{body}</body></html>\n"
    )
    return out_path


@tool(read_only=True)
def html_to_md(path: str) -> str:
    """Convert an HTML file to a Markdown file (<name>.md next to the source).

    Extracts headings, paragraphs, lists, links and emphasis; drops scripts and styles. Use to rescue docs from HTML sources.

    Args:
        path: the HTML file, relative to the workspace
    """
    headings = ("h1", "h2", "h3", "h4", "h5", "h6")

    class Converter(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.out = []
            self.skip = 0
            self.href = None
            self.buf = []

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "head"):
                self.skip += 1
            elif tag == "a":
                self.href = dict(attrs).get("href")
            elif tag in ("b", "strong"):
                self.out.append("**")
            elif tag in ("i", "em"):
                self.out.append("*")
            elif tag in headings:
                self.out.append("\n\n" + "#" * int(tag[1]) + " ")
            elif tag in ("p", "div"):
                self.out.append("\n\n")
            elif tag == "li":
                self.out.append("- ")
            elif tag == "br":
                self.out.append("\n")

        def handle_endtag(self, tag):
            if tag in ("script", "style", "head"):
                self.skip = max(0, self.skip - 1)
            elif tag == "a" and self.href is not None:
                text = "".join(self.buf).strip()
                self.out.append(f"[{text}]({self.href})" if text else "")
                self.href = None
                self.buf = []

        def handle_data(self, data):
            if self.skip:
                return
            if self.href is not None:
                self.buf.append(data)
            else:
                text = re.sub(r"\s+", " ", data)
                if text.strip():
                    self.out.append(text if text.strip() != data.strip() else data)

    parser = Converter()
    parser.feed(read_text(path))
    md_text = "".join(parser.out)
    md_text = re.sub(r"[ \t]+\n", "\n", md_text)
    md_text = re.sub(r"\n{3,}", "\n\n", md_text).strip() + "\n"
    out_path = os.path.splitext(path)[0] + ".md"
    open(out_path, "w", encoding="utf-8").write(md_text)
    return out_path


@tool(read_only=True)
def text_to_md(path: str) -> str:
    """Convert a plain-text file to a Markdown file (<name>.md): the first non-empty line becomes H1, paragraphs keep blank-line separation.

    Use to bring raw text dumps into documentation shape.

    Args:
        path: the plain-text file, relative to the workspace
    """
    text = read_text(path)
    blocks = [b.strip().replace("\n", " ") for b in re.split(r"\n\s*\n", text) if b.strip()]
    if not blocks:
        raise ValueError(f"File is empty: {path}")
    blocks[0] = "# " + blocks[0]
    out_path = os.path.splitext(path)[0] + ".md"
    open(out_path, "w", encoding="utf-8").write("\n\n".join(blocks) + "\n")
    return out_path


def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]*)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return text

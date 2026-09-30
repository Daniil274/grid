import csv
import html
import io
import os
import re
from html.parser import HTMLParser
from urllib.parse import unquote

from grid_tool import tool

from _common import (
    TEXT_SUFFIXES,
    md_headings,
    prose_lines,
    read_text,
    remove_inline_code,
    safe_path,
    scan,
    slugify,
    unique_slugs,
    write_new,
)


def _default_out(path: str, ext: str, out_path: str) -> str:
    if out_path:
        return out_path
    result = os.path.splitext(path)[0] + ext
    if os.path.normpath(result) == os.path.normpath(path):
        raise ValueError(f"Output would overwrite the source; pass out_path: {path}")
    return result


# ---------------------------------------------------------------- analysis

@tool(read_only=True)
def doc_stats(path: str) -> dict:
    """Statistics of a Markdown or plain text document: words (code excluded), sentences, lines, reading time, headings, code blocks, links, skipped heading levels.

    Use to gauge size, readability and heading structure before editing or converting.

    Args:
        path: the file, relative to the workspace
    """
    text = read_text(path)
    prose = "\n".join(line for _, line in prose_lines(text))
    words = re.findall(r"\w+", prose, flags=re.UNICODE)
    sentences = [s for s in re.split(r"[.!?…]+(?:\s|$)", prose) if re.search(r"\w", s)]
    headings = md_headings(text)
    skipped = []
    prev = 0
    for h in headings:
        if prev and h["level"] > prev + 1:
            skipped.append({"after": prev, "level": h["level"], "title": h["title"], "line": h["line"]})
        prev = h["level"]
    fences = sum(1 for _, line, code in scan(text) if code and re.match(r"^\s{0,3}(`{3,}|~{3,})", line))
    return {
        "words": len(words),
        "sentences": len(sentences),
        "avg_sentence_words": round(len(words) / len(sentences), 1) if sentences else 0,
        "lines": len(text.splitlines()),
        "reading_time_minutes": round(len(words) / 200.0, 1),
        "headings": len(headings),
        "h1_count": sum(1 for h in headings if h["level"] == 1),
        "skipped_levels": skipped,
        "code_blocks": fences // 2 + fences % 2,
        "links": len(re.findall(r"(?<!!)\[[^\]]*\]\([^)]*\)", prose)),
        "images": len(re.findall(r"!\[[^\]]*\]\([^)]*\)", prose)),
    }


@tool(read_only=True)
def doc_inventory(directory: str = ".") -> dict:
    """List the text documents (.md, .markdown, .txt, .rst) below a directory with word counts and first heading.

    Use to survey a documentation folder before a review, restructuring or link check.

    Args:
        directory: folder relative to the workspace
    """
    root = safe_path(directory)
    if not os.path.isdir(root):
        raise FileNotFoundError(f"Directory not found: {directory}")
    base = os.path.realpath(os.getcwd())
    docs = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__"))
        for name in sorted(files):
            if not name.lower().endswith(TEXT_SUFFIXES):
                continue
            full = os.path.join(folder, name)
            try:
                with open(full, encoding="utf-8") as handle:
                    text = handle.read()
            except (UnicodeDecodeError, OSError):
                continue
            heads = md_headings(text)
            docs.append({
                "path": os.path.relpath(full, base),
                "words": len(re.findall(r"\w+", text, flags=re.UNICODE)),
                "title": heads[0]["title"] if heads else "",
                "headings": len(heads),
            })
            if len(docs) >= 500:
                return {"documents": docs, "truncated": True}
    return {"documents": docs, "truncated": False}


@tool(read_only=True)
def doc_outline(path: str, with_slugs: bool = True) -> dict:
    """Heading outline of a Markdown file with GitHub-style anchor slugs (duplicates get -1, -2) and a ready Markdown table of contents.

    Use before building or fixing a table of contents. Headings inside code blocks are ignored.

    Args:
        path: the Markdown file, relative to the workspace
        with_slugs: include anchor slugs for each heading
    """
    headings = md_headings(read_text(path))
    slugs = unique_slugs(headings)
    items = []
    toc = []
    top = min((h["level"] for h in headings), default=1)
    for h, slug in zip(headings, slugs):
        item = {"level": h["level"], "title": h["title"], "line": h["line"]}
        if with_slugs:
            item["slug"] = slug
        items.append(item)
        toc.append(f"{'  ' * (h['level'] - top)}- [{h['title']}](#{slug})")
    return {"headings": items, "toc_markdown": "\n".join(toc)}


@tool(read_only=True)
def doc_lint(path: str, max_line_length: int = 0) -> dict:
    """Lint a Markdown file for structure and style problems, with line numbers.

    Rules: missing or multiple H1, skipped heading levels, duplicate headings, trailing punctuation in headings, unclosed or language-less code fences, trailing spaces, tabs, repeated blank lines, missing final newline, images without alt text, empty links, mixed list markers, optional line length. Use for reviews before editing.

    Args:
        path: the Markdown file, relative to the workspace
        max_line_length: report prose lines longer than this; 0 disables the rule
    """
    text = read_text(path)
    issues = []

    def add(line, rule, message):
        issues.append({"line": line, "rule": rule, "message": message})

    lines = text.splitlines()
    scanned = scan(text)
    headings = md_headings(text)
    h1 = [h for h in headings if h["level"] == 1]
    if not h1:
        add(1, "no-h1", "Document has no H1 heading")
    for extra in h1[1:]:
        add(extra["line"], "multiple-h1", f"Additional H1: {extra['title']}")
    prev = 0
    seen = {}
    for h in headings:
        if prev and h["level"] > prev + 1:
            add(h["line"], "heading-skip", f"Heading level jumps from H{prev} to H{h['level']}: {h['title']}")
        prev = h["level"]
        key = h["title"].lower()
        if key in seen:
            add(h["line"], "duplicate-heading", f"Heading repeats line {seen[key]}: {h['title']}")
        else:
            seen[key] = h["line"]
        if re.search(r"[.:,;]$", h["title"]):
            add(h["line"], "heading-punctuation", f"Heading ends with punctuation: {h['title']}")
    open_line = None
    for number, line, in_code in scanned:
        m = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if m and in_code:
            if open_line is None:
                open_line = number
                if not m.group(2).strip():
                    add(number, "fence-language", "Code fence without a language")
            elif not m.group(2).strip():
                open_line = None
    if open_line is not None:
        add(open_line, "unclosed-fence", "Code fence is never closed")
    blank_run = 0
    markers = set()
    for number, line, in_code in scanned:
        if line.strip() == "":
            blank_run += 1
            if blank_run == 2 and not in_code:
                add(number, "blank-lines", "More than one blank line in a row")
            continue
        blank_run = 0
        if line != line.rstrip() and not line.endswith("  "):
            add(number, "trailing-space", "Trailing whitespace")
        if in_code:
            continue
        if "\t" in line:
            add(number, "tab", "Tab character (use spaces)")
        m = re.match(r"^\s*([-*+])\s+", line)
        if m:
            markers.add(m.group(1))
        stripped = remove_inline_code(line)
        for alt, _ in re.findall(r"!\[([^\]]*)\]\(([^)]*)\)", stripped):
            if not alt.strip():
                add(number, "image-alt", "Image without alt text")
        for label, target in re.findall(r"(?<!!)\[([^\]]*)\]\(([^)]*)\)", stripped):
            if not target.strip():
                add(number, "empty-link", f"Link with empty target: [{label}]()")
            elif not label.strip():
                add(number, "empty-link-text", f"Link with empty text: ({target})")
        if max_line_length and len(line) > max_line_length and not re.search(r"https?://", line):
            add(number, "line-length", f"Line is {len(line)} characters (limit {max_line_length})")
    if len(markers) > 1:
        add(1, "list-markers", "Mixed list markers: " + " ".join(sorted(markers)))
    if text and not text.endswith("\n"):
        add(len(lines), "final-newline", "File does not end with a newline")
    issues.sort(key=lambda i: (i["line"], i["rule"]))
    return {"issues": issues, "count": len(issues)}


@tool(read_only=True)
def check_links(path: str) -> dict:
    """Check links of a Markdown file: broken relative files, broken anchors (also in other Markdown files), undefined reference links. External URLs are listed, not fetched.

    Links inside code blocks and inline code are ignored. Use before publishing a doc set.

    Args:
        path: the Markdown file, relative to the workspace
    """
    text = read_text(path)
    slugs = set(unique_slugs(md_headings(text)))
    definitions = {}
    body = []
    for number, line in prose_lines(text):
        line = remove_inline_code(line)
        m = re.match(r"^\s{0,3}\[([^\]]+)\]:\s*(\S+)", line)
        if m:
            definitions[m.group(1).lower()] = (number, m.group(2).strip("<>"))
            continue
        body.append((number, line))
    targets = []
    for number, line in body:
        for target in re.findall(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)", line):
            targets.append((number, target))
        for label in re.findall(r"!?\[[^\]]*\]\[([^\]]*)\]", line):
            key = label.lower()
            if key and key not in definitions:
                targets.append((number, "ref:" + label))
    for key, (number, target) in definitions.items():
        targets.append((number, target))
    base_dir = os.path.dirname(path) or "."
    results = []
    for number, target in targets:
        entry = {"line": number, "link": target}
        if target.startswith("ref:"):
            entry["status"] = "undefined_reference"
        elif re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
            entry["status"] = "external"
        else:
            file_part, _, anchor = target.partition("#")
            file_part = unquote(file_part.split("?")[0])
            if not file_part:
                entry["status"] = "ok" if (not anchor or unquote(anchor).lower() in slugs) else "broken_anchor"
            else:
                resolved = os.path.normpath(os.path.join(base_dir, file_part))
                try:
                    full = safe_path(resolved)
                except ValueError:
                    entry["status"] = "outside_workspace"
                    results.append(entry)
                    continue
                if not os.path.exists(full):
                    entry["status"] = "broken_file"
                elif anchor and os.path.isfile(full) and full.lower().endswith((".md", ".markdown")):
                    try:
                        with open(full, encoding="utf-8") as handle:
                            other = set(unique_slugs(md_headings(handle.read())))
                    except (UnicodeDecodeError, OSError):
                        other = set()
                    entry["status"] = "ok" if unquote(anchor).lower() in other else "broken_anchor"
                else:
                    entry["status"] = "ok"
        results.append(entry)
    bad = [r for r in results if r["status"] not in ("ok", "external")]
    return {
        "links": results,
        "problems": bad,
        "broken": len(bad),
        "external": sum(1 for r in results if r["status"] == "external"),
    }


@tool(read_only=True)
def term_check(path: str, terms: dict, case_sensitive: bool = False, include_code: bool = False) -> dict:
    """Find inconsistent terminology in one file or a folder of text documents.

    Give a mapping preferred term -> list of variants that must not be used, e.g. {"email": ["e-mail", "E-mail"], "sign in": ["log in", "login"]}. Reports every occurrence of a variant with file and line so the writer can fix it. Also counts the preferred term.

    Args:
        path: a text file or a folder, relative to the workspace
        terms: preferred term -> list of variants to flag
        case_sensitive: match variants case-sensitively
        include_code: also search inside code blocks
    """
    if not isinstance(terms, dict) or not terms:
        raise ValueError("terms must be a non-empty mapping preferred -> [variants]")
    full = safe_path(path)
    if not os.path.exists(full):
        raise FileNotFoundError(f"Path not found: {path}")
    base = os.path.realpath(os.getcwd())
    files = []
    if os.path.isdir(full):
        for folder, dirs, names in os.walk(full):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            files += [os.path.join(folder, n) for n in sorted(names) if n.lower().endswith(TEXT_SUFFIXES)]
    else:
        files = [full]
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled = []
    for preferred, variants in terms.items():
        if isinstance(variants, str):
            variants = [variants]
        for variant in variants:
            compiled.append((preferred, variant, re.compile(r"(?<!\w)" + re.escape(variant) + r"(?!\w)", flags)))
    found = []
    preferred_counts = {p: 0 for p in terms}
    for file in files:
        try:
            with open(file, encoding="utf-8") as handle:
                text = handle.read()
        except (UnicodeDecodeError, OSError):
            continue
        lines = [(n, l) for n, l, _ in scan(text)] if include_code else prose_lines(text)
        for number, line in lines:
            scanned_line = line if include_code else remove_inline_code(line)
            for preferred in terms:
                preferred_counts[preferred] += len(
                    re.findall(r"(?<!\w)" + re.escape(preferred) + r"(?!\w)", scanned_line, flags)
                )
            for preferred, variant, pattern in compiled:
                for m in pattern.finditer(scanned_line):
                    found.append({
                        "file": os.path.relpath(file, base),
                        "line": number,
                        "found": m.group(0),
                        "use": preferred,
                    })
    return {"violations": found, "count": len(found), "preferred_counts": preferred_counts, "files_checked": len(files)}


# ------------------------------------------------------------- conversion

_INLINE_CODE = re.compile(r"(`+)(.+?)\1")


def _inline(text: str) -> str:
    codes = []

    def stash(m):
        codes.append("<code>" + html.escape(m.group(2).strip(), quote=False) + "</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _INLINE_CODE.sub(stash, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)",
                  lambda m: f'<img src="{html.escape(m.group(2))}" alt="{html.escape(m.group(1))}">', text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^)]*&quot;)?\)",
                  lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])__(.+?)__(?![\w*])", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w])_(?!\s)(.+?)(?<!\s)_(?![\w])", r"<em>\1</em>", text)
    text = re.sub(r"~~(.+?)~~", r"<del>\1</del>", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)
    return text


_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def _split_row(line: str) -> list:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _render_list(items: list) -> str:
    out = []
    stack = []
    for indent, tag, text in items:
        while stack and indent < stack[-1][0]:
            out.append(f"</li></{stack.pop()[1]}>")
        if stack and indent == stack[-1][0]:
            out.append("</li>")
            out.append(f"<li>{_inline(text)}")
        else:
            out.append(f"<{tag}><li>{_inline(text)}")
            stack.append((indent, tag))
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def _markdown_to_html(text: str) -> str:
    lines = text.splitlines()
    out = []
    i = 0
    slugs_seen = {}
    while i < len(lines):
        line = lines[i]
        fence = re.match(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)", line)
        if fence:
            marker, lang = fence.group(1), fence.group(2)
            i += 1
            code = []
            while i < len(lines) and not (lines[i].strip().startswith(marker[0] * len(marker)) and not lines[i].strip().strip(marker[0])):
                code.append(lines[i])
                i += 1
            i += 1
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(code), quote=False)}\n</code></pre>")
            continue
        if not line.strip():
            i += 1
            continue
        m = re.match(r"^\s{0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$", line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            slug = slugify(title)
            n = slugs_seen.get(slug, 0)
            slugs_seen[slug] = n + 1
            if n:
                slug = f"{slug}-{n}"
            out.append(f'<h{level} id="{html.escape(slug)}">{_inline(title)}</h{level}>')
            i += 1
            continue
        if re.match(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$", line):
            out.append("<hr>")
            i += 1
            continue
        if line.lstrip().startswith(">"):
            quote = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>\n" + _markdown_to_html("\n".join(quote)) + "\n</blockquote>")
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            header = _split_row(line)
            aligns = []
            for cell in _split_row(lines[i + 1]):
                if cell.startswith(":") and cell.endswith(":"):
                    aligns.append("center")
                elif cell.endswith(":"):
                    aligns.append("right")
                elif cell.startswith(":"):
                    aligns.append("left")
                else:
                    aligns.append("")
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1

            def cell(tag, value, idx):
                align = aligns[idx] if idx < len(aligns) and aligns[idx] else ""
                style = f' style="text-align:{align}"' if align else ""
                return f"<{tag}{style}>{_inline(value)}</{tag}>"

            parts = ["<table>", "<thead><tr>" + "".join(cell("th", c, k) for k, c in enumerate(header)) + "</tr></thead>", "<tbody>"]
            for row in rows:
                parts.append("<tr>" + "".join(cell("td", c, k) for k, c in enumerate(row)) + "</tr>")
            parts.append("</tbody></table>")
            out.append("\n".join(parts))
            continue
        m = _LIST_RE.match(line)
        if m:
            items = []
            while i < len(lines):
                m = _LIST_RE.match(lines[i])
                if m:
                    indent = len(m.group(1).replace("\t", "    "))
                    tag = "ol" if m.group(2)[0].isdigit() else "ul"
                    items.append((indent, tag, m.group(3)))
                    i += 1
                elif lines[i].strip() and lines[i].startswith((" ", "\t")) and items:
                    indent, tag, prev = items[-1]
                    items[-1] = (indent, tag, prev + " " + lines[i].strip())
                    i += 1
                else:
                    break
            out.append(_render_list(items))
            continue
        para = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
            r"^\s{0,3}(#{1,6}\s|`{3,}|~{3,}|>)", lines[i]
        ) and not _LIST_RE.match(lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append("<p>" + _inline(" ".join(para)) + "</p>")
    return "\n".join(out)


@tool
def md_to_html(path: str, out_path: str = "", with_css: bool = False, overwrite: bool = False) -> str:
    """Convert a Markdown file to a standalone HTML file (default <name>.html next to the source).

    Supports headings with anchor ids, paragraphs, fenced code with language, nested lists, blockquotes, tables with alignment, horizontal rules, images, links, bold, italic, strikethrough, inline code. Raw HTML in Markdown is escaped. Refuses to replace an existing file unless overwrite is true.

    Args:
        path: the Markdown file, relative to the workspace
        out_path: the HTML file to create; empty means next to the source
        with_css: embed a small readable stylesheet
        overwrite: allow replacing an existing output file
    """
    text = read_text(path)
    target = _default_out(path, ".html", out_path)
    heads = md_headings(text)
    title = html.escape(heads[0]["title"]) if heads else html.escape(os.path.basename(path))
    css = ""
    if with_css:
        css = ("<style>body{max-width:760px;margin:2em auto;padding:0 1em;font:16px/1.6 sans-serif}"
               "pre{background:#f5f5f5;padding:.8em;overflow:auto}code{background:#f5f5f5}"
               "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.3em .6em}"
               "blockquote{border-left:4px solid #ddd;margin-left:0;padding-left:1em;color:#555}</style>")
    body = _markdown_to_html(text)
    doc = (f"<!DOCTYPE html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n<title>{title}</title>\n{css}\n</head>\n"
           f"<body>\n{body}\n</body>\n</html>\n")
    write_new(target, doc, overwrite)
    return target


class _HtmlToMd(HTMLParser):
    HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
    SKIP = ("script", "style", "head", "title", "template")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bufs = [[]]
        self.skip = 0
        self.lists = []
        self.hrefs = []
        self.pre = 0
        self.quotes = []
        self.rows = None
        self.row = None

    def _put(self, s):
        self.bufs[-1].append(s)

    def _tail(self):
        for chunk in reversed(self.bufs[-1]):
            if chunk:
                return chunk[-1]
        return "\n"

    def _block(self):
        joined = "".join(self.bufs[-1])
        if joined and not joined.endswith("\n\n"):
            self._put("\n" if joined.endswith("\n") else "\n\n")

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
        elif self.skip:
            return
        elif tag in self.HEADINGS:
            self._block()
            self._put("#" * int(tag[1]) + " ")
        elif tag in ("p", "div", "section", "article", "header", "footer", "main"):
            self._block()
        elif tag == "br":
            self._put("  \n" if not self.pre else "\n")
        elif tag in ("ul", "ol"):
            if not self.lists:
                self._block()
            self.lists.append([tag, 0])
        elif tag == "li":
            if self._tail() != "\n":
                self._put("\n")
            depth = max(len(self.lists) - 1, 0)
            marker = "-"
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                marker = f"{self.lists[-1][1]}."
            self._put("  " * depth + marker + " ")
        elif tag == "pre":
            self._block()
            self.pre += 1
            self._put("```\n")
        elif tag == "code":
            if not self.pre:
                self._put("`")
            elif a.get("class", "").startswith("language-"):
                joined = self.bufs[-1]
                for idx in range(len(joined) - 1, -1, -1):
                    if joined[idx] == "```\n":
                        joined[idx] = "```" + a["class"][9:].split()[0] + "\n"
                        break
        elif tag in ("strong", "b"):
            self._put("**")
        elif tag in ("em", "i"):
            self._put("*")
        elif tag in ("del", "s", "strike"):
            self._put("~~")
        elif tag == "a":
            self.hrefs.append(a.get("href"))
            self._put("[" if a.get("href") else "")
        elif tag == "img":
            self._put(f"![{a.get('alt', '')}]({a.get('src', '')})")
        elif tag == "hr":
            self._block()
            self._put("---\n\n")
        elif tag == "blockquote":
            self._block()
            self.bufs.append([])
            self.quotes.append(len(self.bufs))
        elif tag == "table":
            self._block()
            self.rows = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.bufs.append([])

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in self.HEADINGS or tag in ("p", "div", "section", "article", "header", "footer", "main"):
            self._block()
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            if not self.lists:
                self._block()
        elif tag == "pre":
            if self._tail() != "\n":
                self._put("\n")
            self._put("```\n\n")
            self.pre = max(0, self.pre - 1)
        elif tag == "code" and not self.pre:
            self._put("`")
        elif tag in ("strong", "b"):
            self._put("**")
        elif tag in ("em", "i"):
            self._put("*")
        elif tag in ("del", "s", "strike"):
            self._put("~~")
        elif tag == "a" and self.hrefs:
            href = self.hrefs.pop()
            if href:
                self._put(f"]({href})")
        elif tag == "blockquote" and self.quotes:
            self.quotes.pop()
            inner = "".join(self.bufs.pop()).strip("\n")
            quoted = "\n".join(("> " + l).rstrip() for l in inner.split("\n"))
            self._put(quoted + "\n\n")
        elif tag in ("td", "th") and self.row is not None:
            cell = re.sub(r"\s+", " ", "".join(self.bufs.pop())).strip().replace("|", "\\|")
            self.row.append(cell)
        elif tag == "tr" and self.rows is not None and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table" and self.rows is not None:
            rows = [r for r in self.rows if r]
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                self._put("| " + " | ".join(rows[0]) + " |\n")
                self._put("|" + "|".join([" --- "] * width) + "|\n")
                for r in rows[1:]:
                    self._put("| " + " | ".join(r) + " |\n")
                self._put("\n")
            self.rows = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre:
            self._put(data)
            return
        text = re.sub(r"\s+", " ", data)
        if not text.strip():
            if text and self._tail() not in ("\n", " "):
                self._put(" ")
            return
        if self._tail() in ("\n", " ") and text.startswith(" "):
            text = text.lstrip()
        self._put(text)


@tool
def html_to_md(path: str, out_path: str = "", overwrite: bool = False) -> str:
    """Convert an HTML file to a Markdown file (default <name>.md next to the source).

    Handles headings, paragraphs, nested and numbered lists, code blocks, links, images, emphasis, blockquotes, tables and rules; drops scripts, styles and the head. Refuses to replace an existing file unless overwrite is true.

    Args:
        path: the HTML file, relative to the workspace
        out_path: the Markdown file to create; empty means next to the source
        overwrite: allow replacing an existing output file
    """
    parser = _HtmlToMd()
    parser.feed(read_text(path))
    parser.close()
    result = "".join(parser.bufs[0])
    result = "\n".join(line.rstrip(" ") if not line.endswith("  ") or line.strip() == "" else line for line in result.split("\n"))
    result = re.sub(r"\n{3,}", "\n\n", result).strip() + "\n"
    target = _default_out(path, ".md", out_path)
    write_new(target, result, overwrite)
    return target


@tool
def text_to_md(path: str, out_path: str = "", overwrite: bool = False) -> str:
    """Convert a plain-text file to Markdown (default <name>.md): first non-empty line becomes H1, lines starting with -, *, or 1. stay lists, indented blocks become code, paragraphs are unwrapped.

    Use to bring raw text dumps into documentation shape. Refuses to replace an existing file unless overwrite is true.

    Args:
        path: the plain-text file, relative to the workspace
        out_path: the Markdown file to create; empty means next to the source
        overwrite: allow replacing an existing output file
    """
    text = read_text(path)
    blocks = [b for b in re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n")) if b.strip()]
    if not blocks:
        raise ValueError(f"File is empty: {path}")
    out = []
    for index, block in enumerate(blocks):
        lines = [l for l in block.strip("\n").split("\n")]
        if index == 0:
            first = lines[0].strip()
            out.append("# " + first)
            rest = " ".join(l.strip() for l in lines[1:])
            if rest:
                out.append(rest)
        elif all(_LIST_RE.match(l) for l in lines if l.strip()):
            out.append("\n".join(l.rstrip() for l in lines))
        elif all(l.startswith(("    ", "\t")) for l in lines if l.strip()):
            out.append("```\n" + "\n".join(re.sub(r"^(    |\t)", "", l).rstrip() for l in lines) + "\n```")
        else:
            out.append(" ".join(l.strip() for l in lines))
    target = _default_out(path, ".md", out_path)
    write_new(target, "\n\n".join(out) + "\n", overwrite)
    return target


@tool
def csv_to_md_table(path: str, out_path: str = "", overwrite: bool = False) -> str:
    """Convert a CSV file (UTF-8, comma or semicolon separated, first row is the header) to a Markdown table file (default <name>.md).

    Refuses to replace an existing file unless overwrite is true.

    Args:
        path: the CSV file, relative to the workspace
        out_path: the Markdown file to create; empty means next to the source
        overwrite: allow replacing an existing output file
    """
    text = read_text(path)
    if not text.strip():
        raise ValueError(f"File is empty: {path}")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if r]
    width = max(len(r) for r in rows)
    rows = [[c.replace("|", "\\|").replace("\n", " ") for c in r] + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join([" --- "] * width) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    target = _default_out(path, ".md", out_path)
    write_new(target, "\n".join(lines) + "\n", overwrite)
    return target


@tool
def md_table_to_csv(path: str, out_path: str = "", table_index: int = 1, overwrite: bool = False) -> str:
    """Extract one table from a Markdown file into a CSV file (default <name>.csv).

    Args:
        path: the Markdown file, relative to the workspace
        out_path: the CSV file to create; empty means next to the source
        table_index: which table of the document, starting at 1
        overwrite: allow replacing an existing output file
    """
    lines = [line for _, line in prose_lines(read_text(path))]
    tables = []
    i = 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            rows = [_split_row(lines[i])]
            i += 2
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1
            tables.append(rows)
        else:
            i += 1
    if not tables:
        raise ValueError(f"No Markdown tables in {path}")
    if not 1 <= table_index <= len(tables):
        raise ValueError(f"table_index {table_index} out of range: the file has {len(tables)} table(s)")
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(tables[table_index - 1])
    target = _default_out(path, ".csv", out_path)
    write_new(target, buffer.getvalue(), overwrite)
    return target


@tool(destructive=True)
def update_toc(path: str, max_level: int = 3, min_level: int = 2) -> dict:
    """Insert or refresh a table of contents in a Markdown file, in place.

    The TOC lives between the markers <!-- toc --> and <!-- tocstop -->. If the markers are absent they are inserted after the first H1 (or at the top). Headings between min_level and max_level are listed. Idempotent: running twice gives the same file.

    Args:
        path: the Markdown file, relative to the workspace
        max_level: deepest heading level to list (1-6)
        min_level: shallowest heading level to list (1-6)
    """
    if not (1 <= min_level <= max_level <= 6):
        raise ValueError("Levels must satisfy 1 <= min_level <= max_level <= 6")
    text = read_text(path)
    headings = md_headings(text)
    slugs = unique_slugs(headings)
    entries = [(h, s) for h, s in zip(headings, slugs) if min_level <= h["level"] <= max_level]
    top = min((h["level"] for h, _ in entries), default=min_level)
    toc = "\n".join(f"{'  ' * (h['level'] - top)}- [{h['title']}](#{s})" for h, s in entries) or "_No headings_"
    block = f"<!-- toc -->\n\n{toc}\n\n<!-- tocstop -->"
    pattern = re.compile(r"<!-- toc -->.*?<!-- tocstop -->", re.DOTALL)
    if pattern.search(text):
        new = pattern.sub(lambda m: block, text, count=1)
        inserted = False
    else:
        lines = text.split("\n")
        at = 0
        for h in headings:
            if h["level"] == 1:
                at = h["line"]
                break
        lines[at:at] = ["", block, ""] if at else [block, ""]
        new = "\n".join(lines)
        inserted = True
    write_new(path, new, overwrite=True)
    return {"path": path, "entries": len(entries), "inserted_markers": inserted}

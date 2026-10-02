"""PDF conversion tools (work offline; fpdf2+olefile installed before run, stdlib fallback otherwise)."""
import csv
import io
import os
import re
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

from grid_tool import tool

from _common import TEXT_SUFFIXES, read_text, safe_path, write_new


def _default_out(path: str, ext: str, out_path: str) -> str:
    if out_path:
        return out_path
    result = os.path.splitext(path)[0] + ext
    if os.path.normpath(result) == os.path.normpath(path):
        raise ValueError(f"Output would overwrite the source; pass out_path: {path}")
    return result


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _strip_md(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)
        line = re.sub(r"(?<!\w)[*_]([^*_]+)[*_](?!\w)", r"\1", line)
        line = re.sub(r"~~(.+?)~~", r"\1", line)
        line = re.sub(r"`+([^`]*)`+", r"\1", line)
        line = line.replace("\\|", "|")
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _utf16_runs(raw: bytes) -> str:
    """Fallback scan of binary .doc for UTF-16LE text runs (Latin + Cyrillic)."""
    parts = []
    # decode as utf-16le in both alignments, keep printable runs
    for offset in (0, 1):
        try:
            s = raw[offset:].decode("utf-16le", errors="ignore")
        except Exception:
            continue
        for m in re.finditer(r"[\w\u0400-\u04FF .,;:!?\-()№\"'«»—–—]{4,}", s):
            frag = m.group(0).strip()
            if len(frag) >= 4 and re.search(r"[\w\u0400-\u04FF]", frag):
                parts.append(frag)
    # dedupe preserving order, drop garbage with too many control chars
    seen = set()
    out = []
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip(" .:;,-")
        if len(p) < 4 or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return "\n".join(out)


def _ole_text(full: str) -> str:
    raw = open(full, "rb").read()
    try:
        import olefile  # type: ignore

        if olefile.isOleFile(full):
            try:
                ole = olefile.OleFileIO(full)
                try:
                    names = ole.listdir()
                except Exception:
                    names = []
                for cand in (["WordDocument"], ["1Table"], ["Data"]):
                    try:
                        if ole.exists(cand[0]) if len(cand) == 1 else False:
                            data = ole.openstream(cand[0]).read()
                            raw += b"\n" + data
                    except Exception:
                        continue
                try:
                    ole.close()
                except Exception:
                    pass
            except Exception:
                pass
    except ImportError:
        pass
    return _utf16_runs(raw)


def _docx_text(full: str) -> str:
    with zipfile.ZipFile(full) as z:
        try:
            xml = z.read("word/document.xml")
        except KeyError:
            raise ValueError("Not a docx file: no word/document.xml")
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    root = ET.fromstring(xml)
    paras = []
    for p in root.findall(".//w:p", ns):
        runs = ["".join(t.text or "" for t in r.findall("w:t", ns)) for r in p.findall("w:r", ns)]
        # also direct t nodes (hyperlinks etc.)
        direct = "".join(t.text or "" for t in p.findall(".//w:t", ns))
        text = direct.strip()
        if text:
            paras.append(text)
    return "\n\n".join(paras)


def _read_any(full: str, path: str) -> tuple:
    low = path.lower()
    if low.endswith(".docx"):
        return _docx_text(full), "docx"
    if low.endswith(".doc"):
        text = _ole_text(full)
        if not text.strip():
            raise ValueError(f"No readable text found in legacy .doc: {path}")
        return text, "doc"
    if low.endswith(".html") or low.endswith(".htm"):
        raw = open(full, encoding="utf-8", errors="strict").read()
        ex = _TextExtractor()
        ex.feed(raw)
        ex.close()
        text = re.sub(r"\n{3,}", "\n\n", "".join(ex.parts)).strip() + "\n"
        return text, "html"
    if low.endswith(".csv"):
        raw = open(full, encoding="utf-8").read()
        if not raw.strip():
            raise ValueError(f"File is empty: {path}")
        try:
            dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = [r for r in csv.reader(io.StringIO(raw), dialect) if r]
        text = "\n".join(" | ".join(cells) for cells in rows) + "\n"
        return text, "csv"
    # txt / md / rst / markdown
    text = read_text(path)
    if low.endswith((".md", ".markdown")):
        return _strip_md(text), "markdown"
    return text, "text"


_UNICODE_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]


def _find_unicode_font():
    for cand in _UNICODE_FONTS:
        if os.path.isfile(cand):
            return cand
    return ""


def _write_pdf_fpdf(lines: list, target_full: str, title: str) -> str:
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=20)
    pdf.add_page()
    font_file = _find_unicode_font()
    if font_file:
        pdf.add_font("Body", "", font_file)
        pdf.set_font("Body", size=11)
    else:
        pdf.set_font("Helvetica", size=11)
    if title:
        pdf.set_font("Helvetica" if not font_file else "Body", style="", size=14)
        try:
            pdf.multi_cell(0, 8, title)
        except Exception:
            pdf.multi_cell(0, 8, title.encode("latin-1", "replace").decode("latin-1"))
        pdf.ln(2)
        pdf.set_font("Helvetica" if not font_file else "Body", size=11)
    for line in lines:
        if not line.strip():
            pdf.ln(4)
            continue
        text = line
        try:
            pdf.multi_cell(0, 6, text)
        except Exception:
            pdf.multi_cell(0, 6, text.encode("latin-1", "replace").decode("latin-1"))
    pdf.output(target_full)
    return "fpdf2" + ("+unicode-ttf" if font_file else "+core-latin")


def _write_pdf_minimal(lines: list, target_full: str, title: str) -> str:
    """Stdlib-only minimal PDF (Helvetica/WinAnsi, latin only). Non-latin chars become ?."""

    def esc(s: str) -> str:
        s = s.encode("latin-1", "replace").decode("latin-1")
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content = ["BT /F1 11 Tf 14 TL 50 800 Td"]
    if title:
        content.append(f"({esc(title)}) Tj T* T*")
    for line in lines:
        if not line.strip():
            content.append("T*")
            continue
        while len(line) > 95:
            content.append(f"({esc(line[:95])}) Tj T*")
            line = line[95:]
        content.append(f"({esc(line)}) Tj T*")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1", "replace")
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("latin-1")
        out += body if isinstance(body, bytes) else body.encode("latin-1")
        out += b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("latin-1")
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("latin-1")
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode("latin-1")
    with open(target_full, "wb") as handle:
        handle.write(bytes(out))
    return "stdlib-minimal+latin-only"


def _write_pdf(text: str, target_full: str, title: str = "") -> dict:
    lines: list = []
    for para in text.replace("\r\n", "\n").split("\n"):
        if len(para) <= 120:
            lines.append(para)
        else:
            while len(para) > 120:
                cut = para.rfind(" ", 0, 120)
                if cut < 40:
                    cut = 120
                lines.append(para[:cut])
                para = para[cut:].lstrip()
            lines.append(para)
        if len(lines) > 2000:
            lines.append("… [truncated: document too long for PDF]")
            break
    try:
        engine = _write_pdf_fpdf(lines, target_full, title)
    except ImportError:
        engine = _write_pdf_minimal(lines, target_full, title)
    size = os.path.getsize(target_full)
    cyrillic = bool(re.search(r"[\u0400-\u04FF]", text))
    notice = ""
    if cyrillic and engine.endswith("core-latin"):
        notice = "No Cyrillic TTF found in container: Cyrillic replaced with '?'. Install DejaVuSans/Liberation for full Cyrillic."
    elif cyrillic and engine.startswith("stdlib"):
        notice = "Stdlib fallback PDF supports Latin only: Cyrillic replaced with '?'. Install fpdf2 + DejaVuSans for full Cyrillic."
    return {"engine": engine, "pages": "unknown", "bytes": size, "notice": notice}


@tool(read_only=True)
def extract_text(path: str, max_chars: int = 20000) -> dict:
    """Extract plain text from a document for preview or PDF conversion.

    Supports txt, md, csv, html, docx (stdlib zip+xml) and legacy OLE .doc
    (UTF-16 scan, olefile if installed). Use before convert_to_pdf to check
    that the source is readable.

    Args:
        path: the file, relative to the workspace
        max_chars: truncate preview to this many characters
    """
    full = safe_path(path)
    if not os.path.isfile(full):
        raise FileNotFoundError(f"File not found: {path}")
    text, kind = _read_any(full, path)
    truncated = len(text) > max_chars
    return {
        "source": path,
        "kind": kind,
        "chars": len(text),
        "truncated": truncated,
        "text": text[:max_chars],
    }


@tool
def convert_to_pdf(path: str, out_path: str = "", overwrite: bool = False) -> str:
    """Convert any readable document (txt, md, csv, html, docx, legacy doc) to PDF next to the source.

    Text is extracted with stdlib (+olefile for .doc when installed) and written
    with fpdf2 when installed, otherwise with a stdlib minimal PDF writer.
    Latin text always works offline; Cyrillic needs a Unicode TTF in the container
    (DejaVuSans/Liberation/Noto) otherwise it is replaced with '?' and the limitation
    is reported by checking the PDF. Refuses to overwrite unless overwrite is true.

    Args:
        path: the source file, relative to the workspace
        out_path: the PDF file to create; empty means next to the source with .pdf
        overwrite: allow replacing an existing PDF file
    """
    full = safe_path(path)
    if not os.path.isfile(full):
        raise FileNotFoundError(f"File not found: {path}")
    target = _default_out(path, ".pdf", out_path)
    target_full = safe_path(target)
    if os.path.exists(target_full) and not overwrite:
        raise FileExistsError(f"File already exists: {target} (pass overwrite=true to replace it)")
    parent = os.path.dirname(target_full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    text, _kind = _read_any(full, path)
    if not text.strip():
        raise ValueError(f"No text to convert in {path}")
    title = os.path.basename(path)
    _write_pdf(text, target_full, title=title)
    return target


@tool
def md_to_pdf(path: str, out_path: str = "", overwrite: bool = False) -> str:
    """Convert a Markdown file to PDF next to the source (Markdown markup is stripped, headings kept as text).

    Same PDF engine as convert_to_pdf: fpdf2 when installed, stdlib fallback otherwise.
    Refuses to overwrite unless overwrite is true.

    Args:
        path: the Markdown file, relative to the workspace
        out_path: the PDF file to create; empty means next to the source with .pdf
        overwrite: allow replacing an existing PDF file
    """
    full = safe_path(path)
    if not os.path.isfile(full):
        raise FileNotFoundError(f"File not found: {path}")
    target = _default_out(path, ".pdf", out_path)
    target_full = safe_path(target)
    if os.path.exists(target_full) and not overwrite:
        raise FileExistsError(f"File already exists: {target} (pass overwrite=true to replace it)")
    parent = os.path.dirname(target_full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    text = read_text(path)
    plain = _strip_md(text)
    if not plain.strip():
        raise ValueError(f"No text to convert in {path}")
    first = ""
    for line in text.splitlines():
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*)", line)
        if m:
            first = m.group(1).strip()
            break
    _write_pdf(plain, target_full, title=first or os.path.basename(path))
    return target

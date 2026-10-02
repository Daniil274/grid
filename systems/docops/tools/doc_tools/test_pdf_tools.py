import os
import tempfile
import zipfile

from pdf_tools import convert_to_pdf, extract_text, md_to_pdf


def workspace(files):
    folder = tempfile.mkdtemp()
    os.chdir(folder)
    for name, content in files.items():
        directory = os.path.dirname(name)
        if directory:
            os.makedirs(directory, exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(name, mode, encoding=None if isinstance(content, bytes) else "utf-8") as handle:
            handle.write(content)
    return folder


def read_bytes(name):
    with open(name, "rb") as handle:
        return handle.read()


def make_docx(path, paragraphs):
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("[Content_Types].xml", "<Types/>")
    return path


def test_extract_txt_and_md():
    workspace({"a.txt": "Hello world\nSecond line\n", "b.md": "# Title\n\nSome **bold** text.\n"})
    r = extract_text("a.txt")
    assert r["kind"] == "text" and r["chars"] > 10 and "Hello" in r["text"]
    m = extract_text("b.md")
    assert m["kind"] == "markdown" and "Title" in m["text"]


def test_extract_html_csv_docx():
    workspace({
        "p.html": "<html><body><h1>Hi</h1><p>Hello <b>there</b>.</p></body></html>",
        "t.csv": "name,qty\napple,3\n",
    })
    make_docx("d.docx", ["Hello docx", "Second para"])
    h = extract_text("p.html")
    assert "Hello" in h["text"] and "<" not in h["text"]
    c = extract_text("t.csv")
    assert "apple" in c["text"]
    d = extract_text("d.docx")
    assert d["kind"] == "docx" and "Hello docx" in d["text"] and "Second para" in d["text"]


def test_extract_legacy_doc_utf16_fallback():
    folder = tempfile.mkdtemp()
    os.chdir(folder)
    payload = "Hello Test Document Alpha".encode("utf-16le")
    blob = b"\xd0\xcf\x11\xe0" + b"\x00" * 64 + payload + b"\x00" * 64
    with open("legacy.doc", "wb") as handle:
        handle.write(blob)
    r = extract_text("legacy.doc")
    assert r["kind"] == "doc" and "Hello Test Document" in r["text"]


def test_extract_missing():
    workspace({})
    try:
        extract_text("missing.txt")
        assert False, "must raise"
    except FileNotFoundError as error:
        assert "missing.txt" in str(error)


def test_md_to_pdf_ascii():
    workspace({"guide.md": "# Guide\n\nUse bold text here.\n\n- one\n- two\n"})
    out = md_to_pdf("guide.md")
    assert out == "guide.pdf"
    data = read_bytes(out)
    assert data.startswith(b"%PDF") and len(data) > 500
    with open("guide.md", encoding="utf-8") as handle:
        assert "# Guide" in handle.read()


def test_md_to_pdf_refuses_overwrite():
    workspace({"a.md": "# T\n\nText.\n", "a.pdf": b"KEEP"})
    try:
        md_to_pdf("a.md")
        assert False, "must refuse"
    except FileExistsError:
        pass
    assert read_bytes("a.pdf") == b"KEEP"
    out = md_to_pdf("a.md", overwrite=True)
    assert read_bytes(out).startswith(b"%PDF")


def test_convert_txt_and_csv_to_pdf():
    workspace({"n.txt": "Plain note line one.\nLine two here.\n", "t.csv": "name,qty\napple,3\npear,5\n"})
    first = convert_to_pdf("n.txt")
    second = convert_to_pdf("t.csv")
    assert first == "n.pdf" and second == "t.pdf"
    assert read_bytes(first).startswith(b"%PDF")
    assert read_bytes(second).startswith(b"%PDF")


def test_convert_docx_to_pdf():
    workspace({})
    make_docx("memo.docx", ["Memo header", "Body line one"])
    out = convert_to_pdf("memo.docx")
    assert out == "memo.pdf"
    assert read_bytes(out).startswith(b"%PDF")


def test_convert_missing_and_empty():
    workspace({"empty.txt": ""})
    try:
        convert_to_pdf("missing.txt")
        assert False
    except FileNotFoundError:
        pass
    try:
        convert_to_pdf("empty.txt")
        assert False
    except ValueError as error:
        assert "No readable text" in str(error) or "empty" in str(error).lower()

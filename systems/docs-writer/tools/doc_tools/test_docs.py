import os
import tempfile

from docs import (check_links, doc_outline, doc_stats, html_to_md, md_to_html,
                  text_to_md)


def make(name, content):
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


DOC = "# Intro\n\nOne two two. Three!\n\n## Details\n\n[missing](no-such.md), [anchor](#intro), [ext](https://x.example)\n"


def test_doc_stats():
    result = doc_stats(make("a.md", DOC))
    assert result["h1_count"] == 1
    assert result["headings"] == 2
    assert result["sentences"] >= 2
    assert result["reading_time_minutes"] > 0
    assert result["words"] > 0


def test_doc_outline_toc():
    result = doc_outline(make("b.md", DOC))
    assert result["headings"][1]["slug"] == "details"
    assert "[Details](#details)" in result["toc_markdown"]


def test_check_links():
    result = check_links(make("c.md", DOC))
    assert result["broken"] == 1
    statuses = {l["link"]: l["status"] for l in result["links"]}
    assert statuses["no-such.md"] == "broken_file"
    assert statuses["#intro"] == "local"
    assert statuses["https://x.example"] == "external"


def test_md_to_html():
    out = md_to_html(make("d.md", DOC))
    html_text = open(out, encoding="utf-8").read()
    assert html_text.startswith("<!DOCTYPE html>")
    assert '<h1 id="intro">' in html_text
    assert "https://x.example" in html_text


def test_html_to_md():
    out = html_to_md(make("e.html", "<h1>Head</h1><p>Some <a href='u.html'>link</a> here</p><ul><li>item</li></ul>"))
    md_text = open(out, encoding="utf-8").read()
    assert "# Head" in md_text
    assert "[link](u.html)" in md_text


def test_text_to_md():
    out = text_to_md(make("f.txt", "First paragraph\nsecond line.\n\nPara two."))
    md_text = open(out, encoding="utf-8").read()
    assert md_text.startswith("# First paragraph second line.")
    assert "Para two." in md_text

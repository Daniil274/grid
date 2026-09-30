import os
import tempfile

from docs import (
    check_links,
    csv_to_md_table,
    doc_inventory,
    doc_lint,
    doc_outline,
    doc_stats,
    html_to_md,
    md_table_to_csv,
    md_to_html,
    term_check,
    text_to_md,
    update_toc,
)


def workspace(files):
    folder = tempfile.mkdtemp()
    os.chdir(folder)
    for name, content in files.items():
        os.makedirs(os.path.dirname(name) or ".", exist_ok=True)
        with open(name, "w", encoding="utf-8") as handle:
            handle.write(content)
    return folder


def read(name):
    with open(name, encoding="utf-8") as handle:
        return handle.read()


def test_stats_ignores_code_and_headings_in_code():
    workspace({"a.md": "# T\n\nOne two. Three!\n\n```\n# not heading\nx y z\n```\n\n### Deep\n"})
    r = doc_stats("a.md")
    assert r["headings"] == 2 and r["h1_count"] == 1
    assert r["skipped_levels"][0]["level"] == 3
    assert r["code_blocks"] == 1
    assert r["words"] < 8


def test_outline_duplicates_and_toc():
    workspace({"a.md": "# A\n\n## Setup\n\n## Setup\n\n## Ещё\n"})
    r = doc_outline("a.md")
    assert [h["slug"] for h in r["headings"]] == ["a", "setup", "setup-1", "ещё"]
    assert "- [Setup](#setup-1)" in r["toc_markdown"]


def test_lint_finds_problems():
    workspace({"a.md": "## Start:\n\n\n```\ncode\n```\n\n# One\n# Two\n- a\n* b\ntext  x\t\n![](i.png)\n[x]()"})
    rules = {i["rule"] for i in doc_lint("a.md")["issues"]}
    for rule in ("multiple-h1", "heading-punctuation", "blank-lines", "fence-language", "list-markers",
                 "image-alt", "empty-link", "final-newline", "tab"):
        assert rule in rules, rule


def test_lint_clean_document():
    workspace({"a.md": "# Title\n\n## Part\n\nText.\n\n```python\nx = 1\n```\n"})
    assert doc_lint("a.md")["count"] == 0


def test_lint_unclosed_fence():
    workspace({"a.md": "# T\n\n```python\nx = 1\n"})
    assert any(i["rule"] == "unclosed-fence" for i in doc_lint("a.md")["issues"])


def test_check_links():
    workspace({
        "docs/a.md": "# A\n\n[ok](b.md) [bad](nope.md) [anc](#a) [bad anc](#zzz) [x](b.md#missing) [y](b.md#bee)\n"
                     "[ext](https://e.com) `[code](fake.md)` [ref][r1] [undef][r2]\n\n[r1]: b.md\n\n```\n[c](zz.md)\n```\n",
        "docs/b.md": "# B\n\n## Bee\n",
    })
    r = check_links("docs/a.md")
    bad = sorted(p["link"] for p in r["problems"])
    assert bad == ["#zzz", "b.md#missing", "nope.md", "ref:r2"]
    assert r["external"] == 1


def test_term_check():
    workspace({"a.md": "# E-mail\n\nSend e-mail or email. `e-mail` in code.\n\n```\ne-mail\n```\n"})
    r = term_check("a.md", {"email": ["e-mail"]})
    assert r["count"] == 2
    assert r["violations"][0]["line"] == 1 and r["violations"][0]["use"] == "email"
    assert r["preferred_counts"]["email"] == 1


def test_md_to_html_features():
    workspace({"a.md": (
        "# Title\n\nText with **bold**, *it*, `a<b` and [link](x.html).\n\n"
        "- one\n  - nested\n- two\n\n1. first\n2. second\n\n"
        "| A | B |\n|:--|--:|\n| 1 | 2 |\n\n> quote\n\n```py\nif a < b:\n    pass\n```\n\n<script>x</script>\n"
    )})
    out = md_to_html("a.md")
    page = read(out)
    assert out == "a.html"
    assert '<h1 id="title">Title</h1>' in page
    assert "<strong>bold</strong>" in page and "<em>it</em>" in page
    assert "<code>a&lt;b</code>" in page
    assert '<a href="x.html">link</a>' in page
    assert "<ul><li>one<ul><li>nested</li></ul></li><li>two</li></ul>" in page
    assert "<ol><li>first</li><li>second</li></ol>" in page
    assert '<th style="text-align:right">B</th>' in page
    assert "<blockquote>" in page
    assert 'class="language-py"' in page and "if a &lt; b:" in page
    assert "<script>" not in page


def test_convert_refuses_overwrite():
    workspace({"a.md": "# T\n", "a.html": "keep"})
    try:
        md_to_html("a.md")
        assert False, "must refuse"
    except FileExistsError:
        pass
    assert read("a.html") == "keep"
    md_to_html("a.md", overwrite=True)
    assert "<h1" in read("a.html")


def test_html_to_md():
    workspace({"p.html": (
        "<html><head><title>x</title><style>p{}</style></head><body><h1>Doc</h1>"
        "<p>Hello <b>big</b> <a href='u.html'>link</a> and <code>x</code>.</p>"
        "<ul><li>one</li><li>two<ul><li>deep</li></ul></li></ul><ol><li>a</li><li>b</li></ol>"
        "<pre><code class='language-py'>a = 1\nb = 2</code></pre>"
        "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        "<script>evil()</script></body></html>"
    )})
    out = html_to_md("p.html")
    md = read(out)
    assert md.startswith("# Doc\n"), md
    assert "Hello **big** [link](u.html) and `x`." in md, md
    assert "- one\n- two\n  - deep" in md, md
    assert "1. a\n2. b" in md, md
    assert "```py\na = 1\nb = 2\n```" in md, md
    assert "| A | B |" in md and "| 1 | 2 |" in md, md
    assert "evil" not in md and "x</title>" not in md, md


def test_text_to_md():
    workspace({"n.txt": "Guide\nsubtitle\n\nFirst para\nwraps here.\n\n- a\n- b\n\n    code line\n"})
    out = text_to_md("n.txt")
    md = read(out)
    assert md.startswith("# Guide\n\nsubtitle"), md
    assert "First para wraps here." in md, md
    assert "- a\n- b" in md, md
    assert "code line" in md, md


def test_csv_and_table_roundtrip():
    workspace({"t.csv": "name;qty\napple;3\npipe|x;4\n"})
    out = csv_to_md_table("t.csv")
    md = read(out)
    assert md.splitlines()[0] == "| name | qty |"
    assert "pipe\\|x" in md
    back = md_table_to_csv("t.md", out_path="back.csv")
    assert read(back).splitlines() == ["name,qty", "apple,3", "pipe|x,4"]


def test_table_index_error():
    workspace({"t.md": "no tables\n"})
    try:
        md_table_to_csv("t.md")
        assert False
    except ValueError as error:
        assert "No Markdown tables" in str(error)


def test_update_toc_idempotent():
    workspace({"a.md": "# Doc\n\nIntro\n\n## One\n\n### Sub\n\n## Two\n"})
    r = update_toc("a.md")
    first = read("a.md")
    assert r["entries"] == 3 and r["inserted_markers"]
    assert "- [One](#one)\n  - [Sub](#sub)\n- [Two](#two)" in first
    assert first.index("<!-- toc -->") < first.index("## One")
    update_toc("a.md")
    assert read("a.md") == first
    assert first.count("<!-- toc -->") == 1


def test_inventory_and_path_safety():
    workspace({"d/a.md": "# A\n", "d/b.txt": "x y", "d/img.png": "zz", ".hidden/c.md": "# C"})
    r = doc_inventory("d")
    assert [d["path"] for d in r["documents"]] == [os.path.join("d", "a.md"), os.path.join("d", "b.txt")]
    for bad in ("../x.md", "/etc/passwd"):
        try:
            doc_stats(bad)
            assert False
        except (ValueError, FileNotFoundError):
            pass


def test_missing_file_message():
    workspace({})
    try:
        doc_stats("missing.md")
        assert False
    except FileNotFoundError as error:
        assert "missing.md" in str(error)

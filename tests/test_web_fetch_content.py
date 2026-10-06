"""web_fetch reads documents by their type and says why a page did not load."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "coder" / "tools"))
import web_tools  # noqa: E402


def _pdf(text: str) -> bytes:
    """A one-page PDF whose text layer is *text*."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def test_a_pdf_is_read_as_the_text_of_its_pages():
    text, error = web_tools._to_text("application/pdf", None, _pdf("Service Level Agreement"))

    assert error == ""
    assert text.startswith("[page 1]") and "Service Level Agreement" in text


def test_a_pdf_is_recognised_by_its_content_when_served_as_something_else():
    text, error = web_tools._to_text("application/octet-stream", None, _pdf("SLA"))

    assert error == "" and "SLA" in text


def test_text_and_json_come_as_they_are():
    assert web_tools._to_text("application/json; charset=utf-8", "utf-8", b'{"a": 1}') == ('{"a": 1}', "")
    assert web_tools._to_text("text/markdown", None, "# Заголовок".encode()) == ("# Заголовок", "")


def test_binary_content_is_refused_with_what_is_readable():
    text, error = web_tools._to_text("image/png", None, b"\x89PNG")

    assert text == "" and "image/png" in error and "PDF" in error


def test_a_timeout_names_the_host_and_the_time():
    message = web_tools._load_error(asyncio.TimeoutError(), "https://yandex.ru/legal/", 30)

    assert message.startswith("❌ Timeout: yandex.ru did not answer within 30 s")


def test_an_exception_without_a_message_is_named_by_its_type():
    message = web_tools._load_error(RuntimeError(), "https://example.com/", 30)

    assert message == "❌ Load error (RuntimeError): RuntimeError"


@pytest.mark.parametrize("extra", [{}, {"render_js": True}])
def test_render_js_is_not_offered_and_old_calls_with_it_still_work(monkeypatch, extra):
    import json
    from types import SimpleNamespace

    assert "render_js" not in web_tools.web_fetch.params_json_schema["properties"]

    async def fetched(url, timeout):
        return 200, "page text", ""

    monkeypatch.setattr(web_tools, "_fetch_url", fetched)
    result = asyncio.run(web_tools.web_fetch.on_invoke_tool(
        SimpleNamespace(context=None, tool_name="web_fetch", tool_call_id="t"),
        json.dumps({"url": "https://example.com/", **extra}),
    ))

    assert result == "Source: https://example.com/\n\npage text"

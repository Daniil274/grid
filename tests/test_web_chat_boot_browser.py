"""Load the real chat shell and modules in Chromium before declaring it ready."""

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pw = pytest.importorskip("playwright.sync_api")
WEB = Path(__file__).resolve().parents[1] / "web_chat"


@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844)])
def test_chat_boots_with_current_composer_markup(width, height):
    requested = []

    def serve(route):
        url = urlsplit(route.request.url)
        if url.netloc != "chat.test":
            route.fulfill(body="", content_type="text/css")
            return
        path = url.path
        requested.append(path)
        responses = {
            "/api/chat/bootstrap": {
                "user": {"id": "test", "username": "browser-test", "role": "user"},
                "accounts": True, "systems": [], "multi_system": True,
                "workspace_path": "/workspace", "isolation_enabled": True,
            },
            "/api/chat/conversations": [],
            "/api/projects": {"projects": []},
            "/api/server/status": {"instance_id": "test", "phase": "running"},
            "/api/auth/usage": {},
            "/api/plan": {},
        }
        if path in responses:
            route.fulfill(body=json.dumps(responses[path]), content_type="application/json")
        else:
            target = WEB / ("index.html" if path == "/" else path.removeprefix("/static/").lstrip("/"))
            if not target.is_file() or not target.resolve().is_relative_to(WEB):
                route.fulfill(status=404)
                return
            kind = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}
            route.fulfill(path=str(target), content_type=kind.get(target.suffix, "text/plain"))

    with pw.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(args=["--no-sandbox"])
        except pw.Error as exc:
            pytest.skip(f"Chromium runtime missing: {exc}")
        try:
            page = browser.new_page(viewport={"width": width, "height": height})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.route("**/*", serve)
            page.goto("http://chat.test/", wait_until="networkidle")
            assert page.locator(".bootError").count() == 0
            assert not errors, (errors, requested)
            assert "/api/chat/bootstrap" in requested
            assert "/api/chat/conversations" in requested
            assert "/api/server/status" in requested
            assert page.locator("#account-name").text_content() == "browser-test"
            assert page.locator("#runtime-status").text_content() == ""
            assert page.locator("#compact-context .compactLabel").text_content() == "Compact context"
            page.locator("#composer-input").fill("A draft")
            assert page.locator("#send").is_enabled()
            page.locator("#composer-tools summary").click()
            assert page.locator("#compact-context .compactLabel").is_visible()
            assert page.locator("#compact-context svg").count() == 1
        finally:
            browser.close()

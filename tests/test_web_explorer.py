"""The workspace explorer: browse, edit and arrange a user's own workspace, never
through a link and never outside it."""

import io
import os
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from schemas.schemas import UploadsPolicy
from web_chat.explorer_api import register_explorer_routes
from web_chat.uploads import register_upload_routes


@pytest.fixture
def explorer(tmp_path):
    workspaces = {}

    async def space(request: Request):
        user = request.headers.get("x-user")
        if user not in {"alice", "bob"}:
            raise HTTPException(status_code=401)
        workspace = tmp_path / user
        workspace.mkdir(exist_ok=True)
        workspaces[user] = workspace
        return SimpleNamespace(workspace_path=workspace)

    api = APIRouter()
    register_explorer_routes(api, space)
    register_upload_routes(api, space, lambda: UploadsPolicy(max_file_mb=1, max_files=3, quota_mb=2))
    app = FastAPI()
    app.include_router(api)
    client = TestClient(app, headers={"x-user": "alice"})
    client.get("/api/workspace/tree")  # makes alice's workspace
    return client, workspaces["alice"], tmp_path


def test_a_folder_lists_folders_first_with_sizes_and_links_as_links(explorer):
    client, root, tmp_path = explorer
    (root / "src").mkdir()
    (root / "b.txt").write_text("hello")
    (root / "A.md").write_text("# a")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    os.symlink(secret, root / "escape")

    listing = client.get("/api/workspace/tree").json()
    assert [entry["name"] for entry in listing["entries"]] == ["src", "A.md", "b.txt", "escape"]
    by_name = {entry["name"]: entry for entry in listing["entries"]}
    assert by_name["b.txt"]["size"] == 5 and by_name["src"]["kind"] == "dir"
    assert by_name["escape"]["kind"] == "link" and by_name["escape"]["target"] == str(secret)
    # Neither read nor listed through the link.
    assert client.get("/api/workspace/text", params={"path": "escape"}).status_code == 404
    assert client.get("/api/workspace/tree", params={"path": "escape"}).status_code == 404
    assert client.get("/api/workspace/tree", params={"path": "../bob"}).status_code == 404


def test_text_is_read_edited_and_guarded_against_a_concurrent_change(explorer):
    client, root, _ = explorer
    (root / "notes.py").write_text("print('hi')\n")
    opened = client.get("/api/workspace/text", params={"path": "notes.py"}).json()
    assert opened["text"] == "print('hi')\n" and not opened["binary"]

    saved = client.put("/api/workspace/text", json={"path": "notes.py", "text": "x = 1\n", "mtime": opened["mtime"]})
    assert saved.status_code == 200, saved.text
    assert (root / "notes.py").read_text() == "x = 1\n"
    # The stale version is refused, not written over the newer one.
    stale = client.put("/api/workspace/text", json={"path": "notes.py", "text": "lost", "mtime": opened["mtime"]})
    assert stale.status_code == 409
    assert (root / "notes.py").read_text() == "x = 1\n"
    assert not [name for name in os.listdir(root) if name.endswith(".tmp")]

    created = client.put("/api/workspace/text", json={"path": "new.md", "text": "# new", "create": True})
    assert created.status_code == 200 and (root / "new.md").read_text() == "# new"
    again = client.put("/api/workspace/text", json={"path": "new.md", "text": "", "create": True})
    assert again.status_code == 409


def test_binary_files_are_not_shown_as_text(explorer):
    client, root, _ = explorer
    (root / "blob.bin").write_bytes(b"\x00\x01\x02")
    opened = client.get("/api/workspace/text", params={"path": "blob.bin"}).json()
    assert opened["binary"] and opened["text"] is None


def test_folders_are_made_renamed_copied_and_deleted(explorer):
    client, root, _ = explorer
    assert client.post("/api/workspace/mkdir", json={"path": "docs"}).status_code == 200
    assert client.post("/api/workspace/mkdir", json={"path": "docs"}).status_code == 409
    (root / "docs" / "a.txt").write_text("a")
    os.symlink("/etc/passwd", root / "docs" / "link")

    copied = client.post("/api/workspace/copy", json={"source": "docs", "target": "docs-copy"})
    assert copied.status_code == 200, copied.text
    assert (root / "docs-copy" / "a.txt").read_text() == "a"
    assert os.readlink(root / "docs-copy" / "link") == "/etc/passwd"
    assert client.post("/api/workspace/copy", json={"source": "docs", "target": "docs/inner"}).status_code == 400

    moved = client.post("/api/workspace/move", json={"source": "docs-copy/a.txt", "target": "b.txt"})
    assert moved.status_code == 200 and (root / "b.txt").read_text() == "a"
    assert client.post("/api/workspace/move", json={"source": "docs", "target": "b.txt"}).status_code == 409
    assert client.post("/api/workspace/move", json={"source": "docs", "target": "docs/x"}).status_code == 400
    assert client.post("/api/workspace/move", json={"source": "b.txt", "target": "../out.txt"}).status_code == 404

    assert client.delete("/api/workspace/entry", params={"path": "docs"}).status_code == 200
    assert not (root / "docs").exists()
    assert os.path.exists("/etc/passwd")
    assert client.delete("/api/workspace/entry", params={"path": ""}).status_code == 400


def test_deleting_a_link_removes_the_link_only(explorer):
    client, root, tmp_path = explorer
    target = tmp_path / "keep"
    target.mkdir()
    (target / "file").write_text("kept")
    os.symlink(target, root / "dir-link")
    assert client.delete("/api/workspace/entry", params={"path": "dir-link"}).status_code == 200
    assert (target / "file").read_text() == "kept"


def test_search_finds_names_below_a_folder_and_skips_caches(explorer):
    client, root, _ = explorer
    (root / "src" / "deep").mkdir(parents=True)
    (root / "src" / "deep" / "Report.md").write_text("")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "report.js").write_text("")
    found = client.get("/api/workspace/search", params={"q": "report"}).json()
    assert [match["path"] for match in found["matches"]] == ["src/deep/Report.md"]
    assert found["complete"]


def test_a_folder_downloads_as_a_zip_without_its_links(explorer):
    client, root, _ = explorer
    (root / "out" / "sub").mkdir(parents=True)
    (root / "out" / "sub" / "r.txt").write_text("result")
    os.symlink("/etc/passwd", root / "out" / "pw")
    response = client.get("/api/workspace/archive", params={"path": "out"})
    assert response.status_code == 200
    assert "out.zip" in response.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert "sub/r.txt" in names and "pw" not in names


def test_files_upload_into_an_existing_folder(explorer):
    client, root, _ = explorer
    (root / "data").mkdir()
    response = client.post("/api/workspace/uploads", params={"dir": "data"}, files={"files": ("x.csv", b"1,2")})
    assert response.status_code == 200, response.text
    assert response.json()["files"][0]["path"] == "data/x.csv"
    assert (root / "data" / "x.csv").read_bytes() == b"1,2"
    root_upload = client.post("/api/workspace/uploads", params={"dir": ""}, files={"files": ("top.txt", b"t")})
    assert root_upload.json()["files"][0]["path"] == "top.txt"
    missing = client.post("/api/workspace/uploads", params={"dir": "nope"}, files={"files": ("x", b"1")})
    assert missing.status_code == 400
    os.symlink("/tmp", root / "tmp-link")
    linked = client.post("/api/workspace/uploads", params={"dir": "tmp-link"}, files={"files": ("x", b"1")})
    assert linked.status_code == 400


def test_one_user_never_reaches_another_users_files(explorer):
    client, root, _ = explorer
    (root / "mine.txt").write_text("alice")
    bob = client.get("/api/workspace/text", params={"path": "mine.txt"}, headers={"x-user": "bob"})
    assert bob.status_code == 404
    assert client.get("/api/workspace/tree", headers={"x-user": "eve"}).status_code == 401

"""Multipart uploads and downloads share the authenticated user's workspace."""

import asyncio
import os
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from schemas.schemas import UploadsPolicy
from tests.conftest import link
from web_chat.uploads import (
    UploadError,
    open_in_workspace,
    register_upload_routes,
    safe_name,
)


@pytest.fixture
def upload_app(tmp_path):
    limits = UploadsPolicy(max_file_mb=1, max_files=3, quota_mb=2)
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
    register_upload_routes(api, space, lambda: limits)
    app = FastAPI()
    app.include_router(api)
    return app, TestClient(app, headers={"x-user": "alice"}), limits, workspaces


def test_files_are_available_to_agents_and_downloadable_by_their_owner(upload_app):
    _, client, _, workspaces = upload_app
    response = client.post(
        "/api/workspace/uploads",
        files=[
            ("files", ("данные.csv", b"a,b\n1,2\n", "text/csv")),
            ("files", ("report.pdf", b"%PDF-1.4", "application/pdf")),
        ],
    )
    assert response.status_code == 200, response.text
    first, second = response.json()["files"]
    assert first["path"] == "uploads/данные.csv"
    assert first["bytes"] == 8 and first["type"] == "text/csv"
    assert (workspaces["alice"] / first["path"]).read_bytes() == b"a,b\n1,2\n"
    download = client.get(first["url"])
    assert download.status_code == 200 and download.content == b"a,b\n1,2\n"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["cache-control"] == "no-store"
    assert client.get(second["url"]).content == b"%PDF-1.4"
    assert client.get(first["url"], headers={"x-user": "bob"}).status_code == 404
    assert client.get(first["url"], headers={"x-user": "unknown"}).status_code == 401


def test_generated_documents_can_be_downloaded_too(upload_app):
    _, client, _, workspaces = upload_app
    client.post("/api/workspace/uploads", files={"files": ("input.txt", b"input")})
    output = workspaces["alice"] / "results" / "finished.docx"
    output.parent.mkdir()
    output.write_bytes(b"document")
    assert (
        client.get("/api/workspace/files/results/finished.docx").content == b"document"
    )


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_an_image_is_downloaded_by_default_and_inline_with_the_flag(upload_app):
    _, client, _, workspaces = upload_app
    client.post("/api/workspace/uploads", files={"files": ("input.txt", b"input")})
    (workspaces["alice"] / "bench.png").write_bytes(PNG)
    url = "/api/workspace/files/bench.png"

    attachment = client.get(url)
    assert attachment.headers["content-disposition"].startswith("attachment;")
    assert attachment.headers["content-type"] == "application/octet-stream"

    inline = client.get(url, params={"inline": "1"})
    assert inline.status_code == 200 and inline.content == PNG
    assert inline.headers["content-type"] == "image/png"
    assert inline.headers["content-disposition"].startswith("inline;")
    assert inline.headers["cache-control"] == "no-store"


def test_inline_never_serves_a_non_image(upload_app):
    _, client, _, workspaces = upload_app
    client.post("/api/workspace/uploads", files={"files": ("input.txt", b"input")})
    (workspaces["alice"] / "notes.txt").write_bytes(b"just text")
    (workspaces["alice"] / "vector.svg").write_bytes(
        b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>"
    )
    for name in ("notes.txt", "vector.svg"):
        response = client.get(f"/api/workspace/files/{name}", params={"inline": "1"})
        assert response.headers["content-disposition"].startswith("attachment;"), name
        assert response.headers["content-type"] == "application/octet-stream", name


def test_live_download_is_bounded_and_has_no_stale_content_length(upload_app, monkeypatch):
    from web_chat import uploads

    _, client, _, workspaces = upload_app
    client.post("/api/workspace/uploads", files={"files": ("report.txt", b"old")})
    original = uploads.open_in_workspace

    def changed_after_open(workspace, relative):
        opened = original(workspace, relative)
        (workspaces["alice"] / relative).write_bytes(b"new and longer")
        return opened

    monkeypatch.setattr(uploads, "open_in_workspace", changed_after_open)
    response = client.get("/api/workspace/files/uploads/report.txt")
    assert response.status_code == 200
    assert response.content == b"new"
    assert "content-length" not in response.headers


def test_repeated_names_preserve_existing_files(upload_app):
    _, client, _, workspaces = upload_app
    first = client.post(
        "/api/workspace/uploads", files={"files": ("report.txt", b"first")}
    ).json()["files"][0]
    second = client.post(
        "/api/workspace/uploads", files={"files": ("report.txt", b"second")}
    ).json()["files"][0]
    assert second["name"] == "report (2).txt"
    assert client.get(first["url"]).content == b"first"
    assert client.get(second["url"]).content == b"second"
    assert (workspaces["alice"] / "uploads").is_dir()


@pytest.mark.parametrize(
    "name, expected",
    [
        ("../../secret.txt", "secret.txt"),
        (r"C:\folder\notes.txt", "notes.txt"),
        ("\x00\n  .  ", "file"),
        ("Документ (1).txt", "Документ (1).txt"),
    ],
)
def test_filename_stays_in_uploads(name, expected):
    assert safe_name(name) == expected


def test_oversized_file_removes_the_whole_batch(upload_app):
    _, client, _, workspaces = upload_app
    response = client.post(
        "/api/workspace/uploads",
        files=[
            ("files", ("small.txt", b"small")),
            ("files", ("large.bin", b"x" * (1024 * 1024 + 1))),
        ],
    )
    assert response.status_code == 400 and "larger" in response.json()["detail"]
    assert list((workspaces["alice"] / "uploads").iterdir()) == []


def test_quota_is_per_user_and_a_failed_batch_leaves_existing_files(upload_app):
    _, client, _, workspaces = upload_app
    payload = b"x" * (1024 * 1024)
    original = client.post(
        "/api/workspace/uploads", files={"files": ("original.bin", payload)}
    )
    assert original.status_code == 200
    response = client.post(
        "/api/workspace/uploads",
        files=[
            ("files", ("fits.bin", payload)),
            ("files", ("extra.txt", b"extra")),
        ],
    )
    assert response.status_code == 400 and "full" in response.json()["detail"]
    assert [p.name for p in (workspaces["alice"] / "uploads").iterdir()] == [
        "original.bin"
    ]
    assert (
        client.post(
            "/api/workspace/uploads",
            headers={"x-user": "bob"},
            files={"files": ("original.bin", payload)},
        ).status_code
        == 200
    )


@pytest.mark.asyncio
async def test_simultaneous_uploads_cannot_exceed_quota_or_overwrite_names(upload_app):
    app, _, limits, workspaces = upload_app
    limits.quota_mb = 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *[
                client.post(
                    "/api/workspace/uploads",
                    headers={"x-user": "alice"},
                    files={"files": ("same.bin", b"x" * (700 * 1024))},
                )
                for _ in range(2)
            ]
        )
    assert sorted(response.status_code for response in responses) == [200, 400]
    assert len(list((workspaces["alice"] / "uploads").iterdir())) == 1


def test_uploads_can_be_disabled_and_require_authentication(upload_app):
    _, client, limits, _ = upload_app
    limits.enabled = False
    assert (
        client.post(
            "/api/workspace/uploads", files={"files": ("a.txt", b"a")}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/workspace/uploads",
            headers={"x-user": "unknown"},
            files={"files": ("a.txt", b"a")},
        ).status_code
        == 401
    )


def test_multipart_shape_and_request_size_are_bounded(upload_app):
    _, client, limits, _ = upload_app
    assert (
        client.post("/api/workspace/uploads", json={"files": "hello"}).status_code
        == 400
    )
    assert (
        client.post(
            "/api/workspace/uploads", files={"wrong": ("a.txt", b"a")}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/workspace/uploads",
            files=[("files", (f"{i}.txt", b"a")) for i in range(limits.max_files + 1)],
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/workspace/uploads",
            files={"files": ("a.bin", b"a" * (4 * 1024 * 1024))},
        ).status_code
        == 413
    )
    # A false Content-Length must not bypass the streaming body limit.
    assert (
        client.post(
            "/api/workspace/uploads",
            headers={"content-length": "1"},
            files={"files": ("a.bin", b"a" * (4 * 1024 * 1024))},
        ).status_code
        == 413
    )


def test_symlinks_and_special_files_never_expose_the_host(upload_app, tmp_path):
    _, client, _, workspaces = upload_app
    client.post("/api/workspace/uploads", files={"files": ("normal.txt", b"ok")})
    workspace = workspaces["alice"]
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    link(workspace / "directory", tmp_path, directory=True)
    refused = ["directory/secret.txt", "uploads", "../secret.txt"]
    try:
        (workspace / "link.txt").symlink_to(secret)
        refused.append("link.txt")
    except OSError:
        if os.name != "nt":  # Windows grants file links only with privileges
            raise
    if hasattr(os, "mkfifo"):
        os.mkfifo(workspace / "pipe")
        refused.append("pipe")
    for path in refused:
        with pytest.raises(UploadError):
            open_in_workspace(workspace, path)
    assert client.get("/api/workspace/files/directory/secret.txt").status_code == 404


def test_upload_directory_cannot_be_a_symlink(upload_app, tmp_path):
    _, client, _, _ = upload_app
    workspace = tmp_path / "alice"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link(workspace / "uploads", outside, directory=True)
    response = client.post(
        "/api/workspace/uploads", files={"files": ("escape.txt", b"no")}
    )
    assert response.status_code == 400
    assert list(outside.iterdir()) == []

"""Files a user hands to the agents, and files the agents hand back.

Images still travel inside the message, for the model to see
(web_chat.attachments). Every other file is uploaded into the user's workspace,
under ``uploads/``, where the agents' file tools and commands reach it; the
chat then mentions it in the message as a link, so the agent knows its path and
the user can download it again. Any file of the workspace can be downloaded the
same way - a document an agent wrote, for instance.

    POST /api/workspace/uploads         multipart ``files`` -> [{path, name, bytes, type, url}]
    GET  /api/workspace/files/{path}    one file of the workspace, as an attachment
                                        (``?inline=1``: a raster image, inline)

The workspace is also the agents' - in a container, their commands can put a
link anywhere in it. The server runs outside that container, often as root,
so it never follows a link: ``uploads`` must be a real directory, a file is
created with ``O_NOFOLLOW`` in that directory's descriptor, and a download is
served only when its real path is inside the workspace.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

UPLOADS_DIR = "uploads"
CHUNK = 1024 * 1024
#: Multipart framing around the files, allowed on top of their bytes.
OVERHEAD_BYTES = 64 * 1024


#: Raster image magic bytes we are willing to serve inline, and their types.
#: SVG is deliberately absent: it is a scriptable document, not a picture, and
#: would run in the chat page's own origin.
IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def image_media_type(head: bytes) -> str | None:
    """The media type of a raster image by its magic bytes, or ``None``."""
    for signature, media_type in IMAGE_SIGNATURES:
        if head.startswith(signature):
            return media_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


class UploadError(ValueError):
    """A refused upload; the message is for the user."""


class BodyTooLarge(MultiPartException):
    """Stops multipart parsing and closes its temporary files."""


def safe_name(name: str) -> str:
    """A file name that stays one plain name inside ``uploads/``."""
    name = unicodedata.normalize(
        "NFC", Path(str(name or "")).name.replace("\\", "/").split("/")[-1]
    )
    name = (
        "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*')
        .strip()
        .strip(".")
    )
    name = re.sub(r"\s+", " ", name)[:120]
    return name or "file"


def _owner(workspace: Path) -> tuple[int, int]:
    info = os.stat(workspace)
    return info.st_uid, info.st_gid


def _uploads_fd(workspace: Path) -> int:
    """A descriptor of ``<workspace>/uploads``, made when missing, never through a link."""
    workspace_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            os.mkdir(UPLOADS_DIR, 0o755, dir_fd=workspace_fd)
            if os.geteuid() == 0:
                uid, gid = _owner(workspace)
                os.chown(
                    UPLOADS_DIR, uid, gid, dir_fd=workspace_fd, follow_symlinks=False
                )
        except FileExistsError:
            pass
        try:
            return os.open(
                UPLOADS_DIR,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=workspace_fd,
            )
        except OSError:
            raise UploadError(
                "The workspace's uploads/ is not a plain directory; move it away to upload files."
            ) from None
    finally:
        os.close(workspace_fd)


def _used_bytes(directory_fd: int) -> int:
    total = 0
    for entry in os.scandir(directory_fd):
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def _free_name(directory_fd: int, name: str) -> str:
    """*name*, or *name (2)*, … - the first that does not exist yet."""
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem:
        stem, suffix = name, ""
    candidate, number = name, 1
    existing = set(os.listdir(directory_fd))
    while candidate in existing:
        number += 1
        candidate = f"{stem} ({number}).{suffix}" if suffix else f"{stem} ({number})"
    return candidate


async def save_upload(
    workspace: Path, upload: UploadFile, *, max_bytes: int, quota_bytes: int
) -> dict[str, Any]:
    """Write one uploaded file into ``uploads/``; what it became."""
    workspace = Path(workspace).resolve()
    directory_fd = _uploads_fd(workspace)
    try:
        room = quota_bytes - _used_bytes(directory_fd)
        name = _free_name(directory_fd, safe_name(upload.filename or "file"))
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=directory_fd,
        )
        written = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                while chunk := await upload.read(CHUNK):
                    written += len(chunk)
                    if written > max_bytes:
                        raise UploadError(
                            f"{name} is larger than {max_bytes // (1024 * 1024)} MB."
                        )
                    if written > room:
                        raise UploadError(
                            "Your uploads are full: delete some files of uploads/ first."
                        )
                    handle.write(chunk)
                if os.geteuid() == 0:
                    uid, gid = _owner(workspace)
                    os.fchown(handle.fileno(), uid, gid)
        except BaseException:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(directory_fd)
    path = f"{UPLOADS_DIR}/{name}"
    return {
        "path": path,
        "name": name,
        "bytes": written,
        "type": upload.content_type or "application/octet-stream",
        "url": download_url(path),
    }


def download_url(path: str) -> str:
    from urllib.parse import quote

    return f"/api/workspace/files/{quote(path)}"


def open_in_workspace(workspace: Path, relative: str) -> tuple[int, str, int]:
    """Open the workspace file *relative* names: (descriptor, name, size).

    Walked one component at a time from the workspace with ``O_NOFOLLOW``, so a
    link anywhere on the way - planted before or during the request - is
    refused, never followed. The caller owns the descriptor.
    """
    parts = [part for part in str(relative or "").split("/") if part not in ("", ".")]
    if not parts or any(part == ".." or "\0" in part for part in parts):
        raise UploadError("No such file.")
    fd = os.open(Path(workspace).resolve(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            except OSError:
                raise UploadError("No such file.") from None
            os.close(fd)
            fd = next_fd
        try:
            file_fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
            )
        except OSError:
            raise UploadError("No such file.") from None
    finally:
        os.close(fd)
    info = os.fstat(file_fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(file_fd)
        raise UploadError("No such file.")
    return file_fd, parts[-1], info.st_size


def register_upload_routes(
    api: APIRouter, current_space: Callable[..., Any], policy: Callable[[], Any]
) -> None:
    # Serialize quota checks and filename allocation in each user's workspace.
    locks: dict[Path, asyncio.Lock] = {}

    @api.post("/api/workspace/uploads")
    async def upload(
        request: Request, space: Any = Depends(current_space)
    ) -> JSONResponse:
        limits = policy()
        if not limits.enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="File uploads are off on this server.",
            )
        max_bytes = limits.max_file_mb * 1024 * 1024
        max_body = limits.max_files * max_bytes + OVERHEAD_BYTES
        # The form is spooled to disk as it is parsed: refuse an oversized one before.
        declared = request.headers.get("content-length")
        if declared is None or not declared.isdigit():
            raise HTTPException(
                status_code=status.HTTP_411_LENGTH_REQUIRED,
                detail="Content-Length is required.",
            )
        if int(declared) > max_body:
            raise HTTPException(
                status_code=413,
                detail=f"Too large: at most {limits.max_file_mb} MB a file.",
            )
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "multipart/form-data"
        ):
            raise HTTPException(status_code=400, detail="Expected multipart files.")

        async def bounded_stream():
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > max_body:
                    raise BodyTooLarge("Upload request is too large.")
                yield chunk

        try:
            form = await MultiPartParser(
                request.headers,
                bounded_stream(),
                max_files=limits.max_files,
                max_fields=5,
            ).parse()
        except MultiPartException as exc:
            raise HTTPException(
                status_code=413 if isinstance(exc, BodyTooLarge) else 400,
                detail=str(exc),
            ) from None

        saved: list[dict[str, Any]] = []
        try:
            files = form.getlist("files")
            if not files or any(not isinstance(item, UploadFile) for item in files):
                raise HTTPException(status_code=400, detail="No files.")
            if any(key != "files" for key, _ in form.multi_items()):
                raise HTTPException(
                    status_code=400, detail="Use the files field for every upload."
                )
            workspace = Path(space.workspace_path).resolve()
            async with locks.setdefault(workspace, asyncio.Lock()):
                directory_fd = _uploads_fd(workspace)
                try:
                    for item in files:
                        saved.append(
                            await save_upload(
                                workspace,
                                item,
                                max_bytes=max_bytes,
                                quota_bytes=limits.quota_mb * 1024 * 1024,
                            )
                        )
                except BaseException:
                    # A batch is all-or-nothing: refused batches leave no files
                    # that the client never learned how to refer to.
                    for item in saved:
                        try:
                            os.unlink(item["name"], dir_fd=directory_fd)
                        except FileNotFoundError:
                            pass
                    raise
                finally:
                    os.close(directory_fd)
        except UploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        finally:
            await form.close()
        return JSONResponse({"files": saved})

    @api.get("/api/workspace/files/{path:path}")
    async def download(
        path: str, inline: bool = False, space: Any = Depends(current_space)
    ) -> StreamingResponse:
        try:
            fd, name, size = open_in_workspace(Path(space.workspace_path), path)
        except UploadError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

        # ``?inline=1`` shows a picture inside the chat's own page. Only a real
        # raster image qualifies - not an SVG, which is a scriptable document
        # rather than a picture; anything else stays a forced attachment.
        media_type = image_media_type(os.pread(fd, 12, 0)) if inline else None

        def chunks():
            with handle:
                remaining = size
                while remaining:
                    block = handle.read(min(CHUNK, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block

        # A non-image is always an attachment: a workspace file is never
        # rendered as a page of this site.
        from urllib.parse import quote

        handle = os.fdopen(fd, "rb")
        return StreamingResponse(
            chunks(),
            media_type=media_type or "application/octet-stream",
            headers={
                # The agent can still truncate or rewrite this open inode.
                # StreamingResponse must not promise the old size as its
                # Content-Length, and a growing file must not stream forever.
                "Content-Disposition": (
                    f"{'inline' if media_type else 'attachment'}; "
                    f"filename*=UTF-8''{quote(name)}"
                ),
                "Cache-Control": "no-store",
            },
            background=BackgroundTask(handle.close),
        )

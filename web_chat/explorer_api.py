"""The workspace explorer's API: browse, read, edit and arrange the files of a
user's workspace (the page is web_chat/files.html).

    GET    /api/workspace/tree?path=            a folder's entries
    GET    /api/workspace/text?path=            a file as text, to show or edit
    PUT    /api/workspace/text                  {path, text, mtime?, create?} write it
    POST   /api/workspace/mkdir                 {path}
    POST   /api/workspace/move                  {source, target}: rename or move
    POST   /api/workspace/copy                  {source, target}: a file or a whole folder
    DELETE /api/workspace/entry?path=           a file, a link, or a folder with its contents
    GET    /api/workspace/search?q=&path=       names containing q, below path
    GET    /api/workspace/archive?path=         a folder as a zip

Every route takes ``context`` too: a chat may work in a directory of its own
(web_chat/space.py), and the explorer then shows that directory. A chat with a
space of its own also reaches its project's and its user's spaces, by a path's
first name (``project/...``, ``user/...``, web_chat.uploads.locate): its top
folder lists them as folders, and moving or copying between them hands a file
from one space to another. Uploading into a folder and downloading a file are
web_chat/uploads.py's routes.

The agents change the workspace at the same time - in a container, as another
user - and the server is often root. So, as for uploads, nothing here follows a
link: a path is walked from the workspace one name at a time with
``O_NOFOLLOW`` (utils.confined_fs), and every operation acts relative to the
descriptor of the folder it found. A link is listed, renamed, copied and
deleted as the link itself, never through it. What the server makes is given
to the workspace's owner, so the agents can change it in turn.

Without ``dir_fd`` (Windows) the same rules are checked by path, as uploads do.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from utils.confined_fs import SUPPORTED as BY_DESCRIPTOR
from utils.confined_fs import LinkRefused, _give_to_workspace_owner, _walk

#: Text shown and edited in the page; a larger file is shown truncated, read-only.
MAX_TEXT_BYTES = 2 * 1024 * 1024
#: Search stops after this many matches, or after visiting this many entries.
MAX_MATCHES = 500
MAX_VISITED = 100_000
#: Folders the search does not descend into: tools' caches, rarely what is sought.
SEARCH_SKIPS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"})
#: A copy or an archive stops past these.
MAX_COPY_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_ENTRIES = 50_000
CHUNK = 1024 * 1024
O_BINARY = getattr(os, "O_BINARY", 0)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class ExplorerError(Exception):
    """A refused operation; the message is for the user."""

    status = 400


class NotFound(ExplorerError):
    status = 404


class Conflict(ExplorerError):
    status = 409


class TooLarge(ExplorerError):
    status = 413


def split(relative: Optional[str]) -> list[str]:
    """The names of a workspace path; ``""`` is the workspace itself."""
    text = str(relative or "").replace("\\", "/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." or "\0" in part for part in parts):
        raise NotFound("No such file or folder.")
    return parts


def join(parts: list[str]) -> str:
    return "/".join(parts)


def valid_name(name: str) -> str:
    """*name* as one plain entry name, or refused."""
    name = str(name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise ExplorerError(f"'{name}' is not a valid name.")
    if len(name.encode("utf-8", "surrogateescape")) > 255:
        raise ExplorerError("The name is too long.")
    return name


def _plain(info: os.stat_result, is_kind: Callable[[int], bool]) -> bool:
    return not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_reparse_tag", 0) and is_kind(info.st_mode)


def kind_of(info: os.stat_result) -> str:
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0):
        return "link"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "other"


class Folder:
    """A folder of the workspace, reached without following a link.

    Held by a descriptor where the system opens relative to one; by path
    elsewhere. Its methods take plain names of entries in it.
    """

    def __init__(self, root: Path, parts: list[str], fd: Optional[int]) -> None:
        self.root, self.parts, self.fd = root, parts, fd
        self.path = root.joinpath(*parts) if parts else root

    # -- opening ------------------------------------------------------------
    @classmethod
    @contextmanager
    def open(cls, root: Path, parts: list[str]) -> Iterator["Folder"]:
        if BY_DESCRIPTOR:
            try:
                fd = _walk(root, parts, create=False)
            except (FileNotFoundError, NotADirectoryError, LinkRefused):
                raise NotFound("No such folder.") from None
            try:
                yield cls(root, parts, fd)
            finally:
                os.close(fd)
            return
        path = root
        for part in parts:
            path = path / part
            try:
                plain = _plain(os.lstat(path), stat.S_ISDIR)
            except OSError:
                plain = False
            if not plain:
                raise NotFound("No such folder.")
        yield cls(root, parts, None)

    @contextmanager
    def sub(self, name: str) -> Iterator["Folder"]:
        """The folder *name* in this one."""
        if self.fd is None:
            with Folder.open(self.root, self.parts + [name]) as folder:
                yield folder
            return
        try:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | O_CLOEXEC, dir_fd=self.fd)
        except OSError:
            raise NotFound("No such folder.") from None
        try:
            yield Folder(self.root, self.parts + [name], fd)
        finally:
            os.close(fd)

    def _at(self, name: str) -> tuple[Any, dict[str, Any]]:
        if self.fd is None:
            return self.path / name, {}
        return name, {"dir_fd": self.fd}

    # -- reading ------------------------------------------------------------
    def names(self) -> list[str]:
        return os.listdir(self.fd if self.fd is not None else self.path)

    def lstat(self, name: str) -> Optional[os.stat_result]:
        where, at = self._at(name)
        try:
            return os.stat(where, follow_symlinks=False, **at)
        except FileNotFoundError:
            return None

    def readlink(self, name: str) -> str:
        where, at = self._at(name)
        return os.readlink(where, **at)

    def open_file(self, name: str, flags: int, mode: int = 0o644) -> int:
        where, at = self._at(name)
        if self.fd is not None:
            flags |= os.O_NOFOLLOW
        elif flags & os.O_CREAT == 0:
            info = self.lstat(name)
            if info is None or not _plain(info, stat.S_ISREG):
                raise NotFound("No such file.")
        try:
            return os.open(where, flags | O_BINARY | O_CLOEXEC, mode, **at)
        except FileExistsError:
            raise Conflict(f"'{name}' already exists.") from None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.EISDIR, errno.ENOTDIR):
                raise NotFound("No such file.") from None
            raise

    # -- changing -----------------------------------------------------------
    def own(self, fd: int) -> None:
        _give_to_workspace_owner(fd, self.root)

    def mkdir(self, name: str) -> None:
        where, at = self._at(name)
        try:
            os.mkdir(where, 0o755, **at)
        except FileExistsError:
            raise Conflict(f"'{name}' already exists.") from None
        with self.sub(name) as made:
            if made.fd is not None:
                self.own(made.fd)

    def symlink(self, target: str, name: str) -> None:
        where, at = self._at(name)
        os.symlink(target, where, **at)

    def remove(self, name: str) -> None:
        """Delete entry *name*: a folder with everything in it; a link as itself."""
        info = self.lstat(name)
        if info is None:
            raise NotFound("No such file or folder.")
        where, at = self._at(name)
        if kind_of(info) == "dir":
            # rmtree with dir_fd walks by descriptors and never follows a link.
            shutil.rmtree(where, **at)
        else:
            os.unlink(where, **at)

    def rename(self, name: str, target: "Folder", new_name: str) -> None:
        if target.lstat(new_name) is not None:
            raise Conflict(f"'{new_name}' already exists in {join(target.parts) or 'the workspace'}.")
        source, source_at = self._at(name)
        destination, destination_at = target._at(new_name)
        kwargs: dict[str, Any] = {}
        if "dir_fd" in source_at:
            kwargs = {"src_dir_fd": source_at["dir_fd"], "dst_dir_fd": destination_at["dir_fd"]}
        try:
            os.rename(source, destination, **kwargs)
        except OSError as exc:
            if exc.errno == errno.EINVAL:
                raise ExplorerError("A folder cannot be moved into itself.") from None
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise Conflict(f"'{new_name}' already exists.") from None
            raise


# -- the operations, each on a workspace root ---------------------------------
def _entry(folder: Folder, name: str, info: os.stat_result) -> dict[str, Any]:
    kind = kind_of(info)
    entry: dict[str, Any] = {
        "name": name,
        "path": join(folder.parts + [name]),
        "kind": kind,
        "size": info.st_size if kind == "file" else None,
        "mtime": info.st_mtime,
    }
    if kind == "link":
        try:
            entry["target"] = folder.readlink(name)
        except OSError:
            entry["target"] = None
    return entry


def list_folder(root: Path, relative: str) -> dict[str, Any]:
    parts = split(relative)
    with Folder.open(root, parts) as folder:
        entries = []
        for name in folder.names():
            info = folder.lstat(name)
            if info is not None:
                entries.append(_entry(folder, name, info))
    entries.sort(key=lambda entry: (entry["kind"] != "dir", entry["name"].casefold()))
    return {"path": join(parts), "entries": entries}


def _parent_and_name(relative: str) -> tuple[list[str], str]:
    parts = split(relative)
    if not parts:
        raise ExplorerError("This is the workspace itself.")
    return parts[:-1], parts[-1]


def read_text(root: Path, relative: str) -> dict[str, Any]:
    parent, name = _parent_and_name(relative)
    with Folder.open(root, parent) as folder:
        fd = folder.open_file(name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise NotFound("No such file.")
        data = handle.read(MAX_TEXT_BYTES + 1)
    truncated = len(data) > MAX_TEXT_BYTES
    data = data[:MAX_TEXT_BYTES]
    result = {
        "path": join(parent + [name]),
        "size": info.st_size,
        "mtime": str(info.st_mtime_ns),
        "truncated": truncated,
        "binary": False,
        "text": None,
    }
    if b"\0" in data[:8192]:
        result["binary"] = True
        return result
    try:
        result["text"] = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A cut through a multi-byte character at the limit is still text.
        if truncated and exc.start >= len(data) - 3:
            result["text"] = data[: exc.start].decode("utf-8")
        else:
            result["binary"] = True
    return result


def write_text(
    root: Path, relative: str, text: str, *, expected_mtime: Optional[str], create: bool
) -> dict[str, Any]:
    data = text.encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise TooLarge(f"At most {MAX_TEXT_BYTES // (1024 * 1024)} MB of text.")
    parent, name = _parent_and_name(relative)
    valid_name(name)
    with Folder.open(root, parent) as folder:
        current = folder.lstat(name)
        if create and current is not None:
            raise Conflict(f"'{name}' already exists.")
        if not create:
            if current is None or kind_of(current) != "file":
                raise NotFound("No such file: it was moved or deleted.")
            if expected_mtime is not None and str(current.st_mtime_ns) != str(expected_mtime):
                raise Conflict("The file changed since it was opened. Reload it, or save a copy.")
        # Written beside the file and renamed over it: a reader never sees half
        # of it, and the inode the agents may hold open is left as it was.
        temporary = f".{name}.grid-{os.getpid()}-{os.urandom(4).hex()}.tmp"
        mode = stat.S_IMODE(current.st_mode) if current is not None else 0o644
        fd = folder.open_file(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                if current is not None and hasattr(os, "fchown") and os.geteuid() == 0:
                    os.fchown(handle.fileno(), current.st_uid, current.st_gid)
                else:
                    folder.own(handle.fileno())
            if create and folder.lstat(name) is not None:
                raise Conflict(f"'{name}' already exists.")
            where, at = folder._at(temporary)
            destination, destination_at = folder._at(name)
            kwargs = {"src_dir_fd": at["dir_fd"], "dst_dir_fd": destination_at["dir_fd"]} if at else {}
            os.replace(where, destination, **kwargs)
        except BaseException:
            try:
                folder.remove(temporary)
            except (OSError, ExplorerError):
                pass
            raise
        info = folder.lstat(name)
    return {"path": join(parent + [name]), "size": info.st_size, "mtime": str(info.st_mtime_ns)}


def make_folder(root: Path, relative: str) -> dict[str, Any]:
    parent, name = _parent_and_name(relative)
    valid_name(name)
    with Folder.open(root, parent) as folder:
        folder.mkdir(name)
    return {"path": join(parent + [name])}


def move(root: Path, source: str, target: str, target_root: Optional[Path] = None) -> dict[str, Any]:
    """Move *source* to *target*; *target* is in *target_root* when given (another space)."""
    target_root = target_root or root
    source_parent, source_name = _parent_and_name(source)
    target_parent, target_name = _parent_and_name(target)
    valid_name(target_name)
    if target_root == root and target_parent[: len(source_parent) + 1] == source_parent + [source_name]:
        raise ExplorerError("A folder cannot be moved into itself.")
    with Folder.open(root, source_parent) as origin:
        if origin.lstat(source_name) is None:
            raise NotFound("No such file or folder.")
        with Folder.open(target_root, target_parent) as destination:
            try:
                origin.rename(source_name, destination, target_name)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise ExplorerError("These spaces are on different disks: copy it there, then delete it here.") from None
                raise
    return {"path": join(target_parent + [target_name])}


class _Budget:
    def __init__(self, max_bytes: int) -> None:
        self.bytes, self.entries, self.max_bytes = 0, 0, max_bytes

    def spend(self, size: int) -> None:
        self.bytes += size
        self.entries += 1
        if self.bytes > self.max_bytes or self.entries > MAX_ENTRIES:
            raise TooLarge("Too much to copy at once.")


def _copy_entry(origin: Folder, name: str, destination: Folder, new_name: str, budget: _Budget) -> None:
    info = origin.lstat(name)
    if info is None:
        return
    kind = kind_of(info)
    if kind == "link":
        budget.spend(0)
        destination.symlink(origin.readlink(name), new_name)
    elif kind == "dir":
        budget.spend(0)
        destination.mkdir(new_name)
        with origin.sub(name) as inner, destination.sub(new_name) as made:
            for child in inner.names():
                _copy_entry(inner, child, made, child, budget)
    elif kind == "file":
        budget.spend(info.st_size)
        source_fd = origin.open_file(name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(source_fd, "rb") as reader:
            target_fd = destination.open_file(new_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IMODE(info.st_mode))
            with os.fdopen(target_fd, "wb") as writer:
                destination.own(writer.fileno())
                copied = 0
                while block := reader.read(CHUNK):
                    copied += len(block)
                    if copied > info.st_size:
                        budget.spend(len(block))
                    writer.write(block)
    # Sockets, pipes and devices are not copied.


def copy(root: Path, source: str, target: str, target_root: Optional[Path] = None) -> dict[str, Any]:
    """Copy *source* to *target*; *target* is in *target_root* when given (another space)."""
    target_root = target_root or root
    source_parent, source_name = _parent_and_name(source)
    target_parent, target_name = _parent_and_name(target)
    valid_name(target_name)
    if target_root == root and target_parent[: len(source_parent) + 1] == source_parent + [source_name]:
        raise ExplorerError("A folder cannot be copied into itself.")
    with Folder.open(root, source_parent) as origin, Folder.open(target_root, target_parent) as destination:
        if origin.lstat(source_name) is None:
            raise NotFound("No such file or folder.")
        if destination.lstat(target_name) is not None:
            raise Conflict(f"'{target_name}' already exists.")
        try:
            _copy_entry(origin, source_name, destination, target_name, _Budget(MAX_COPY_BYTES))
        except BaseException:
            # All or nothing: a refused copy leaves no half of itself behind.
            try:
                destination.remove(target_name)
            except (OSError, ExplorerError):
                pass
            raise
    return {"path": join(target_parent + [target_name])}


def delete(root: Path, relative: str) -> None:
    parent, name = _parent_and_name(relative)
    with Folder.open(root, parent) as folder:
        folder.remove(name)


def search(root: Path, relative: str, query: str) -> dict[str, Any]:
    needle = str(query or "").strip().casefold()
    if not needle:
        raise ExplorerError("Type part of a name to search for.")
    matches: list[dict[str, Any]] = []
    visited = 0
    complete = True

    def visit(folder: Folder) -> None:
        nonlocal visited, complete
        for name in sorted(folder.names(), key=str.casefold):
            visited += 1
            if len(matches) >= MAX_MATCHES or visited > MAX_VISITED:
                complete = False
                return
            info = folder.lstat(name)
            if info is None:
                continue
            if needle in name.casefold():
                matches.append(_entry(folder, name, info))
            if kind_of(info) == "dir" and name not in SEARCH_SKIPS:
                try:
                    with folder.sub(name) as inner:
                        visit(inner)
                except (NotFound, PermissionError):
                    continue
                if not complete:
                    return

    with Folder.open(root, split(relative)) as start:
        visit(start)
    return {"query": query, "matches": matches, "complete": complete}


def archive(root: Path, relative: str, title: Optional[str] = None) -> tuple[Any, str]:
    """A temporary file holding the zip of folder *relative*, and its name
    (*title*, for a whole space, else the folder's)."""
    parts = split(relative)
    name = (parts[-1] if parts else title or root.name or "workspace") + ".zip"
    spool = tempfile.TemporaryFile()
    budget = _Budget(MAX_ARCHIVE_BYTES)
    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:

            def add(folder: Folder, prefix: str) -> None:
                for entry in sorted(folder.names()):
                    info = folder.lstat(entry)
                    if info is None:
                        continue
                    kind = kind_of(info)
                    inside = f"{prefix}{entry}"
                    if kind == "dir":
                        budget.spend(0)
                        bundle.writestr(inside + "/", b"")
                        with folder.sub(entry) as inner:
                            add(inner, inside + "/")
                    elif kind == "file":
                        budget.spend(info.st_size)
                        fd = folder.open_file(entry, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
                        with os.fdopen(fd, "rb") as reader, bundle.open(inside, "w", force_zip64=True) as writer:
                            while block := reader.read(CHUNK):
                                writer.write(block)
                    # Links and special files stay out of the archive.

            with Folder.open(root, parts) as start:
                add(start, "")
        spool.seek(0)
    except BaseException:
        spool.close()
        raise
    return spool, name


# -- routes -------------------------------------------------------------------
class TextWrite(BaseModel):
    path: str
    text: str
    mtime: Optional[str] = None
    create: bool = False


class PathBody(BaseModel):
    path: str


class MoveBody(BaseModel):
    source: str
    target: str


def _prefixed(value: Any, prefix: str) -> Any:
    """*value* with every ``path`` in it named as the chat names its space: ``project/...``."""
    if not prefix:
        return value
    if isinstance(value, list):
        return [_prefixed(item, prefix) for item in value]
    if isinstance(value, dict):
        out = {key: _prefixed(item, prefix) for key, item in value.items()}
        if isinstance(value.get("path"), str):
            out["path"] = f"{prefix}/{value['path']}" if value["path"] else prefix
        return out
    return value


def register_explorer_routes(api: APIRouter, current_space: Callable[..., Any]) -> None:
    from web_chat.uploads import locate, space_prefix

    def where(space: Any, context: Optional[str], path: Optional[str]) -> tuple[Path, str, str]:
        """(space directory, path within it, the name the chat gives that space)."""
        root, rest = locate(space, context, path)
        root = root.resolve()
        return root, rest, space_prefix(space, context, root)

    def spaces_of(space: Any, context: Optional[str]) -> dict[str, dict[str, str]]:
        """The other spaces chat *context* reaches, by name: their labels."""
        of_spaces = getattr(space, "file_spaces", None)
        if of_spaces is None:
            return {}
        _primary, named = of_spaces(context)
        labels = getattr(space, "file_space_labels", lambda _c: {})(context)
        return {name: {"label": labels.get(name, name)} for name in named}

    async def run(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """*operation* off the event loop, its refusals as HTTP errors."""
        try:
            return await asyncio.to_thread(operation, *args, **kwargs)
        except ExplorerError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        except LinkRefused:
            raise HTTPException(status_code=404, detail="Links are not followed here.") from None
        except PermissionError:
            raise HTTPException(status_code=403, detail="The server may not do that here.") from None
        except OSError as exc:
            raise HTTPException(status_code=400, detail=exc.strerror or str(exc)) from None

    @api.get("/api/workspace/tree")
    async def tree(path: str = "", context: Optional[str] = None, space: Any = Depends(current_space)) -> JSONResponse:
        root, rest, prefix = where(space, context, path)
        listing = _prefixed(await run(list_folder, root, rest), prefix)
        others = spaces_of(space, context)
        if not prefix and not split(rest) and others:
            # The chat's own top folder: the other spaces first, as folders; an
            # entry of the chat's own with their names is not reachable by path.
            entries = [entry for entry in listing["entries"] if entry["name"] not in others]
            spaces = [
                {"name": name, "path": name, "kind": "dir", "size": None, "mtime": None,
                 "space": name, "label": info["label"]}
                for name, info in others.items()
            ]
            listing["entries"] = spaces + entries
        listing["workspace"] = getattr(space, "workspace_label_of", lambda _c: str(root))(context)
        listing["root_label"] = "This chat" if others else "Workspace"
        listing["spaces"] = others
        return JSONResponse(listing, headers={"Cache-Control": "no-store"})

    @api.get("/api/workspace/text")
    async def text(path: str, context: Optional[str] = None, space: Any = Depends(current_space)) -> JSONResponse:
        root, rest, prefix = where(space, context, path)
        result = _prefixed(await run(read_text, root, rest), prefix)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @api.put("/api/workspace/text")
    async def save(body: TextWrite, context: Optional[str] = None, space: Any = Depends(current_space)) -> dict:
        root, rest, prefix = where(space, context, body.path)
        result = await run(write_text, root, rest, body.text, expected_mtime=body.mtime, create=body.create)
        return _prefixed(result, prefix)

    @api.post("/api/workspace/mkdir")
    async def mkdir(body: PathBody, context: Optional[str] = None, space: Any = Depends(current_space)) -> dict:
        root, rest, prefix = where(space, context, body.path)
        return _prefixed(await run(make_folder, root, rest), prefix)

    @api.post("/api/workspace/move")
    async def rename(body: MoveBody, context: Optional[str] = None, space: Any = Depends(current_space)) -> dict:
        root, source, _ = where(space, context, body.source)
        target_root, target, prefix = where(space, context, body.target)
        return _prefixed(await run(move, root, source, target, target_root), prefix)

    @api.post("/api/workspace/copy")
    async def duplicate(body: MoveBody, context: Optional[str] = None, space: Any = Depends(current_space)) -> dict:
        root, source, _ = where(space, context, body.source)
        target_root, target, prefix = where(space, context, body.target)
        return _prefixed(await run(copy, root, source, target, target_root), prefix)

    @api.delete("/api/workspace/entry")
    async def remove(path: str, context: Optional[str] = None, space: Any = Depends(current_space)) -> dict:
        root, rest, _ = where(space, context, path)
        await run(delete, root, rest)
        return {"deleted": path}

    @api.get("/api/workspace/search")
    async def find(
        q: str, path: str = "", context: Optional[str] = None, space: Any = Depends(current_space)
    ) -> JSONResponse:
        root, rest, prefix = where(space, context, path)
        result = _prefixed(await run(search, root, rest, q), prefix)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @api.get("/api/workspace/archive")
    async def download_folder(
        path: str = "", context: Optional[str] = None, space: Any = Depends(current_space)
    ) -> StreamingResponse:
        from urllib.parse import quote

        root, rest, prefix = where(space, context, path)
        spool, name = await run(archive, root, rest, prefix or None)

        def chunks() -> Iterator[bytes]:
            while block := spool.read(CHUNK):
                yield block

        return StreamingResponse(
            chunks(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                "Cache-Control": "no-store",
            },
            background=BackgroundTask(spool.close),
        )

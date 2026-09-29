"""Files of a workspace that a container changes at the same time.

On a server with accounts a user's agents write their workspace from a
container, while the file tools read and write it from the server, on the
host. A path that was checked (utils.path_utils) and is then opened can be
changed in between: a directory swapped for a symbolic link to ``/`` makes
the open land anywhere on the host - its files, other users' spaces.

So file tools go through this module. When the run has a container and the
system can open relative to a directory (``dir_fd``, Linux), every directory
of the path is opened from the workspace root down, one name at a time,
never following a symbolic link (``O_NOFOLLOW``); the file itself too.
Whatever the container does meanwhile, nothing is reached outside the
workspace: a symbolic link on the way is refused instead.

Without a container nobody else changes the workspace, and the plain
operations apply - links inside the workspace keep working. Without
``dir_fd`` (Windows) the check of utils.path_utils is what applies; a Linux
container's links do not act on a Windows host's paths.
"""

from __future__ import annotations

import errno
import io
import os
from pathlib import Path
from typing import IO, Any, List, Optional, Tuple

from utils.path_utils import _sandbox_root, get_current_factory

#: Whether this system can walk a path without following links.
SUPPORTED = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")

_MODES = {
    "r": os.O_RDONLY,
    "w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
    "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND,
    "x": os.O_WRONLY | os.O_CREAT | os.O_EXCL,
}


class LinkRefused(PermissionError):
    """A symbolic link on the way, in a workspace a container shares."""

    def __init__(self) -> None:
        super().__init__("Symbolic links are not followed in an isolated workspace")


def _confined(path: Any) -> Optional[Tuple[Path, List[str]]]:
    """(workspace root, names below it) when the walk applies; else None.

    *path* is already resolved inside the workspace (utils.path_utils); a path
    outside it is refused here too.
    """
    factory = get_current_factory()
    if not SUPPORTED or factory is None or not getattr(factory, "container_id", None):
        return None
    root = _sandbox_root(factory)
    try:
        relative = Path(os.path.abspath(path)).relative_to(root)
    except ValueError:
        raise ValueError("Path escapes working directory") from None
    return root, list(relative.parts)


def _give_to_workspace_owner(fd: int, root: Path) -> None:
    """Give what the server made to the owner of the workspace.

    A server running as root makes root's files, and the container's
    unprivileged user - the owner of its workspace (core.managers.
    container_manager) - could then not change them.
    """
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    owner = os.stat(root)
    made = os.fstat(fd)
    if (made.st_uid, made.st_gid) != (owner.st_uid, owner.st_gid):
        os.fchown(fd, owner.st_uid, owner.st_gid)


def _walk(root: Path, names: List[str], *, create: bool) -> int:
    """A descriptor of the directory *names* below *root*, reached without
    following links; missing directories are made when *create*."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in names:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            try:
                next_fd = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, 0o777, dir_fd=fd)
                next_fd = os.open(name, flags, dir_fd=fd)
                try:
                    _give_to_workspace_owner(next_fd, root)
                except BaseException:
                    os.close(next_fd)
                    raise
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise LinkRefused() from None
                raise
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_file(
    path: Any,
    mode: str = "r",
    *,
    encoding: Optional[str] = None,
    errors: Optional[str] = None,
    newline: Optional[str] = None,
) -> IO:
    """``open(path, mode, ...)``, reaching nothing outside the workspace."""
    confined = _confined(path)
    if confined is None:
        return open(path, mode, encoding=encoding, errors=errors, newline=newline)
    root, names = confined
    if not names:
        raise IsADirectoryError(str(path))
    flags = _MODES[mode.replace("b", "").replace("t", "").replace("+", "")]
    if "+" in mode:
        flags = (flags & ~(os.O_RDONLY | os.O_WRONLY)) | os.O_RDWR
    parent = _walk(root, names[:-1], create=False)
    try:
        fd = os.open(names[-1], flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o666, dir_fd=parent)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LinkRefused() from None
        raise
    finally:
        os.close(parent)
    if flags & os.O_CREAT:
        try:
            _give_to_workspace_owner(fd, root)
        except BaseException:
            os.close(fd)
            raise
    if "b" in mode:
        return os.fdopen(fd, mode)
    return os.fdopen(fd, mode, encoding=encoding or "utf-8", errors=errors, newline=newline)


def read_text(path: Any, *, encoding: str = "utf-8", errors: Optional[str] = None) -> str:
    with open_file(path, "r", encoding=encoding, errors=errors) as file:
        return file.read()


def read_bytes(path: Any) -> bytes:
    with open_file(path, "rb") as file:
        return file.read()


def write_text(path: Any, text: str, *, encoding: str = "utf-8", newline: Optional[str] = None) -> None:
    with open_file(path, "w", encoding=encoding, newline=newline) as file:
        file.write(text)


def make_dirs(path: Any) -> None:
    """``Path(path).mkdir(parents=True, exist_ok=True)``, inside the workspace."""
    confined = _confined(path)
    if confined is None:
        Path(path).mkdir(parents=True, exist_ok=True)
        return
    root, names = confined
    os.close(_walk(root, names, create=True))


def unlink(path: Any) -> None:
    """Remove a file; a link is removed itself, never what it points to."""
    confined = _confined(path)
    if confined is None:
        Path(path).unlink()
        return
    root, names = confined
    if not names:
        raise IsADirectoryError(str(path))
    parent = _walk(root, names[:-1], create=False)
    try:
        os.unlink(names[-1], dir_fd=parent)
    finally:
        os.close(parent)


def image_file(path: Any) -> io.BytesIO:
    """An image's bytes, for ``PIL.Image.open``, read inside the workspace."""
    return io.BytesIO(read_bytes(path))

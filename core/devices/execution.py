"""Where a chat's tools act: on the server (the cloud) or on one of the user's devices.

A conversation may be pointed at a device - a user's PC, a phone, a browser
machine - instead of the server's workspace (``execution_target`` in the chat's
metadata: ``"cloud"`` or ``"device:<id>"``). Its base tools then keep their
names, their arguments and their answers, and run on that device:

    file_read   -> fs_read      glob_tool -> fs_glob   bash_tool -> shell_exec
    file_write  -> fs_write     grep_tool -> fs_grep   <package tool> -> tool_exec
    file_edit   -> fs_edit
    file_append -> fs_append

The call goes through the hub's server backend (``DeviceHub.system_call``): the
node's ``fs_*`` methods are not an agent's look/do/run, so the class check does
not apply, while the lease and a person's control still do. Paths are confined
to the device's working folder; a path outside it has already been put to the
user by the action policy (``core.action_policy``) before the tool ran, which is
why ``allow_external`` is passed here.

The node's methods live on the device's own event loop, so a synchronous tool
(one that runs in a tool thread, ``core.tool_threads``) hands the call back to
the server's loop instead of starting one of its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Callable, Dict, Optional, Tuple

from core.devices.hub import CALL_SECONDS, DeviceError, current_holder, hub

logger = logging.getLogger("grid.devices")

#: The target of a chat that works here: the server, its container or workspace.
CLOUD = "cloud"
#: Prefix of a target that names a device: ``device:<device id>``.
DEVICE_PREFIX = "device:"
#: A device id is a plain identifier; anything else names no device.
_DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
#: The platform a device's paths belong to.
WINDOWS, POSIX = "windows", "posix"
#: Seconds a base tool waits for a node's answer beyond the call's own limit.
ANSWER_MARGIN = 15.0


def device_id_of(target: Any) -> Optional[str]:
    """The device id an execution target names, or None for the cloud."""
    if not isinstance(target, str):
        return None
    value = target.strip()
    if not value or value.casefold() == CLOUD:
        return None
    if not value.casefold().startswith(DEVICE_PREFIX):
        return None
    device_id = value[len(DEVICE_PREFIX):].strip()
    return device_id if _DEVICE_ID.match(device_id) else None


def target_for(device_id: str) -> str:
    """The execution target that names *device_id*."""
    return f"{DEVICE_PREFIX}{device_id}"


def platform_of(device: Any) -> str:
    """``windows`` or ``posix``: the path rules the device's node follows."""
    value = str(getattr(device, "platform", "") or "")
    return WINDOWS if "windows" in value.casefold() else POSIX


@dataclass(frozen=True)
class Target:
    """The device a chat works on, and how its tools reach it."""

    device_id: str
    name: str
    platform: str
    #: The folder the device's file and shell tools stay inside.
    workdir: str
    #: The user the device belongs to, and the holder the call is made as.
    user: str
    holder: str

    @property
    def windows(self) -> bool:
        return self.platform == WINDOWS


def _target_of(factory: Any) -> Tuple[str, str]:
    """(target, workdir) the factory's conversation keeps in its metadata."""
    reader = getattr(factory, "execution_target_of", None)
    if callable(reader):
        try:
            target, workdir = reader()
        except Exception:  # noqa: BLE001 - a factory without a context manager
            logger.debug("No execution target on %r", factory, exc_info=True)
            return CLOUD, ""
        return (target if isinstance(target, str) else CLOUD), (workdir if isinstance(workdir, str) else "")
    target = getattr(factory, "execution_target", CLOUD)
    workdir = getattr(factory, "execution_workdir", "")
    return (target if isinstance(target, str) else CLOUD), (workdir if isinstance(workdir, str) else "")


def _conversation(factory: Any) -> str:
    """The chat whose device lease this is: the run's conversation."""
    for name in ("get_active_context_id",):
        reader = getattr(factory, name, None)
        if callable(reader):
            try:
                value = reader()
            except Exception:  # noqa: BLE001 - no conversation: one holder of its own
                value = None
            if isinstance(value, str) and value:
                return value
    return "default"


def user_of(factory: Any) -> str:
    """The user whose devices the factory may use."""
    configured = getattr(factory, "execution_user", None)
    if isinstance(configured, str) and configured:
        return configured
    manager = getattr(factory, "context_manager", None)
    if manager is not None:
        try:
            metadata = manager.get_context_metadata(_conversation(factory)) or {}
        except Exception:  # noqa: BLE001 - metadata is a convenience, never a requirement
            metadata = {}
        stored = metadata.get("user_id")
        if isinstance(stored, str) and stored:
            return stored
    return "local"


def chosen(factory: Any = None) -> Optional[Target]:
    """The device this factory's tools act on; None when the chat works here.

    Raises :class:`DeviceError` when the chat is pointed at a device that is not
    connected: a write meant for the user's PC must fail, never land on the
    server's disk.
    """
    if factory is None:
        from utils.path_utils import get_current_factory

        factory = get_current_factory()
    if factory is None:
        return None
    target_value, workdir = _target_of(factory)
    device_id = device_id_of(target_value)
    if device_id is None:
        return None
    user = user_of(factory)
    device = hub().get(user, device_id)
    workdir = (workdir.strip() or str(getattr(device, "default_workdir", "") or "")).strip()
    return Target(
        device_id=device_id,
        name=str(getattr(device, "name", "") or device_id),
        platform=platform_of(device),
        workdir=workdir,
        user=user,
        holder=current_holder(_conversation(factory)),
    )


def with_target(target: Target, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The call's own arguments, with the device's folder and its freedom of path.

    ``allow_external`` is true because a path outside the working folder has
    already been put to the user by the action policy: the node is not the
    place to ask a second time, and refusing here would lose an approval.
    """
    payload: Dict[str, Any] = dict(arguments or {})
    if target.workdir:
        payload["workdir"] = target.workdir
    payload["allow_external"] = True
    return payload


def as_text(result: Any) -> str:
    """A node's MCP answer as the text a base tool shows the agent."""
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        kind = getattr(item, "type", None)
        if kind == "text":
            parts.append(getattr(item, "text", "") or "")
        elif kind == "resource" and getattr(item.resource, "text", None) is not None:
            parts.append(item.resource.text)
        else:
            parts.append(f"[{kind} content from the device]")
    text = "\n".join(part for part in parts if part)
    if getattr(result, "isError", False):
        raise DeviceError(text.strip() or "the device reported an error")
    return text


async def call_in_device(
    target: Target, method: str, arguments: Optional[Dict[str, Any]] = None, *, timeout: float = CALL_SECONDS
) -> str:
    """Call a system method of the device (``fs_read``, ``shell_exec``, ...); its text."""
    try:
        result = await hub().system_call(
            target.user,
            target.device_id,
            method,
            with_target(target, arguments),
            holder=target.holder,
            timeout=timeout,
        )
    except DeviceError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the node's failure is the agent's to read
        raise DeviceError(f"{target.name}: {exc}") from exc
    return as_text(result)


def _run_on_server_loop(coro: Any, timeout: Optional[float] = None) -> Any:
    """Run *coro* where the device's session lives, from a synchronous tool.

    A tool written as a plain function runs in a tool thread
    (``core.tool_threads``), which carries the server's loop with it: the call
    goes back there rather than to a loop of its own, because the node's MCP
    session belongs to the server's.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        from core.tool_threads import server_loop

        loop = server_loop()
        if loop is not None and loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)
        return asyncio.run(coro)
    coro.close()
    raise DeviceError(
        "a device call cannot be made from the server's event loop: the tool must run off it"
    )


def call(
    target: Target, method: str, arguments: Optional[Dict[str, Any]] = None, *, timeout: float = CALL_SECONDS
) -> str:
    """:func:`call_in_device` for a synchronous tool."""
    return _run_on_server_loop(call_in_device(target, method, arguments, timeout=timeout), timeout + ANSWER_MARGIN)


# -- paths on the device ------------------------------------------------------------------
def locate_within(path: Any, workdir: str, platform: str) -> Optional[str]:
    """*path* as a path relative to *workdir*, or None when it is outside it.

    Windows devices are read with ``PureWindowsPath`` (drive letters, case
    insensitive, backslashes) and everything else with ``PurePosixPath``, so a
    path means on the device what it means to the device's own tools. None is a
    path outside the working folder - through ``..``, another drive or another
    root - which the action policy routes as an outside action.
    """
    if not isinstance(path, str) or not path.strip():
        return None
    text = path.strip()
    windows = platform == WINDOWS
    path_type = PureWindowsPath if windows else PurePosixPath
    if not windows and (re.match(r"^[A-Za-z]:", text) or text.startswith("//")):
        # Another machine's drive or share is never a file of this folder.
        return None
    candidate = path_type(text)
    if candidate.drive and not candidate.is_absolute():
        # "C:relative-file" means something different in every shell.
        return None
    if candidate.is_absolute():
        base = path_type(workdir.strip()) if workdir else None
        if base is None or not base.is_absolute():
            return None
        try:
            relative = candidate.relative_to(base)
        except ValueError:
            return None
        return relative.as_posix() or "."
    normalized = posixpath.normpath(candidate.as_posix())
    if normalized in ("", "."):
        return "."
    if normalized == ".." or normalized.startswith("../") or normalized.startswith("/"):
        return None
    return PurePosixPath(normalized).as_posix()


def relative_locator(target: Target) -> Callable[[str], Optional[str]]:
    """A policy locator for a chat that works on *target* (core.action_policy)."""
    return lambda raw: locate_within(raw, target.workdir, target.platform)


def platform_label(platform: str) -> str:
    """How the prompt names the device's system."""
    return "Windows" if platform == WINDOWS else "Linux/macOS"


def arguments_of(raw: Any) -> Dict[str, Any]:
    """A tool call's arguments as a dict: the package runner hands JSON text or a dict."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


__all__ = [
    "ANSWER_MARGIN",
    "CLOUD",
    "DEVICE_PREFIX",
    "DeviceError",
    "POSIX",
    "Target",
    "WINDOWS",
    "arguments_of",
    "as_text",
    "call",
    "call_in_device",
    "chosen",
    "device_id_of",
    "locate_within",
    "platform_label",
    "platform_of",
    "relative_locator",
    "target_for",
    "user_of",
    "with_target",
]

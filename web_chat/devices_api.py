"""The devices page's API: browser machines, paired devices, the live view and taking control.

For a signed-in user (the ``api`` router):

    GET    /api/devices                        connected devices, paired devices, machines
    GET    /api/devices/{id}/dirs              the directory to open: dirs, roots, bookmarks
    POST   /api/devices/{id}/dirs              {"path", "mkdir"}: make a folder, list it back
    POST   /api/devices/machines               start a browser machine
    DELETE /api/devices/machines/{id}          stop one
    POST   /api/devices/{id}/control           {"taken": true|false}: take control or give it back
    WS     /api/devices/{id}/view              frames of the screen; the person's input when in control
    POST   /api/devices/pairing                a one-time code to pair a device of one's own
    DELETE /api/devices/paired/{id}            forget a paired device

For a device's node, which has a code or a token and no session - outside /api,
which is for signed-in users only:

    POST   /node/pair                          {"code", "name", "kind"} -> {"device", "token"}
    WS     /node/connect                       Authorization: Bearer <token>; the device's tunnel
    GET    /node/tools/{pkg}/{hash}.tar.gz     Authorization: Bearer <token>; a tool package
    GET    /node/install.sh                    the installer a device fetches
    GET    /node/install.ps1                   the same for Windows
    GET    /node/bin/{target}                  a compiled grid-node binary
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import logging
import re
import tarfile
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
)
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from core.devices.hub import DeviceError, DeviceHub
from core.devices.machines import MachineError, MachinePool
from core.devices.pairing import PairingStore
from grid_node.tunnel import Tunnel
from web_chat.accounts.service import SessionRevoked

logger = logging.getLogger("grid.web.devices")

#: How often an idle live view revalidates the session that opened it. Every
#: input event revalidates too; this covers a viewer that only watches.
VIEW_REVALIDATE_SECONDS = 30.0


async def _revalidated(current_user: Callable[..., Any], websocket: WebSocket, user: Any) -> Any:
    """The user *websocket* acts for now, or :class:`SessionRevoked`.

    The routes already depend on *current_user* at the handshake; asking it
    again reads the session cookie again, so sign-out, expiry or a disabled
    account stops a view that is already open. A refusal is the server's
    ``WebSocketException`` (1008); here it becomes :class:`SessionRevoked`.
    """
    try:
        authorized = await current_user(websocket)
    except WebSocketException as exc:
        raise SessionRevoked(str(exc.reason or "The session ended")) from None
    except Exception as exc:
        raise SessionRevoked("Session authorization could not be verified") from exc
    if getattr(authorized, "id", None) != getattr(user, "id", None):
        raise SessionRevoked("The session no longer belongs to this user")
    return authorized


async def _watch_view(current_user: Callable[..., Any], websocket: WebSocket, user: Any) -> None:
    """Close an idle live view once its session ends."""
    while True:
        await asyncio.sleep(VIEW_REVALIDATE_SECONDS)
        try:
            await _revalidated(current_user, websocket, user)
        except SessionRevoked:
            logger.info("Closing device view %s: its session ended", user.id)
            with suppress(RuntimeError, OSError):
                await websocket.close(code=1008, reason="Session ended")
            return


class _Control(BaseModel):
    taken: bool


class _Pair(BaseModel):
    code: str = Field(min_length=4, max_length=40)
    name: str = Field(default="", max_length=80)
    kind: str = Field(default="desktop", pattern="^(desktop|mobile|browser)$")


class _Dirs(BaseModel):
    path: str = Field(default="", max_length=4096)
    mkdir: str = Field(default="", max_length=4096)


class _FastAPISocket:
    """A FastAPI WebSocket as the tunnel's socket (grid_node.tunnel.Socket)."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket

    async def send_text(self, text: str) -> None:
        await self.websocket.send_text(text)

    async def receive_text(self) -> str:
        return await self.websocket.receive_text()

    async def close(self, code: int = 1000) -> None:
        await self.websocket.close(code)


def _bearer(connection: Any) -> str:
    """The device token a WebSocket or an HTTP request carries, if any."""
    value = connection.headers.get("authorization") or ""
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


#: The system method a node answers a directory listing with (grid_node's fs tools).
LIST_DIRS = "fs_list_dirs"

#: A tool package's name: one directory under a ``tools/`` root, nothing else.
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: The binaries this server hands out, by the name the installer asks for.
NODE_TARGETS = ("grid-node-linux-amd64", "grid-node-linux-arm64", "grid-node-windows-amd64.exe")


def server_holder(user_id: str) -> str:
    """The holder the server's own backend calls a device under.

    It is not a conversation's, so it never waits on nor takes an agent's turn
    in the same device; its lease is given back right after the call
    (:meth:`DeviceHub.release`), so an agent finds the device free.
    """
    return f"web/{user_id}"


def _result_json(result: Any) -> Any:
    """What a node's system method answered: the JSON its text holds, or the text itself."""
    parts: List[str] = []
    for part in getattr(result, "content", None) or []:
        text = getattr(part, "text", None)
        if text is not None:
            parts.append(str(text))
    joined = "\n".join(parts).strip()
    if not joined:
        return {}
    try:
        return json.loads(joined)
    except ValueError:
        return {"text": joined}


def _dir_entry(entry: Any) -> Dict[str, Any]:
    """One folder of a listing, whichever shape the node spelled it in."""
    if isinstance(entry, str):
        name = entry.rstrip("/").rsplit("/", 1)[-1] or entry
        return {"name": name, "path": entry}
    if isinstance(entry, dict):
        path = str(entry.get("path") or entry.get("dir") or "")
        name = str(entry.get("name") or path.rstrip("/").rsplit("/", 1)[-1] or path)
        return {**{k: v for k, v in entry.items() if k not in ("name", "path")}, "name": name, "path": path}
    return {"name": str(entry), "path": str(entry)}


def _dirs_answer(device: Any, path: str, asked: str, result: Any) -> Dict[str, Any]:
    """The Devices page's answer: where it is, what is below it, and where one may go."""
    answer = _result_json(result)
    if not isinstance(answer, dict):
        answer = {"entries": answer}
    roots = answer.get("roots") or answer.get("disks") or []
    listing = {
        "device": device.id,
        "path": str(answer.get("path") or path or device.default_workdir),
        "workdir": str(answer.get("workdir") or device.default_workdir),
        "roots": [str(root) if isinstance(root, str) else _dir_entry(root) for root in roots],
        "dirs": [_dir_entry(entry) for entry in (answer.get("dirs") or answer.get("entries") or [])],
        "bookmarks": [str(bookmark) if isinstance(bookmark, str) else _dir_entry(bookmark)
                      for bookmark in (answer.get("bookmarks") or [])],
    }
    if answer.get("parent") is not None:
        listing["parent"] = str(answer["parent"])
    if asked:
        listing["created"] = asked
    return listing


def packages_root() -> Path:
    """Where the tool packages of this server live: the repository it runs from."""
    return Path(__file__).resolve().parents[1]


def tool_package(name: str, *, root: Optional[Path] = None) -> Optional[Path]:
    """The directory of tool package *name*, wherever this server keeps its tools."""
    if not PACKAGE_NAME.match(name or ""):
        return None
    base = root if root is not None else packages_root()
    for pattern in (f"examples/*/tools/{name}", f"systems/*/tools/{name}", f"tools/{name}"):
        for candidate in sorted(base.glob(pattern)):
            if candidate.is_dir():
                return candidate
    return None


def tool_package_archive(directory: Path) -> tuple[bytes, str]:
    """The ``.tar.gz`` of a tool package and its sha256.

    The same tree always gives the same bytes - times, owners and modes are
    fixed and the gzip header carries no name - so the hash in the package's
    URL names its content and a device can check what it downloaded.
    """
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as packed:
        with tarfile.open(fileobj=packed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(directory.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                info = archive.gettarinfo(str(path), arcname=str(path.relative_to(directory.parent)))
                info.mtime, info.uid, info.gid = 0, 0, 0
                info.uname = info.gname = ""
                info.mode = 0o755 if info.mode & 0o111 else 0o644
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def binaries_roots() -> List[Path]:
    """Where this server keeps the compiled grid-node binaries."""
    base = packages_root()
    return [base / "dist" / "grid-node", base / "grid_node" / "bin"]


def node_binary(target: str) -> Optional[Path]:
    """The binary *target* names, if this server has it."""
    if target not in NODE_TARGETS:
        return None
    for root in binaries_roots():
        candidate = root / target
        if candidate.is_file():
            return candidate
    return None


def server_url(request: Request) -> str:
    """The address a device reaches this server at, as the installer should use it."""
    return str(request.base_url).rstrip("/")


#: The installer a Linux device fetches; ``@@SERVER@@`` and ``@@CODE@@`` are
#: filled in per request, so the script already knows where it came from.
INSTALL_SH = """#!/bin/sh
# Grid device installer: fetches grid-node for this machine and pairs it.
#
#     curl -fsSL @@SERVER@@/node/install.sh | sh -s -- ABCDE-FGHJK
#
# The pairing code may also come from GRID_PAIR_CODE, and the server (behind a
# proxy, say) from GRID_SERVER.
set -eu

SERVER="${GRID_SERVER:-@@SERVER@@}"
CODE="${1:-${GRID_PAIR_CODE:-@@CODE@@}}"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64|Linux-amd64) TARGET=grid-node-linux-amd64 ;;
  Linux-aarch64|Linux-arm64) TARGET=grid-node-linux-arm64 ;;
  *)
    echo "grid-node: this server has no build for $(uname -s) $(uname -m); see $SERVER/node/bin/" >&2
    exit 1
    ;;
esac

BIN_DIR="${GRID_NODE_HOME:-$HOME/.grid-node}/bin"
mkdir -p "$BIN_DIR"
URL="$SERVER/node/bin/$TARGET"
TMP="$BIN_DIR/grid-node.tmp"
echo "grid-node: downloading $URL"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$URL" -o "$TMP"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$TMP" "$URL"
else
  echo "grid-node: neither curl nor wget is installed" >&2
  exit 1
fi
chmod +x "$TMP"
mv "$TMP" "$BIN_DIR/grid-node"

if [ -n "$CODE" ]; then
  exec "$BIN_DIR/grid-node" install --server "$SERVER" --pair "$CODE"
fi
exec "$BIN_DIR/grid-node" install --server "$SERVER"
"""

#: The same installer for Windows (PowerShell).
INSTALL_PS1 = """# Grid device installer: fetches grid-node.exe and pairs this computer.
#
#     iwr @@SERVER@@/node/install.ps1 -UseBasicParsing | iex
#
# Name the pairing code as an argument or in GRID_PAIR_CODE, and the server
# (behind a proxy, say) in GRID_SERVER.
$ErrorActionPreference = "Stop"

$Server = if ($env:GRID_SERVER) { $env:GRID_SERVER } else { "@@SERVER@@" }
if ($args.Count -ge 1) { $Code = $args[0] } elseif ($env:GRID_PAIR_CODE) { $Code = $env:GRID_PAIR_CODE } else { $Code = "@@CODE@@" }

$Target = "grid-node-windows-amd64.exe"
$Dir = Join-Path $env:LOCALAPPDATA "grid-node"
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$Bin = Join-Path $Dir "grid-node.exe"
$Url = "$Server/node/bin/$Target"
Write-Host "grid-node: downloading $Url"
Invoke-WebRequest -Uri $Url -OutFile $Bin -UseBasicParsing

$Install = @("install", "--server", $Server)
if ($Code) { $Install += @("--pair", $Code) }
& $Bin @Install
"""

#: What a pairing code may be: the alphabet the server hands out, nothing else.
PAIRING_CODE = re.compile(r"^[A-Za-z0-9-]{4,40}$")


def pairing_code_wanted(code: str) -> str:
    """The pairing code an installer should start with, or nothing if it is not one."""
    code = (code or "").strip()
    return code if PAIRING_CODE.match(code) else ""


def installer_script(template: str, request: Request, code: str = "") -> str:
    """An installer with this server's address (and a code, if one was named) filled in."""
    return template.replace("@@SERVER@@", server_url(request)).replace("@@CODE@@", pairing_code_wanted(code))


def register_device_routes(
    app: FastAPI,
    api: APIRouter,
    current_user: Callable[..., Any],
    *,
    hub: DeviceHub,
    machines: Optional[MachinePool],
    pairing: Optional[PairingStore],
) -> None:
    @api.get("/api/devices")
    async def list_devices(user: Any = Depends(current_user)) -> Dict[str, Any]:
        connected = {device.id: device for device in hub.devices(user.id)}
        paired = pairing.devices(user.id) if pairing is not None else []
        return {
            "devices": [
                {**device.summary(), "origin": device.origin, "view": device.view, "person": device.person}
                for device in connected.values()
            ],
            "paired": [
                {"device": p.id, "name": p.name, "kind": p.kind, "online": p.id in connected, "last_seen": p.last_seen}
                for p in paired
            ],
            "machines": machines.capacity(user.id) if machines is not None else None,
            "pairing": pairing is not None,
        }

    @api.post("/api/devices/machines")
    async def start_machine(user: Any = Depends(current_user)) -> Dict[str, Any]:
        if machines is None:
            raise HTTPException(status_code=404, detail="This server starts no browser machines.")
        try:
            return {"device": await machines.start(user.id)}
        except MachineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.delete("/api/devices/machines/{device_id}")
    async def stop_machine(device_id: str, user: Any = Depends(current_user)) -> Dict[str, Any]:
        if machines is None:
            raise HTTPException(status_code=404, detail="This server starts no browser machines.")
        try:
            await machines.stop(user.id, device_id)
        except MachineError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"stopped": device_id}

    @api.post("/api/devices/{device_id}/control")
    async def control(device_id: str, body: _Control, user: Any = Depends(current_user)) -> Dict[str, Any]:
        try:
            await hub.take_control(user.id, device_id, body.taken)
        except DeviceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"device": device_id, "person": body.taken}

    async def list_dirs_of(user_id: str, device_id: str, path: str, mkdir: str) -> Dict[str, Any]:
        """Ask the device's node what is in a directory, and let its lease go."""
        try:
            device = hub.get(user_id, device_id)
        except DeviceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        where = path or device.default_workdir
        if device.tools.get(LIST_DIRS) is None:
            # A node that lists no directories still tells where it works: the
            # page opens there and offers to type a path.
            return {
                "device": device.id,
                "path": where,
                "workdir": device.default_workdir,
                "roots": [],
                "dirs": [],
                "bookmarks": [],
                "detail": "This device lists no directories: type a path instead.",
            }
        asked: Dict[str, Any] = {}
        if where:
            asked["path"] = where
        if mkdir:
            asked["mkdir"] = mkdir
        holder = server_holder(user_id)
        try:
            result = await hub.system_call(user_id, device_id, LIST_DIRS, asked, holder=holder)
        except DeviceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            # The server's own call is over: an agent's turn finds the device free.
            hub.release(holder)
        return _dirs_answer(device, where, mkdir, result)

    @api.get("/api/devices/{device_id}/dirs")
    async def list_dirs(
        device_id: str, path: str = "", mkdir: str = "", user: Any = Depends(current_user)
    ) -> Dict[str, Any]:
        return await list_dirs_of(user.id, device_id, path, mkdir)

    @api.post("/api/devices/{device_id}/dirs")
    async def make_dir(device_id: str, body: _Dirs, user: Any = Depends(current_user)) -> Dict[str, Any]:
        # Making a folder is the same call with ``mkdir`` set: the node makes it
        # and answers with the listing of the directory it is in.
        return await list_dirs_of(user.id, device_id, body.path, body.mkdir)

    @api.websocket("/api/devices/{device_id}/view")
    async def view(websocket: WebSocket, device_id: str, user: Any = Depends(current_user)) -> None:
        await websocket.accept()

        async def sink(frame: Dict[str, Any]) -> None:
            await websocket.send_json(frame)

        watcher = asyncio.create_task(_watch_view(current_user, websocket, user), name="device-view-watch")
        try:
            async with hub.watching(user.id, device_id, sink):
                while True:
                    event = await websocket.receive_json()
                    # The view may outlive the session that opened it: the
                    # cookie is read again before the person's input is passed on.
                    try:
                        await _revalidated(current_user, websocket, user)
                    except SessionRevoked:
                        await websocket.close(code=1008, reason="Session ended")
                        return
                    if isinstance(event, dict) and event.get("type") == "input":
                        try:
                            await hub.person_input(user.id, device_id, event)
                        except DeviceError as exc:
                            await websocket.send_json({"type": "error", "detail": str(exc)})
        except DeviceError as exc:
            await websocket.send_json({"type": "error", "detail": str(exc)})
            await websocket.close(4404)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            # The viewer is gone - a closed tab, a dropped socket, an ended
            # session - so the person's control goes with it, whatever way the
            # block above left: an agent must not find the device locked by a
            # hand nobody can see. (hub.watching gives it back when the last
            # viewer of a device goes; this covers the view's own ending.)
            release = getattr(hub, "release_control", None)
            if release is not None:
                with suppress(DeviceError):
                    await release(user.id, device_id)

    @api.post("/api/devices/pairing")
    async def new_pairing(user: Any = Depends(current_user)) -> Dict[str, Any]:
        if pairing is None:
            raise HTTPException(status_code=404, detail="This server pairs no devices.")
        code, expires = pairing.new_code(user.id)
        return {"code": f"{code[:5]}-{code[5:]}", "expires_at": expires}

    @api.delete("/api/devices/paired/{device_id}")
    async def forget(device_id: str, user: Any = Depends(current_user)) -> Dict[str, Any]:
        if pairing is None or not pairing.revoke(user.id, device_id):
            raise HTTPException(status_code=404, detail="No such paired device.")
        for device in hub.devices(user.id):
            if device.id == device_id:
                await device.tunnel.close("unpaired")
        return {"forgotten": device_id}

    # -- the device's side: no cookie, a code or a token ------------------------

    @app.post("/node/pair")
    async def pair(body: _Pair) -> Dict[str, Any]:
        if pairing is None:
            raise HTTPException(status_code=404, detail="This server pairs no devices.")
        redeemed = await asyncio.to_thread(pairing.redeem, body.code, body.name, body.kind)
        if redeemed is None:
            raise HTTPException(status_code=403, detail="That code is wrong, used or expired; ask the web chat for a new one.")
        device, token = redeemed
        return {"device": device.id, "token": token}

    @app.websocket("/node/connect")
    async def connect(websocket: WebSocket) -> None:
        device = await asyncio.to_thread(pairing.verify, _bearer(websocket)) if pairing is not None else None
        if device is None:
            await websocket.close(4401)
            return
        await websocket.accept()
        try:
            await hub.serve(Tunnel(_FastAPISocket(websocket)), device_id=device.id, user_id=device.user_id, origin="paired")
        except Exception:
            logger.debug("Device %s connection ended", device.id, exc_info=True)

    @app.get("/node/tools/{package_name}/{package_hash}.tar.gz")
    async def tool_package_archive_route(package_name: str, package_hash: str, request: Request) -> Response:
        """A tool package, to a device that has paired: the ``.tar.gz`` its hash names."""
        if pairing is None:
            raise HTTPException(status_code=404, detail="This server pairs no devices.")
        device = await asyncio.to_thread(pairing.verify, _bearer(request))
        if device is None:
            raise HTTPException(status_code=401, detail="Pair this device first: a paired device's token is needed.")
        directory = tool_package(package_name)
        if directory is None:
            raise HTTPException(status_code=404, detail=f"No tool package {package_name} on this server.")
        data, digest = await asyncio.to_thread(tool_package_archive, directory)
        wanted = (package_hash or "").lower()
        # The hash in the URL names the content a device is about to run; a
        # shorter prefix is enough to ask for it, "latest" takes what is here.
        if wanted != "latest" and wanted != digest and not (len(wanted) >= 8 and digest.startswith(wanted)):
            raise HTTPException(
                status_code=409, detail=f"{package_name} on this server is {digest}, not {package_hash}."
            )
        logger.info("Device %s takes tool package %s (%s)", device.id, package_name, digest[:12])
        return Response(
            data,
            media_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="{directory.name}.tar.gz"',
                "X-Package-Sha256": digest,
                "Cache-Control": "no-store",
            },
        )

    @app.get("/node/install.sh", response_class=PlainTextResponse)
    async def install_sh(request: Request, code: str = "") -> PlainTextResponse:
        """The installer a Linux or macOS device runs: it fetches the binary and pairs."""
        return PlainTextResponse(
            installer_script(INSTALL_SH, request, code),
            media_type="text/x-shellscript",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/node/install.ps1", response_class=PlainTextResponse)
    async def install_ps1(request: Request, code: str = "") -> PlainTextResponse:
        """The same installer for Windows."""
        return PlainTextResponse(
            installer_script(INSTALL_PS1, request, code),
            media_type="text/plain",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/node/bin/{target}")
    async def node_bin(target: str) -> FileResponse:
        """A compiled grid-node binary, by the name the installer asks for."""
        path = node_binary(target)
        if path is None:
            raise HTTPException(status_code=404, detail=f"This server has no {target} yet.")
        return FileResponse(path, media_type="application/octet-stream", filename=target)

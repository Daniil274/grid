"""The devices page's API: browser machines, paired devices, the live view and taking control.

For a signed-in user (the ``api`` router):

    GET    /api/devices                        connected devices, paired devices, machines
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
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from core.devices.hub import DeviceError, DeviceHub
from core.devices.machines import MachineError, MachinePool
from core.devices.pairing import PairingStore
from grid_node.tunnel import Tunnel

logger = logging.getLogger("grid.web.devices")


class _Control(BaseModel):
    taken: bool


class _Pair(BaseModel):
    code: str = Field(min_length=4, max_length=40)
    name: str = Field(default="", max_length=80)
    kind: str = Field(default="desktop", pattern="^(desktop|mobile|browser)$")


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


def _bearer(websocket: WebSocket) -> str:
    value = websocket.headers.get("authorization") or ""
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


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

    @api.websocket("/api/devices/{device_id}/view")
    async def view(websocket: WebSocket, device_id: str, user: Any = Depends(current_user)) -> None:
        await websocket.accept()

        async def sink(frame: Dict[str, Any]) -> None:
            await websocket.send_json(frame)

        try:
            async with hub.watching(user.id, device_id, sink):
                while True:
                    event = await websocket.receive_json()
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

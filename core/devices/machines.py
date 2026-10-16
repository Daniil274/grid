"""Browser machines on the server: a container each, started for a user and served through the hub.

A machine is a container of the ``grid-browser-machine`` image (a Chromium on a
virtual display and a node, grid_node) on a network of its own: machines
reach the internet, not each other, and the host's services listen on
loopback only. The server connects to the machine's node with a token made
for it alone and holds it as one of the user's devices (core.devices.hub).

Each user's machines use profile slots - a Docker volume per slot - so the
logins made in a machine are there in the next one; parallel machines of one
user take different slots, since a Chromium profile is one browser's. A
machine nobody calls or watches for ``idle_seconds`` is stopped; the server
stops the machines of a former process when it starts.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.devices.hub import DeviceHub
from grid_node.tunnel import Tunnel, WebsocketsSocket

logger = logging.getLogger("grid.devices.machines")

IMAGE = "grid-browser-machine:local"
NETWORK = "grid-machines"
LABEL = "grid.machine"
NODE_PORT = 7700
#: Seconds a new machine gets to start its browser and connect.
START_SECONDS = 90


class MachineError(Exception):
    """A machine that cannot be started or stopped; its text is for the user or agent."""


@dataclass
class Machine:
    device_id: str
    user_id: str
    slot: int
    container_id: str
    address: str
    token: str
    started_at: float = field(default_factory=time.time)
    stopping: bool = False
    connector: Optional[asyncio.Task] = None


def _safe(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", value)[:40]


class MachinePool:
    """The browser machines of this server."""

    def __init__(
        self,
        hub: DeviceHub,
        *,
        image: str = IMAGE,
        limit: int = 4,
        per_user: int = 2,
        idle_seconds: int = 600,
        memory: str = "1200m",
        cpus: float = 1.0,
        timezone: str = "UTC",
        locale: str = "",
        client: Any = None,
        node_port: int = NODE_PORT,
    ) -> None:
        self.hub = hub
        self.image = image
        self.limit = limit
        self.per_user = per_user
        self.idle_seconds = idle_seconds
        self.memory = memory
        self.cpus = cpus
        self.timezone = timezone
        self.locale = locale
        self._client = client
        self.node_port = node_port
        self._machines: Dict[str, Machine] = {}
        self._starting = asyncio.Lock()
        self._reaper: Optional[asyncio.Task] = None

    @property
    def client(self) -> Any:
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    # -- what there is ----------------------------------------------------------

    def machines(self, user_id: str) -> List[Machine]:
        return [m for m in self._machines.values() if m.user_id == user_id]

    def capacity(self, user_id: str) -> Dict[str, Any]:
        return {
            "yours": [m.device_id for m in self.machines(user_id)],
            "you_may_start": max(0, min(self.per_user - len(self.machines(user_id)), self.limit - len(self._machines))),
        }

    # -- starting and stopping ---------------------------------------------------

    async def start(self, user_id: str) -> str:
        """Start a machine for *user_id* and return its device id once it is connected."""
        async with self._starting:
            mine = self.machines(user_id)
            if len(mine) >= self.per_user:
                raise MachineError(
                    f"You have {len(mine)} browser machines running, the most one user may; stop one first."
                )
            if len(self._machines) >= self.limit:
                raise MachineError("Every browser machine of this server is in use; try again in a few minutes.")
            slot = min(set(range(self.per_user)) - {m.slot for m in mine})
            device_id = f"browser-{secrets.token_hex(4)}"
            token = secrets.token_urlsafe(32)
            container_id, address = await asyncio.to_thread(self._run, user_id, device_id, slot, token)
            machine = Machine(device_id, user_id, slot, container_id, address, token)
            self._machines[device_id] = machine
        machine.connector = asyncio.create_task(self._connect(machine), name=f"machine:{device_id}")
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            if any(d.id == device_id for d in self.hub.devices(user_id)):
                logger.info("Browser machine %s of %s is up", device_id, user_id)
                self._ensure_reaper()
                return device_id
            await asyncio.sleep(0.5)
        await self.stop(user_id, device_id)
        raise MachineError("The browser machine did not start in time; try again.")

    def _run(self, user_id: str, device_id: str, slot: int, token: str) -> tuple[str, str]:
        client = self.client
        self._ensure_network(client)
        environment = {"NODE_TOKEN": token, "NODE_NAME": f"Browser {slot + 1}", "TZ": self.timezone}
        if self.locale:
            environment["NODE_LOCALE"] = self.locale
        container = client.containers.run(
            self.image,
            name=f"grid-machine-{device_id}",
            detach=True,
            auto_remove=True,
            network=NETWORK,
            environment=environment,
            volumes={f"grid-browser-{_safe(user_id)}-{slot}": {"bind": "/home/node/profile", "mode": "rw"}},
            labels={LABEL: "1", "grid.user": user_id, "grid.device": device_id},
            mem_limit=self.memory,
            memswap_limit=self.memory,
            nano_cpus=int(self.cpus * 1e9),
            shm_size="1g",
            pids_limit=1024,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
        )
        container.reload()
        address = container.attrs["NetworkSettings"]["Networks"][NETWORK]["IPAddress"]
        return container.id, address

    @staticmethod
    def _ensure_network(client: Any) -> None:
        if not client.networks.list(names=[NETWORK]):
            # Machines reach the internet, not one another.
            client.networks.create(NETWORK, driver="bridge", options={"com.docker.network.bridge.enable_icc": "false"})

    async def _connect(self, machine: Machine) -> None:
        """Hold the machine's node as a device of its user, connecting again after a drop."""
        import websockets

        url = f"ws://{machine.address}:{self.node_port}"
        while not machine.stopping:
            try:
                async with websockets.connect(
                    url, additional_headers={"Authorization": f"Bearer {machine.token}"}, max_size=None, open_timeout=5,
                ) as connection:
                    await self.hub.serve(
                        Tunnel(WebsocketsSocket(connection)),
                        device_id=machine.device_id, user_id=machine.user_id, origin="machine",
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Machine %s not reachable yet: %s", machine.device_id, exc)
            if not machine.stopping:
                await asyncio.sleep(1.0)

    async def stop(self, user_id: str, device_id: str) -> None:
        machine = self._machines.get(device_id)
        if machine is None or machine.user_id != user_id:
            raise MachineError(f"You have no browser machine {device_id}.")
        await self._stop(machine)

    async def _stop(self, machine: Machine) -> None:
        machine.stopping = True
        self._machines.pop(machine.device_id, None)
        if machine.connector is not None:
            machine.connector.cancel()
        await asyncio.to_thread(self._remove, machine.container_id)
        logger.info("Browser machine %s of %s stopped", machine.device_id, machine.user_id)

    def _remove(self, container_id: str) -> None:
        try:
            container = self.client.containers.get(container_id)
            container.stop(timeout=5)
        except Exception as exc:  # already gone: auto_remove took it
            logger.debug("Machine container %s: %s", container_id, exc)

    # -- upkeep -----------------------------------------------------------------

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_forever(), name="machine-reaper")

    async def _reap_forever(self) -> None:
        while self._machines:
            await asyncio.sleep(30)
            await self.reap()

    async def reap(self) -> List[str]:
        """Stop the machines nobody called or watched for ``idle_seconds``; their ids."""
        now = time.time()
        idle = []
        for machine in list(self._machines.values()):
            if self.hub.in_use(machine.device_id):
                continue
            last = self.hub.last_used(machine.device_id) or machine.started_at
            if now - last > self.idle_seconds:
                idle.append(machine)
        for machine in idle:
            await self._stop(machine)
        return [m.device_id for m in idle]

    async def prune_orphans(self) -> int:
        """Stop machines a former server process left running."""

        def prune() -> int:
            stale = self.client.containers.list(filters={"label": LABEL})
            for container in stale:
                try:
                    container.stop(timeout=5)
                except Exception:
                    logger.debug("Could not stop orphan machine %s", container.name, exc_info=True)
            return len(stale)

        try:
            return await asyncio.to_thread(prune)
        except Exception:
            logger.warning("Could not look for orphan browser machines", exc_info=True)
            return 0

    async def shutdown(self) -> None:
        for machine in list(self._machines.values()):
            await self._stop(machine)
        if self._reaper is not None:
            self._reaper.cancel()


_pool: Optional[MachinePool] = None


def pool() -> Optional[MachinePool]:
    """The machine pool of this process; None where the server starts no machines."""
    return _pool


def set_pool(machines: Optional[MachinePool]) -> None:
    global _pool
    _pool = machines

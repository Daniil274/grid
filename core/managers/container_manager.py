"""
Container Manager for agent isolation using Docker.

Every user's containers run on a bridge network of their own, made here: two
users share no network, so one user's agents cannot open a connection to
another user's container, nor find its ports. Egress to the internet - the
model providers - goes out through that network's own NAT; a proxy the server
names by its loopback is renamed to the Docker host gateway for the container
(:func:`proxy_environment`).

What the network does *not* do, without infrastructure this module does not
touch:

* the host stays reachable wherever a bridge can reach it at all: a service
  bound to the Docker gateway address (``docker0``, the host's side of the
  bridge) or to ``0.0.0.0`` - and so every port published on the host - is one
  connection away. Docker offers no per-network switch that drops that route;
  closing it needs firewall rules on the host, or an ``internal`` network with
  an explicit egress proxy as the only way out. Services bound to the host's
  loopback alone are, unlike under ``network_mode="host"``, out of reach.
* the cloud metadata endpoint (169.254.169.254) is such a route on a host that
  has one; the same firewall closes it.
* nothing here publishes a port of a tenant container, and nothing here could
  stop Docker from doing so: publishing is a firewall question too.

These are the remaining boundaries recorded in the security audit; the code
below keeps them explicit rather than claiming a tenant-to-host guarantee it
cannot give.
"""

import hashlib
import json
import logging
import os
import re
import stat as stat_module
import sys
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from urllib.parse import urlsplit, urlunsplit
try:
    import docker
    from docker.errors import NotFound, DockerException
    from docker.models.containers import Container
except ImportError:  # pragma: no cover - environment-dependent
    docker = None

    class DockerException(Exception):
        """Fallback docker exception when docker-py is unavailable."""

    class NotFound(DockerException):
        """Fallback not-found exception when docker-py is unavailable."""

    class Container:  # type: ignore[override]
        """Fallback container type used only for typing when docker is absent."""

from utils.process_limits import CONTAINER_OOM_SCORE_ADJ, cgroup_parent, thread_environment

logger = logging.getLogger("grid.container_manager")

# Path inside the container where the host workspace is mounted. Docker forbids bind to "/", so we use /workspace.
# Agent-facing paths are shown as "/"; this is the actual mount point for docker,
# and file tools take paths under it too (utils.path_utils.CONTAINER_WORKDIR).
CONTAINER_WORKDIR = "/workspace"

#: The label naming the settings a container was made with (ContainerManager.profile).
PROFILE_LABEL = "grid.isolation.profile"

#: The uid and gid each image's commands run as, asked once per server: every
#: chat with a space of its own has a manager of its own.
_AGENT_IDS: Dict[str, Tuple[int, int]] = {}

#: The Grid install this module belongs to: servers run from different copies
#: (worktrees) never share a container, even for the same workspace.
GRID_ROOT = Path(__file__).resolve().parents[2]

#: The shape of the network a tenant's containers are put on. It is part of
#: :attr:`ContainerManager.profile`: a container made by an older Grid - the
#: ones that ran with ``network_mode="host"`` - is recreated on its own
#: network the next time it is asked for, and never reused.
NETWORK_POLICY = "tenant-bridge-v1"

#: The prefix of a tenant network's name (the rest names the tenant).
TENANT_NETWORK_PREFIX = "grid-tenant-"

#: The driver a tenant network is made with, and the option that keeps two
#: containers of one tenant from reaching each other. A network of a tenant's
#: name with another driver or without that option is not the one this module
#: makes: it may be the default bridge of a shim, one an administrator made by
#: hand, or one a compatibility layer accepted from "docker run --network".
#: Such a network is never used, exactly as a missing one is never assumed.
TENANT_NETWORK_DRIVER = "bridge"
TENANT_NETWORK_ICC_OPTION = "com.docker.network.bridge.enable_icc"
TENANT_NETWORK_OPTIONS = {TENANT_NETWORK_ICC_OPTION: "false"}

#: The labels naming the tenant a network belongs to and the Grid install that
#: made it: only networks of this install are ever pruned, and none is shared
#: with a server of another copy.
TENANT_NETWORK_LABEL = "grid.network.tenant"
NETWORK_ROOT_LABEL = "grid.network.root"

#: The proxy variables a container inherits from the server.
PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
)

#: Of those, the ones holding a URL a container must be able to connect to.
PROXY_URL_VARIABLES = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}

#: Hosts that name the server itself to the server, and the container itself
#: to a container: a tenant network reaches the host by its gateway instead.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

#: The name a container reaches the host by (Docker Desktop provides it; on
#: Linux an entry in the container's own hosts file gives it the gateway).
HOST_GATEWAY_NAME = "host.docker.internal"


def _digest(value: str) -> str:
    """A short, stable digest of *value* for a name Docker accepts."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def tenant_network_name(user_id: str) -> str:
    """The name of the bridge network of *user_id*, the tenant.

    One network per user: two users' containers are on two networks and reach
    no port of each other's. The name carries a digest of the install and the
    user, so it is unique per copy of Grid and safe for any user id (an
    account id may hold characters a Docker network name may not).
    """
    readable = re.sub(r"[^A-Za-z0-9_.-]", "-", user_id).strip("-._")[:24] or "user"
    return f"{TENANT_NETWORK_PREFIX}{readable}-{_tenant_digest(user_id)}"


def _tenant_digest(user_id: str) -> str:
    """The digest of a tenant: its user id within this install of Grid."""
    return _digest(f"{GRID_ROOT}\n{user_id}")


def _proxy_url_for_a_container(name: str, value: str) -> str:
    """*value*, the proxy URL of the server, as a container reaches it."""
    if name not in PROXY_URL_VARIABLES:
        return value
    try:
        parsed = urlsplit(value if "://" in value else f"http://{value}")
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return value
    if host not in LOOPBACK_HOSTS:
        return value
    netloc = HOST_GATEWAY_NAME + (f":{port}" if port else "")
    if parsed.username:
        credentials = parsed.username + (f":{parsed.password}" if parsed.password else "")
        netloc = f"{credentials}@{netloc}"
    target = urlunsplit(parsed._replace(netloc=netloc))
    logger.warning(
        "The proxy variable %s names the server itself; in a container of its own network "
        "that is the container, so its host is replaced with %s. That works only where the "
        "proxy listens on the Docker gateway address, not on the host's loopback alone.",
        name, HOST_GATEWAY_NAME,
    )
    return target


def proxy_environment(environ: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, str], Dict[str, str]]:
    """The proxy variables a container gets, and the hosts entries it needs.

    A server behind a proxy names it as the server sees it, often
    ``http://127.0.0.1:3128``. With the host's network that worked; on a
    tenant's own network it does not, so a loopback proxy is renamed to the
    host gateway (and named in the container's hosts file). A proxy named by
    any other host is passed on unchanged. ``NO_PROXY`` is a list of hosts, not
    an address to connect to, and is passed on as it is.
    """
    environ = os.environ if environ is None else environ
    environment: Dict[str, str] = {}
    hosts: Dict[str, str] = {}
    for name in PROXY_VARIABLES:
        value = environ.get(name)
        if not value:
            continue
        rewritten = _proxy_url_for_a_container(name, value)
        environment[name] = rewritten
        if rewritten != value:
            hosts[HOST_GATEWAY_NAME] = "host-gateway"
    return environment, hosts


def container_name(user_id: str, workspace: Path) -> str:
    """The container of *user_id* working in *workspace* (a resolved host path).

    The name tells workspaces apart: two servers - or two chats - working in
    different directories each get their own container. A name of the user
    alone made a second server, started with another ``--path``, find "its"
    container mounting the wrong directory and recreate it, removing the
    container the first server's tools were still running in.
    """
    digest = hashlib.sha256(f"{GRID_ROOT}\n{workspace}".encode()).hexdigest()[:12]
    return f"grid-agent-{user_id}-{digest}"

class ContainerManager:
    """
    Manages Docker containers for agent isolation.
    Implements 'One Container per User and Workspace' strategy.
    """

    def __init__(self, config: Any, *, enabled: Optional[bool] = None):
        """``enabled`` None follows the config's ``isolation.enabled``; True or
        False decides regardless - a server with accounts isolates every user,
        whatever the config says. The config still gives image and limits."""
        from schemas.schemas import IsolationConfig

        self.config = config
        self.client = None
        self._agent_ids_of: Optional[Tuple[int, int]] = None
        self.enabled = False

        isolation_config = getattr(config.config, "isolation", None)
        if isinstance(isolation_config, dict):
            isolation_config = IsolationConfig(**isolation_config)
        self.settings: IsolationConfig = isolation_config or IsolationConfig()
        self.image = self.settings.image
        if not (self.settings.enabled if enabled is None else enabled):
            return
        if docker is None:
            logger.warning("Docker isolation requested but docker SDK is not installed; disabling isolation")
            return
        self.enabled = True
        try:
            self.client = docker.from_env()
            logger.info("Docker client initialized")
        except DockerException as e:
            logger.error("Failed to initialize Docker client: %s", e)
            self.enabled = False

    @property
    def cgroup_parent(self) -> Optional[str]:
        """The cgroup the containers go under (utils.process_limits)."""
        return cgroup_parent(self.settings.cgroup_parent)

    @property
    def profile(self) -> str:
        """A digest of the settings a container is made with. A container made
        with other settings - an older Grid, an edited config - is recreated,
        so the limits in force are always the configured ones."""
        settings = {
            "image": self.image,
            "memory": self.settings.memory,
            "cpus": self.settings.cpus,
            "pids_limit": self.settings.pids_limit,
            "cgroup_parent": self.cgroup_parent,
            "hardening": ["cap_drop:ALL", "no-new-privileges", "no-restart", "init"],
            "sharing": ["no-swap", f"oom_score_adj:{CONTAINER_OOM_SCORE_ADJ}", "thread-env"],
            # Containers made before the workspace was given to their user are
            # recreated, and so their workspace given.
            "workspace_owner": "agent",
            # Containers made on the host's network - by any Grid before this
            # one - are recreated on the tenant's own network. The policy is
            # part of the digest, so that recreation cannot depend on the old
            # container's labels alone.
            "network": [NETWORK_POLICY, "per-tenant-bridge", "icc:off", "host-network:no"],
        }
        return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:16]

    def get_or_create_container(
        self, user_id: str, workspace: Path, mounts: Optional[Dict[str, Path]] = None
    ) -> Optional[Container]:
        """The running container of *user_id*, with *workspace* - a host
        directory - mounted at CONTAINER_WORKDIR, and each host directory of
        *mounts* at its container path (a chat's project and user spaces);
        made when missing. None when isolation is off or Docker fails."""
        if not self.enabled or not self.client:
            return None

        # One spelling of the host path for the name, the mount and its check:
        # a path spelled differently would recreate the container on every call.
        workspace = Path(workspace).resolve()
        mounts = {target: Path(source).resolve() for target, source in (mounts or {}).items()}
        name = container_name(user_id, workspace)
        expected = {CONTAINER_WORKDIR: str(workspace), **{target: str(source) for target, source in mounts.items()}}

        try:
            container = self.client.containers.get(name)

            # Check that the existing container mounts the expected host paths, and
            # no others. Mounts cannot be changed on a running container — recreate
            # if mismatched (a chat moved to another project, for one).
            current = {
                mount.get("Destination"): mount.get("Source")
                for mount in container.attrs.get("Mounts", [])
                if mount.get("Type", "bind") == "bind"
            }

            made_with = (container.attrs.get("Config", {}).get("Labels") or {}).get(PROFILE_LABEL)
            # The network is checked on its own too: a container the profile
            # label does not speak for - one that ran on the host's network -
            # is never reused, whatever it claims. So is a container on any
            # network but the tenant's own, including one with extra networks
            # attached beside it, and one whose tenant network has gone or is
            # not the network this install makes.
            network = tenant_network_name(user_id)
            current_network = (container.attrs.get("HostConfig") or {}).get("NetworkMode")
            attached = (container.attrs.get("NetworkSettings") or {}).get("Networks")
            extra_networks = set(attached) - {network} if isinstance(attached, dict) else set()
            unsafe_network = current_network != network or bool(extra_networks)
            if current != expected or made_with != self.profile or unsafe_network:
                logger.warning(
                    "Container %s mounts %s with settings %s on network %s%s; expected %s with %s on %s. Recreating.",
                    name, current, made_with, current_network,
                    f" (plus {sorted(extra_networks)})" if extra_networks else "",
                    expected, self.profile, network,
                )
                container.remove(force=True)
                return self._create_container(user_id, name, workspace=workspace, mounts=mounts)
            if not self._tenant_network_is_intact(user_id):
                logger.warning(
                    "The tenant network %s of container %s is not the one this install makes. Recreating.",
                    network, name,
                )
                container.remove(force=True)
                return self._create_container(user_id, name, workspace=workspace, mounts=mounts)
            if not self._runs_current_image(container):
                logger.info("Container %s runs an older build of %s. Recreating.", name, self.image)
                container.remove(force=True)
                return self._create_container(user_id, name, workspace=workspace, mounts=mounts)

            if container.status != "running":
                container.start()
            return container
        except NotFound:
            # Create new container
            return self._create_container(user_id, name, workspace=workspace, mounts=mounts)
        except Exception as e:
            logger.error(f"Error getting container {name}: {e}")
            return None

    def stop_container(self, user_id: str, container_id: str) -> None:
        """Stop this user's exact container when its space is retired.

        Found by its ID, so a late close never stops a replacement container
        that a newer space started under the same name.
        """
        if not self.enabled or not self.client:
            return
        try:
            container = self.client.containers.get(container_id)
            if container.id == container_id and container.status == "running":
                container.stop(timeout=5)
        except NotFound:
            return
        except Exception as exc:
            logger.warning("Could not stop the container of %s: %s", user_id, exc)

    def remove_container(self, user_id: str, container_id: str) -> None:
        """Remove this user's exact container: a chat's, whose files all live
        in the spaces it mounts, when the chat's runtime is dropped. Found by
        its ID, as :meth:`stop_container` finds it."""
        if not self.enabled or not self.client:
            return
        try:
            container = self.client.containers.get(container_id)
            if container.id == container_id:
                container.remove(force=True)
        except NotFound:
            return
        except Exception as exc:
            logger.warning("Could not remove the container of %s: %s", user_id, exc)

    def prune_chat_containers(self, chat_spaces: Path) -> int:
        """Remove the containers of chats whose spaces lie under *chat_spaces*
        (``<users>/<id>/chats``, any user): left by a server that stopped
        without closing them. Called at start, before any chat runs; how many."""
        if not self.enabled or not self.client:
            return 0
        removed = 0
        try:
            containers = self.client.containers.list(all=True, filters={"name": "grid-agent-"})
        except Exception as exc:
            logger.warning("Cannot list containers to prune: %s", exc)
            return 0
        for container in containers:
            for mount in container.attrs.get("Mounts", []):
                if mount.get("Destination") != CONTAINER_WORKDIR:
                    continue
                source = Path(mount.get("Source") or "/")
                if source.parent.name == "chats" and source.is_relative_to(chat_spaces):
                    try:
                        container.remove(force=True)
                        removed += 1
                    except Exception as exc:
                        logger.warning("Could not remove the orphaned container %s: %s", container.name, exc)
        if removed:
            logger.info("Removed %d container(s) of chats left by an earlier run", removed)
        # The networks of the users whose containers are all gone go with
        # them; a user still running keeps theirs, and Docker refuses to
        # remove a network a container is attached to in any case.
        self.prune_tenant_networks()
        return removed

    def prune_tenant_networks(self) -> int:
        """Remove the tenant networks of this Grid install that no container is
        attached to any more; how many.

        Called at start, before any chat runs: a user's network outlives the
        containers of its chats, so only an empty one is removed - and only one
        this install made (both labels are checked). A network made by another
        back end, or of another copy of Grid, is never touched.
        """
        if not self.enabled or not self.client:
            return 0
        root = _digest(str(GRID_ROOT))
        networks_api = getattr(self.client, "networks", None)
        if networks_api is None:
            return 0
        try:
            networks = networks_api.list(filters={"label": f"{NETWORK_ROOT_LABEL}={root}"})
        except Exception as exc:
            logger.warning("Cannot list tenant networks to prune: %s", exc)
            return 0
        removed = 0
        for network in networks:
            labels = (network.attrs.get("Labels") or {})
            tenant = labels.get(TENANT_NETWORK_LABEL)
            if labels.get(NETWORK_ROOT_LABEL) != root or not tenant:
                continue
            if not self._network_is_tenant(network, tenant):
                # Named and labelled as one of ours but not made as one: it is
                # not this install's to remove.
                continue
            if network.attrs.get("Containers"):
                continue
            try:
                network.remove()
                removed += 1
            except Exception as exc:  # a container was attached in between, or no permission
                logger.warning("Could not remove the tenant network %s: %s", network.name, exc)
        if removed:
            logger.info("Removed %d tenant network(s) no container uses", removed)
        return removed

    def _runs_current_image(self, container: Container) -> bool:
        """Whether *container* runs the build the image tag names now: after a
        rebuild of the image the container is recreated on the new one. Its
        workspace is a host directory, so nothing of the user's is lost."""
        try:
            return container.attrs.get("Image") == self.client.images.get(self.image).id
        except Exception as exc:  # the tag is gone or Docker does not say: keep what runs
            logger.warning("Cannot tell which build of %s container %s runs: %s", self.image, container.name, exc)
            return True

    def _agent_ids(self) -> Optional[Tuple[int, int]]:
        """The uid and gid the image's commands run as, asked of the image once.
        None when it cannot be told."""
        if self._agent_ids_of is None and self.image in _AGENT_IDS:
            self._agent_ids_of = _AGENT_IDS[self.image]
        if self._agent_ids_of is None:
            try:
                output = self.client.containers.run(
                    self.image, ["sh", "-c", "id -u; id -g"], user="agent",
                    remove=True, network_disabled=True,
                )
                uid, gid = (int(value) for value in output.decode().split())
                self._agent_ids_of = _AGENT_IDS[self.image] = (uid, gid)
            except Exception as exc:
                logger.warning("Cannot tell which uid %s runs as: %s", self.image, exc)
                return None
        return self._agent_ids_of

    def _own_directory(self, directory: Path) -> None:
        """Give a shared space's directory itself - not what is in it - to the
        container's user when this server made it as root.

        Other containers (other chats) may be changing what is inside it, so it
        is never walked: its contents became theirs when they were made there.
        """
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return
        ids = self._agent_ids()
        if ids is None:
            return
        info = os.lstat(directory)
        if info.st_uid == 0 and stat_module.S_ISDIR(info.st_mode):
            os.lchown(directory, *ids)

    def _own_workspace(self, workspace: Path) -> None:
        """Give *workspace* to the container's user when this server runs as root.

        The server makes the directory, so it is root's, and the container's
        unprivileged user could not write its own workspace. Called while no
        container of the space runs: nothing swaps a directory for a link
        during the walk, and links themselves are changed, never followed.
        """
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return
        ids = self._agent_ids()
        if ids is None:
            return
        uid, gid = ids
        for directory, dirs, files in os.walk(workspace):
            for name in [directory, *(os.path.join(directory, entry) for entry in dirs + files)]:
                stat = os.lstat(name)
                if (stat.st_uid, stat.st_gid) != (uid, gid):
                    os.lchown(name, uid, gid)

    def _ensure_tenant_network(self, user_id: str) -> Optional[str]:
        """The name of the bridge network of *user_id*, made when missing.

        One network per user, made by this module: a container of one user is
        on no network of another's, so no port of one user's is reachable from
        the other's. Egress is untouched - a Docker bridge network is NAT'd to
        the host's routes - so the model providers stay reachable; what is
        dropped is only container-to-container traffic on that bridge
        (``enable_icc=false``), which no tenant needs.

        None when Docker will not make it, and then no container is made: a
        tenant is never put on somebody else's network - neither the host's,
        nor the default bridge, which every container on the host shares.
        A client that cannot manage networks is refused just as a Docker
        daemon that cannot make one: the name alone is not a network, and a
        compatibility layer may accept a container on a network that does not
        exist. A network already under this name is used only when it is the
        one this install makes - driver, options and labels are checked first,
        so the default bridge of a shim, or a network an administrator made by
        hand, is never mistaken for it.
        """
        name = tenant_network_name(user_id)
        networks = getattr(self.client, "networks", None)
        if networks is None:
            # Not a docker.from_env() client: nothing here can make or check a
            # network, and a name is not a network. The tenant gets no
            # container rather than one on a network this server cannot vouch
            # for (a shim may accept a container on a network that was never
            # made, and the container would then sit on a shared one).
            logger.error(
                "The Docker client of this server manages no networks; the tenant network %s "
                "cannot be made or checked, so %s gets no container.", name, user_id,
            )
            return None
        labels = {TENANT_NETWORK_LABEL: name, NETWORK_ROOT_LABEL: _digest(str(GRID_ROOT))}
        try:
            found = networks.list(names=[name])
        except Exception as exc:
            logger.error("Cannot look for the tenant network %s: %s", name, exc)
            return None
        if found:
            if not self._network_is_tenant(found[0], name):
                logger.error(
                    "The network %s exists but is not the one this install makes for %s "
                    "(driver %s, options %s, labels %s); refusing to put a container on it.",
                    name, user_id,
                    (found[0].attrs or {}).get("Driver"), (found[0].attrs or {}).get("Options"),
                    (found[0].attrs or {}).get("Labels"),
                )
                return None
            return name
        try:
            networks.create(name, driver=TENANT_NETWORK_DRIVER, labels=labels, options=dict(TENANT_NETWORK_OPTIONS))
            logger.info("Made the tenant network %s", name)
            return name
        except Exception as exc:
            # Another manager of the same user may have made it a moment ago:
            # only the network itself, checked, is accepted as the answer.
            try:
                found = networks.list(names=[name])
                if found and self._network_is_tenant(found[0], name):
                    return name
            except Exception:
                pass
            logger.error("Cannot make the tenant network %s: %s", name, exc)
            return None

    def _network_is_tenant(self, network: Any, name: str) -> bool:
        """Whether *network* is the tenant bridge this install makes under *name*.

        A name alone says nothing: a compatibility layer, a ``docker run
        --network`` of an administrator, or a daemon whose default bridge
        answers that name would otherwise pass. The driver, the option that
        keeps a tenant's containers from each other, and both labels are part
        of what the network must be.
        """
        attrs = getattr(network, "attrs", None) or {}
        labels = attrs.get("Labels") or {}
        # docker-py reports these in ``attrs``; a plain object may carry them
        # as its own fields. Neither saying is a refusal, never a pass.
        driver = attrs.get("Driver", getattr(network, "driver", None))
        options = attrs.get("Options") or getattr(network, "options", None) or {}
        return (
            getattr(network, "name", None) == name
            and driver == TENANT_NETWORK_DRIVER
            and options.get(TENANT_NETWORK_ICC_OPTION) == "false"
            and labels.get(TENANT_NETWORK_LABEL) == name
            and labels.get(NETWORK_ROOT_LABEL) == _digest(str(GRID_ROOT))
        )

    def _tenant_network_is_intact(self, user_id: str) -> bool:
        """Whether the tenant's network, if the daemon lists it, is this install's.

        Checked before an existing container is reused: a container attached to
        a network that carries the tenant's name but is not the one made here -
        another driver, another option, another install's labels - is not
        reused, whatever its own labels say. A missing network or failed
        inspection is not evidence of isolation and is refused.
        """
        name = tenant_network_name(user_id)
        networks = getattr(self.client, "networks", None)
        if networks is None:
            return False
        try:
            found = networks.list(names=[name])
        except Exception as exc:
            logger.warning("Cannot check the tenant network %s: %s", name, exc)
            return False
        return bool(found) and all(self._network_is_tenant(network, name) for network in found)

    def _create_container(
        self, user_id: str, container_name: str, workspace: Path, mounts: Optional[Dict[str, Path]] = None
    ) -> Optional[Container]:
        """Create and start the container of *user_id*; *workspace* and the
        sources of *mounts* are resolved."""
        try:
            # The network first: without one of its own, the user gets no
            # container at all rather than one on the host's or the shared
            # default bridge.
            network = self._ensure_tenant_network(user_id)
            if network is None:
                return None
            workspace.mkdir(parents=True, exist_ok=True)
            self._own_workspace(workspace)
            # The workspace, and the spaces *mounts* names, are the only host
            # directories the container sees.
            volumes = {str(workspace): {"bind": CONTAINER_WORKDIR, "mode": "rw"}}
            for target, source in (mounts or {}).items():
                source.mkdir(parents=True, exist_ok=True)
                self._own_directory(source)
                volumes[str(source)] = {"bind": target, "mode": "rw"}
            logger.info("Creating container %s with workspace %s", container_name, workspace)


            container_env = {"BEADS_DAEMON": "0", **thread_environment(self.settings.cpus)}
            proxy_env, proxy_hosts = proxy_environment()
            container_env.update(proxy_env)
            # Docker Desktop already resolves the host name; on a Linux daemon
            # the container's hosts file is what names the bridge gateway.
            extra_hosts = proxy_hosts if sys.platform.startswith("linux") else {}

            # No restart policy: a user's container runs when Grid starts it for
            # that user's space, not whenever the Docker daemon comes up.
            container = self.client.containers.run(
                self.image,
                name=container_name,
                detach=True,
                tty=True,        # Keep running
                stdin_open=True, # Keep stdin open
                volumes=volumes,
                working_dir=CONTAINER_WORKDIR,
                user="agent",
                environment=container_env, # Disable beads daemon and forward proxy env vars
                labels={PROFILE_LABEL: self.profile},
                # The tenant's own network - never the host's, and never the
                # default bridge every container on the machine shares.
                network=network,
                # Under one cgroup with every other user's: its limits are the
                # ceiling of all users together, and equal weights share it.
                **({"cgroup_parent": self.cgroup_parent} if self.cgroup_parent else {}),
                # One user's agents cannot take the machine, nor any privilege.
                mem_limit=self.settings.memory,
                # No swap: a container swapping its memory slows the whole host.
                memswap_limit=self.settings.memory,
                # Under memory pressure the kernel kills in a container, not the server.
                oom_score_adj=CONTAINER_OOM_SCORE_ADJ,
                nano_cpus=int(self.settings.cpus * 1e9),
                pids_limit=self.settings.pids_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges=true"],
                # An init as PID 1 reaps orphaned processes (bd leaves some); without
                # it they stay zombies until pids_limit refuses every new process.
                init=True,
                **({"extra_hosts": extra_hosts} if extra_hosts else {}),
            )
            return container
        except Exception as e:
            logger.error(f"Failed to create container {container_name}: {e}")
            return None

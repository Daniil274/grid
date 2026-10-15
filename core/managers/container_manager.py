"""
Container Manager for agent isolation using Docker.
"""

import hashlib
import json
import logging
import os
import stat as stat_module
import sys
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
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
            if current != expected or made_with != self.profile:
                logger.warning(
                    "Container %s mounts %s with settings %s; expected %s with %s. Recreating.",
                    name, current, made_with, expected, self.profile,
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

    def _create_container(
        self, user_id: str, container_name: str, workspace: Path, mounts: Optional[Dict[str, Path]] = None
    ) -> Optional[Container]:
        """Create and start the container of *user_id*; *workspace* and the
        sources of *mounts* are resolved."""
        try:
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

            # Docker Desktop (Windows/macOS) does not support network_mode="host" for Linux containers.
            # Use host networking only on Linux.
            run_kwargs: Dict[str, Any] = {}
            if sys.platform.startswith("linux"):
                run_kwargs["network_mode"] = "host"  # Allow access to host's proxy at 127.0.0.1
            else:
                # On Docker Desktop, access host services via host.docker.internal when needed.
                # Keep default bridge networking for portability.
                pass

            container_env = {"BEADS_DAEMON": "0", **thread_environment(self.settings.cpus)}
            for proxy_var in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "no_proxy",
                "all_proxy",
            ):
                proxy_val = os.environ.get(proxy_var)
                if proxy_val:
                    container_env[proxy_var] = proxy_val

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
                **run_kwargs,
            )
            return container
        except Exception as e:
            logger.error(f"Failed to create container {container_name}: {e}")
            return None

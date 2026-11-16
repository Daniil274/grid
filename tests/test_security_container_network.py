"""A tenant's containers run on a network of their own (audit S4).

Fake Docker throughout: no daemon, no network, no container is touched. What is
asserted is what the manager asks Docker for - the network a container is
started on, the network it makes for a user, which old containers it refuses to
reuse - and what it does when Docker will not make that network.
"""

from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from core.managers import container_manager as module
from core.managers.container_manager import (
    CONTAINER_WORKDIR,
    NETWORK_POLICY,
    NETWORK_ROOT_LABEL,
    PROFILE_LABEL,
    TENANT_NETWORK_LABEL,
    ContainerManager,
    container_name,
    proxy_environment,
    tenant_network_name,
)
from schemas.schemas import IsolationConfig

#: The build the image tag names in these tests.
CURRENT_BUILD = "sha256:current"

#: Networks a tenant container must never be started on: the host's, the
#: default bridge every container on the machine shares, or none at all.
SHARED_NETWORKS = {"host", "bridge", "default", "none", "container"}


class FakeNetwork:
    def __init__(self, name, labels=None, driver="bridge", options=None):
        self.name = name
        # As Docker's own inspect answers: the driver, the options and the
        # labels are what say whether a network is the one Grid made.
        self.attrs = {
            "Labels": dict(labels or {}),
            "Containers": {},
            "Driver": driver,
            "Options": dict(options or {}),
        }
        self.removed = False

    def remove(self):
        if self.attrs["Containers"]:
            raise RuntimeError(f"network {self.name} has active endpoints")
        self.removed = True


class FakeNetworks:
    """The network collection of a Docker client, as much of it as is used."""

    def __init__(self):
        self.made = {}
        self.creations = []
        self.failure = None

    def list(self, names=None, filters=None):
        found = list(self.made.values())
        if names:
            found = [network for network in found if network.name in names]
        if filters and filters.get("label"):
            key, _, value = filters["label"].partition("=")
            found = [network for network in found if network.attrs["Labels"].get(key) == value]
        return found

    def create(self, name, driver=None, labels=None, options=None):
        if self.failure:
            raise RuntimeError(self.failure)
        network = FakeNetwork(name, labels, driver=driver or "bridge", options=options)
        self.made[name] = network
        self.creations.append({"name": name, "driver": driver, "labels": labels, "options": options})
        return network


class FakeContainer:
    def __init__(self, name, mounts, labels, network=None, image=CURRENT_BUILD, status="running"):
        self.name, self.id, self.status = name, f"id-{name}", status
        self.attrs = {
            "Mounts": mounts,
            "Config": {"Labels": labels},
            "HostConfig": {"NetworkMode": network},
            # As inspect answers for a running container: the networks it is
            # attached to, by name.
            "NetworkSettings": {"Networks": {network: {}} if network else {}},
            "Image": image,
        }
        self.network = network
        self.removed = False
        self.started = False
        self.stopped = False

    def remove(self, force=False):
        self.removed = True

    def start(self):
        self.started = True

    def stop(self, timeout=5):
        self.stopped = True


class FakeClient:
    def __init__(self):
        self.networks = FakeNetworks()
        self.existing = {}
        self.runs = []
        self.probes = []
        self.containers = self
        self.images = SimpleNamespace(get=lambda name: SimpleNamespace(id=CURRENT_BUILD))

    def get(self, name_or_id):
        for container in self.existing.values():
            if name_or_id in (container.name, container.id):
                return container
        raise module.NotFound("missing")

    def list(self, all=False, filters=None):
        return list(self.existing.values())

    def run(self, image, command=None, **kwargs):
        if command is not None:  # the question of which uid the image runs as
            self.probes.append((image, command, kwargs))
            return b"1000\n1000\n"
        self.runs.append(kwargs)
        network = kwargs.get("network")
        container = FakeContainer(
            kwargs["name"],
            [
                {"Type": "bind", "Destination": spec["bind"], "Source": source}
                for source, spec in kwargs["volumes"].items()
            ],
            kwargs["labels"],
            network,
        )
        self.existing[container.name] = container
        if network in self.networks.made:
            self.networks.made[network].attrs["Containers"][container.id] = container.name
        original_remove = container.remove

        def remove(force=False):
            if network in self.networks.made:
                self.networks.made[network].attrs["Containers"].pop(container.id, None)
            original_remove(force=force)

        container.remove = remove
        return container


@pytest.fixture(autouse=True)
def no_proxy_from_the_environment(monkeypatch):
    """A developer's own proxy must not decide what these tests see."""
    for name in module.PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def manager(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(module, "docker", SimpleNamespace(from_env=lambda: client))
    monkeypatch.setattr(module, "_AGENT_IDS", {})
    config = SimpleNamespace(config=SimpleNamespace(isolation=IsolationConfig(enabled=True)))
    manager = ContainerManager(config)
    assert manager.enabled
    return manager, client


def _old_container(client, user_id, workspace, *, labels, network):
    """A container as an older Grid left it: the labels and the network given."""
    name = container_name(user_id, workspace.resolve())
    if network not in SHARED_NETWORKS and network not in client.networks.made:
        # A container attached to the tenant's network means the network is
        # there; on a real daemon it always is.
        client.networks.made[network] = FakeNetwork(
            network,
            {TENANT_NETWORK_LABEL: network, NETWORK_ROOT_LABEL: module._digest(str(module.GRID_ROOT))},
            options=dict(module.TENANT_NETWORK_OPTIONS),
        )
    container = FakeContainer(
        name,
        [{"Type": "bind", "Destination": CONTAINER_WORKDIR, "Source": str(workspace.resolve())}],
        labels,
        network,
    )
    client.existing[name] = container
    return container


# -- the network a container is started on ------------------------------------------
def test_a_tenant_container_is_started_on_its_own_network_and_never_on_the_hosts(manager, tmp_path):
    manager, client = manager

    manager.get_or_create_container("u1", workspace=tmp_path)

    [run] = client.runs
    assert run["network"] == tenant_network_name("u1")
    assert "network_mode" not in run
    assert run["network"] not in SHARED_NETWORKS


def test_two_users_share_no_network_and_one_user_keeps_his_own(manager, tmp_path):
    manager, client = manager
    first, second = tmp_path / "a", tmp_path / "b"

    manager.get_or_create_container("u1", workspace=first)
    manager.get_or_create_container("u2", workspace=second)
    manager.get_or_create_container("u1", workspace=tmp_path / "c")

    networks = [run["network"] for run in client.runs]
    assert networks[0] == networks[2] != networks[1]
    assert tenant_network_name("u1") != tenant_network_name("u2")


def test_a_user_id_docker_would_not_accept_still_names_a_network(manager, tmp_path):
    manager, client = manager

    manager.get_or_create_container("user@example.com/../x", workspace=tmp_path)

    [run] = client.runs
    assert run["network"] == tenant_network_name("user@example.com/../x")
    assert set(run["network"]) <= set("abcdefghijklmnopqrstuvwxyz0123456789-._")


def test_the_tenant_network_keeps_egress_and_fences_the_tenant_off(manager, tmp_path):
    manager, client = manager

    manager.get_or_create_container("u1", workspace=tmp_path)

    [creation] = client.networks.creations
    assert creation["driver"] == "bridge"
    # Egress to the providers is the network's own NAT: not an internal-only
    # network, and not networking-disabled.
    assert creation["name"] == tenant_network_name("u1")
    assert client.runs[0].get("network_disabled") is not True
    # No container-to-container traffic even inside one user's network.
    assert creation["options"] == {"com.docker.network.bridge.enable_icc": "false"}
    assert creation["labels"][TENANT_NETWORK_LABEL] == tenant_network_name("u1")
    assert creation["labels"][NETWORK_ROOT_LABEL] == module._digest(str(module.GRID_ROOT))
    # Nothing is published: a port of a tenant container is not on the host.
    assert "ports" not in client.runs[0] and "port_bindings" not in client.runs[0]


def test_the_network_is_made_once_per_tenant(manager, tmp_path):
    manager, client = manager

    manager.get_or_create_container("u1", workspace=tmp_path)
    manager.get_or_create_container("u1", workspace=tmp_path / "chat")
    manager.get_or_create_container("u1", workspace=tmp_path)

    assert len(client.networks.creations) == 1


def test_a_container_running_on_its_network_is_reused(manager, tmp_path):
    manager, client = manager
    current = _old_container(
        client, "u1", tmp_path, labels={PROFILE_LABEL: manager.profile}, network=tenant_network_name("u1")
    )
    current.status = "exited"

    assert manager.get_or_create_container("u1", workspace=tmp_path) is current
    assert current.started and not current.removed and client.runs == []


# -- old containers: the profile invalidates the host network -----------------------
def test_a_container_of_an_older_grid_on_the_hosts_network_is_replaced(manager, tmp_path):
    manager, client = manager
    old = _old_container(client, "u1", tmp_path, labels={PROFILE_LABEL: "profile-of-an-older-grid"}, network="host")

    manager.get_or_create_container("u1", workspace=tmp_path)

    assert old.removed and old.started is False
    [run] = client.runs
    assert run["network"] == tenant_network_name("u1") and "network_mode" not in run


def test_a_container_that_claims_the_current_settings_from_the_host_network_is_replaced(manager, tmp_path):
    manager, client = manager
    # Settings that look current, but the container is on the host's network:
    # the label alone decides nothing.
    old = _old_container(client, "u1", tmp_path, labels={PROFILE_LABEL: manager.profile}, network="host")

    manager.get_or_create_container("u1", workspace=tmp_path)

    assert old.removed
    [run] = client.runs
    assert run["network"] == tenant_network_name("u1")


def test_a_container_of_an_older_grid_on_the_default_bridge_is_replaced(manager, tmp_path):
    manager, client = manager
    old = _old_container(client, "u1", tmp_path, labels={PROFILE_LABEL: manager.profile}, network="bridge")

    manager.get_or_create_container("u1", workspace=tmp_path)

    assert old.removed and client.runs[0]["network"] == tenant_network_name("u1")


def test_the_network_policy_is_part_of_the_profile(manager, monkeypatch):
    manager, _ = manager
    before = manager.profile

    monkeypatch.setattr(module, "NETWORK_POLICY", "tenant-bridge-next")

    assert manager.profile != before
    assert NETWORK_POLICY == "tenant-bridge-v1"


# -- no network, no container ------------------------------------------------------
def test_a_user_gets_no_container_when_docker_will_not_make_their_network(manager, tmp_path):
    manager, client = manager
    client.networks.failure = "permission denied"

    assert manager.get_or_create_container("u1", workspace=tmp_path) is None
    assert client.runs == [] and client.existing == {}


def test_a_daemon_that_refuses_the_network_leaves_no_container_behind(manager, tmp_path):
    manager, client = manager

    def refuse(image, command=None, **kwargs):
        raise module.DockerException("no such network")

    client.run = refuse

    assert manager.get_or_create_container("u1", workspace=tmp_path) is None


# -- leftover networks -------------------------------------------------------------
def test_networks_of_departed_users_are_removed_and_used_ones_are_kept(manager, tmp_path):
    manager, client = manager
    manager.get_or_create_container("u1", workspace=tmp_path)
    kept = client.networks.made[tenant_network_name("u1")]
    empty = client.networks.create(
        tenant_network_name("u2"),
        driver="bridge",
        labels={
            TENANT_NETWORK_LABEL: tenant_network_name("u2"),
            NETWORK_ROOT_LABEL: module._digest(str(module.GRID_ROOT)),
        },
        options=dict(module.TENANT_NETWORK_OPTIONS),
    )
    foreign = client.networks.create(
        tenant_network_name("u3"),
        driver="bridge",
        labels={TENANT_NETWORK_LABEL: tenant_network_name("u3"), NETWORK_ROOT_LABEL: "another-grid-copy"},
        options=dict(module.TENANT_NETWORK_OPTIONS),
    )

    assert manager.prune_tenant_networks() == 1

    assert empty.removed and not kept.removed and not foreign.removed


def test_a_network_shared_with_a_live_container_is_never_cut_off(manager, tmp_path):
    manager, client = manager
    users = tmp_path / "users"
    chat = users / "u1" / "chats" / "ctx-1"
    chat.mkdir(parents=True)
    user = users / "u1" / "workspace"
    leaving = manager.get_or_create_container("u1", chat, mounts={"/user": user})
    staying = manager.get_or_create_container("u1", user)
    network = client.networks.made[tenant_network_name("u1")]
    client.containers.list = lambda all=False, filters=None: [leaving, staying]

    assert manager.prune_chat_containers(users) == 1

    assert leaving.removed and not staying.removed
    assert not network.removed and list(network.attrs["Containers"]) == [staying.id]


def test_the_network_of_a_chat_left_behind_goes_with_it(manager, tmp_path):
    manager, client = manager
    users = tmp_path / "users"
    chat = users / "u1" / "chats" / "ctx-1"
    chat.mkdir(parents=True)
    left = manager.get_or_create_container("u1", chat)
    network = client.networks.made[tenant_network_name("u1")]
    client.containers.list = lambda all=False, filters=None: [left]

    manager.prune_chat_containers(users)

    assert left.removed and network.removed


# -- the proxy the server itself knows --------------------------------------------
def test_a_loopback_proxy_is_named_by_the_host_gateway(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")

    environment, hosts = proxy_environment()

    assert environment == {"HTTPS_PROXY": f"http://{module.HOST_GATEWAY_NAME}:3128"}
    assert hosts == {module.HOST_GATEWAY_NAME: "host-gateway"}


def test_a_proxy_with_credentials_and_a_localhost_name_keeps_both(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://user:secret@localhost:8080")

    environment, _ = proxy_environment()

    parsed = urlsplit(environment["HTTP_PROXY"])
    assert (parsed.hostname, parsed.port, parsed.username) == (module.HOST_GATEWAY_NAME, 8080, "user")


def test_a_proxy_named_by_a_reachable_host_is_passed_on_unchanged(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("ALL_PROXY", "socks5://10.1.2.3:1080")

    environment, hosts = proxy_environment()

    assert environment == {"HTTPS_PROXY": "http://proxy.internal:3128", "ALL_PROXY": "socks5://10.1.2.3:1080"}
    assert hosts == {}


def test_no_proxy_is_a_list_of_hosts_and_is_not_rewritten(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,.internal")

    environment, hosts = proxy_environment()

    assert environment == {"NO_PROXY": "127.0.0.1,localhost,.internal"}
    assert hosts == {}


def test_a_container_gets_the_host_gateway_name_only_when_its_proxy_needs_it(manager, tmp_path, monkeypatch):
    manager, client = manager
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")

    manager.get_or_create_container("u1", workspace=tmp_path)

    [run] = client.runs
    assert run["environment"]["HTTPS_PROXY"] == f"http://{module.HOST_GATEWAY_NAME}:3128"
    # Docker Desktop resolves the name itself; a Linux daemon needs the entry.
    if module.sys.platform.startswith("linux"):
        assert run["extra_hosts"] == {module.HOST_GATEWAY_NAME: "host-gateway"}
    else:
        assert "extra_hosts" not in run


def test_a_container_without_a_loopback_proxy_gets_no_extra_hosts(manager, tmp_path):
    manager, client = manager

    manager.get_or_create_container("u1", workspace=tmp_path)

    [run] = client.runs
    assert "extra_hosts" not in run
    assert not any(name in run["environment"] for name in module.PROXY_VARIABLES)


def test_reuse_requires_the_tenant_network_to_be_inspectable(manager, tmp_path):
    manager, client = manager
    old = manager.get_or_create_container("u1", workspace=tmp_path)
    assert old is not None
    client.networks.made.clear()
    # A missing network is not proof that a running container is isolated.
    assert manager._tenant_network_is_intact("u1") is False
    rebuilt = manager.get_or_create_container("u1", workspace=tmp_path)
    assert old.removed and rebuilt is not old
    assert tenant_network_name("u1") in client.networks.made


def test_proxy_rewrite_does_not_log_proxy_credentials(monkeypatch, caplog):
    monkeypatch.setenv("HTTPS_PROXY", "http://audit-user:audit-secret@127.0.0.1:3128")
    with caplog.at_level("WARNING"):
        environment, _ = proxy_environment()
    assert "audit-secret" in environment["HTTPS_PROXY"]
    assert "audit-secret" not in caplog.text
    assert "audit-user" not in caplog.text

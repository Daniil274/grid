"""Per-user containers: limited, unprivileged, not restarted by Docker, recreated on new settings or a new image build."""

from types import SimpleNamespace

import pytest

from core.managers import container_manager as module
from core.managers.container_manager import CONTAINER_WORKDIR, PROFILE_LABEL, ContainerManager, container_name
from schemas.schemas import IsolationConfig


#: The build the image tag names in these tests.
CURRENT_BUILD = "sha256:current"


class FakeContainer:
    def __init__(self, name, source, labels, status="running", image=CURRENT_BUILD):
        self.name, self.id, self.status = name, f"id-{name}", status
        self.attrs = {
            "Mounts": [{"Destination": CONTAINER_WORKDIR, "Source": source}],
            "Config": {"Labels": labels},
            "Image": image,
        }
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

    def run(self, image, command=None, **kwargs):
        if command is not None:  # the question of which uid the image runs as
            self.probes.append((image, command, kwargs))
            return b"1000\n1000\n"
        self.runs.append((image, kwargs))
        container = FakeContainer(kwargs["name"], next(iter(kwargs["volumes"])), kwargs["labels"])
        self.existing[container.name] = container
        return container


@pytest.fixture
def manager(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(module, "docker", SimpleNamespace(from_env=lambda: client))
    config = SimpleNamespace(config=SimpleNamespace(isolation=IsolationConfig(enabled=True, memory="1g", cpus=1.5, pids_limit=256)))
    manager = ContainerManager(config)
    assert manager.enabled
    return manager, client


def test_a_container_is_limited_and_unprivileged_and_not_restarted_by_docker(manager, tmp_path):
    manager, client = manager

    manager.get_or_create_container("u1", workspace=tmp_path)

    [(image, run)] = client.runs
    assert image == "grid-agent:latest"
    assert run["mem_limit"] == "1g" and run["nano_cpus"] == 1_500_000_000 and run["pids_limit"] == 256
    assert run["cap_drop"] == ["ALL"] and run["security_opt"] == ["no-new-privileges=true"]
    assert "restart_policy" not in run
    assert run["init"] is True
    assert run["user"] == "agent"
    assert run["labels"] == {PROFILE_LABEL: manager.profile}


def test_a_container_made_with_other_settings_is_recreated(manager, tmp_path):
    manager, client = manager
    source = str(tmp_path.resolve())
    name = container_name("u1", tmp_path.resolve())
    old = client.existing[name] = FakeContainer(name, source, labels={})

    manager.get_or_create_container("u1", workspace=tmp_path)

    assert old.removed and len(client.runs) == 1


def test_a_current_container_is_reused_and_started(manager, tmp_path):
    manager, client = manager
    source = str(tmp_path.resolve())
    name = container_name("u1", tmp_path.resolve())
    current = client.existing[name] = FakeContainer(
        name, source, labels={PROFILE_LABEL: manager.profile}, status="exited"
    )

    assert manager.get_or_create_container("u1", workspace=tmp_path) is current
    assert current.started and not current.removed and client.runs == []


def test_retiring_a_space_stops_only_its_exact_container(manager, tmp_path):
    manager, client = manager
    name = container_name("u1", tmp_path.resolve())
    current = client.existing[name] = FakeContainer(
        name, str(tmp_path), labels={PROFILE_LABEL: manager.profile}
    )
    manager.stop_container("u1", "old-container-id")
    assert not current.stopped
    manager.stop_container("u1", current.id)
    assert current.stopped


def test_a_container_of_an_older_image_build_is_recreated(manager, tmp_path):
    manager, client = manager
    source = str(tmp_path.resolve())
    name = container_name("u1", tmp_path.resolve())
    old = client.existing[name] = FakeContainer(
        name, source, labels={PROFILE_LABEL: manager.profile}, image="sha256:older"
    )

    manager.get_or_create_container("u1", workspace=tmp_path)

    assert old.removed and len(client.runs) == 1


def test_servers_working_in_different_directories_keep_their_own_containers(manager, tmp_path):
    # Two one-user servers (both "default_user") started with different --path:
    # the second used to find the first's container, see the wrong mount and
    # remove it - the first server's shell then answered "No such container".
    manager, client = manager
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir(), second.mkdir()

    one = manager.get_or_create_container("default_user", workspace=first)
    other = manager.get_or_create_container("default_user", workspace=second)

    assert one.name != other.name and not one.removed
    assert manager.get_or_create_container("default_user", workspace=first) is one
    assert len(client.runs) == 2


def test_a_container_is_named_by_user_and_workspace(tmp_path):
    assert container_name("u1", tmp_path) == container_name("u1", tmp_path)
    assert container_name("u1", tmp_path) != container_name("u2", tmp_path)
    assert container_name("u1", tmp_path) != container_name("u1", tmp_path / "x")
    assert container_name("u1", tmp_path).startswith("grid-agent-u1-")


def test_changing_a_limit_changes_the_profile(monkeypatch):
    monkeypatch.setattr(module, "docker", SimpleNamespace(from_env=FakeClient))

    def profile(**limits):
        return ContainerManager(SimpleNamespace(config=SimpleNamespace(isolation=IsolationConfig(enabled=True, **limits)))).profile

    assert profile(memory="2g") != profile(memory="4g")
    assert profile() == profile()


@pytest.mark.parametrize("memory", ["2", "2gb", "0g", "-1g"])
def test_malformed_memory_limits_are_refused(memory):
    with pytest.raises(ValueError):
        IsolationConfig(memory=memory)


def test_an_explicit_choice_overrides_the_configs_flag(monkeypatch):
    monkeypatch.setattr(module, "docker", SimpleNamespace(from_env=FakeClient))
    off = SimpleNamespace(config=SimpleNamespace(isolation=IsolationConfig(enabled=False)))
    on = SimpleNamespace(config=SimpleNamespace(isolation=IsolationConfig(enabled=True)))

    assert ContainerManager(off).enabled is False
    assert ContainerManager(off, enabled=True).enabled is True
    assert ContainerManager(on, enabled=False).enabled is False


def _chowns(monkeypatch, euid):
    monkeypatch.setattr(module.os, "geteuid", lambda: euid, raising=False)
    calls = []
    monkeypatch.setattr(
        module.os, "lchown", lambda path, uid, gid: calls.append((path, uid, gid)), raising=False
    )
    return calls


# Ownership exists only on POSIX: a Windows server has neither root nor chown.
@pytest.mark.skipif(not hasattr(module.os, "geteuid"), reason="a POSIX server's file ownership")
def test_a_server_running_as_root_gives_the_workspace_to_the_containers_user(manager, tmp_path, monkeypatch):
    # The server makes the workspace, so it is root's: the container's user
    # could not write its own workspace.
    manager, client = manager
    calls = _chowns(monkeypatch, 0)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file").write_text("x")
    (tmp_path / "link").symlink_to("/etc/passwd")

    manager.get_or_create_container("u1", workspace=tmp_path)
    manager.get_or_create_container("u2", workspace=tmp_path)

    root = str(tmp_path.resolve())
    owned = {path for path, uid, gid in calls if (uid, gid) == (1000, 1000)}
    assert {root, f"{root}/sub", f"{root}/sub/file", f"{root}/link"} <= owned
    assert len(client.probes) == 1  # asked of the image once
    [(_, _, probe)] = client.probes
    assert probe["user"] == "agent" and probe["remove"] and probe["network_disabled"]


def test_a_server_not_running_as_root_leaves_ownership_alone(manager, tmp_path, monkeypatch):
    manager, client = manager
    calls = _chowns(monkeypatch, 1000)

    manager.get_or_create_container("u1", workspace=tmp_path)

    assert calls == [] and client.probes == [] and len(client.runs) == 1


def test_an_image_that_cannot_say_its_uid_still_gets_a_container(manager, tmp_path, monkeypatch):
    manager, client = manager
    calls = _chowns(monkeypatch, 0)

    def refuse(*args, **kwargs):
        raise RuntimeError("no such image")

    monkeypatch.setattr(manager, "_agent_ids_of", None)
    original_run = client.run
    client.run = lambda image, command=None, **kwargs: refuse() if command else original_run(image, **kwargs)

    manager.get_or_create_container("u1", workspace=tmp_path)

    assert calls == [] and len(client.runs) == 1

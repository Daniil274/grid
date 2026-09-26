"""Per-user containers: limited, unprivileged, not restarted by Docker, recreated on new settings."""

from types import SimpleNamespace

import pytest

from core.managers import container_manager as module
from core.managers.container_manager import CONTAINER_WORKDIR, PROFILE_LABEL, ContainerManager
from schemas.schemas import IsolationConfig


class FakeContainer:
    def __init__(self, name, source, labels, status="running"):
        self.name, self.id, self.status = name, f"id-{name}", status
        self.attrs = {"Mounts": [{"Destination": CONTAINER_WORKDIR, "Source": source}], "Config": {"Labels": labels}}
        self.removed = False
        self.started = False

    def remove(self, force=False):
        self.removed = True

    def start(self):
        self.started = True


class FakeClient:
    def __init__(self):
        self.existing = {}
        self.runs = []
        self.containers = self

    def get(self, name):
        if name not in self.existing:
            raise module.NotFound("missing")
        return self.existing[name]

    def run(self, image, **kwargs):
        self.runs.append((image, kwargs))
        return FakeContainer(kwargs["name"], next(iter(kwargs["volumes"])), kwargs["labels"])


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
    assert run["cap_drop"] == ["ALL"] and run["security_opt"] == ["no-new-privileges:true"]
    assert "restart_policy" not in run
    assert run["user"] == "agent"
    assert run["labels"] == {PROFILE_LABEL: manager.profile}


def test_a_container_made_with_other_settings_is_recreated(manager, tmp_path):
    manager, client = manager
    source = str(tmp_path.resolve())
    old = client.existing["grid-agent-u1"] = FakeContainer("grid-agent-u1", source, labels={})

    manager.get_or_create_container("u1", workspace=tmp_path)

    assert old.removed and len(client.runs) == 1


def test_a_current_container_is_reused_and_started(manager, tmp_path):
    manager, client = manager
    source = str(tmp_path.resolve())
    current = client.existing["grid-agent-u1"] = FakeContainer(
        "grid-agent-u1", source, labels={PROFILE_LABEL: manager.profile}, status="exited"
    )

    assert manager.get_or_create_container("u1", workspace=tmp_path) is current
    assert current.started and not current.removed and client.runs == []


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

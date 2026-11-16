"""The security fixes refuse what they cannot vouch for, instead of guessing.

Regressions of the residual findings of the 2026-11-06 audit recheck:

- **B**: a programmatic server built without ``trusted_hosts`` must not answer
  a Host it was never published under - the old guard accepted any Origin whose
  netloc repeated the request's own Host;
- **C**: a Host entry that names a default port must match the portless Host a
  browser sends, and a guard built with no names must not fall back to
  ``Host == Origin``;
- **G**: a config bound to a user's workspace keeps that workspace when the
  public setter is called, instead of moving to another user's directory;
- **container network**: no network (a client without the networks API), a
  network with another install's labels, another driver or another option,
  and a container with an extra network attached - none is used or reused.

These are negative assertions: every case says what must *not* happen.
Sources are not modified for the tests; fake Docker and in-process ASGI only.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.managers import container_manager as module
from core.managers.container_manager import (
    NETWORK_ROOT_LABEL,
    TENANT_NETWORK_ICC_OPTION,
    TENANT_NETWORK_LABEL,
    tenant_network_name,
)
from schemas.schemas import IsolationConfig
from tests.test_security_web import Connection, build_server
from web_chat.security import OriginGuard, TrustedHosts


# -- B: no configured Host check cannot mean "any Host" -------------------------


def test_no_host_check_cannot_be_asked_for_with_none():
    """``trusted_hosts=None`` used to skip the check; now it is not a host list."""
    with pytest.raises(TypeError):
        build_server(trusted_hosts=None)


def test_a_programmatic_server_refuses_a_forged_host_and_origin():
    """The S5 reproduction: the attacker's page repeats its own name as Host."""
    server = build_server(trusted_hosts=())
    with TestClient(server.app) as client:
        forged = client.post(
            "/api/chat/conversations",
            headers={"Host": "evil.example", "Origin": "http://evil.example"},
        )
        assert forged.status_code == 400
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
        # A loopback name is the machine's own and still answered.
        assert client.get("/", headers={"Host": "127.0.0.1:8000"}).status_code == 200


# -- C: default ports and the local tools' guard ---------------------------------


def test_a_default_port_entry_matches_the_portless_host_a_browser_sends():
    hosts = TrustedHosts(["chat.example.com:443"])

    assert hosts.allows("chat.example.com", "https")
    assert hosts.allows("chat.example.com:443", "https")
    assert not hosts.allows("chat.example.com.evil.example", "https")


def test_the_default_port_name_is_served_end_to_end():
    server = build_server(trusted_hosts=("chat.example.com:443",))
    with TestClient(server.app, base_url="https://chat.example.com") as client:
        assert client.get("/").status_code == 200
        assert client.get("/", headers={"Host": "other.example"}).status_code == 400


def test_a_guard_with_no_names_does_not_repeat_the_request_host_as_trust():
    """timeline/context_inspector build ``same_site_only()`` without names."""
    guard = OriginGuard()

    assert not guard.allows(Connection(method="POST", origin="http://evil.example", host="evil.example"))
    assert guard.allows(Connection(method="POST", origin="http://127.0.0.1:8000", host="127.0.0.1:8000"))


# -- G: a bound config cannot be moved by its public setter ----------------------

CONFIG_TEMPLATE = """
settings:
  default_agent: a
  working_directory: .
  allow_path_override: true
  mcp_enabled: false
agents:
  a: {name: A, model: m1, tools: []}
models:
  m1: {name: m1, provider: p}
providers:
  p: {name: p, base_url: "https://example.com/v1", api_key_env: TEST_KEY}
"""


def test_a_bound_config_keeps_its_workspace_when_the_setter_is_called(tmp_path, monkeypatch):
    from core.config.config import Config

    monkeypatch.setenv("TEST_KEY", "x")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    alice = tmp_path / "users" / "alice" / "workspace"
    alice.mkdir(parents=True)
    bob = tmp_path / "users" / "bob" / "workspace"
    bob.mkdir(parents=True)

    config = Config(str(config_path), workspace_binding=str(alice))
    config.set_working_directory(str(bob))

    assert Path(config.get_working_directory()) == alice.resolve()
    assert Path(config.get_working_directory()) != bob.resolve()


# -- container network: absent, wrongly labelled, wrongly optioned, extra --------


class BareClient:
    """A Docker-compatible client that manages no networks (a shim)."""

    def __init__(self):
        self.runs = []
        self.containers = self
        self.images = SimpleNamespace(get=lambda name: SimpleNamespace(id="sha256:current"))

    def get(self, name):
        raise module.NotFound("missing")

    def list(self, all=False, filters=None):
        return []

    def run(self, image, command=None, **kwargs):
        self.runs.append(kwargs)
        return b"1000\n1000\n" if command is not None else SimpleNamespace(id="c1")


@pytest.fixture(autouse=True)
def no_proxy_from_the_environment(monkeypatch):
    for name in module.PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _manager(monkeypatch, client):
    monkeypatch.setattr(module, "docker", SimpleNamespace(from_env=lambda: client))
    monkeypatch.setattr(module, "_AGENT_IDS", {})
    manager = module.ContainerManager(
        SimpleNamespace(config=SimpleNamespace(isolation=IsolationConfig(enabled=True)))
    )
    assert manager.enabled
    return manager


@pytest.fixture
def fake_client(monkeypatch):
    from tests.test_security_container_network import FakeClient

    client = FakeClient()
    return _manager(monkeypatch, client), client


def test_a_client_without_a_networks_api_gets_no_container(monkeypatch, tmp_path):
    client = BareClient()
    manager = _manager(monkeypatch, client)

    assert manager.get_or_create_container("u1", workspace=tmp_path) is None
    assert client.runs == []


@pytest.mark.parametrize(
    "labels,driver,options",
    [
        # Another install made it: the root label is not this Grid's.
        (
            {TENANT_NETWORK_LABEL: tenant_network_name("u1"), NETWORK_ROOT_LABEL: "another-grid-copy"},
            "bridge",
            {TENANT_NETWORK_ICC_OPTION: "false"},
        ),
        # This install's labels, but container-to-container traffic is on.
        (
            {TENANT_NETWORK_LABEL: tenant_network_name("u1")},
            "bridge",
            {TENANT_NETWORK_ICC_OPTION: "true"},
        ),
        # This install's labels and options on another driver.
        (
            {TENANT_NETWORK_LABEL: tenant_network_name("u1")},
            "host",
            {TENANT_NETWORK_ICC_OPTION: "false"},
        ),
    ],
)
def test_a_network_that_is_not_the_one_grid_makes_is_refused(fake_client, tmp_path, labels, driver, options):
    manager, client = fake_client
    name = tenant_network_name("u1")
    # Fill in this install's root label where the case is about options/driver.
    if labels.get(NETWORK_ROOT_LABEL) is None:
        labels = {**labels, NETWORK_ROOT_LABEL: module._digest(str(module.GRID_ROOT))}
    client.networks.create(name, driver=driver, labels=labels, options=options)

    assert manager.get_or_create_container("u1", workspace=tmp_path) is None
    assert client.runs == []


def test_a_container_with_an_extra_network_attached_is_recreated(fake_client, tmp_path):
    manager, client = fake_client
    first = manager.get_or_create_container("u1", workspace=tmp_path)
    assert first is not None
    first.attrs["NetworkSettings"]["Networks"]["bridge"] = {}

    rebuilt = manager.get_or_create_container("u1", workspace=tmp_path)

    assert first.removed
    assert rebuilt is not None and rebuilt is not first
    assert client.runs[-1]["network"] == tenant_network_name("u1")

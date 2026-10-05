"""Every agent of a factory reaches the same tracker, in the factory's container."""

from types import SimpleNamespace

from tools import beads_tools


def test_container_comes_from_the_run_first():
    ctx = SimpleNamespace(context=SimpleNamespace(container_id="run-c", factory=None))
    assert beads_tools._get_container_id(ctx) == "run-c"


def test_a_run_without_container_falls_back_to_its_factory():
    # A run context built without the container (a dynamic agent started by an
    # older caller) must not send bd to the host: a different bd build there
    # would open the same mounted database.
    factory = SimpleNamespace(container_id="factory-c")
    ctx = SimpleNamespace(context=SimpleNamespace(container_id=None, factory=factory))
    assert beads_tools._get_container_id(ctx) == "factory-c"


def test_no_container_anywhere_means_the_host(monkeypatch):
    import utils.path_utils

    monkeypatch.setattr(utils.path_utils, "get_current_factory", lambda: None)
    ctx = SimpleNamespace(context=SimpleNamespace(container_id=None, factory=None))
    assert beads_tools._get_container_id(ctx) is None

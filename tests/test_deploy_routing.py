"""The server catalog deploy/routing.yaml follows routing.yaml, less host-only systems."""

from pathlib import Path

import yaml

from core.config import Config
from core.routing import AutoRouter

ROOT = Path(__file__).resolve().parent.parent
HOST_ONLY = {"desktop", "video"}


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_server_catalog_is_the_main_one_without_host_only_systems():
    main, server = _load(ROOT / "routing.yaml"), _load(ROOT / "deploy" / "routing.yaml")

    main_systems = main["routing"].pop("systems")
    server_systems = server["routing"].pop("systems")
    # Relative to each file; test_server_catalog_paths_resolve_like_the_main_ones compares them.
    main["routing"].pop("systems_dir")
    server["routing"].pop("systems_dir")
    main["settings"]["action_policy"].pop("policy_file")
    server["settings"]["action_policy"].pop("policy_file")
    assert server == main
    assert set(server_systems) == set(main_systems) - HOST_ONLY
    for name, system in server_systems.items():
        assert system["description"] == main_systems[name]["description"]


def test_server_catalog_paths_resolve_like_the_main_ones():
    main = AutoRouter.from_config(Config(str(ROOT / "routing.yaml")))
    server = AutoRouter.from_config(Config(str(ROOT / "deploy" / "routing.yaml")))

    for name in server.systems():
        assert server.system_config_path(name) == main.system_config_path(name)
    assert server.created_store().root == main.created_store().root == ROOT / "systems"

    def policy(config):
        loaded = config.root_config.config.settings.action_policy
        return {k: v for k, v in loaded.model_dump().items() if k != "policy_file"}

    assert policy(server) == policy(main)

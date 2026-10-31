"""The deployed systems declare real, provider-independent model reserves.

A chain is a promise: a plan or a provider that runs out of quota must not stop
a run. These tests read the actual configs, so a chain that names an undefined
model, an undeclared provider, or only models of the provider that just ran out
fails here instead of in the middle of a chat.
"""

from pathlib import Path

import pytest

from core.config import Config

ROOT = Path(__file__).resolve().parents[1]

# Each system and the agents whose chains carry work. `systems/` is deployed
# data (gitignored), so its config is checked when present.
SYSTEMS = {
    "examples/coder/config.yaml": [
        "engineer",
        "general_purpose",
        "web_spider",
    ],
    "examples/system-builder/config.yaml": ["builder"],
    "systems/artex-explorer/config.yaml": [
        "main",
        "decomposer",
        "worker",
        "verifier",
    ],
}


def _config(relative_path: str) -> Config:
    path = ROOT / relative_path
    if not path.exists():
        pytest.skip(f"{relative_path} is not deployed in this checkout")
    return Config(str(path))


def _chain(config: Config, agent_key: str):
    keys = config.get_agent(agent_key).model_keys()
    return keys, [config.get_model(key).provider for key in keys]


@pytest.mark.parametrize("relative_path,agents", SYSTEMS.items())
def test_every_model_and_provider_in_every_chain_exists(relative_path, agents):
    config = _config(relative_path)
    for agent in agents:
        keys, providers = _chain(config, agent)
        assert keys, f"{agent}: empty model chain"
        assert len(keys) == len(set(keys)), f"{agent}: duplicate model in chain"
        for key, provider in zip(keys, providers):
            # get_model / get_provider raise ConfigError on an unknown name.
            assert config.get_model(key).provider == provider
            config.get_provider(provider)


@pytest.mark.parametrize("relative_path,agents", SYSTEMS.items())
def test_a_paid_openrouter_route_is_never_the_only_way_out(relative_path, agents):
    """A chain that names OpenRouter must cross providers, and a paid primary
    must have a reserve from another provider behind it."""
    config = _config(relative_path)
    for agent in agents:
        keys, providers = _chain(config, agent)
        if "openrouter" not in providers:
            continue
        assert len(set(providers)) > 1, (
            f"{agent}: every model sits on openrouter in {keys}"
        )
        if providers[0] == "openrouter":
            assert any(provider != "openrouter" for provider in providers[1:]), (
                f"{agent}: the paid primary has no reserve in {keys}"
            )


def test_engineer_chain_crosses_three_providers():
    """Direct Kimi, then ChatGPT, then OpenCode: no single provider ends a run."""
    config = _config("examples/coder/config.yaml")
    keys, providers = _chain(config, "engineer")

    assert keys == ["kimi-k3.1-pro", "gpt-6-luna", "deepseek-flash-latest"]
    assert providers == ["kimi", "chatgpt", "opencode"]
    assert len(set(providers)) == len(providers), (
        "each engineer model must belong to its own provider"
    )


def test_builder_reserve_crosses_providers():
    config = _config("examples/system-builder/config.yaml")
    keys, providers = _chain(config, "builder")

    assert keys[0] == "glm-latest", "the primary model stays as it was"
    assert providers[0] == "opencode"
    assert "kimi" in providers, "the last resort crosses the OpenCode provider"


def test_artex_chains_start_on_opencode_and_keep_a_cross_provider_reserve():
    config = _config("systems/artex-explorer/config.yaml")
    for agent in ("main", "decomposer", "worker", "verifier"):
        keys, providers = _chain(config, agent)
        assert providers[0] == "opencode", f"{agent}: {keys}"
        assert len(set(providers)) > 1, f"{agent}: chain stays inside one provider"

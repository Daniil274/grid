"""Tests for external provider and model YAML registries (core.model_registry)."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from core.config import Config
from core.factory.models import ModelProvider
from core.model_registry import materialize_document, resolve_document
from schemas import GridConfig, ProviderConfig, ProviderUsageConfig
from utils.exceptions import ConfigError


def _write_yaml(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_config_loads_providers_and_models_from_external_files_with_use_and_refine(tmp_path):
    _write_yaml(
        tmp_path / "catalog" / "providers.yaml",
        {
            "version": 1,
            "providers": {
                "openrouter": {
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key_env": "OPENROUTER_API_KEY",
                    "usage": {
                        "mode": "openrouter_key",
                        "limits": {"month_usd": 50.0},
                    },
                },
                "opencode": {
                    "base_url": "https://opencode.ai/zen/go/v1",
                    "api_key_env": "OPENCODE_API_KEY",
                    "usage": {
                        "mode": "metered",
                        "windows": {"five_hour": 1_000_000},
                    },
                },
            },
        },
    )
    _write_yaml(
        tmp_path / "catalog" / "models.yaml",
        {
            "version": 1,
            "models": {
                "glm-latest": {
                    "name": "glm-5.3",
                    "provider": "opencode",
                    "max_tokens": 64000,
                    "context_window": 1000000,
                    "temperature": 0.7,
                },
                "router": {
                    "name": "typesafe/jev-1.13",
                    "provider": "openrouter",
                    "max_tokens": 1024,
                    "context_window": 32000,
                },
            },
        },
    )
    cfg_path = _write_yaml(
        tmp_path / "systems" / "demo" / "config.yaml",
        {
            "providers_file": "../../catalog/providers.yaml",
            "models_file": "../../catalog/models.yaml",
            "settings": {"default_agent": "main"},
            "providers": {"use": ["opencode", "openrouter"]},
            "models": {
                "use": ["glm-latest"],
                "refine": {"glm-latest": {"temperature": 0.2}},
            },
            "tools": {},
            "agents": {
                "main": {
                    "name": "Main",
                    "model": "glm-latest",
                    "tools": [],
                }
            },
        },
    )

    config = Config(str(cfg_path))
    assert set(config.config.providers) == {"opencode", "openrouter"}
    assert config.get_provider("openrouter").name == "openrouter"
    assert config.get_provider("openrouter").usage.mode == "openrouter_key"
    assert config.get_provider("openrouter").usage.limits.month_usd == 50.0
    assert config.get_provider("opencode").usage.windows == {"five_hour": 1_000_000}
    assert set(config.config.models) == {"glm-latest"}
    assert config.get_model("glm-latest").temperature == 0.2
    assert config.get_model("glm-latest").name == "glm-5.3"


def test_layered_registry_files_and_inline_overrides(tmp_path):
    base_models = _write_yaml(
        tmp_path / "models.base.yaml",
        {
            "models": {
                "m1": {
                    "name": "base-m1",
                    "provider": "p1",
                    "max_tokens": 4000,
                    "context_window": 64000,
                    "temperature": 0.6,
                }
            }
        },
    )
    overlay_models = _write_yaml(
        tmp_path / "models.overlay.yaml",
        {
            "models": {
                "m1": {
                    "temperature": 0.3,
                    "description": "Overlay description",
                }
            }
        },
    )
    providers_file = _write_yaml(
        tmp_path / "providers.yaml",
        {"providers": {"p1": {"base_url": "https://example.com/v1"}}},
    )
    raw = {
        "providers_file": str(providers_file.name),
        "models_file": [str(base_models.name), str(overlay_models.name)],
        "providers": {"use": ["p1"]},
        "models": {
            "use": ["m1"],
            "m1": {"max_tokens": 8000},
        },
    }
    resolved = resolve_document(raw, tmp_path / "config.yaml")
    assert resolved["models"]["m1"]["name"] == "base-m1"
    assert resolved["models"]["m1"]["temperature"] == 0.3
    assert resolved["models"]["m1"]["description"] == "Overlay description"
    assert resolved["models"]["m1"]["max_tokens"] == 8000


def test_unknown_key_in_use_or_refine_raises_clear_config_error(tmp_path):
    _write_yaml(
        tmp_path / "providers.yaml",
        {"providers": {"openrouter": {"base_url": "https://openrouter.ai/api/v1"}}},
    )
    with pytest.raises(ConfigError, match="Unknown provider key.*missing.*Available in registry: openrouter"):
        resolve_document(
            {
                "providers_file": "providers.yaml",
                "providers": {"use": ["missing"]},
            },
            tmp_path / "config.yaml",
        )


def test_disallow_route_override_rejects_provider_or_name_change_in_refine(tmp_path):
    _write_yaml(
        tmp_path / "models.yaml",
        {"models": {"m1": {"name": "model-1", "provider": "openrouter"}}},
    )
    with pytest.raises(ConfigError, match="Cannot override provider"):
        resolve_document(
            {
                "models_file": "models.yaml",
                "models": {"use": ["m1"], "refine": {"m1": {"provider": "other"}}},
            },
            tmp_path / "config.yaml",
            allow_route_override=False,
        )


def test_materialize_document_Produces_standalone_config(tmp_path):
    _write_yaml(
        tmp_path / "catalog" / "providers.yaml",
        {"providers": {"openrouter": {"base_url": "https://openrouter.ai/api/v1"}}},
    )
    _write_yaml(
        tmp_path / "catalog" / "models.yaml",
        {"models": {"m1": {"name": "model-1", "provider": "openrouter"}}},
    )
    doc = {
        "providers_file": "catalog/providers.yaml",
        "models_file": "catalog/models.yaml",
        "providers": {"use": ["openrouter"]},
        "models": {"use": ["m1"]},
    }
    standalone = materialize_document(doc, tmp_path / "config.yaml")
    assert "providers_file" not in standalone
    assert "models_file" not in standalone
    cfg = GridConfig(**standalone)
    assert cfg.providers["openrouter"].name == "openrouter"
    assert cfg.models["m1"].provider == "openrouter"


def test_grid_config_rejects_unexpanded_directives_and_fills_provider_name():
    with pytest.raises(ValidationError, match="registry directives"):
        GridConfig(providers={"use": ["openrouter"]})

    cfg = GridConfig(providers={"openrouter": {"base_url": "https://openrouter.ai/api/v1"}})
    assert cfg.providers["openrouter"].name == "openrouter"
    assert isinstance(cfg.providers["openrouter"], ProviderConfig)
    assert isinstance(cfg.providers["openrouter"].usage, ProviderUsageConfig)


def test_two_keys_of_one_gateway_model_carry_their_own_effort(tmp_path):
    """The same model at two reasoning efforts: two catalog keys, one name.

    ``glm-latest`` stays the coordinator's high-effort model while
    ``glm-latest-medium`` serves the executors. Both are valid keys of the
    gateway model, and each key's ``reasoning.effort`` reaches ModelSettings.
    """
    _write_yaml(
        tmp_path / "catalog" / "providers.yaml",
        {"providers": {"openrouter": {"base_url": "https://openrouter.ai/api/v1"}}},
    )
    _write_yaml(
        tmp_path / "catalog" / "models.yaml",
        {
            "version": 1,
            "models": {
                "glm-latest": {
                    "name": "~z-ai/glm-latest",
                    "provider": "openrouter",
                    "max_tokens": 131072,
                    "context_window": 1000000,
                    "reasoning": {"effort": "high"},
                },
                "glm-latest-medium": {
                    "name": "~z-ai/glm-latest",
                    "provider": "openrouter",
                    "max_tokens": 131072,
                    "context_window": 1000000,
                    "reasoning": {"effort": "medium"},
                },
            },
        },
    )
    cfg_path = _write_yaml(
        tmp_path / "systems" / "demo" / "config.yaml",
        {
            "providers_file": "../../catalog/providers.yaml",
            "models_file": "../../catalog/models.yaml",
            "providers": {"use": ["openrouter"]},
            "models": {"use": ["glm-latest", "glm-latest-medium"]},
            "tools": {},
            "agents": {
                "main": {"name": "Main", "model": "glm-latest", "tools": []},
            },
        },
    )

    config = Config(str(cfg_path))

    # Both keys are usable and point at the very same gateway model.
    assert set(config.config.models) == {"glm-latest", "glm-latest-medium"}
    high = config.get_model("glm-latest")
    medium = config.get_model("glm-latest-medium")
    assert high.name == medium.name == "~z-ai/glm-latest"
    assert high.reasoning == {"effort": "high"}
    assert medium.reasoning == {"effort": "medium"}

    # And each key's effort is what the SDK call is built with.
    models = ModelProvider(config=config, runtime_support=None, compact_config=None)
    assert models.settings(high).reasoning.effort == "high"
    assert models.settings(medium).reasoning.effort == "medium"

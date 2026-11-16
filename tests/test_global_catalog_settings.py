"""Global definitions are edited separately; systems keep only selections."""
import pytest
import yaml

from web_chat.deployment import Deployment, MODELS_TARGET, PROVIDERS_TARGET
from web_chat.schemas import SettingsStructuredUpdateRequest, SettingsYamlUpdateRequest


@pytest.fixture
def registry_deployment(tmp_path):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "providers.yaml").write_text(yaml.safe_dump({"providers": {
        "p": {"name": "p", "base_url": "https://example.com/v1", "api_key_env": "TEST_KEY"},
        "unused": {"name": "unused", "base_url": "https://example.com/v1", "api_key_env": "TEST_KEY"},
    }}))
    (catalog / "models.yaml").write_text(yaml.safe_dump({"models": {
        "first": {"name": "first", "provider": "p"},
        "fallback": {"name": "fallback", "provider": "p"},
        "unused": {"name": "unused", "provider": "p"},
    }}))
    system = tmp_path / "config.yaml"
    system.write_text(yaml.safe_dump({
        "settings": {"default_agent": "main"},
        "agents": {"main": {"name": "Main", "model": ["first", "fallback"], "tools": []}},
        "models_file": "catalog/models.yaml", "providers_file": "catalog/providers.yaml",
        "models": {"use": ["first", "fallback"]},
    }))
    return Deployment(config_path=str(system), routing_path=None)


def test_full_registry_not_system_selection(registry_deployment):
    deployment = registry_deployment
    assert deployment.config_file(MODELS_TARGET).path.name == "models.yaml"
    assert set(deployment.config_dict(MODELS_TARGET)["models"]) == {"first", "fallback", "unused"}
    assert deployment.config_dict()["models"] == {"use": ["first", "fallback"]}


def test_edit_only_registry_preserves_system(registry_deployment):
    deployment = registry_deployment
    before = deployment.config_path.read_bytes()
    payload = deployment.config_dict(MODELS_TARGET)
    payload["models"]["first"]["name"] = "new-model-id"
    deployment.save_structured_config(payload, MODELS_TARGET)
    assert deployment.config_path.read_bytes() == before
    assert deployment.config_dict(MODELS_TARGET)["models"]["first"]["name"] == "new-model-id"
    assert deployment.config.config.models["first"].name == "new-model-id"


@pytest.mark.parametrize("key", ["first", "fallback"])
def test_used_model_deletion_is_rejected(registry_deployment, key):
    deployment = registry_deployment
    path = deployment.config_file(MODELS_TARGET).path
    before = path.read_bytes()
    payload = deployment.config_dict(MODELS_TARGET)
    del payload["models"][key]
    with pytest.raises(Exception):
        deployment.save_structured_config(payload, MODELS_TARGET)
    assert path.read_bytes() == before


def test_provider_deletion_and_unknown_provider_rejected(registry_deployment):
    deployment = registry_deployment
    payload = deployment.config_dict(PROVIDERS_TARGET)
    del payload["providers"]["p"]
    with pytest.raises(ValueError, match="unknown provider"):
        deployment.save_structured_config(payload, PROVIDERS_TARGET)
    payload = deployment.config_dict(MODELS_TARGET)
    payload["models"]["first"]["provider"] = "missing"
    with pytest.raises(ValueError, match="unknown provider"):
        deployment.save_structured_config(payload, MODELS_TARGET)


def test_unused_deletion_and_yaml_save(registry_deployment):
    deployment = registry_deployment
    payload = deployment.config_dict(MODELS_TARGET)
    del payload["models"]["unused"]
    deployment.save_yaml_config(yaml.safe_dump(payload), MODELS_TARGET)
    assert set(deployment.config_dict(MODELS_TARGET)["models"]) == {"first", "fallback"}


def test_multi_file_registry_not_writable(registry_deployment, monkeypatch):
    deployment = registry_deployment
    registry = deployment.model_registry()
    monkeypatch.setenv("GRID_MODELS_FILE", ":".join([str(registry.model_files[0])] * 2))
    # Explicit config reference wins over the environment. Test layered explicit files.
    raw = deployment.config_dict()
    raw["models_file"] = ["catalog/models.yaml", "catalog/models.yaml"]
    deployment.config_path.write_text(yaml.safe_dump(raw))
    assert MODELS_TARGET not in {file.key for file in deployment.config_files()}


def test_revision_survives_request_validation():
    assert SettingsStructuredUpdateRequest(config={}, revision="v1").revision == "v1"
    assert SettingsYamlUpdateRequest(yaml_content="{}", revision="v2").revision == "v2"


@pytest.mark.parametrize("section", ["models", "providers"])
def test_system_cannot_edit_definitions_even_via_yaml(registry_deployment, section):
    deployment = registry_deployment
    before = deployment.config_path.read_bytes()
    raw = deployment.config_dict()
    raw.setdefault(section, {})["injected"] = {"name": "injected", "provider": "p"}
    with pytest.raises(ValueError, match="global catalog"):
        deployment.save_yaml_config(yaml.safe_dump(raw))
    assert deployment.config_path.read_bytes() == before

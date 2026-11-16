"""Registry configs work in the builder, copied systems and usage cards."""

import pytest
import yaml

from core.config import Config
from core.system_store import BuilderAccess, StoreError, SystemStore, canonicalize_config
from web_chat.deployment import Deployment
from web_chat.system_hub import _validate, blank_config, copy_config


@pytest.fixture
def registry_config(tmp_path):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "providers.yaml").write_text(yaml.safe_dump({"providers": {"p": {
        "base_url": "https://example.com/v1", "api_key_env": "TEST_PROVIDER_KEY",
        "default_headers": {"Authorization": "Bearer secret"},
        "usage": {"mode": "metered", "note": "Initial usage policy"},
    }}}))
    (catalog / "models.yaml").write_text(yaml.safe_dump({"models": {"m": {
        "name": "model", "provider": "p",
    }}}))
    source = tmp_path / "base" / "config.yaml"
    source.parent.mkdir()
    source.write_text(yaml.safe_dump({
        "providers_file": "../catalog/providers.yaml",
        "models_file": ["../catalog/models.yaml"],
        "providers": {"use": ["p"]}, "models": {"use": ["m"]},
        "settings": {"default_agent": "main"},
        "agents": {"main": {"name": "Main", "model": "m", "tools": []}},
    }))
    return source


def _access(source, *, shared=True):
    return BuilderAccess(
        store=SystemStore(source.parent.parent / "systems"), catalog=None,
        base_config=Config(str(source)), user_id="owner", image="grid-agent:latest",
        record=lambda *_: None, changed=lambda: None, shared=shared,
    )


def test_builder_catalog_expands_and_redacts_registry_providers(registry_config):
    providers = _access(registry_config).providers()
    assert set(providers) == {"p"}
    assert providers["p"]["usage"]["mode"] == "metered"
    assert providers["p"]["default_headers"]["Authorization"] == "<hidden>"
    # Reading an example's directives must not treat its use list as a provider.
    assert BuilderAccess.redact_providers({"use": ["p"]}) == {"use": ["p"]}
    refined = BuilderAccess.redact_providers({"refine": {"p": {"api_key": "secret"}}})
    assert refined["refine"]["p"]["api_key"] == "<hidden: use api_key_env>"


def test_blank_system_from_registry_config_is_standalone(registry_config, tmp_path):
    text = blank_config(Config(str(registry_config)))
    document = yaml.safe_load(text)
    assert document["models"]["m"]["name"] == "model"
    assert document["providers"]["p"]["base_url"] == "https://example.com/v1"
    path = tmp_path / "blank.yaml"
    path.write_text(text)
    assert Config(str(path)).get_model("m").provider == "p"


def test_copied_config_keeps_registry_paths_at_a_different_depth(registry_config, tmp_path):
    target = tmp_path / "nested" / "systems" / "copy" / "config.yaml"
    text = copy_config(registry_config, target.parent, None)
    _validate(text, target)
    document = yaml.safe_load(text)
    assert (target.parent / document["providers_file"]).resolve() == tmp_path / "catalog" / "providers.yaml"
    assert (target.parent / document["models_file"][0]).resolve() == tmp_path / "catalog" / "models.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(text)
    assert Config(str(target)).get_model("m").name == "model"


def test_builder_validates_shared_registry_configs_and_reports_bad_keys(registry_config):
    access = _access(registry_config)
    path = access.store.config_path("draft")
    text = copy_config(registry_config, path.parent, None)
    canonical = canonicalize_config(text, path)
    assert yaml.safe_load(canonical)["models"] == {"use": ["m"]}
    access.validate(canonical, "draft")
    broken = yaml.safe_load(canonical)
    broken["models"]["use"] = ["missing"]
    with pytest.raises(StoreError, match="Unknown model"):
        canonicalize_config(yaml.safe_dump(broken), path)
    with pytest.raises(StoreError, match="Unknown model"):
        _validate(yaml.safe_dump(broken), path)


def test_private_system_cannot_reference_arbitrary_registry_files(registry_config):
    with pytest.raises(StoreError, match="registry file references are not allowed"):
        _access(registry_config, shared=False).validate(registry_config.read_text(), "draft")


def test_usage_reloads_registry_when_only_the_provider_file_changes(registry_config):
    deployment = Deployment(config_path=str(registry_config))
    assert deployment.usage_config().providers["p"].usage.note == "Initial usage policy"
    path = registry_config.parent.parent / "catalog" / "providers.yaml"
    document = yaml.safe_load(path.read_text())
    document["providers"]["p"]["usage"]["note"] = "Changed usage policy"
    path.write_text(yaml.safe_dump(document))
    assert deployment.usage_config().providers["p"].usage.note == "Changed usage policy"

"""The configuration page's files: the catalog and every system, edited in place."""

from pathlib import Path

import pytest
import yaml

from web_chat.deployment import CATALOG_TARGET, Deployment, merge_yaml

SYSTEM = """\
# The coder system.
settings:
  default_agent: coder
  # Rounds of tool calls per message.
  max_turns: 5
  working_directory: .
agents:
  coder:
    name: Coder
    model: m1
    tools: []
    description: >-
      Writes code
      and reviews it.
models:
  m1:
    name: m1
    provider: openrouter
providers:
  openrouter:
    name: openrouter
    base_url: https://example.com/v1
    api_key_env: TEST_OPENROUTER_KEY
"""

CATALOG = """\
# Shared by every system.
providers:
  openrouter:
    name: openrouter
    base_url: https://example.com/v1
    api_key_env: TEST_OPENROUTER_KEY
models:
  router:
    name: decisions-model
    provider: openrouter
routing:
  model: router
  api: decisions
  default_system: coder
  systems:
    coder:
      config: coder/config.yaml
      description: Code.
    writer:
      config: writer/config.yaml
      description: Prose.
"""


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    for name in ("coder", "writer"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "config.yaml").write_text(SYSTEM.replace("coder", name), encoding="utf-8")
    path = tmp_path / "routing.yaml"
    path.write_text(CATALOG, encoding="utf-8")
    return path


def test_every_system_and_the_catalog_can_be_opened(catalog: Path) -> None:
    deployment = Deployment(routing_path=str(catalog))

    files = deployment.config_files()

    assert [(file.key, file.kind) for file in files] == [
        (CATALOG_TARGET, "catalog"),
        ("coder", "system"),
        ("writer", "system"),
    ]
    assert files[2].path == (catalog.parent / "writer" / "config.yaml").resolve()
    # No target is the default system, as before files could be chosen.
    assert deployment.config_file(None).key == "coder"
    assert deployment.config_dict("writer")["settings"]["default_agent"] == "writer"
    with pytest.raises(KeyError):
        deployment.config_file("missing")


def test_a_single_system_is_the_only_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    config = tmp_path / "coder" / "config.yaml"
    config.parent.mkdir()
    config.write_text(SYSTEM, encoding="utf-8")

    deployment = Deployment(config_path=str(config), routing_path=None)

    assert [(file.key, file.kind) for file in deployment.config_files()] == [("coder", "system")]
    assert deployment.config_file(None).path == config.resolve()


def test_saving_a_system_other_than_the_default(catalog: Path) -> None:
    deployment = Deployment(routing_path=str(catalog))
    config = deployment.config_dict("writer")
    config["settings"]["max_turns"] = 9

    deployment.save_structured_config(config, "writer")

    assert deployment.config_dict("writer")["settings"]["max_turns"] == 9
    assert deployment.config_dict("coder")["settings"]["max_turns"] == 5


def test_saving_the_catalog_reloads_it(catalog: Path) -> None:
    deployment = Deployment(routing_path=str(catalog))
    config = deployment.config_dict(CATALOG_TARGET)
    config["routing"]["default_system"] = "writer"

    deployment.save_structured_config(config, CATALOG_TARGET)

    assert deployment.catalog.config.routing.default_system == "writer"
    assert deployment.config_path == (catalog.parent / "writer" / "config.yaml").resolve()


def test_an_invalid_config_is_not_written(catalog: Path) -> None:
    deployment = Deployment(routing_path=str(catalog))
    before = (catalog.parent / "writer" / "config.yaml").read_text(encoding="utf-8")
    config = deployment.config_dict("writer")
    config["settings"]["max_turns"] = 0

    with pytest.raises(ValueError):
        deployment.save_structured_config(config, "writer")

    assert (catalog.parent / "writer" / "config.yaml").read_text(encoding="utf-8") == before


def test_a_form_save_keeps_the_comments_and_layout() -> None:
    config = yaml.safe_load(SYSTEM)
    config["settings"]["max_turns"] = 7
    config["agents"]["coder"]["tools"] = ["bash"]

    text = merge_yaml(SYSTEM, config)

    assert "# The coder system." in text
    assert "  # Rounds of tool calls per message.\n  max_turns: 7\n" in text
    # An untouched folded string stays folded.
    assert "    description: >-\n      Writes code\n      and reviews it.\n" in text
    assert yaml.safe_load(text) == config


def test_a_form_save_removes_what_the_form_removed() -> None:
    config = yaml.safe_load(SYSTEM)
    del config["settings"]["working_directory"]
    config["settings"]["debug"] = True

    assert yaml.safe_load(merge_yaml(SYSTEM, config)) == config


def test_a_boolean_is_not_mistaken_for_a_number() -> None:
    text = "settings:\n  max_turns: 1\n"

    assert yaml.safe_load(merge_yaml(text, {"settings": {"max_turns": True}})) == {"settings": {"max_turns": True}}


def test_an_unchanged_file_is_written_back_as_it_was(catalog: Path) -> None:
    assert merge_yaml(SYSTEM, yaml.safe_load(SYSTEM)) == SYSTEM
    assert merge_yaml(CATALOG, yaml.safe_load(CATALOG)) == CATALOG

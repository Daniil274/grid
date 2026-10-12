"""Prompt caching is requested for Claude models (ModelConfig.prompt_cache).

The wire field is provider/API specific: only an HTTPS OpenRouter Chat request
supports it, so every case here runs through a configured provider. Capability
gating itself is covered by ``test_review_cache_capability``.
"""

from types import SimpleNamespace

from core.factory.models import ModelProvider
from schemas.schemas import ModelConfig

CACHE = {"type": "ephemeral"}
OPENROUTER = "https://openrouter.ai/api/v1"


def settings_for(**fields):
    config = SimpleNamespace(
        get_provider=lambda key: SimpleNamespace(base_url=OPENROUTER, auth="api_key")
    )
    return ModelProvider(config, None, None).settings(ModelConfig(provider="openrouter", **fields))


def test_claude_models_ask_for_prompt_caching():
    assert settings_for(name="anthropic/claude-sonnet-5.5").extra_body == {"cache_control": CACHE}


def test_other_models_do_not_unless_configured():
    assert settings_for(name="deepseek/deepseek-v4.1-flash").extra_body is None
    assert settings_for(name="deepseek/deepseek-v4.1-flash", prompt_cache=True).extra_body == {
        "cache_control": CACHE
    }


def test_caching_can_be_turned_off_for_claude():
    assert settings_for(name="anthropic/claude-opus-5.5", prompt_cache=False).extra_body is None


def test_unknown_endpoint_never_gets_cache_control():
    """A model name is not proof of API capability; fail closed without config."""
    config = SimpleNamespace(
        get_provider=lambda key: SimpleNamespace(base_url="https://api.openai.com/v1", auth="api_key")
    )
    settings = ModelProvider(config, None, None).settings(
        ModelConfig(provider="openai", name="anthropic/claude-sonnet-5.5")
    )
    assert settings.extra_body is None

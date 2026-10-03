import json

import pytest

from web_chat import prices

DOCUMENT = {
    "openai": {"api": None, "models": {"gpt": {"cost": {"input": 1, "output": 2}, "limit": {"context": 5}}, "free": {}}},
    "opencode-go": {"api": "https://opencode.ai/zen/go/v1", "models": {"kimi": {"cost": {"input": 1, "output": 3}}}},
    "other": {"models": {"x": {"cost": {"input": 9, "output": 9}}}},
}


def test_refresh_keeps_only_prices_of_the_wanted_providers(tmp_path):
    out = tmp_path / "sub" / "prices.json"
    book = prices.refresh(out, ["openai", "opencode-go"], source=lambda: DOCUMENT)
    saved = json.loads(out.read_text())
    assert set(saved) == {"openai", "opencode-go"}
    assert saved["openai"]["models"] == {"gpt": {"cost": {"input": 1, "output": 2}}}
    assert book.lookup("https://opencode.ai/zen/go/v1", "kimi").output == 3
    assert not list(out.parent.glob("*.tmp"))


def test_an_unknown_provider_is_an_error_and_writes_nothing(tmp_path):
    out = tmp_path / "prices.json"
    with pytest.raises(ValueError, match="nope"):
        prices.refresh(out, ["openai", "nope"], source=lambda: DOCUMENT)
    assert not out.exists()

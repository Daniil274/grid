import pytest

from core.credentials import Credential
from web_chat.accounts.store import AccountStore
from web_chat.vault import Cipher, ReauthRequired, Vault, VaultUnavailable


@pytest.fixture
def parts(tmp_path):
    store = AccountStore(tmp_path / "accounts.db")
    from web_chat.identity import User

    for user_id in ("u1", "u2"):
        store.add_user(User(id=user_id, username=user_id + "name", role="user"), "hash", 1.0)
    return store, Cipher([Cipher.new_key()])


def test_a_key_is_sealed_listed_without_its_secret_and_opened_for_its_owner(parts):
    store, cipher = parts
    vault = Vault(store, cipher)
    hint = vault.put_api_key("u1", "openrouter", "sk-or-v1-abcdefghijklmnop")
    assert hint == "…mnop"
    row = store.credential("u1", "openrouter")
    assert b"abcdefgh" not in row.ciphertext
    assert vault.listing("u1") == [{"kind": "openrouter", "hint": "…mnop", "status": "ok", "updated_at": row.updated_at}]
    assert vault.listing("u2") == []
    import asyncio

    found = asyncio.run(vault.credential("u1", "openrouter"))
    assert (found.secret, found.source, found.charged) == ("sk-or-v1-abcdefghijklmnop", "own", False)
    assert vault.has("u1", "openrouter") and not vault.has("u2", "openrouter")


@pytest.mark.asyncio
async def test_a_row_copied_to_another_user_does_not_open(parts):
    store, cipher = parts
    vault = Vault(store, cipher)
    vault.put_api_key("u1", "openai", "sk-aaaaaaaaaaaaaaaa")
    row = store.credential("u1", "openai")
    store.put_credential("u2", "openai", row.ciphertext, row.hint, 2.0)
    assert await vault.credential("u2", "openai") is None


@pytest.mark.asyncio
async def test_a_different_key_opens_nothing(parts):
    store, cipher = parts
    Vault(store, cipher).put_api_key("u1", "openai", "sk-aaaaaaaaaaaaaaaa")
    other = Cipher.from_environment({"GRID_SECRETS_KEY": Cipher.new_key()})
    assert await Vault(store, other).credential("u1", "openai") is None


@pytest.mark.asyncio
async def test_rotation_with_the_old_key_kept_behind_the_new_one(tmp_path):
    store = AccountStore(tmp_path / "a.db")
    from web_chat.identity import User

    store.add_user(User(id="u1", username="user1", role="user"), "h", 1.0)
    old_key, new_key = Cipher.new_key(), Cipher.new_key()
    Vault(store, Cipher([old_key])).put_api_key("u1", "openai", "sk-aaaaaaaaaaaaaaaa")
    rotated = Vault(store, Cipher.from_environment({"GRID_SECRETS_KEY": f"{new_key},{old_key}"}))
    assert (await rotated.credential("u1", "openai")).secret == "sk-aaaaaaaaaaaaaaaa"
    rotated.put_api_key("u1", "openai", "sk-bbbbbbbbbbbbbbbb")
    assert (await Vault(store, Cipher([new_key])).credential("u1", "openai")).secret == "sk-bbbbbbbbbbbbbbbb"


def test_without_a_secrets_key_nothing_is_stored(parts):
    store, _ = parts
    assert Cipher.from_environment({}) is None
    vault = Vault(store, None)
    assert not vault.enabled
    with pytest.raises(VaultUnavailable):
        vault.put_api_key("u1", "openai", "sk-aaaaaaaaaaaaaaaa")
    with pytest.raises(VaultUnavailable, match="not a valid key"):
        Cipher.from_environment({"GRID_SECRETS_KEY": "short"})


def test_only_provider_presets_take_a_key(parts):
    store, cipher = parts
    with pytest.raises(ValueError):
        Vault(store, cipher).put_api_key("u1", "evil", "sk-aaaaaaaaaaaaaaaa")


class Login:
    """A subscription login whose access token lasts until its `exp`."""

    def __init__(self):
        self.refreshes = 0

    async def credential(self, state, now):
        if state["exp"] > now:
            return Credential(state["access"], "chatgpt", charged=False, subscription=True), None
        if state["refresh"] == "dead":
            raise ReauthRequired()
        self.refreshes += 1
        renewed = {**state, "access": f"fresh-{self.refreshes}", "exp": now + 3600}
        return Credential(renewed["access"], "chatgpt", charged=False, subscription=True), renewed


@pytest.mark.asyncio
async def test_an_expired_login_is_refreshed_once_and_the_new_state_stored(parts):
    import asyncio

    store, cipher = parts
    login = Login()
    vault = Vault(store, cipher, oauth={"chatgpt": login}, clock=lambda: 1000.0)
    vault.put_state("u1", "chatgpt", {"access": "old", "refresh": "r1", "exp": 500.0})
    first, second = await asyncio.gather(vault.credential("u1", "chatgpt"), vault.credential("u1", "chatgpt"))
    assert (first.secret, second.secret) == ("fresh-1", "fresh-1") and login.refreshes == 1
    assert (await vault.credential("u1", "chatgpt")).subscription is True


@pytest.mark.asyncio
async def test_a_login_that_cannot_refresh_is_marked_for_reauth_not_retried(parts):
    store, cipher = parts
    vault = Vault(store, cipher, oauth={"chatgpt": Login()}, clock=lambda: 1000.0)
    vault.put_state("u1", "chatgpt", {"access": "old", "refresh": "dead", "exp": 500.0})
    assert await vault.credential("u1", "chatgpt") is None
    assert vault.listing("u1")[0]["status"] == "needs_reauth"
    assert not vault.has("u1", "chatgpt")


def test_a_file_store_keeps_sealed_rows_owner_only_and_works_as_a_vault_store(tmp_path):
    import os
    import stat

    from web_chat.vault import FileCredentialStore

    path = tmp_path / "data" / "credentials.json"
    vault = Vault(FileCredentialStore(path), Cipher.local_file(tmp_path / "data" / "secrets.key"))
    vault.put_api_key("me", "openai", "sk-aaaaaaaaaaaaaaaa")
    assert "aaaaaaaa" not in path.read_text()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "data" / "secrets.key").st_mode) == 0o600
    reopened = Vault(FileCredentialStore(path), Cipher.local_file(tmp_path / "data" / "secrets.key"))
    import asyncio

    assert asyncio.run(reopened.credential("me", "openai")).secret == "sk-aaaaaaaaaaaaaaaa"
    assert reopened.listing("me")[0]["kind"] == "openai" and reopened.listing("other") == []
    assert reopened.delete("me", "openai") and not reopened.delete("me", "openai")

"""Accounts: passwords, invites, sessions, roles and the rate limit."""

import sqlite3

import pytest

from web_chat.accounts import passwords
from web_chat.accounts.limits import AttemptLimiter
from web_chat.accounts.service import (
    SESSION_IDLE,
    SESSION_MAX_AGE,
    SESSION_TOUCH_INTERVAL,
    AccountError,
    Accounts,
    SignInFailed,
)
from web_chat.accounts.store import AccountStore

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch):
    """The lowest scrypt cost the parser accepts: the rules, not the cost, are tested."""
    monkeypatch.setattr(passwords, "LOG2_N", 10)


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "accounts.db"


@pytest.fixture
def accounts(db_path, clock):
    store = AccountStore(db_path)
    yield Accounts(store, clock=clock)
    store.close()


# -- passwords -------------------------------------------------------------------


def test_a_password_verifies_against_its_own_hash_only():
    stored = passwords.hash_password(PASSWORD)

    assert passwords.verify_password(PASSWORD, stored)
    assert not passwords.verify_password(PASSWORD + "!", stored)
    assert stored != passwords.hash_password(PASSWORD)  # salted


@pytest.mark.parametrize(
    "stored",
    ["", "plain", "scrypt$15$8$1$@@@$@@@", "scrypt$30$8$1$AAAA$AAAA", "bcrypt$15$8$1$AAAA$AAAA"],
)
def test_a_malformed_or_foreign_hash_never_matches(stored):
    assert not passwords.verify_password(PASSWORD, stored)
    assert passwords.needs_rehash(stored)


def test_a_hash_made_at_another_cost_asks_to_be_redone(monkeypatch):
    stored = passwords.hash_password(PASSWORD)
    monkeypatch.setattr(passwords, "LOG2_N", 11)

    assert passwords.verify_password(PASSWORD, stored)
    assert passwords.needs_rehash(stored)


@pytest.mark.parametrize(
    "password, username",
    [("short", ""), (" padded password ", ""), ("aaaaaaaaaaaa", ""), ("SameAsName99", "samease"), ("x" * 257, "")],
)
def test_weak_passwords_are_refused(password, username):
    if username:
        username = password.lower()
    with pytest.raises(passwords.WeakPassword):
        passwords.check_strength(password, username=username)


# -- users and invites -----------------------------------------------------------


def test_usernames_are_unique_regardless_of_case(accounts):
    accounts.create_user("Alice", PASSWORD)

    with pytest.raises(AccountError, match="taken"):
        accounts.create_user("alice", PASSWORD)


@pytest.mark.parametrize("username", ["ab", "a" * 33, "-alice", "al ice", "алиса", "a/b"])
def test_malformed_usernames_are_refused(accounts, username):
    with pytest.raises(AccountError):
        accounts.create_user(username, PASSWORD)


def test_an_invite_admits_one_user_with_its_role(accounts):
    code, invite = accounts.create_invite(None, role="admin")

    user = accounts.register(code, "carol", PASSWORD)

    assert user.role == "admin"
    assert accounts.invites()[0].used_by == user.id
    with pytest.raises(AccountError, match="not valid"):
        accounts.register(code, "dave", PASSWORD)


def test_an_expired_or_unknown_invite_admits_nobody(accounts, clock):
    code, _ = accounts.create_invite(None, ttl=60)
    clock.now += 61

    with pytest.raises(AccountError, match="not valid"):
        accounts.register(code, "carol", PASSWORD)
    with pytest.raises(AccountError, match="not valid"):
        accounts.register("made-up-code", "carol", PASSWORD)
    assert not accounts.has_users()


def test_a_failed_registration_leaves_the_invite_unspent(accounts):
    code, _ = accounts.create_invite(None)

    with pytest.raises(passwords.WeakPassword):
        accounts.register(code, "carol", "short")

    assert accounts.register(code, "carol", PASSWORD).username == "carol"


def test_a_revoked_invite_admits_nobody(accounts):
    code, invite = accounts.create_invite(None)

    assert accounts.revoke_invite(invite.id)
    with pytest.raises(AccountError):
        accounts.register(code, "carol", PASSWORD)


def test_secrets_are_stored_only_as_hashes(accounts, db_path):
    code, _ = accounts.create_invite(None)
    accounts.create_user("alice", PASSWORD)
    _, token = accounts.sign_in("alice", PASSWORD)

    # The database and its write-ahead log, where fresh rows land first.
    files = [db_path, db_path.with_name(db_path.name + "-wal")]
    raw = b"".join(path.read_bytes() for path in files if path.exists())

    for secret in (code, token, PASSWORD):
        assert secret.encode() not in raw


# -- signing in ------------------------------------------------------------------


def test_a_wrong_password_and_a_missing_user_get_the_same_answer(accounts):
    accounts.create_user("alice", PASSWORD)

    with pytest.raises(SignInFailed) as wrong:
        accounts.sign_in("alice", "not the password")
    with pytest.raises(SignInFailed) as missing:
        accounts.sign_in("nobody", PASSWORD)

    assert str(wrong.value) == str(missing.value)


def test_a_session_opens_until_it_is_idle_too_long(accounts, clock):
    user = accounts.create_user("alice", PASSWORD)
    _, token = accounts.sign_in("alice", PASSWORD)

    clock.now += SESSION_IDLE - 1
    assert accounts.user_for_session(token) == user
    clock.now += SESSION_IDLE - 1  # the read above moved the expiry on
    assert accounts.user_for_session(token) == user
    clock.now += SESSION_IDLE
    assert accounts.user_for_session(token) is None


def test_a_session_ends_at_its_maximum_age_however_often_it_is_used(accounts, clock):
    accounts.create_user("alice", PASSWORD)
    _, token = accounts.sign_in("alice", PASSWORD)
    start = clock.now

    while clock.now < start + SESSION_MAX_AGE - SESSION_TOUCH_INTERVAL:
        clock.now += SESSION_TOUCH_INTERVAL
        assert accounts.user_for_session(token) is not None
    clock.now = start + SESSION_MAX_AGE
    assert accounts.user_for_session(token) is None


def test_signing_out_ends_the_session(accounts):
    accounts.create_user("alice", PASSWORD)
    _, token = accounts.sign_in("alice", PASSWORD)

    accounts.sign_out(token)

    assert accounts.user_for_session(token) is None
    assert accounts.user_for_session("never-issued") is None


def test_a_password_change_ends_every_other_session(accounts):
    user = accounts.create_user("alice", PASSWORD)
    _, here = accounts.sign_in("alice", PASSWORD)
    _, elsewhere = accounts.sign_in("alice", PASSWORD)

    accounts.change_password(user, PASSWORD, "a new long password", token=here)

    assert accounts.user_for_session(here) == user
    assert accounts.user_for_session(elsewhere) is None
    with pytest.raises(SignInFailed):
        accounts.sign_in("alice", PASSWORD)
    accounts.sign_in("alice", "a new long password")


def test_a_password_change_needs_the_current_password(accounts):
    user = accounts.create_user("alice", PASSWORD)
    _, token = accounts.sign_in("alice", PASSWORD)

    with pytest.raises(AccountError, match="current password"):
        accounts.change_password(user, "wrong", "a new long password", token=token)


def test_a_stored_hash_is_upgraded_at_sign_in(accounts, db_path, monkeypatch):
    accounts.create_user("alice", PASSWORD)
    monkeypatch.setattr(passwords, "LOG2_N", 11)

    accounts.sign_in("alice", PASSWORD)

    stored = sqlite3.connect(db_path).execute("SELECT password_hash FROM users").fetchone()[0]
    assert stored.startswith("scrypt$11$")


# -- admins ----------------------------------------------------------------------


def test_disabling_a_user_ends_their_sessions_and_sign_in(accounts):
    admin = accounts.create_user("root", PASSWORD, role="admin")
    user = accounts.create_user("alice", PASSWORD)
    _, token = accounts.sign_in("alice", PASSWORD)

    accounts.set_disabled(admin, user.id, True)

    assert accounts.user_for_session(token) is None
    with pytest.raises(SignInFailed, match="disabled"):
        accounts.sign_in("alice", PASSWORD)
    accounts.set_disabled(admin, user.id, False)
    accounts.sign_in("alice", PASSWORD)


def test_the_last_active_admin_stays(accounts):
    root = accounts.create_user("root", PASSWORD, role="admin")
    other = accounts.create_user("other", PASSWORD, role="admin")

    accounts.set_disabled(root, other.id, True)
    with pytest.raises(AccountError, match="at least one active admin"):
        accounts.set_role(other, root.id, "user")
    with pytest.raises(AccountError, match="your own account"):
        accounts.set_disabled(root, root.id, True)


def test_a_role_change_takes_effect_at_the_next_sign_in(accounts):
    root = accounts.create_user("root", PASSWORD, role="admin")
    user = accounts.create_user("alice", PASSWORD)
    _, token = accounts.sign_in("alice", PASSWORD)

    accounts.set_role(root, user.id, "admin")

    assert accounts.user_for_session(token) is None
    promoted, _ = accounts.sign_in("alice", PASSWORD)
    assert promoted.is_admin


def test_the_database_refuses_a_newer_schema(db_path):
    AccountStore(db_path).close()
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 99")

    with pytest.raises(RuntimeError, match="newer than this code"):
        AccountStore(db_path)


# -- rate limit ------------------------------------------------------------------


def test_the_limiter_refuses_past_the_limit_until_the_window_moves_on(clock):
    limiter = AttemptLimiter(limit=3, window=60, clock=clock)

    for _ in range(3):
        assert limiter.retry_after("k") == 0
        limiter.fail("k")
    assert limiter.retry_after("k") == pytest.approx(60)
    clock.now += 30
    assert limiter.retry_after("k") == pytest.approx(30)
    clock.now += 30
    assert limiter.retry_after("k") == 0


def test_a_success_clears_the_count_and_idle_keys_are_pruned(clock):
    limiter = AttemptLimiter(limit=1, window=60, clock=clock)
    limiter.fail("a")
    limiter.forget("a")
    assert limiter.retry_after("a") == 0

    limiter.fail("b")
    clock.now += 61
    limiter.prune()
    assert limiter._failures == {}


def test_asking_about_a_key_records_nothing(clock):
    """Only failures take memory: a spray of names that sign in fine leaves no trace."""
    limiter = AttemptLimiter(limit=1, window=60, clock=clock)

    assert limiter.retry_after("never-failed") == 0
    assert limiter._failures == {}


def test_expired_sessions_are_purged(accounts, clock):
    accounts.create_user("alice", PASSWORD)
    _, token = accounts.sign_in("alice", PASSWORD)
    clock.now += SESSION_IDLE

    assert accounts.purge_expired_sessions() == 1
    assert accounts.user_for_session(token) is None

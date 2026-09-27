"""The operator's account commands."""

import sqlite3

import pytest

from core.context import ContextManager
from tests.test_web_space import MINIMAL_CONFIG
from web_chat.accounts import open_accounts, passwords
from web_chat.accounts.cli import main


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch):
    monkeypatch.setattr(passwords, "LOG2_N", 10)


@pytest.fixture
def one_user_server(tmp_path, monkeypatch):
    """The records a one-user server left: a chat and an agent session."""
    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    system = tmp_path / "system"
    (system / "logs").mkdir(parents=True)
    config = system / "config.yaml"
    config.write_text(MINIMAL_CONFIG.replace("settings:\n", "settings:\n  logs_directory: logs\n"), encoding="utf-8")
    manager = ContextManager(persist_path=str(system / "logs" / "context.json"))
    manager.start_new_context("ctx-old")
    manager.add_message("user", "an old question")
    db = sqlite3.connect(system / "logs" / "agent_sessions.db")
    with db:  # commits
        db.execute("CREATE TABLE marker (value TEXT)")
        db.execute("INSERT INTO marker VALUES ('kept')")
    db.close()
    return config


def test_import_copies_the_chats_into_the_users_space(tmp_path, one_user_server):
    data = tmp_path / "data"
    user = open_accounts(data).create_user("admin", "correct horse battery", role="admin")

    assert main(["--data-dir", str(data), "import-chats", "--username", "admin", "--config", str(one_user_server)]) == 0

    space = data / "users" / user.id
    imported = ContextManager(persist_path=str(space / "conversations.json"))
    assert imported.conversation_view("ctx-old")["messages"][0].content == "an old question"
    copy = sqlite3.connect(space / "agent_sessions.db")
    assert copy.execute("SELECT value FROM marker").fetchone() == ("kept",)
    copy.close()
    assert (one_user_server.parent / "logs" / "context.json").exists()  # copied, not moved


def test_import_never_overwrites_a_users_records(tmp_path, one_user_server, capsys):
    data = tmp_path / "data"
    open_accounts(data).create_user("admin", "correct horse battery", role="admin")
    arguments = ["--data-dir", str(data), "import-chats", "--username", "admin", "--config", str(one_user_server)]
    main(arguments)

    assert main(arguments) == 1
    assert "already has records" in capsys.readouterr().err
    assert main(["--data-dir", str(data), "import-chats", "--username", "nobody", "--config", str(one_user_server)]) == 1

"""Closing an agent factory must preserve the durable conversation session."""

import asyncio

from agents import SQLiteSession

from core.managers.session_manager import SessionManager, agent_session_id


def test_cleanup_preserves_file_backed_session(tmp_path):
    database = tmp_path / "sessions.db"
    make_session = lambda session_id: SQLiteSession(session_id, db_path=database)

    async def run():
        first = SessionManager(make_session)
        session = first.get_agent_session("writer", "conversation")
        item = {"role": "user", "content": "Keep this turn"}
        await session.add_items([item])
        await first.cleanup_sessions()

        reopened = SessionManager(make_session).get_agent_session("writer", "conversation")
        assert await reopened.get_items() == [item]
        reopened.close()

    asyncio.run(run())


def test_same_agent_and_conversation_are_isolated_by_system(tmp_path):
    database = tmp_path / "sessions.db"
    make_session = lambda session_id: SQLiteSession(session_id, db_path=database)

    async def run():
        first = SessionManager(make_session, namespace="system-a")
        second = SessionManager(make_session, namespace="system-b")
        original = first.get_agent_session("writer", "conversation")
        other = second.get_agent_session("writer", "conversation")
        await original.add_items([{"role": "user", "content": "system A"}])
        assert await other.get_items() == []
        await first.cleanup_sessions()
        reopened = SessionManager(make_session, namespace="system-a")
        assert await reopened.get_agent_session("writer", "conversation").get_items() == [
            {"role": "user", "content": "system A"}
        ]
        await reopened.cleanup_sessions()
        await second.cleanup_sessions()

    asyncio.run(run())
    assert agent_session_id("writer", "conversation") == "agent_writer_conversation"
    assert agent_session_id("writer", "conversation", "system-a") != agent_session_id(
        "writer", "conversation", "system-b"
    )

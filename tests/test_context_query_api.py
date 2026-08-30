"""Tests for ContextManager read/query API used by Context Inspector."""

from __future__ import annotations

import json
from pathlib import Path

from core.context import ContextManager
from schemas import AgentExecution


def _write_persist(path: Path) -> str:
    context_id = "ctx-demo01"
    payload = {
        "active_context_id": context_id,
        "contexts": {
            context_id: {
                "conversation_history": [
                    {
                        "role": "user",
                        "content": "Hello from user",
                        "timestamp": "2026-01-01T00:00:00",
                        "metadata": {},
                    },
                    {
                        "role": "assistant",
                        "content": "Hello from assistant with secret token=abc",
                        "timestamp": "2026-01-01T00:00:01",
                        "metadata": {},
                    },
                ],
                "execution_history": [
                    {
                        "agent_name": "demo_agent",
                        "start_time": 1704067200.0,
                        "end_time": 1704067202.0,
                        "input_message": "Hello from user",
                        "output": "Hello from assistant",
                        "error": None,
                        "context_id": context_id,
                    }
                ],
                "metadata": {
                    "user_id": "tester",
                    "last_invocation": {"agent": "demo_agent"},
                    "last_context_assembly": {
                        "context_id": context_id,
                        "history_strategy": "smart",
                        "instruction_length": 42,
                        "sections": [
                            {
                                "key": "base_prompt",
                                "scope": "static",
                                "length": 20,
                                "preview": "You are a demo agent",
                            },
                            {
                                "key": "conversation",
                                "scope": "dynamic",
                                "length": 22,
                                "preview": "user: Hello",
                            },
                        ],
                        "metadata": {"agent_key": "demo_agent"},
                    },
                    "pending_agent_run": {
                        "agent": "demo_agent",
                        "context_id": context_id,
                        "status": "running",
                        "retry_count": 0,
                        "updated_at": "2026-01-01T00:00:03",
                        "input_preview": "Hello from user",
                        "tool_events": [
                            {
                                "event_type": "tool_called",
                                "timestamp": "2026-01-01T00:00:01",
                                "tool_name": "bash",
                                "arguments": "ls",
                                "output": None,
                            }
                        ],
                    },
                },
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:03",
            }
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return context_id


def test_list_and_detail_query_api(temp_dir):
    path = temp_dir / "context.json"
    context_id = _write_persist(path)

    cm = ContextManager(persist_path=str(path), read_only=True, create_initial_context=False)

    ids = cm.list_context_ids()
    assert context_id in ids
    assert len(ids) == 1

    summaries = cm.list_context_summaries()
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["id"] == context_id
    assert summary["agent"] == "demo_agent"
    assert summary["has_assembly"] is True
    assert summary["message_count"] == 2
    assert summary["tool_event_count"] == 1

    detail = cm.get_context_bucket(context_id)
    assert detail is not None
    assert detail["assembly"]["instruction_length"] == 42
    assert len(detail["assembly"]["sections"]) == 2
    assert detail["pending_agent_run"]["status"] == "running"
    assert detail["messages"][0]["preview"] == "Hello from user"
    assert "content" not in detail["messages"][0]
    assert detail["executions"][0]["agent_name"] == "demo_agent"

    assembly = cm.get_context_assembly(context_id)
    assert assembly["available"] is True
    assert assembly["assembly"]["history_strategy"] == "smart"

    messages = cm.get_context_messages(context_id, limit=1)
    assert messages["count"] == 2
    assert len(messages["messages"]) == 1
    assert messages["messages"][0]["role"] == "assistant"


def test_read_only_does_not_write_persistence(temp_dir):
    path = temp_dir / "context.json"
    context_id = _write_persist(path)
    before = path.read_text(encoding="utf-8")
    mtime = path.stat().st_mtime

    cm = ContextManager(persist_path=str(path), read_only=True, create_initial_context=False)
    cm.set_metadata("should_not_persist", "x")
    cm.add_message("user", "should not write either")
    cm._save_to_file()

    after = path.read_text(encoding="utf-8")
    assert after == before
    assert path.stat().st_mtime == mtime
    assert context_id in cm.list_context_ids()


def test_image_payload_not_leaked_in_preview():
    cm = ContextManager(create_initial_context=True)
    huge = "a" * 5000
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "input_image", "image_url": f"data:image/png;base64,{huge}"},
        ],
        "timestamp": "2026-01-01T00:00:00",
        "metadata": {},
    }
    sanitized = cm._sanitize_message_unlocked(msg, preview_limit=100, include_full=True)
    assert sanitized["has_image"] is True
    assert huge not in json.dumps(sanitized)
    assert sanitized["preview"]


def test_execution_sanitize_truncates():
    cm = ContextManager(create_initial_context=True)
    ex = AgentExecution(
        agent_name="a",
        start_time=1.0,
        end_time=2.0,
        input_message="x" * 2000,
        output="y" * 2000,
        error="z" * 2000,
        context_id="ctx",
    )
    sanitized = cm._sanitize_execution_unlocked(ex, preview_limit=50)
    assert sanitized["input_length"] == 2000
    assert len(sanitized["input_preview"]) <= 51
    assert sanitized["has_error"] is True


def test_detail_can_include_full_message_and_execution_content(temp_dir):
    path = temp_dir / "context.json"
    context_id = _write_persist(path)
    cm = ContextManager(persist_path=str(path), read_only=True, create_initial_context=False)

    detail = cm.get_context_bucket(
        context_id,
        include_full_messages=True,
        include_full_executions=True,
    )

    assert detail["messages"][1]["content"] == "Hello from assistant with secret token=abc"
    assert detail["executions"][0]["output"] == "Hello from assistant"


def test_detail_can_include_full_runtime_event_output(temp_dir):
    path = temp_dir / "context.json"
    context_id = _write_persist(path)
    cm = ContextManager(persist_path=str(path), read_only=True, create_initial_context=False)
    cm._contexts[context_id]["metadata"]["pending_agent_run"] = {
        "tool_events": [{"event_type": "tool_output", "output": "x" * 600}]
    }

    detail = cm.get_context_bucket(context_id, include_full_runtime=True)

    assert detail["pending_agent_run"]["tool_events"][0]["output"] == "x" * 600

"""
Unit tests for per-agent skill isolation.
"""

from core.managers.skill_manager import SkillManager
from core.memory_store import MemoryStore


def _create_manager(tmp_path):
    memory_store = MemoryStore(str(tmp_path / "memory.db"))
    return SkillManager(memory_store=memory_store, workspace_root=tmp_path)


def test_skill_storage_is_isolated_per_agent(tmp_path):
    manager = _create_manager(tmp_path)

    manager.create_skill("user-1", "agent-alpha", "shared-name", "alpha content")
    manager.create_skill("user-1", "agent-beta", "shared-name", "beta content")

    assert manager.get_skill("user-1", "agent-alpha", "shared-name") == "alpha content"
    assert manager.get_skill("user-1", "agent-beta", "shared-name") == "beta content"
    assert manager.list_skills("user-1", "agent-alpha") == ["shared-name"]
    assert manager.list_skills("user-1", "agent-beta") == ["shared-name"]


def test_skill_search_is_isolated_per_agent(tmp_path):
    manager = _create_manager(tmp_path)

    manager.create_skill("user-1", "agent-alpha", "alpha-skill", "uses postgres and redis")
    manager.create_skill("user-1", "agent-beta", "beta-skill", "uses sqlite only")

    alpha_results = manager.search_skills("user-1", "agent-alpha", "postgres")
    beta_results = manager.search_skills("user-1", "agent-beta", "postgres")

    assert [entry.tags for entry in alpha_results] == ["skill:alpha-skill"]
    assert beta_results == []


def test_skill_delete_only_affects_current_agent(tmp_path):
    manager = _create_manager(tmp_path)

    manager.create_skill("user-1", "agent-alpha", "cleanup", "alpha content")
    manager.create_skill("user-1", "agent-beta", "cleanup", "beta content")

    assert manager.delete_skill("user-1", "agent-alpha", "cleanup") is True

    assert manager.get_skill("user-1", "agent-alpha", "cleanup") is None
    assert manager.get_skill("user-1", "agent-beta", "cleanup") == "beta content"
    assert manager.list_skills("user-1", "agent-beta") == ["cleanup"]

"""
Unit tests for MemoryStore graph methods.

Tests:
- get_entity_graph() - search for entries by entity
- get_connected_entries() - BFS traversal of connection graph
- find_entity_connections() - find path between entities
- _get_outgoing_connections() - get outgoing connections
- _get_incoming_connections() - get incoming connections
"""

import pytest
import json
import sqlite3
from pathlib import Path
from datetime import datetime

from core.memory.store import MemoryStore, MemoryEntry


@pytest.fixture
def temp_db_path(temp_dir):
    """Create a temporary database path for testing."""
    return str(temp_dir / "test_memory.db")


@pytest.fixture
def store(temp_db_path):
    """Create a MemoryStore instance with temporary database."""
    store = MemoryStore(temp_db_path)
    yield store
    store.close()


def _update_entities(store, entry_id, entities):
    """Helper to update entities via direct SQL."""
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.execute(
            "UPDATE memory SET entities = ?, updated_at = datetime('now') WHERE id = ?",
            (entities, entry_id)
        )
        conn.commit()


def _update_connections(store, entry_id, connections):
    """Helper to update connections via direct SQL."""
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.execute(
            "UPDATE memory SET connections = ?, updated_at = datetime('now') WHERE id = ?",
            (connections, entry_id)
        )
        conn.commit()


def _update_entry(store, entry_id, entities=None, connections=None, importance=None, is_archived=None):
    """Helper to update entry fields via direct SQL."""
    updates = []
    params = []
    
    if entities is not None:
        updates.append("entities = ?")
        params.append(entities)
    if connections is not None:
        updates.append("connections = ?")
        params.append(connections)
    if importance is not None:
        updates.append("importance = ?")
        params.append(importance)
    if is_archived is not None:
        updates.append("is_archived = ?")
        params.append(1 if is_archived else 0)
    
    if not updates:
        return
    
    updates.append("updated_at = datetime('now')")
    params.append(entry_id)
    
    with sqlite3.connect(str(store.db_path)) as conn:
        conn.execute(
            f"UPDATE memory SET {', '.join(updates)} WHERE id = ?",
            params
        )
        conn.commit()


class TestGetOutgoingConnections:
    """Test _get_outgoing_connections() method."""

    def test_get_outgoing_no_connections(self, store):
        """Entry without connections should return an empty list."""
        entry_id = store.save(
            content="Test entry",
            type="long_term"
        )
        
        result = store._get_outgoing_connections(entry_id)
        
        assert result == []

    def test_get_outgoing_single_connection(self, store):
        """Check getting a single outgoing connection."""
        # Create two entries
        entry1_id = store.save(content="Entry 1", type="long_term")
        entry2_id = store.save(content="Entry 2", type="long_term")
        
        # Update connections for entry1
        connections = json.dumps([{"target_id": entry2_id, "relation": "relates_to"}])
        _update_connections(store, entry1_id, connections)
        
        result = store._get_outgoing_connections(entry1_id)
        
        assert len(result) == 1
        assert result[0] == (entry2_id, "relates_to")

    def test_get_outgoing_multiple_connections(self, store):
        """Check getting multiple outgoing connections."""
        entry1_id = store.save(content="Entry 1", type="long_term")
        entry2_id = store.save(content="Entry 2", type="long_term")
        entry3_id = store.save(content="Entry 3", type="long_term")
        
        connections = json.dumps([
            {"target_id": entry2_id, "relation": "parent_of"},
            {"target_id": entry3_id, "relation": "references"}
        ])
        _update_connections(store, entry1_id, connections)
        
        result = store._get_outgoing_connections(entry1_id)
        
        assert len(result) == 2
        target_ids = [t for t, r in result]
        assert entry2_id in target_ids
        assert entry3_id in target_ids

    def test_get_outgoing_nonexistent_entry(self, store):
        """Query for non-existent entry should return an empty list."""
        result = store._get_outgoing_connections(99999)
        
        assert result == []

    def test_get_outgoing_invalid_json_connections(self, store):
        """Invalid JSON in connections should return an empty list."""
        entry_id = store.save(content="Test", type="long_term")
        
        # Set invalid JSON directly via SQL
        with sqlite3.connect(str(store.db_path)) as conn:
            conn.execute("UPDATE memory SET connections = ? WHERE id = ?", ("not a json", entry_id))
            conn.commit()
        
        result = store._get_outgoing_connections(entry_id)
        
        assert result == []

    def test_get_outgoing_missing_target_id(self, store):
        """Connection without target_id should be skipped."""
        entry_id = store.save(content="Test", type="long_term")
        
        connections = json.dumps([
            {"relation": "invalid"},  # No target_id
            {"target_id": None, "relation": "also_invalid"}
        ])
        _update_connections(store, entry_id, connections)
        
        result = store._get_outgoing_connections(entry_id)
        
        assert result == []

    def test_get_outgoing_string_target_id(self, store):
        """target_id as string should be converted to int."""
        entry1_id = store.save(content="Entry 1", type="long_term")
        entry2_id = store.save(content="Entry 2", type="long_term")
        
        connections = json.dumps([{"target_id": str(entry2_id), "relation": "link"}])
        _update_connections(store, entry1_id, connections)
        
        result = store._get_outgoing_connections(entry1_id)
        
        assert len(result) == 1
        assert result[0][0] == entry2_id


class TestGetIncomingConnections:
    """Test _get_incoming_connections() method."""

    def test_get_incoming_no_connections(self, store):
        """Entry without incoming connections should return an empty list."""
        entry_id = store.save(content="Test entry", type="long_term")
        
        result = store._get_incoming_connections(entry_id)
        
        assert result == []

    def test_get_incoming_single_connection(self, store):
        """Check getting a single incoming connection."""
        entry1_id = store.save(content="Source entry", type="long_term")
        entry2_id = store.save(content="Target entry", type="long_term")
        
        # entry1 points to entry2
        connections = json.dumps([{"target_id": entry2_id, "relation": "references"}])
        _update_connections(store, entry1_id, connections)
        
        result = store._get_incoming_connections(entry2_id)
        
        assert len(result) == 1
        assert result[0] == (entry1_id, "references")

    def test_get_incoming_multiple_connections(self, store):
        """Check getting multiple incoming connections."""
        entry1_id = store.save(content="Source 1", type="long_term")
        entry2_id = store.save(content="Source 2", type="long_term")
        target_id = store.save(content="Target", type="long_term")
        
        connections1 = json.dumps([{"target_id": target_id, "relation": "parent"}])
        connections2 = json.dumps([{"target_id": target_id, "relation": "child"}])
        
        _update_connections(store, entry1_id, connections1)
        _update_connections(store, entry2_id, connections2)
        
        result = store._get_incoming_connections(target_id)
        
        assert len(result) == 2
        source_ids = [s for s, r in result]
        assert entry1_id in source_ids
        assert entry2_id in source_ids

    def test_get_incoming_ignores_archived(self, store):
        """Archived entries should not be considered."""
        entry1_id = store.save(content="Source", type="long_term")
        entry2_id = store.save(content="Target", type="long_term")
        
        connections = json.dumps([{"target_id": entry2_id, "relation": "link"}])
        _update_connections(store, entry1_id, connections)
        
        # Archive source
        _update_entry(store, entry1_id, is_archived=True)
        
        result = store._get_incoming_connections(entry2_id)
        
        assert result == []

    def test_get_incoming_missing_relation(self, store):
        """Connection without relation should return an empty string."""
        entry1_id = store.save(content="Source", type="long_term")
        entry2_id = store.save(content="Target", type="long_term")
        
        # connection without relation
        connections = json.dumps([{"target_id": entry2_id}])
        _update_connections(store, entry1_id, connections)
        
        result = store._get_incoming_connections(entry2_id)
        
        assert len(result) == 1
        assert result[0] == (entry1_id, '')


class TestGetEntityGraph:
    """Test get_entity_graph() method."""

    def test_get_entity_graph_empty_result(self, store):
        """Search for non-existent entity should return an empty list."""
        result = store.get_entity_graph("NonExistentEntity")
        
        assert result == []

    def test_get_entity_graph_single_match(self, store):
        """Search should find entries with the specified entity."""
        entities = json.dumps([{"name": "Python", "type": "programming_language"}])
        entry_id = store.save(content="Python is great", type="long_term", entities=entities)
        
        result = store.get_entity_graph("Python")
        
        assert len(result) == 1
        assert result[0].id == entry_id

    def test_get_entity_graph_multiple_matches(self, store):
        """Search should return all entries with the specified entity."""
        entities_python = json.dumps([{"name": "Python"}])
        
        entry1_id = store.save(content="Python for backend", type="long_term", entities=entities_python)
        entry2_id = store.save(content="Python for ML", type="long_term", entities=entities_python)
        entry3_id = store.save(content="JavaScript for frontend", type="long_term", 
                               entities=json.dumps([{"name": "JavaScript"}]))
        
        result = store.get_entity_graph("Python")
        
        assert len(result) == 2
        contents = [e.content for e in result]
        assert "Python for backend" in contents
        assert "Python for ML" in contents

    def test_get_entity_graph_respects_limit(self, store):
        """Result should be limited by limit."""
        entities = json.dumps([{"name": "TestEntity"}])
        
        for i in range(30):
            store.save(content=f"Entry {i}", type="long_term", entities=entities)
        
        result = store.get_entity_graph("TestEntity", limit=10)
        
        assert len(result) == 10

    def test_get_entity_graph_sorted_by_importance(self, store):
        """Results should be sorted by importance DESC."""
        entities = json.dumps([{"name": "Important"}])
        
        entry1_id = store.save(content="Low importance", type="long_term", 
                               entities=entities, importance=0.3)
        entry2_id = store.save(content="High importance", type="long_term", 
                               entities=entities, importance=0.9)
        entry3_id = store.save(content="Medium importance", type="long_term", 
                               entities=entities, importance=0.5)
        
        result = store.get_entity_graph("Important")
        
        assert result[0].importance == 0.9
        assert result[1].importance == 0.5
        assert result[2].importance == 0.3

    def test_get_entity_graph_ignores_archived(self, store):
        """Archived entries should not be included."""
        entities = json.dumps([{"name": "Archived"}])
        
        entry1_id = store.save(content="Active entry", type="long_term", entities=entities)
        entry2_id = store.save(content="Archived entry", type="long_term", entities=entities)
        
        # Archive entry2
        _update_entry(store, entry2_id, is_archived=True)
        
        result = store.get_entity_graph("Archived")
        
        assert len(result) == 1
        assert result[0].content == "Active entry"

    def test_get_entity_graph_multiple_entities_per_entry(self, store):
        """Entry with multiple entities should be found by any of them."""
        entities = json.dumps([
            {"name": "Python"},
            {"name": "Django"}
        ])
        entry_id = store.save(content="Python Django project", type="long_term", entities=entities)
        
        result_python = store.get_entity_graph("Python")
        result_django = store.get_entity_graph("Django")
        
        assert len(result_python) == 1
        assert len(result_django) == 1
        assert result_python[0].id == entry_id
        assert result_django[0].id == entry_id


class TestGetConnectedEntries:
    """Test get_connected_entries() method - BFS graph traversal."""

    def test_get_connected_nonexistent_entry(self, store):
        """Query for non-existent entry should return an empty list."""
        result = store.get_connected_entries(99999, max_depth=2)
        
        assert result == []

    def test_get_connected_no_connections(self, store):
        """Entry without connections should return an empty list."""
        entry_id = store.save(content="Isolated entry", type="long_term")
        
        result = store.get_connected_entries(entry_id)
        
        assert result == []

    def test_get_connected_direct_outgoing(self, store):
        """Check direct outgoing connections (depth=1)."""
        entry1_id = store.save(content="Source", type="long_term")
        entry2_id = store.save(content="Target", type="long_term")
        
        connections = json.dumps([{"target_id": entry2_id, "relation": "links_to"}])
        _update_connections(store, entry1_id, connections)
        
        result = store.get_connected_entries(entry1_id, max_depth=1)
        
        assert len(result) == 1
        entry, relation = result[0]
        assert entry.id == entry2_id
        assert relation == "links_to"

    def test_get_connected_direct_incoming(self, store):
        """Check direct incoming connections (depth=1)."""
        entry1_id = store.save(content="Source", type="long_term")
        entry2_id = store.save(content="Target", type="long_term")
        
        connections = json.dumps([{"target_id": entry2_id, "relation": "parent"}])
        _update_connections(store, entry1_id, connections)
        
        # entry2 should see entry1 as an incoming connection
        result = store.get_connected_entries(entry2_id, max_depth=1)
        
        assert len(result) == 1
        entry, relation = result[0]
        assert entry.id == entry1_id
        assert relation == "parent"

    def test_get_connected_bidirectional(self, store):
        """Check bidirectional traversal (outgoing + incoming)."""
        #   entry1 -> entry2 -> entry3
        #   entry4 -> entry2 (entry2 has incoming from entry4)
        entry1_id = store.save(content="Entry 1", type="long_term")
        entry2_id = store.save(content="Entry 2", type="long_term")
        entry3_id = store.save(content="Entry 3", type="long_term")
        entry4_id = store.save(content="Entry 4", type="long_term")
        
        _update_connections(store, entry1_id, json.dumps([
            {"target_id": entry2_id, "relation": "links"}
        ]))
        _update_connections(store, entry2_id, json.dumps([
            {"target_id": entry3_id, "relation": "next"}
        ]))
        _update_connections(store, entry4_id, json.dumps([
            {"target_id": entry2_id, "relation": "references"}
        ]))
        
        # entry2 should have 3 connections: entry1 (incoming), entry3 (outgoing), entry4 (incoming)
        result = store.get_connected_entries(entry2_id, max_depth=1)
        
        assert len(result) == 3
        connected_ids = {e.id for e, r in result}
        assert entry1_id in connected_ids
        assert entry3_id in connected_ids
        assert entry4_id in connected_ids

    def test_get_connected_respects_max_depth(self, store):
        """Check max depth traversal limit."""
        # Chain: entry1 -> entry2 -> entry3 -> entry4
        entry1_id = store.save(content="E1", type="long_term")
        entry2_id = store.save(content="E2", type="long_term")
        entry3_id = store.save(content="E3", type="long_term")
        entry4_id = store.save(content="E4", type="long_term")
        
        _update_connections(store, entry1_id, json.dumps([
            {"target_id": entry2_id, "relation": "r1"}
        ]))
        _update_connections(store, entry2_id, json.dumps([
            {"target_id": entry3_id, "relation": "r2"}
        ]))
        _update_connections(store, entry3_id, json.dumps([
            {"target_id": entry4_id, "relation": "r3"}
        ]))
        
        # max_depth=1: only direct connections
        result1 = store.get_connected_entries(entry1_id, max_depth=1)
        assert len(result1) == 1
        assert result1[0][0].id == entry2_id
        
        # max_depth=2: entry2 and entry3
        result2 = store.get_connected_entries(entry1_id, max_depth=2)
        connected_ids = {e.id for e, r in result2}
        assert entry2_id in connected_ids
        assert entry3_id in connected_ids
        assert entry4_id not in connected_ids
        
        # max_depth=3: all three
        result3 = store.get_connected_entries(entry1_id, max_depth=3)
        connected_ids = {e.id for e, r in result3}
        assert entry4_id in connected_ids

    def test_get_connected_handles_cycles(self, store):
        """Handle cycles in the graph (should not loop infinitely)."""
        # Cycle: entry1 -> entry2 -> entry1
        entry1_id = store.save(content="E1", type="long_term")
        entry2_id = store.save(content="E2", type="long_term")
        
        _update_connections(store, entry1_id, json.dumps([
            {"target_id": entry2_id, "relation": "to2"}
        ]))
        _update_connections(store, entry2_id, json.dumps([
            {"target_id": entry1_id, "relation": "to1"}
        ]))
        
        # Should not loop infinitely
        result = store.get_connected_entries(entry1_id, max_depth=5)
        
        # Each entry should appear only once
        connected_ids = [e.id for e, r in result]
        assert len(connected_ids) == len(set(connected_ids))
        assert entry2_id in connected_ids

    def test_get_connected_complex_graph(self, store):
        """Test on a complex graph."""
        #      entry1
        #      /    \
        #   entry2  entry3
        #      \    /
        #      entry4
        entry1_id = store.save(content="Root", type="long_term")
        entry2_id = store.save(content="Left", type="long_term")
        entry3_id = store.save(content="Right", type="long_term")
        entry4_id = store.save(content="Bottom", type="long_term")
        
        _update_connections(store, entry1_id, json.dumps([
            {"target_id": entry2_id, "relation": "left"},
            {"target_id": entry3_id, "relation": "right"}
        ]))
        _update_connections(store, entry2_id, json.dumps([
            {"target_id": entry4_id, "relation": "down"}
        ]))
        _update_connections(store, entry3_id, json.dumps([
            {"target_id": entry4_id, "relation": "down"}
        ]))
        
        result = store.get_connected_entries(entry1_id, max_depth=2)
        
        # entry1 is connected to entry2, entry3, entry4 (via two paths)
        connected_ids = {e.id for e, r in result}
        assert entry2_id in connected_ids
        assert entry3_id in connected_ids
        assert entry4_id in connected_ids


class TestFindEntityConnections:
    """Test find_entity_connections() method - shortest path between entities."""

    def test_find_connections_no_start_entity(self, store):
        """Search without a start entity should return an empty list."""
        result = store.find_entity_connections("NonExistent1", "Python")
        
        assert result == []

    def test_find_connections_no_target_entity(self, store):
        """Search without a target entity should return an empty list."""
        entities = json.dumps([{"name": "Python"}])
        store.save(content="Python entry", type="long_term", entities=entities)
        
        result = store.find_entity_connections("Python", "NonExistent")
        
        assert result == []

    def test_find_connections_same_entity(self, store):
        """Searching for a path between the same entity should return an empty list."""
        entities = json.dumps([{"name": "Python"}])
        store.save(content="Python entry", type="long_term", entities=entities)
        
        result = store.find_entity_connections("Python", "Python")
        
        # Direct overlap - empty result
        assert result == []

    def test_find_connections_direct_path(self, store):
        """Search for a direct path between two entities."""
        # entry1 with entity1 -> entry2 with entity2
        entry1_id = store.save(content="Python project", type="long_term", 
                               entities=json.dumps([{"name": "Python"}]))
        entry2_id = store.save(content="Django framework", type="long_term", 
                               entities=json.dumps([{"name": "Django"}]))
        
        # Create connection
        _update_connections(store, entry1_id, json.dumps([
            {"target_id": entry2_id, "relation": "uses"}
        ]))
        
        result = store.find_entity_connections("Python", "Django")
        
        assert len(result) == 1
        assert result[0]["from_id"] == entry1_id
        assert result[0]["to_id"] == entry2_id
        assert result[0]["relation"] == "uses"

    def test_find_connections_indirect_path(self, store):
        """Find indirect path through intermediate entries."""
        # Python -> Framework -> Django
        entry1_id = store.save(content="Python", type="long_term", 
                               entities=json.dumps([{"name": "Python"}]))
        entry2_id = store.save(content="Web Framework", type="long_term", 
                               entities=json.dumps([{"name": "Framework"}]))
        entry3_id = store.save(content="Django", type="long_term", 
                               entities=json.dumps([{"name": "Django"}]))
        
        _update_connections(store, entry1_id, json.dumps([
            {"target_id": entry2_id, "relation": "has"}
        ]))
        _update_connections(store, entry2_id, json.dumps([
            {"target_id": entry3_id, "relation": "includes"}
        ]))
        
        result = store.find_entity_connections("Python", "Django", max_depth=3)
        
        assert len(result) == 2
        # Path: Python -> Framework -> Django
        assert result[0]["from_id"] == entry1_id
        assert result[0]["to_id"] == entry2_id
        assert result[1]["from_id"] == entry2_id
        assert result[1]["to_id"] == entry3_id

    def test_find_connections_no_path_within_depth(self, store):
        """Path longer than max_depth should not be found."""
        entry1_id = store.save(content="E1", type="long_term", 
                               entities=json.dumps([{"name": "A"}]))
        entry2_id = store.save(content="E2", type="long_term")
        entry3_id = store.save(content="E3", type="long_term")
        entry4_id = store.save(content="E4", type="long_term", 
                               entities=json.dumps([{"name": "D"}]))
        
        # Chain of length 3
        _update_connections(store, entry1_id, json.dumps([
            {"target_id": entry2_id, "relation": "r1"}
        ]))
        _update_connections(store, entry2_id, json.dumps([
            {"target_id": entry3_id, "relation": "r2"}
        ]))
        _update_connections(store, entry3_id, json.dumps([
            {"target_id": entry4_id, "relation": "r3"}
        ]))
        
        # max_depth=2 is not enough for a path of length 3
        result = store.find_entity_connections("A", "D", max_depth=2)
        
        assert result == []

    def test_find_connections_respects_max_depth(self, store):
        """Check max_depth limit."""
        entry1_id = store.save(content="E1", type="long_term", 
                               entities=json.dumps([{"name": "Start"}]))
        entry2_id = store.save(content="E2", type="long_term")
        entry3_id = store.save(content="E3", type="long_term", 
                               entities=json.dumps([{"name": "End"}]))
        
        _update_connections(store, entry1_id, json.dumps([
            {"target_id": entry2_id, "relation": "r1"}
        ]))
        _update_connections(store, entry2_id, json.dumps([
            {"target_id": entry3_id, "relation": "r2"}
        ]))
        
        # Path of length 2, max_depth=2 should find it
        result = store.find_entity_connections("Start", "End", max_depth=2)
        assert len(result) == 2
        
        # max_depth=1 is not enough
        result2 = store.find_entity_connections("Start", "End", max_depth=1)
        assert result2 == []

    def test_find_connections_shortest_path(self, store):
        """Should find the shortest path when multiple exist."""
        # Two paths from A to D:
        # Path 1 (length 2): A -> B -> D
        # Path 2 (length 3): A -> C -> E -> D
        
        entry_a = store.save(content="A", type="long_term", 
                             entities=json.dumps([{"name": "A"}]))
        entry_b = store.save(content="B", type="long_term")
        entry_c = store.save(content="C", type="long_term")
        entry_d = store.save(content="D", type="long_term", 
                             entities=json.dumps([{"name": "D"}]))
        entry_e = store.save(content="E", type="long_term")
        
        # Short path
        _update_connections(store, entry_a, json.dumps([
            {"target_id": entry_b, "relation": "toB"}
        ]))
        _update_connections(store, entry_b, json.dumps([
            {"target_id": entry_d, "relation": "toD"}
        ]))
        
        # Long path (add to existing connections)
        _update_connections(store, entry_a, json.dumps([
            {"target_id": entry_b, "relation": "toB"},
            {"target_id": entry_c, "relation": "toC"}
        ]))
        _update_connections(store, entry_c, json.dumps([
            {"target_id": entry_e, "relation": "toE"}
        ]))
        _update_connections(store, entry_e, json.dumps([
            {"target_id": entry_d, "relation": "toD2"}
        ]))
        
        result = store.find_entity_connections("A", "D", max_depth=5)
        
        # BFS will find the short path (length 2)
        assert len(result) == 2

    def test_find_connections_bidirectional(self, store):
        """Search should work in both directions (outgoing + incoming)."""
        entry1_id = store.save(content="E1", type="long_term", 
                               entities=json.dumps([{"name": "Start"}]))
        entry2_id = store.save(content="E2", type="long_term", 
                               entities=json.dumps([{"name": "End"}]))
        
        # Connection from E1 to E2
        _update_connections(store, entry1_id, json.dumps([
            {"target_id": entry2_id, "relation": "forward"}
        ]))
        
        # Search Start -> End (via outgoing)
        result1 = store.find_entity_connections("Start", "End", max_depth=1)
        assert len(result1) == 1
        
        # Now check reverse search - should find via incoming
        # End can find Start through incoming connection
        result2 = store.find_entity_connections("End", "Start", max_depth=1)
        assert len(result2) == 1

    def test_find_connections_includes_entity_in_path(self, store):
        """Path must include entity from the target entry."""
        entry1_id = store.save(content="Python entry", type="long_term", 
                               entities=json.dumps([{"name": "Python", "type": "language"}]))
        entry2_id = store.save(content="Django entry", type="long_term", 
                               entities=json.dumps([{"name": "Django", "type": "framework"}]))
        
        _update_connections(store, entry1_id, json.dumps([
            {"target_id": entry2_id, "relation": "uses"}
        ]))
        
        result = store.find_entity_connections("Python", "Django")
        
        assert len(result) == 1
        # entity from target entry
        assert result[0]["entity"] == "Django"

    def test_find_connections_complex_graph(self, store):
        """Test on complex graph with multiple entities."""
        # Create graph:
        # User -> Project (has)
        # Project -> Python (uses)
        # Project -> Django (uses)
        # Django -> Web (is_a)
        
        user_id = store.save(content="User Danil", type="long_term", 
                             entities=json.dumps([{"name": "User"}]))
        project_id = store.save(content="Grid Agent System", type="long_term", 
                                entities=json.dumps([{"name": "Project"}]))
        python_id = store.save(content="Python language", type="long_term", 
                               entities=json.dumps([{"name": "Python"}]))
        django_id = store.save(content="Django framework", type="long_term", 
                               entities=json.dumps([{"name": "Django"}]))
        web_id = store.save(content="Web development", type="long_term", 
                            entities=json.dumps([{"name": "Web"}]))
        
        _update_connections(store, user_id, json.dumps([
            {"target_id": project_id, "relation": "has"}
        ]))
        _update_connections(store, project_id, json.dumps([
            {"target_id": python_id, "relation": "uses"},
            {"target_id": django_id, "relation": "uses"}
        ]))
        _update_connections(store, django_id, json.dumps([
            {"target_id": web_id, "relation": "is_a"}
        ]))
        
        # Find path User -> Django
        result = store.find_entity_connections("User", "Django", max_depth=3)
        
        assert len(result) == 2
        # User -> Project -> Django
        path_entities = [step.get("entity", "") for step in result]
        assert "Project" in path_entities or "Django" in path_entities

    def test_find_connections_multiple_start_entries(self, store):
        """Search when multiple entries share one entity."""
        # Multiple entries with entity "Python"
        entry1_id = store.save(content="Python entry 1", type="long_term", 
                               entities=json.dumps([{"name": "Python"}]))
        entry2_id = store.save(content="Python entry 2", type="long_term", 
                               entities=json.dumps([{"name": "Python"}]))
        entry3_id = store.save(content="Target", type="long_term", 
                               entities=json.dumps([{"name": "Target"}]))
        
        # Only entry2 is linked to target
        _update_connections(store, entry2_id, json.dumps([
            {"target_id": entry3_id, "relation": "links"}
        ]))
        
        result = store.find_entity_connections("Python", "Target")
        
        # Should find a path through entry2
        assert len(result) == 1


class TestGraphEdgeCases:
    """Edge case tests for graph methods."""

    def test_empty_database(self, store):
        """All methods should work with an empty database."""
        assert store.get_entity_graph("Anything") == []
        assert store.get_connected_entries(1) == []
        assert store.find_entity_connections("A", "B") == []

    def test_self_connection(self, store):
        """Entry can reference itself."""
        entry_id = store.save(content="Self-referential", type="long_term")
        _update_connections(store, entry_id, json.dumps([
            {"target_id": entry_id, "relation": "self"}
        ]))
        
        # Should not loop
        result = store.get_connected_entries(entry_id, max_depth=3)
        
        # For self-loop, BFS won't add the starting entry to the result
        # (it is skipped when rel is None)
        assert isinstance(result, list)

    def test_orphaned_connection_target(self, store):
        entry_id = store.save(content="Orphan link", type="long_term")
        _update_connections(store, entry_id, json.dumps([
            {"target_id": 99999, "relation": "broken"}
        ]))
        
        result = store.get_connected_entries(entry_id, max_depth=1)
        
        # Несуществующий target должен быть пропущен
        # (_get_entry_by_id вернёт None)
        assert result == []

    def test_large_max_depth(self, store):
        """Большой max_depth не должен вызывать проблем."""
        entry_id = store.save(content="Root", type="long_term")
        
        # Одиночный entry без связей
        result = store.get_connected_entries(entry_id, max_depth=100)
        
        assert result == []

    def test_unicode_entity_names(self, store):
        """Unicode в именах entities."""
        entities = json.dumps([{"name": "Программирование"}])
        entry_id = store.save(content="Unicode test", type="long_term", entities=entities)
        
        result = store.get_entity_graph("Программирование")
        
        assert len(result) == 1

    def test_special_characters_in_relations(self, store):
        """Специальные символы в relations."""
        entry1_id = store.save(content="E1", type="long_term")
        entry2_id = store.save(content="E2", type="long_term")
        
        _update_connections(store, entry1_id, json.dumps([
            {"target_id": entry2_id, "relation": "type->subtype"}
        ]))
        
        result = store.get_connected_entries(entry1_id, max_depth=1)
        
        assert len(result) == 1
        assert result[0][1] == "type->subtype"

    def test_entity_with_empty_name(self, store):
        """Entity с пустым именем."""
        entities = json.dumps([{"name": ""}, {"name": "Valid"}])
        entry_id = store.save(content="Test", type="long_term", entities=entities)
        
        # Валидное имя должно находиться
        result_valid = store.get_entity_graph("Valid")
        
        assert len(result_valid) == 1


class TestGraphIntegration:
    """Интеграционные тесты графовых методов."""

    def test_full_graph_workflow(self, store):
        """Полный сценарий работы с графом."""
        # 1. Создаём entities
        user_id = store.save(content="User Danil", type="long_term", importance=0.9,
                             entities=json.dumps([{"name": "Danil", "type": "person"}]))
        project_id = store.save(content="Grid Agent System", type="long_term", importance=0.8,
                                entities=json.dumps([{"name": "GridAgent", "type": "project"}]))
        tech_id = store.save(content="Python technology", type="long_term", importance=0.7,
                             entities=json.dumps([{"name": "Python", "type": "technology"}]))
        
        # 3. Создаём связи
        _update_connections(store, user_id, json.dumps([
            {"target_id": project_id, "relation": "owns"}
        ]))
        _update_connections(store, project_id, json.dumps([
            {"target_id": tech_id, "relation": "uses"}
        ]))
        
        # 4. Тестируем get_entity_graph
        danil_entries = store.get_entity_graph("Danil")
        assert len(danil_entries) == 1
        assert danil_entries[0].importance == 0.9
        
        # 5. Тестируем get_connected_entries
        user_connections = store.get_connected_entries(user_id, max_depth=2)
        connected_ids = {e.id for e, r in user_connections}
        assert project_id in connected_ids
        assert tech_id in connected_ids
        
        # 6. Тестируем find_entity_connections
        path = store.find_entity_connections("Danil", "Python", max_depth=3)
        assert len(path) == 2  # Danil -> GridAgent -> Python

    def test_graph_with_archived_nodes(self, store):
        """Граф с архивированными nodes."""
        entry1_id = store.save(content="Active", type="long_term",
                               entities=json.dumps([{"name": "Start"}]))
        entry2_id = store.save(content="To be archived", type="long_term",
                               entities=json.dumps([{"name": "Middle"}]))
        entry3_id = store.save(content="Target", type="long_term",
                               entities=json.dumps([{"name": "End"}]))
        
        _update_connections(store, entry1_id, json.dumps([
            {"target_id": entry2_id, "relation": "to_middle"}
        ]))
        _update_connections(store, entry2_id, json.dumps([
            {"target_id": entry3_id, "relation": "to_end"}
        ]))
        
        # Архивируем middle
        _update_entry(store, entry2_id, is_archived=True)
        
        # Start должен найти только себя
        start_entries = store.get_entity_graph("Start")
        assert len(start_entries) == 1
        
        # Middle не должен находиться
        middle_entries = store.get_entity_graph("Middle")
        assert len(middle_entries) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

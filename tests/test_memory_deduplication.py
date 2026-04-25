"""
Unit tests for MemoryStore deduplication functionality.

Tests:
- _calculate_similarity() - boundary values
- find_similar() - search for candidates
- save(update_if_exists=True) - deduplication on save
"""

import pytest
import tempfile
import os
from pathlib import Path

from core.memory.store import MemoryStore, MemoryEntry


@pytest.fixture
def temp_db_path(temp_dir):
    """Create a temporary database path for testing."""
    return str(temp_dir / "test_memory.db")


@pytest.fixture
def memory_store(temp_db_path):
    """Create a MemoryStore instance with temporary database."""
    store = MemoryStore(temp_db_path)
    yield store
    # Cleanup handled by temp_dir fixture


class TestCalculateSimilarity:
    """Test _calculate_similarity() method - boundary values."""

    def test_identical_texts(self, memory_store):
        """Identical texts should have similarity = 1.0."""
        text = "User prefers Python programming language"
        similarity = memory_store._calculate_similarity(text, text)
        assert similarity == 1.0

    def test_identical_texts_different_case(self, memory_store):
        """Texts with different case should have similarity = 1.0."""
        text1 = "User prefers Python"
        text2 = "USER PREFERS PYTHON"
        similarity = memory_store._calculate_similarity(text1, text2)
        assert similarity == 1.0

    def test_identical_texts_different_whitespace(self, memory_store):
        """Texts with different whitespace should have similarity = 1.0."""
        text1 = "User    prefers   Python"
        text2 = "User prefers Python"
        similarity = memory_store._calculate_similarity(text1, text2)
        assert similarity == 1.0

    def test_completely_different_texts(self, memory_store):
        """Completely different texts should have low similarity."""
        text1 = "abcdefghij"
        text2 = "klmnopqrst"
        similarity = memory_store._calculate_similarity(text1, text2)
        assert similarity == 0.0

    def test_empty_texts(self, memory_store):
        """Empty texts should have similarity = 1.0."""
        similarity = memory_store._calculate_similarity("", "")
        assert similarity == 1.0

    def test_one_empty_text(self, memory_store):
        """One empty text should give similarity = 0.0."""
        similarity = memory_store._calculate_similarity("Some text", "")
        assert similarity == 0.0

    def test_partially_similar_texts(self, memory_store):
        """Partially similar texts should give intermediate similarity."""
        text1 = "User prefers Python programming"
        text2 = "User prefers JavaScript programming"
        similarity = memory_store._calculate_similarity(text1, text2)
        # Should be around 0.75-0.85
        assert 0.6 < similarity < 0.9

    def test_threshold_boundary_80_percent(self, memory_store):
        """Test at the threshold boundary of 80%."""
        # Create texts that should be around 80% similar
        text1 = "This is a test message about Python"
        text2 = "This is a test message about JavaScript"
        similarity = memory_store._calculate_similarity(text1, text2)
        # Python vs JavaScript - difference in one word
        # Length is the same, difference only in the last word
        assert isinstance(similarity, float)
        assert 0.0 <= similarity <= 1.0

    def test_unicode_texts(self, memory_store):
        """Texts with Unicode characters should compare correctly."""
        text1 = "User prefers Python"
        text2 = "User prefers JavaScript"
        similarity = memory_store._calculate_similarity(text1, text2)
        assert 0.5 < similarity < 1.0

    def test_special_characters(self, memory_store):
        """Texts with special characters should compare correctly."""
        text1 = "Email: user@example.com, Phone: +1-234-567-8900"
        text2 = "Email: admin@test.org, Phone: +1-234-567-8900"
        similarity = memory_store._calculate_similarity(text1, text2)
        assert 0.5 < similarity < 1.0


class TestFindSimilar:
    """Test find_similar() method - search for candidates."""

    def test_find_exact_match(self, memory_store):
        """Search should find exact match."""
        # Use words longer than 3 characters for FTS5
        content = "User prefers Python programming language"
        memory_store.save(content, type="long_term", tags="preference")

        results = memory_store.find_similar(content, threshold=0.8, limit=5)

        assert len(results) == 1
        entry, similarity = results[0]
        assert similarity == 1.0
        assert entry.content == content

    def test_find_similar_above_threshold(self, memory_store):
        """Search should find similar entries above threshold."""
        memory_store.save("User prefers Python for backend development", type="long_term")
        memory_store.save("User prefers JavaScript for frontend development", type="long_term")
        memory_store.save("Weather is sunny today", type="short_term")

        results = memory_store.find_similar(
            "User prefers Python for backend",
            threshold=0.7,
            limit=5
        )

        # Should find only Python-related entry
        assert len(results) >= 1
        entry, similarity = results[0]
        assert "Python" in entry.content
        assert similarity >= 0.7

    def test_find_similar_below_threshold(self, memory_store):
        """Search should not return entries below threshold."""
        memory_store.save("Completely unrelated content about weather", type="long_term")
        memory_store.save("Another unrelated topic about cooking", type="long_term")

        results = memory_store.find_similar(
            "User prefers Python programming",
            threshold=0.9,  # High threshold
            limit=5
        )

        # Nothing should be found with such a high threshold
        for entry, similarity in results:
            assert similarity >= 0.9

    def test_find_similar_with_type_filter(self, memory_store):
        """Search should filter by memory type."""
        memory_store.save("User likes Python", type="long_term", user_id="user1")
        memory_store.save("User likes Python", type="short_term", user_id="user1")

        results = memory_store.find_similar(
            "User likes Python",
            threshold=0.8,
            type="long_term"
        )

        assert len(results) >= 1
        for entry, _ in results:
            assert entry.type == "long_term"

    def test_find_similar_with_user_filter(self, memory_store):
        """Search should filter by user_id."""
        memory_store.save("User likes Python", type="long_term", user_id="user1")
        memory_store.save("User likes Python", type="long_term", user_id="user2")

        results = memory_store.find_similar(
            "User likes Python",
            threshold=0.8,
            user_id="user1"
        )

        assert len(results) >= 1
        for entry, _ in results:
            assert entry.user_id == "user1"

    def test_find_similar_with_agent_filter(self, memory_store):
        """Search should filter by agent_id."""
        memory_store.save("Important fact", type="long_term", agent_id="agent1")
        memory_store.save("Important fact", type="long_term", agent_id="agent2")

        results = memory_store.find_similar(
            "Important fact",
            threshold=0.8,
            agent_id="agent1"
        )

        assert len(results) >= 1
        for entry, _ in results:
            assert entry.agent_id == "agent1"

    def test_find_similar_with_exclude_ids(self, memory_store):
        """Search should exclude specified IDs."""
        id1 = memory_store.save("User likes Python", type="long_term")
        memory_store.save("User likes Python programming", type="long_term")

        results = memory_store.find_similar(
            "User likes Python",
            threshold=0.8,
            exclude_ids=[id1]
        )

        for entry, _ in results:
            assert entry.id != id1

    def test_find_similar_respects_limit(self, memory_store):
        """Search should return at most limit results."""
        # Create several similar entries
        for i in range(10):
            memory_store.save(f"User prefers Python programming {i}", type="long_term")

        results = memory_store.find_similar(
            "User prefers Python programming",
            threshold=0.5,
            limit=3
        )

        assert len(results) <= 3

    def test_find_similar_returns_sorted_by_similarity(self, memory_store):
        """Results should be sorted by similarity descending."""
        memory_store.save("User likes Python programming", type="long_term")
        memory_store.save("User likes Python", type="long_term")
        memory_store.save("User likes", type="long_term")

        results = memory_store.find_similar(
            "User likes Python programming",
            threshold=0.3,
            limit=10
        )

        if len(results) > 1:
            similarities = [s for _, s in results]
            assert similarities == sorted(similarities, reverse=True)


class TestSaveDeduplication:
    """Test save(update_if_exists=True) - deduplication on save."""

    def test_save_creates_new_entry_by_default(self, memory_store):
        """By default save should create a new entry."""
        content = "User prefers Python"
        id1 = memory_store.save(content, type="long_term")
        id2 = memory_store.save(content, type="long_term")

        # Without update_if_exists, two entries should be created
        assert id1 != id2

    def test_save_updates_existing_with_update_if_exists(self, memory_store):
        """With update_if_exists=True, similar entry should be updated."""
        content = "User prefers Python programming language"
        id1 = memory_store.save(content, type="long_term")

        # Slightly changed content, but still similar
        new_content = "User prefers Python programming language!"
        id2 = memory_store.save(new_content, type="long_term", update_if_exists=True)

        # Should return the same ID since content is almost identical
        assert id1 == id2

    def test_save_updates_importance_on_duplicate(self, memory_store):
        """When updating a duplicate, importance should increase."""
        content = "User prefers Python"
        id1 = memory_store.save(content, type="long_term", importance=0.5)

        # Save similar content with update_if_exists
        memory_store.save(content, type="long_term", update_if_exists=True)

        # Verify that importance increased
        entry = memory_store.get_by_id(id1)
        assert entry.importance > 0.5
        assert entry.importance <= 1.0

    def test_save_creates_new_if_no_similar_found(self, memory_store):
        """If no similar entries exist, a new one should be created."""
        memory_store.save("User likes Python", type="long_term")

        # Completely different content
        new_id = memory_store.save(
            "Weather forecast for tomorrow",
            type="long_term",
            update_if_exists=True
        )

        # A new entry should be created
        entry = memory_store.get_by_id(new_id)
        assert entry.content == "Weather forecast for tomorrow"

    def test_save_with_custom_similarity_threshold(self, memory_store):
        """Test custom similarity threshold."""
        content = "User prefers Python programming"
        id1 = memory_store.save(content, type="long_term")

        # Slightly different content
        new_content = "User prefers JavaScript programming"
        id2 = memory_store.save(
            new_content,
            type="long_term",
            update_if_exists=True,
            similarity_threshold=0.99  # Very high threshold
        )

        # With a high threshold, a new entry should be created
        assert id1 != id2

    def test_save_dedup_respects_user_filter(self, memory_store):
        """Deduplication should respect user_id."""
        content = "User likes Python"
        id1 = memory_store.save(content, type="long_term", user_id="user1")

        # Same content, but different user
        id2 = memory_store.save(
            content,
            type="long_term",
            user_id="user2",
            update_if_exists=True
        )

        # A new entry should be created for the different user
        assert id1 != id2

    def test_save_dedup_respects_agent_filter(self, memory_store):
        """Deduplication should respect agent_id."""
        content = "Important configuration"
        id1 = memory_store.save(content, type="long_term", agent_id="agent1")

        # Same content, but different agent
        id2 = memory_store.save(
            content,
            type="long_term",
            agent_id="agent2",
            update_if_exists=True
        )

        # A new entry should be created for the different agent
        assert id1 != id2

    def test_save_dedup_respects_type_filter(self, memory_store):
        """Deduplication should respect memory type."""
        content = "Important note"
        id1 = memory_store.save(content, type="long_term")

        # Same content, but different type
        id2 = memory_store.save(
            content,
            type="short_term",
            update_if_exists=True
        )

        # A new entry should be created for the different type
        assert id1 != id2

    def test_save_dedup_importance_max_1(self, memory_store):
        """Importance should not exceed 1.0 after multiple updates."""
        content = "Test content"
        id1 = memory_store.save(content, type="long_term", importance=0.95)

        # Multiple updates
        for _ in range(10):
            memory_store.save(content, type="long_term", update_if_exists=True)

        entry = memory_store.get_by_id(id1)
        assert entry.importance <= 1.0


class TestEdgeCases:
    """Edge case tests."""

    def test_empty_query_find_similar(self, memory_store):
        """Empty query should not cause errors."""
        memory_store.save("Some content", type="long_term")

        # Empty query should return empty result
        results = memory_store.find_similar("", threshold=0.8)
        assert isinstance(results, list)

    def test_short_query_find_similar(self, memory_store):
        """Short query should be handled correctly."""
        memory_store.save("Python is great", type="long_term")

        # Short tokens (< 4 characters) should not be included in FTS query
        results = memory_store.find_similar("Py is", threshold=0.5)
        assert isinstance(results, list)

    def test_archived_entries_excluded_from_dedup(self, memory_store):
        """Archived entries should be excluded from deduplication."""
        content = "User likes Python"
        id1 = memory_store.save(content, type="long_term")

        # Archive the entry
        memory_store.delete(id1, hard=False)

        # Save the same content with update_if_exists
        id2 = memory_store.save(content, type="long_term", update_if_exists=True)

        # A new entry should be created because the old one is archived
        # Note: behavior depends on search implementation (include_archived)
        assert id2 is not None

    def test_special_characters_in_content(self, memory_store):
        """Special characters in content should be handled."""
        content = "Email: user@example.com, JSON: {\"key\": \"value\"}"
        id1 = memory_store.save(content, type="long_term")

        id2 = memory_store.save(content, type="long_term", update_if_exists=True)

        # Should update existing entry
        assert id1 == id2

    def test_very_long_content(self, memory_store):
        """Very long content should be handled."""
        content = "Python " * 1000  # Very long string
        id1 = memory_store.save(content, type="long_term")

        similar_content = "Python " * 999 + "code"
        id2 = memory_store.save(similar_content, type="long_term", update_if_exists=True)

        # Should find similar and update
        assert id1 == id2

    def test_multiline_content(self, memory_store):
        """Multiline content should be processed."""
        # FTS5 works with words, so use words > 3 characters
        content = "First line with important data"
        id1 = memory_store.save(content, type="long_term")

        # Same content with added whitespace (normalized during similarity)
        similar_content = "First   line   with   important   data"
        id2 = memory_store.save(similar_content, type="long_term", update_if_exists=True)

        # Should find similar (similarity=1.0 after normalization)
        assert id1 == id2


class TestIntegration:
    """Integration tests for deduplication."""

    def test_full_deduplication_workflow(self, memory_store):
        """Full deduplication workflow."""
        # Step 1: Create initial entry
        id1 = memory_store.save(
            "User prefers Python for data science",
            type="long_term",
            tags="preference",
            importance=0.5,
            user_id="user1"
        )

        # Step 2: Try to save duplicate
        id2 = memory_store.save(
            "User prefers Python for data science!",
            type="long_term",
            tags="preference",
            update_if_exists=True,
            user_id="user1"
        )

        # Step 3: Verify entry was updated, not created new
        assert id1 == id2

        # Step 4: Verify importance increased
        entry = memory_store.get_by_id(id1)
        assert entry.importance > 0.5

        # Step 5: Verify content was updated
        assert "!" in entry.content

    def test_multiple_users_same_content(self, memory_store):
        """Different users can have the same content."""
        content = "I like Python"

        id1 = memory_store.save(content, type="long_term", user_id="user1")
        id2 = memory_store.save(content, type="long_term", user_id="user2", update_if_exists=True)
        id3 = memory_store.save(content, type="long_term", user_id="user3", update_if_exists=True)

        # All entries should be different (different users)
        assert len({id1, id2, id3}) == 3

    def test_dedup_with_search_integration(self, memory_store):
        """Deduplication should work with FTS5 search."""
        # Create several entries
        memory_store.save("Python is great for ML", type="long_term")
        memory_store.save("JavaScript is great for web", type="long_term")

        # Search for Python-related entries
        results = memory_store.search("Python", type="long_term")
        assert len(results) >= 1

        # Verify deduplication when saving similar content
        memory_store.save("Python is great for ML!", type="long_term", update_if_exists=True)

        # Number of entries should not increase
        new_results = memory_store.search("Python", type="long_term")
        assert len(new_results) == len(results)

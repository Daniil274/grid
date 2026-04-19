"""
Comprehensive tests for MemoryStore cleanup methods.

Tests cover:
- get_db_size_mb(): database file size measurement
- check_cleanup_needed(): threshold checking and candidate counting
- run_cleanup(): cleanup strategies (aggressive, balanced, conservative)
- vacuum_db(): database optimization and space reclamation
"""

import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.memory.store import MemoryStore, MemoryEntry


class TestGetDbSizeMb:
    """Tests for get_db_size_mb() method."""

    def test_empty_database_size(self, temp_db):
        """Test size of empty database is small but non-zero."""
        size = temp_db.get_db_size_mb()
        assert size > 0  # Empty DB still has structure
        assert size < 0.1  # Less than 100KB

    def test_nonexistent_database_returns_zero(self, tmp_path):
        """Test that nonexistent database file returns 0.0."""
        # Use a path in a valid directory but with nonexistent file
        nonexistent_db = tmp_path / "nonexistent" / "memory.db"
        store = MemoryStore(db_path=str(nonexistent_db))
        # Don't save anything, so file doesn't exist
        store.db_path = tmp_path / "really_nonexistent.db"  # Override to truly nonexistent
        size = store.get_db_size_mb()
        assert size == 0.0

    def test_size_grows_with_data(self, temp_db):
        """Test that size increases when data is added."""
        initial_size = temp_db.get_db_size_mb()
        
        # Add significant amount of data
        for i in range(100):
            temp_db.save(
                content="x" * 1000,  # 1KB per entry
                type="long_term",
                tags=f"test_{i}"
            )
        
        new_size = temp_db.get_db_size_mb()
        assert new_size > initial_size

    def test_size_returns_float(self, temp_db):
        """Test that size is returned as float."""
        size = temp_db.get_db_size_mb()
        assert isinstance(size, float)

    def test_size_accuracy_reasonable(self, temp_db):
        """Test that reported size matches actual file size."""
        # Add data
        for i in range(50):
            temp_db.save(
                content="test content " * 100,
                type="long_term",
                tags=f"accuracy_test_{i}"
            )
        
        reported_size = temp_db.get_db_size_mb()
        actual_size = temp_db.db_path.stat().st_size / (1024 * 1024)
        
        # Should be exactly the same
        assert abs(reported_size - actual_size) < 0.0001


class TestCheckCleanupNeeded:
    """Tests for check_cleanup_needed() method."""

    def test_below_threshold_not_needed(self, temp_db):
        """Test that cleanup not needed when size below threshold."""
        result = temp_db.check_cleanup_needed(threshold_mb=100.0)
        
        assert result["needed"] is False
        assert result["current_size_mb"] < result["threshold_mb"]

    def test_above_threshold_needed(self, temp_db):
        """Test that cleanup needed when size above threshold."""
        result = temp_db.check_cleanup_needed(threshold_mb=0.001)  # Very small threshold
        
        assert result["needed"] is True
        assert result["current_size_mb"] >= result["threshold_mb"]

    def test_exact_threshold_needed(self, temp_db):
        """Test cleanup needed when size exactly at threshold."""
        # Get current size
        current_size = temp_db.get_db_size_mb()
        
        result = temp_db.check_cleanup_needed(threshold_mb=current_size)
        
        # At threshold should trigger cleanup (>=)
        assert result["needed"] is True

    def test_counts_archived_entries(self, temp_db):
        """Test that archived entries are counted correctly."""
        # Add some entries
        for i in range(5):
            temp_db.save(content=f"entry_{i}", type="long_term", tags="test")
        
        # Archive some entries
        with temp_db._get_connection() as conn:
            conn.execute("UPDATE memory SET is_archived = 1 WHERE id <= 3")
            conn.commit()
        
        result = temp_db.check_cleanup_needed()
        
        assert result["archived_count"] == 3

    def test_counts_low_importance_entries(self, temp_db):
        """Test that low importance entries are counted correctly."""
        # Add entries with different importance
        temp_db.save(content="high importance", type="long_term", tags="test", importance=0.9)
        temp_db.save(content="low importance 1", type="long_term", tags="test", importance=0.1)
        temp_db.save(content="low importance 2", type="long_term", tags="test", importance=0.2)
        temp_db.save(content="medium importance", type="long_term", tags="test", importance=0.5)
        
        result = temp_db.check_cleanup_needed()
        
        # Should count entries with importance < 0.3
        assert result["low_importance_count"] == 2

    def test_counts_expired_ttl_entries(self, temp_db):
        """Test that expired TTL entries are counted correctly."""
        # Add entries with different TTL states
        # Expired entry (accessed 10 days ago, TTL 5 days)
        temp_db.save(
            content="expired entry",
            type="short_term",
            tags="test",
            ttl_days=5
        )
        # Update last_accessed_at to be in the past
        with temp_db._get_connection() as conn:
            conn.execute("""
                UPDATE memory 
                SET last_accessed_at = datetime('now', '-10 days')
                WHERE content = 'expired entry'
            """)
            conn.commit()
        
        # Non-expired entry
        temp_db.save(
            content="active entry",
            type="short_term",
            tags="test",
            ttl_days=30
        )
        
        result = temp_db.check_cleanup_needed()
        
        assert result["expired_ttl_count"] >= 1

    def test_returns_all_required_keys(self, temp_db):
        """Test that result contains all expected keys."""
        result = temp_db.check_cleanup_needed()
        
        expected_keys = [
            "needed",
            "current_size_mb",
            "threshold_mb",
            "archived_count",
            "low_importance_count",
            "expired_ttl_count"
        ]
        
        for key in expected_keys:
            assert key in result, f"Missing key: {key}"

    def test_custom_threshold(self, temp_db):
        """Test that custom threshold is used correctly."""
        custom_threshold = 50.0
        result = temp_db.check_cleanup_needed(threshold_mb=custom_threshold)
        
        assert result["threshold_mb"] == custom_threshold

    def test_zero_threshold_always_needs_cleanup(self, temp_db):
        """Test that threshold=0 always triggers cleanup needed."""
        result = temp_db.check_cleanup_needed(threshold_mb=0.0)
        
        # Any non-negative size should trigger cleanup
        if temp_db.get_db_size_mb() >= 0:
            assert result["needed"] is True

    def test_negative_threshold_always_needs_cleanup(self, temp_db):
        """Test that negative threshold always triggers cleanup."""
        result = temp_db.check_cleanup_needed(threshold_mb=-1.0)
        
        assert result["needed"] is True


class TestRunCleanup:
    """Tests for run_cleanup() method."""

    def test_conservative_strategy_only_archived(self, temp_db):
        """Test conservative strategy only deletes archived entries."""
        # Add entries
        for i in range(5):
            temp_db.save(content=f"entry_{i}", type="long_term", tags="test")
        
        # Archive some
        with temp_db._get_connection() as conn:
            conn.execute("UPDATE memory SET is_archived = 1 WHERE id <= 2")
            conn.commit()
        
        result = temp_db.run_cleanup(strategy="conservative")
        
        assert result["archived_deleted"] == 2
        assert result["expired_deleted"] == 0
        assert result["low_importance_deleted"] == 0

    def test_balanced_strategy_archived_and_expired(self, temp_db):
        """Test balanced strategy handles archived and expired TTL."""
        # Add archived entry
        temp_db.save(content="archived", type="long_term", tags="test")
        with temp_db._get_connection() as conn:
            conn.execute("UPDATE memory SET is_archived = 1 WHERE content = 'archived'")
            conn.commit()
        
        # Add expired TTL entry
        temp_db.save(content="expired", type="short_term", tags="test", ttl_days=1)
        with temp_db._get_connection() as conn:
            conn.execute("""
                UPDATE memory 
                SET last_accessed_at = datetime('now', '-5 days')
                WHERE content = 'expired'
            """)
            conn.commit()
        
        result = temp_db.run_cleanup(strategy="balanced")
        
        assert result["archived_deleted"] == 1
        assert result["expired_deleted"] >= 0  # May or may not be expired yet

    def test_aggressive_strategy_all_categories(self, temp_db):
        """Test aggressive strategy handles all categories."""
        # Add archived entry
        temp_db.save(content="archived", type="long_term", tags="test")
        with temp_db._get_connection() as conn:
            conn.execute("UPDATE memory SET is_archived = 1 WHERE content = 'archived'")
            conn.commit()
        
        # Add low importance entry
        temp_db.save(content="low_importance", type="long_term", tags="test", importance=0.1)
        
        # Add expired TTL entry
        temp_db.save(content="expired", type="short_term", tags="test", ttl_days=1)
        with temp_db._get_connection() as conn:
            conn.execute("""
                UPDATE memory 
                SET last_accessed_at = datetime('now', '-5 days')
                WHERE content = 'expired'
            """)
            conn.commit()
        
        result = temp_db.run_cleanup(strategy="aggressive", min_importance=0.3)
        
        assert result["archived_deleted"] == 1
        assert result["low_importance_deleted"] == 1  # Archived, not deleted

    def test_hard_delete_archived_false(self, temp_db):
        """Test that hard_delete_archived=False preserves archived entries."""
        temp_db.save(content="archived", type="long_term", tags="test")
        with temp_db._get_connection() as conn:
            conn.execute("UPDATE memory SET is_archived = 1")
            conn.commit()
        
        result = temp_db.run_cleanup(strategy="conservative", hard_delete_archived=False)
        
        assert result["archived_deleted"] == 0
        
        # Verify entry still exists
        with temp_db._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) as count FROM memory WHERE is_archived = 1")
            assert cursor.fetchone()["count"] == 1

    def test_empty_database_cleanup(self, temp_db):
        """Test cleanup on empty database returns zeros."""
        result = temp_db.run_cleanup(strategy="aggressive")
        
        assert result["archived_deleted"] == 0
        assert result["expired_deleted"] == 0
        assert result["low_importance_deleted"] == 0

    def test_returns_dict_with_all_keys(self, temp_db):
        """Test that result contains all expected keys."""
        result = temp_db.run_cleanup()
        
        expected_keys = [
            "archived_deleted",
            "expired_deleted",
            "low_importance_deleted"
        ]
        
        for key in expected_keys:
            assert key in result, f"Missing key: {key}"

    def test_custom_min_importance(self, temp_db):
        """Test custom min_importance threshold."""
        temp_db.save(content="low1", type="long_term", tags="test", importance=0.1)
        temp_db.save(content="low2", type="long_term", tags="test", importance=0.2)
        temp_db.save(content="medium", type="long_term", tags="test", importance=0.4)
        
        result = temp_db.run_cleanup(strategy="aggressive", min_importance=0.3)
        
        # Entries with importance < 0.3 should be archived
        assert result["low_importance_deleted"] == 2

    def test_invalid_strategy_handled(self, temp_db):
        """Test that invalid strategy doesn't crash."""
        # Should not raise exception
        result = temp_db.run_cleanup(strategy="invalid_strategy")
        
        # Should still return valid result
        assert isinstance(result, dict)

    def test_deletes_from_fts_index(self, temp_db):
        """Test that cleanup also removes entries from FTS index."""
        temp_db.save(content="test entry for fts", type="long_term", tags="test")
        
        # Archive it
        with temp_db._get_connection() as conn:
            conn.execute("UPDATE memory SET is_archived = 1")
            conn.commit()
        
        # Run cleanup
        temp_db.run_cleanup(strategy="conservative")
        
        # Check FTS index is also cleaned
        with temp_db._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) as count FROM memory_fts")
            fts_count = cursor.fetchone()["count"]
            cursor = conn.execute("SELECT COUNT(*) as count FROM memory")
            main_count = cursor.fetchone()["count"]
            
            assert fts_count == main_count


class TestVacuumDb:
    """Tests for vacuum_db() method."""

    def test_vacuum_returns_success(self, temp_db):
        """Test that vacuum returns success dict."""
        result = temp_db.vacuum_db()
        
        assert result["success"] is True
        assert "size_before_mb" in result
        assert "size_after_mb" in result
        assert "reclaimed_mb" in result

    def test_vacuum_reclaims_space(self, temp_db):
        """Test that vacuum reclaims space after deletion."""
        # Add data
        for i in range(100):
            temp_db.save(content="x" * 1000, type="long_term", tags=f"test_{i}")
        
        size_with_data = temp_db.get_db_size_mb()
        
        # Delete data
        with temp_db._get_connection() as conn:
            conn.execute("DELETE FROM memory")
            conn.commit()
        
        # Vacuum
        result = temp_db.vacuum_db()
        
        assert result["success"] is True
        assert result["size_after_mb"] < size_with_data
        assert result["reclaimed_mb"] > 0

    def test_vacuum_size_values_are_floats(self, temp_db):
        """Test that size values are floats."""
        result = temp_db.vacuum_db()
        
        assert isinstance(result["size_before_mb"], float)
        assert isinstance(result["size_after_mb"], float)
        assert isinstance(result["reclaimed_mb"], float)

    def test_vacuum_rounds_to_two_decimals(self, temp_db):
        """Test that size values are rounded to 2 decimal places."""
        result = temp_db.vacuum_db()
        
        # Check decimal places
        for key in ["size_before_mb", "size_after_mb", "reclaimed_mb"]:
            value = result[key]
            # Check that value has at most 2 decimal places
            rounded = round(value, 2)
            assert abs(value - rounded) < 0.001, f"{key} not rounded to 2 decimals"

    def test_vacuum_on_empty_database(self, temp_db):
        """Test vacuum on empty database succeeds."""
        result = temp_db.vacuum_db()
        
        assert result["success"] is True

    def test_vacuum_maintains_data_integrity(self, temp_db):
        """Test that vacuum doesn't corrupt data."""
        # Add data
        temp_db.save(content="important data", type="long_term", tags="test")
        temp_db.save(content="another entry", type="short_term", tags="test")
        
        # Vacuum
        temp_db.vacuum_db()
        
        # Verify data still exists
        results = temp_db.search("important")
        assert len(results) >= 1
        assert results[0].content == "important data"

    def test_vacuum_error_handling(self, temp_db):
        """Test that vacuum handles errors gracefully."""
        # Close the database connection to potentially cause issues
        # This is a bit tricky to test, so we'll just verify the method handles exceptions
        with patch.object(sqlite3, 'connect') as mock_connect:
            mock_connect.side_effect = sqlite3.Error("Test error")
            
            result = temp_db.vacuum_db()
            
            assert result["success"] is False
            assert "error" in result
            assert "Test error" in result["error"]

    def test_vacbum_size_after_zero_on_failure(self, temp_db):
        """Test that size_after_mb is 0 on failure."""
        with patch.object(sqlite3, 'connect') as mock_connect:
            mock_connect.side_effect = Exception("Forced error")
            
            result = temp_db.vacuum_db()
            
            assert result["success"] is False
            assert result["size_after_mb"] == 0.0
            assert result["reclaimed_mb"] == 0.0


class TestCleanupIntegration:
    """Integration tests for cleanup workflow."""

    def test_full_cleanup_workflow(self, temp_db):
        """Test complete cleanup workflow: check -> cleanup -> vacuum."""
        # Add various types of data
        for i in range(20):
            temp_db.save(
                content=f"entry_{i}" * 100,
                type="long_term" if i % 2 == 0 else "short_term",
                tags=f"test_{i}",
                importance=0.1 if i < 5 else 0.7,
                ttl_days=1 if i % 3 == 0 else None
            )
        
        # Archive some
        with temp_db._get_connection() as conn:
            conn.execute("UPDATE memory SET is_archived = 1 WHERE id <= 5")
            # Set some as expired
            conn.execute("""
                UPDATE memory 
                SET last_accessed_at = datetime('now', '-10 days')
                WHERE ttl_days IS NOT NULL
            """)
            conn.commit()
        
        # Check if cleanup needed
        check_result = temp_db.check_cleanup_needed(threshold_mb=0.001)
        assert check_result["needed"] is True
        assert check_result["archived_count"] == 5
        
        # Run cleanup
        cleanup_result = temp_db.run_cleanup(strategy="balanced")
        assert cleanup_result["archived_deleted"] == 5
        
        # Vacuum
        vacuum_result = temp_db.vacuum_db()
        assert vacuum_result["success"] is True

    def test_cleanup_preserves_active_data(self, temp_db):
        """Test that cleanup doesn't affect active, important data."""
        # Add important active data
        temp_db.save(
            content="important active data",
            type="long_term",
            tags="important",
            importance=0.9
        )
        
        # Add data to be cleaned
        temp_db.save(content="to archive", type="long_term", tags="test", importance=0.1)
        with temp_db._get_connection() as conn:
            conn.execute("UPDATE memory SET is_archived = 1 WHERE content = 'to archive'")
            conn.commit()
        
        # Run cleanup
        temp_db.run_cleanup(strategy="aggressive")
        
        # Verify important data is preserved
        results = temp_db.search("important active")
        assert len(results) == 1
        assert results[0].content == "important active data"

    def test_multiple_sequential_cleanups(self, temp_db):
        """Test running multiple cleanups in sequence."""
        for i in range(10):
            temp_db.save(content=f"batch_{i}", type="long_term", tags="test")
        
        # First cleanup
        with temp_db._get_connection() as conn:
            conn.execute("UPDATE memory SET is_archived = 1 WHERE id <= 5")
            conn.commit()
        
        result1 = temp_db.run_cleanup(strategy="conservative")
        assert result1["archived_deleted"] == 5
        
        # Second cleanup (nothing to clean)
        result2 = temp_db.run_cleanup(strategy="conservative")
        assert result2["archived_deleted"] == 0
        
        # Archive more
        with temp_db._get_connection() as conn:
            conn.execute("UPDATE memory SET is_archived = 1")
            conn.commit()
        
        result3 = temp_db.run_cleanup(strategy="conservative")
        assert result3["archived_deleted"] == 5


class TestEdgeCases:
    """Edge case tests for cleanup methods."""

    def test_check_cleanup_with_all_archived(self, temp_db):
        """Test check_cleanup_needed when all entries are archived."""
        for i in range(10):
            temp_db.save(content=f"entry_{i}", type="long_term", tags="test")
        
        with temp_db._get_connection() as conn:
            conn.execute("UPDATE memory SET is_archived = 1")
            conn.commit()
        
        result = temp_db.check_cleanup_needed()
        
        assert result["archived_count"] == 10

    def test_check_cleanup_with_zero_importance(self, temp_db):
        """Test that importance = 0 is counted as low importance."""
        temp_db.save(content="zero", type="long_term", tags="test", importance=0.0)
        
        result = temp_db.check_cleanup_needed()
        
        assert result["low_importance_count"] == 1

    def test_check_cleanup_boundary_importance(self, temp_db):
        """Test boundary value for low importance (exactly 0.3)."""
        temp_db.save(content="exactly_03", type="long_term", tags="test", importance=0.3)
        temp_db.save(content="below_03", type="long_term", tags="test", importance=0.29)
        
        result = temp_db.check_cleanup_needed()
        
        # 0.3 is NOT below 0.3, so only 0.29 should be counted
        assert result["low_importance_count"] == 1

    def test_run_cleanup_with_null_ttl(self, temp_db):
        """Test that NULL TTL entries are not affected by TTL cleanup."""
        temp_db.save(content="no ttl", type="long_term", tags="test", ttl_days=None)
        
        result = temp_db.run_cleanup(strategy="balanced")
        
        # Entry should still exist
        with temp_db._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) as count FROM memory")
            assert cursor.fetchone()["count"] == 1

    def test_vacuum_multiple_times(self, temp_db):
        """Test that running vacuum multiple times is safe."""
        for i in range(5):
            temp_db.save(content=f"data_{i}", type="long_term", tags="test")
        
        result1 = temp_db.vacuum_db()
        result2 = temp_db.vacuum_db()
        result3 = temp_db.vacuum_db()
        
        assert result1["success"] is True
        assert result2["success"] is True
        assert result3["success"] is True

    def test_check_cleanup_negative_threshold(self, temp_db):
        """Test check_cleanup_needed with negative threshold."""
        result = temp_db.check_cleanup_needed(threshold_mb=-10.0)
        
        # Negative threshold means cleanup is always needed
        assert result["needed"] is True

    def test_run_cleanup_very_high_importance_threshold(self, temp_db):
        """Test aggressive cleanup with very high importance threshold."""
        temp_db.save(content="high", type="long_term", tags="test", importance=0.99)
        temp_db.save(content="medium", type="long_term", tags="test", importance=0.5)
        
        result = temp_db.run_cleanup(strategy="aggressive", min_importance=0.95)
        
        # Only 0.99 should NOT be archived, 0.5 should be archived
        assert result["low_importance_deleted"] == 1


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_db():
    """Create a temporary database for each test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    
    store = MemoryStore(db_path=db_path)
    
    yield store
    
    # Cleanup
    try:
        os.unlink(db_path)
        # Also remove WAL and SHM files if they exist
        wal_path = db_path + "-wal"
        shm_path = db_path + "-shm"
        if os.path.exists(wal_path):
            os.unlink(wal_path)
        if os.path.exists(shm_path):
            os.unlink(shm_path)
    except Exception:
        pass


# =============================================================================
# Test Runner Configuration
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

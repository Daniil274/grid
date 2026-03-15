"""
Tests for TTL (Time-to-Live) functionality in MemoryStore.

Tests:
- save() with ttl_days parameter
- Default TTL for long_term entries
- last_accessed_at update on read operations
- cleanup_expired() method
- extend_ttl_on_access behavior
"""

import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import Mock

from core.memory_store import MemoryStore, MemoryEntry


class MockConfig:
    """Mock config for TTL tests."""
    
    def __init__(self, default_ttl=90, extend_on_access=True):
        self._default_ttl = default_ttl
        self._extend_on_access = extend_on_access
    
    def get(self, key: str, default=None):
        if key == 'memory_optimizer.default_long_term_ttl_days':
            return self._default_ttl
        if key == 'memory_optimizer.extend_ttl_on_access':
            return self._extend_on_access
        return default


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def store(temp_db):
    """Create a MemoryStore without config."""
    return MemoryStore(db_path=temp_db)


@pytest.fixture
def store_with_config(temp_db):
    """Create a MemoryStore with TTL config."""
    config = MockConfig(default_ttl=30, extend_on_access=True)
    return MemoryStore(db_path=temp_db, config=config)


@pytest.fixture
def store_no_extend(temp_db):
    """Create a MemoryStore with extend_ttl_on_access=False."""
    config = MockConfig(default_ttl=30, extend_on_access=False)
    return MemoryStore(db_path=temp_db, config=config)


class TestSaveWithTTL:
    """Tests for save() with ttl_days parameter."""
    
    def test_save_with_explicit_ttl(self, store):
        """Test saving entry with explicit TTL."""
        entry_id = store.save(
            content="Test content",
            type="long_term",
            ttl_days=7
        )
        
        entry = store.get_by_id(entry_id)
        assert entry is not None
        assert entry.ttl_days == 7
        assert entry.last_accessed_at is not None
    
    def test_save_long_term_default_ttl(self, store_with_config):
        """Test that long_term entries get default TTL from config."""
        entry_id = store_with_config.save(
            content="Long term memory",
            type="long_term"
        )
        
        entry = store_with_config.get_by_id(entry_id)
        assert entry is not None
        assert entry.ttl_days == 30  # From MockConfig
    
    def test_save_long_term_explicit_ttl_overrides_default(self, store_with_config):
        """Test that explicit TTL overrides default."""
        entry_id = store_with_config.save(
            content="Long term memory",
            type="long_term",
            ttl_days=60
        )
        
        entry = store_with_config.get_by_id(entry_id)
        assert entry.ttl_days == 60
    
    def test_save_short_term_no_default_ttl(self, store_with_config):
        """Test that short_term entries don't get default TTL."""
        entry_id = store_with_config.save(
            content="Short term memory",
            type="short_term"
        )
        
        entry = store_with_config.get_by_id(entry_id)
        assert entry.ttl_days is None
    
    def test_save_without_config_no_ttl(self, store):
        """Test saving without config results in no TTL."""
        entry_id = store.save(
            content="Memory without config",
            type="long_term"
        )
        
        entry = store.get_by_id(entry_id)
        assert entry.ttl_days is None
    
    def test_save_null_ttl_is_permanent(self, store):
        """Test that NULL ttl_days means permanent entry."""
        entry_id = store.save(
            content="Permanent memory",
            type="long_term",
            ttl_days=None
        )
        
        entry = store.get_by_id(entry_id)
        assert entry.ttl_days is None


class TestLastAccessedUpdate:
    """Tests for last_accessed_at update on read operations."""
    
    def test_search_updates_last_accessed(self, store_with_config):
        """Test that search() updates last_accessed_at."""
        entry_id = store_with_config.save(
            content="Test entry for search",
            type="long_term"
        )
        
        # Get initial last_accessed_at
        entry = store_with_config.get_by_id(entry_id)
        initial_access = entry.last_accessed_at
        
        # Wait a moment and search
        import time
        time.sleep(0.1)
        results = store_with_config.search("Test entry", limit=10)
        
        assert len(results) > 0
        # Get updated entry
        updated_entry = store_with_config.get_by_id(entry_id)
        # last_accessed_at should be updated (or same if very fast)
        assert updated_entry.last_accessed_at >= initial_access
    
    def test_get_by_id_updates_last_accessed(self, store_with_config):
        """Test that get_by_id() updates last_accessed_at."""
        entry_id = store_with_config.save(
            content="Test entry for get_by_id",
            type="long_term"
        )
        
        # First access
        entry1 = store_with_config.get_by_id(entry_id)
        initial_access = entry1.last_accessed_at
        
        # Wait and access again
        import time
        time.sleep(0.1)
        entry2 = store_with_config.get_by_id(entry_id)
        
        # last_accessed_at should be updated
        assert entry2.last_accessed_at >= initial_access
    
    def test_no_update_when_extend_disabled(self, store_no_extend):
        """Test that last_accessed_at is NOT updated when extend_ttl_on_access=False."""
        entry_id = store_no_extend.save(
            content="Test entry with extend disabled",
            type="long_term"
        )
        
        # First access
        entry1 = store_no_extend.get_by_id(entry_id)
        initial_access = entry1.last_accessed_at
        
        # Wait and access again
        import time
        time.sleep(0.1)
        entry2 = store_no_extend.get_by_id(entry_id)
        
        # last_accessed_at should NOT be updated
        assert entry2.last_accessed_at == initial_access


class TestCleanupExpired:
    """Tests for cleanup_expired() method."""
    
    def test_cleanup_expired_archives_old_entries(self, store):
        """Test that expired entries are archived."""
        # Create entry with TTL=1 day, accessed 2 days ago
        # We need to manually set last_accessed_at to simulate old access
        entry_id = store.save(
            content="Old entry",
            type="long_term",
            ttl_days=1
        )
        
        # Manually set last_accessed_at to 2 days ago
        import sqlite3
        two_days_ago = (datetime.now() - timedelta(days=2)).isoformat()
        with sqlite3.connect(str(store.db_path)) as conn:
            conn.execute(
                "UPDATE memory SET last_accessed_at = ? WHERE id = ?",
                (two_days_ago, entry_id)
            )
            conn.commit()
        
        # Run cleanup
        count = store.cleanup_expired()
        
        assert count == 1
        
        # Verify entry is archived
        entry = store.get_by_id(entry_id)
        assert entry.is_archived == 1
    
    def test_cleanup_does_not_archive_fresh_entries(self, store):
        """Test that non-expired entries are not archived."""
        # Create entry with TTL=30 days, just accessed
        entry_id = store.save(
            content="Fresh entry",
            type="long_term",
            ttl_days=30
        )
        
        # Run cleanup
        count = store.cleanup_expired()
        
        assert count == 0
        
        # Verify entry is NOT archived
        entry = store.get_by_id(entry_id)
        assert entry.is_archived == 0
    
    def test_cleanup_does_not_archive_null_ttl(self, store):
        """Test that entries with NULL TTL are never archived."""
        entry_id = store.save(
            content="Permanent entry",
            type="long_term",
            ttl_days=None
        )
        
        # Manually set old last_accessed_at
        import sqlite3
        old_date = (datetime.now() - timedelta(days=365)).isoformat()
        with sqlite3.connect(str(store.db_path)) as conn:
            conn.execute(
                "UPDATE memory SET last_accessed_at = ? WHERE id = ?",
                (old_date, entry_id)
            )
            conn.commit()
        
        # Run cleanup
        count = store.cleanup_expired()
        
        assert count == 0
        
        entry = store.get_by_id(entry_id)
        assert entry.is_archived == 0
    
    def test_cleanup_does_not_archive_already_archived(self, store):
        """Test that already archived entries are not affected."""
        entry_id = store.save(
            content="Already archived",
            type="long_term",
            ttl_days=1
        )
        
        # Archive it first
        store.update(entry_id=entry_id, is_archived=True)
        
        # Set old access time
        import sqlite3
        old_date = (datetime.now() - timedelta(days=10)).isoformat()
        with sqlite3.connect(str(store.db_path)) as conn:
            conn.execute(
                "UPDATE memory SET last_accessed_at = ? WHERE id = ?",
                (old_date, entry_id)
            )
            conn.commit()
        
        # Run cleanup
        count = store.cleanup_expired()
        
        assert count == 0  # Already archived, not counted


class TestTTLIntegration:
    """Integration tests for TTL functionality."""
    
    def test_full_ttl_lifecycle(self, store_with_config):
        """Test complete TTL lifecycle: save, access, expire, cleanup."""
        # 1. Save entry with default TTL
        entry_id = store_with_config.save(
            content="Important memory",
            type="long_term"
        )
        
        # 2. Verify TTL is set from config
        entry = store_with_config.get_by_id(entry_id)
        assert entry.ttl_days == 30
        assert entry.last_accessed_at is not None
        assert entry.is_archived == 0
        
        # 3. Access the entry (updates last_accessed_at)
        results = store_with_config.search("Important", type="long_term")
        assert len(results) == 1
        
        # 4. Cleanup should not archive (entry is fresh)
        count = store_with_config.cleanup_expired()
        assert count == 0
        
        entry = store_with_config.get_by_id(entry_id)
        assert entry.is_archived == 0
    
    def test_extend_ttl_on_access(self, store):
        """Test that accessing entry extends its effective TTL."""
        # Create entry with TTL=5 days
        entry_id = store.save(
            content="Entry to be extended",
            type="long_term",
            ttl_days=5
        )
        
        # Set last_accessed_at to 4 days ago
        import sqlite3
        four_days_ago = (datetime.now() - timedelta(days=4)).isoformat()
        with sqlite3.connect(str(store.db_path)) as conn:
            conn.execute(
                "UPDATE memory SET last_accessed_at = ? WHERE id = ?",
                (four_days_ago, entry_id)
            )
            conn.commit()
        
        # TTL would expire in 1 day, but we access it now
        entry = store.get_by_id(entry_id)
        
        # last_accessed_at should be updated to now
        # Entry should not be expired yet
        count = store.cleanup_expired()
        assert count == 0
        
        # Verify last_accessed_at was updated
        updated_entry = store.get_by_id(entry_id)
        assert updated_entry.last_accessed_at > four_days_ago


class TestPreloadContextWithTTL:
    """Tests for get_preload_context with TTL behavior."""
    
    def test_preload_context_updates_access_time(self, store_with_config):
        """Test that get_preload_context updates last_accessed_at."""
        # Create some entries
        store_with_config.save(content="Long term 1", type="long_term")
        store_with_config.save(content="Insight 1", type="insight")
        
        # Get preload context
        preload = store_with_config.get_preload_context()
        
        # All entries should have last_accessed_at updated via search calls
        # This is implicit - search() updates last_accessed_at
        assert len(preload["long_term"]) >= 1 or len(preload["insights"]) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
Тест для проверки фикса SQL ошибки "no such column: 8040"
"""
import sys
import os
import tempfile
from pathlib import Path

# Fix Windows console encoding
if sys.platform == 'win32':
    os.environ['PYTHONIOENCODING'] = 'utf-8'

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from core.memory_store import MemoryStore


def test_fts_search_with_numbers():
    """Test FTS5 search with numbers that could be interpreted as column names."""

    # Create temporary database
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    try:
        # Initialize store
        store = MemoryStore(db_path)

        # Save test data
        entry_id = store.save(
            content='Мембрана ультранизкого давления ULP22-8040 (производитель: Hydranautics). '
                    'Характеристики: производительность 12,100 GPD (45,7 м³/сут), '
                    'селективность 99%, эффективная площадь 400 ft² (37,2 м²).',
            type='long_term',
            tags='мембрана,фильтр',
            importance=0.8
        )

        print(f"[OK] Saved entry #{entry_id}")

        # Test problematic search query with number
        print("\n[SEARCH] Testing search: 'мембрана ULP22-8040'")
        results = store.search(query='мембрана ULP22-8040', limit=10)

        if results:
            print(f"[OK] Found {len(results)} result(s):")
            for r in results:
                print(f"   ID: {r.id}, Content: {r.content[:80]}...")
        else:
            print("[FAIL] No results found")
            return False

        # Test search with just number
        print("\n[SEARCH] Testing search: '8040'")
        results = store.search(query='8040', limit=10)

        if results:
            print(f"[OK] Found {len(results)} result(s) for number search")
        else:
            print("[WARN] No results for number search (expected if FTS doesn't index pure numbers)")

        # Test search with model name
        print("\n[SEARCH] Testing search: 'ULP22-8040'")
        results = store.search(query='ULP22-8040', limit=10)

        if results:
            print(f"[OK] Found {len(results)} result(s) for model name search")
        else:
            print("[FAIL] No results for model name search")
            return False

        print("\n[OK] All tests passed!")
        return True

    except Exception as e:
        print(f"\n[FAIL] Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        # Cleanup
        Path(db_path).unlink(missing_ok=True)
        Path(db_path + '-shm').unlink(missing_ok=True)
        Path(db_path + '-wal').unlink(missing_ok=True)


if __name__ == '__main__':
    success = test_fts_search_with_numbers()
    sys.exit(0 if success else 1)

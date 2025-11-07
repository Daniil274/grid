"""
Tests for embeddings and semantic search functionality.
"""

import pytest
from pathlib import Path
import tempfile
import shutil

# Import embeddings module
try:
    from core.embeddings import (
        EmbeddingsManager,
        CodeSearchManager,
        create_embeddings_manager,
        create_code_search_manager
    )
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False

# Import context with embeddings
from core.context import ContextManager


# Skip all tests if dependencies not available
pytestmark = pytest.mark.skipif(
    not EMBEDDINGS_AVAILABLE,
    reason="sentence-transformers or chromadb not installed"
)


class TestEmbeddingsManager:
    """Test EmbeddingsManager functionality."""

    def test_embeddings_manager_creation(self):
        """Test creating embeddings manager."""
        manager = create_embeddings_manager()
        assert manager is not None
        assert manager.model_name == "all-MiniLM-L6-v2"

    def test_embed_single_text(self):
        """Test embedding a single text."""
        manager = create_embeddings_manager()
        if manager is None:
            pytest.skip("Embeddings manager not available")

        text = "This is a test sentence for embeddings"
        embedding = manager.embed_text(text)

        assert embedding is not None
        assert len(embedding) > 0
        assert isinstance(embedding, list)
        assert all(isinstance(x, float) for x in embedding)

    def test_embed_multiple_texts(self):
        """Test embedding multiple texts."""
        manager = create_embeddings_manager()
        if manager is None:
            pytest.skip("Embeddings manager not available")

        texts = [
            "First test sentence",
            "Second test sentence",
            "Third test sentence"
        ]
        embeddings = manager.embed_texts(texts)

        assert len(embeddings) == len(texts)
        assert all(len(emb) > 0 for emb in embeddings)

    def test_add_and_search_texts(self):
        """Test adding texts and searching."""
        manager = create_embeddings_manager()
        if manager is None:
            pytest.skip("Embeddings manager not available")

        # Add some texts
        texts = [
            "Python is a programming language",
            "JavaScript is used for web development",
            "Machine learning uses neural networks",
            "Data science involves statistics"
        ]
        manager.add_texts(texts)

        # Search for similar text
        query = "programming languages"
        results = manager.search(query, n_results=2)

        assert len(results) > 0
        assert results[0]['text'] in texts
        # Python sentence should be most relevant
        assert 'Python' in results[0]['text'] or 'JavaScript' in results[0]['text']

    def test_empty_query(self):
        """Test searching with empty query."""
        manager = create_embeddings_manager()
        if manager is None:
            pytest.skip("Embeddings manager not available")

        results = manager.search("")
        assert len(results) == 0

    def test_collection_count(self):
        """Test getting collection count."""
        manager = create_embeddings_manager()
        if manager is None:
            pytest.skip("Embeddings manager not available")

        initial_count = manager.get_collection_count()

        # Add texts
        manager.add_texts(["Test 1", "Test 2", "Test 3"])

        new_count = manager.get_collection_count()
        assert new_count >= initial_count + 3


class TestCodeSearchManager:
    """Test CodeSearchManager functionality."""

    def test_code_search_manager_creation(self):
        """Test creating code search manager."""
        manager = create_code_search_manager()
        assert manager is not None

    def test_index_and_search_code_files(self):
        """Test indexing code files and searching."""
        manager = create_code_search_manager()
        if manager is None:
            pytest.skip("Code search manager not available")

        # Create temporary directory with test files
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Create test Python files
            (tmppath / "test1.py").write_text("""
def calculate_sum(a, b):
    '''Calculate sum of two numbers'''
    return a + b
""")

            (tmppath / "test2.py").write_text("""
class DataProcessor:
    '''Process data with various methods'''
    def process(self, data):
        return data.strip()
""")

            (tmppath / "test3.py").write_text("""
async def handle_request(request):
    '''Handle HTTP request asynchronously'''
    return {"status": "ok"}
""")

            # Index files
            files = list(tmppath.glob("*.py"))
            manager.add_code_files(files, base_path=tmppath)

            # Search for function definitions
            results = manager.search_code("function definition", n_results=3)

            assert len(results) > 0
            # Should find at least one of our functions
            found_code = '\n'.join(r['text'] for r in results)
            assert 'def' in found_code or 'class' in found_code

    def test_chunk_text(self):
        """Test text chunking."""
        manager = create_code_search_manager()
        if manager is None:
            pytest.skip("Code search manager not available")

        # Small text - should not be chunked
        small_text = "Short text"
        chunks = manager._chunk_text(small_text, chunk_size=100)
        assert len(chunks) == 1
        assert chunks[0] == small_text

        # Large text - should be chunked
        large_text = "\n".join([f"Line {i}" for i in range(100)])
        chunks = manager._chunk_text(large_text, chunk_size=50)
        assert len(chunks) > 1


class TestContextManagerWithEmbeddings:
    """Test ContextManager with embeddings integration."""

    def test_context_manager_with_embeddings(self):
        """Test context manager with embeddings enabled."""
        context = ContextManager(
            max_history=10,
            enable_embeddings=True
        )

        # Check if embeddings are enabled
        assert context.is_embeddings_enabled() or not EMBEDDINGS_AVAILABLE

    def test_semantic_search_history(self):
        """Test semantic search through conversation history."""
        context = ContextManager(
            max_history=10,
            enable_embeddings=True
        )

        if not context.is_embeddings_enabled():
            pytest.skip("Embeddings not enabled")

        # Add some messages
        context.add_message("user", "How do I implement authentication?")
        context.add_message("assistant", "You can use JWT tokens for authentication")
        context.add_message("user", "What about database queries?")
        context.add_message("assistant", "Use SQL with parameterized queries")

        # Search for authentication-related messages
        results = context.semantic_search_history("user authentication", n_results=2)

        if len(results) > 0:
            # Should find authentication-related messages
            found_text = '\n'.join(r['text'] for r in results)
            assert 'authentication' in found_text.lower() or 'JWT' in found_text

    def test_get_relevant_context_semantic(self):
        """Test getting relevant context using semantic search."""
        context = ContextManager(
            max_history=10,
            enable_embeddings=True
        )

        if not context.is_embeddings_enabled():
            pytest.skip("Embeddings not enabled")

        # Add messages
        context.add_message("user", "How do I handle errors in Python?")
        context.add_message("assistant", "Use try-except blocks for error handling")
        context.add_message("user", "What about logging?")
        context.add_message("assistant", "Use the logging module")

        # Get relevant context
        relevant = context.get_relevant_context_semantic(
            query="error handling in code",
            max_results=2
        )

        assert len(relevant) > 0
        # Should mention error handling
        assert 'error' in relevant.lower() or 'try' in relevant.lower()

    def test_embeddings_stats(self):
        """Test getting embeddings statistics."""
        context = ContextManager(
            max_history=10,
            enable_embeddings=True
        )

        stats = context.get_embeddings_stats()

        assert 'enabled' in stats
        assert 'indexed_messages' in stats

        if context.is_embeddings_enabled():
            assert stats['enabled'] is True
            assert isinstance(stats['indexed_messages'], int)


class TestSemanticTools:
    """Test semantic search tools."""

    def test_import_semantic_tools(self):
        """Test importing semantic tools."""
        try:
            from tools.semantic_tools import semantic_search_code, index_codebase
            assert callable(semantic_search_code)
            assert callable(index_codebase)
        except ImportError:
            pytest.skip("Semantic tools not available")

    def test_semantic_tools_in_available_tools(self):
        """Test that semantic tools are in AVAILABLE_TOOLS."""
        try:
            from tools.function_tools import AVAILABLE_TOOLS, HAS_SEMANTIC_TOOLS
            if HAS_SEMANTIC_TOOLS:
                assert 'semantic_search' in AVAILABLE_TOOLS or 'semantic_search_code' in TOOL_ALIASES
        except ImportError:
            pytest.skip("Function tools not available")


def test_embeddings_graceful_degradation():
    """Test that system works when embeddings are not available."""
    # This test should pass even if embeddings dependencies are not installed

    # Context manager should work without embeddings
    context = ContextManager(max_history=10, enable_embeddings=False)
    assert not context.is_embeddings_enabled()

    # Should still be able to use regular context
    context.add_message("user", "Test message")
    conv_context = context.get_conversation_context()
    assert "Test message" in conv_context

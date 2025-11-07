"""
Embeddings and semantic search module for Grid agents.

This module provides:
- Text embedding generation using sentence-transformers
- Vector storage and retrieval using ChromaDB
- Semantic search capabilities for code and conversations
- Caching for improved performance
"""

import os
import logging
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
import hashlib
import json

try:
    from sentence_transformers import SentenceTransformer
    SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:
    SENTENCE_TRANSFORMERS_AVAILABLE = False

try:
    import chromadb
    from chromadb.config import Settings
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False

from utils.exceptions import ConfigError

logger = logging.getLogger("core.embeddings")


class EmbeddingsManager:
    """
    Manages text embeddings and semantic search using sentence-transformers and ChromaDB.

    Features:
    - Lazy loading of models to avoid overhead
    - In-memory or persistent vector storage
    - Efficient caching
    - Semantic similarity search
    """

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        persist_directory: Optional[str] = None,
        collection_name: str = "grid_embeddings"
    ):
        """
        Initialize embeddings manager.

        Args:
            model_name: Name of sentence-transformers model (default: all-MiniLM-L6-v2)
            persist_directory: Directory for persistent storage (None for in-memory)
            collection_name: Name of ChromaDB collection
        """
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            raise ConfigError(
                "sentence-transformers not installed. "
                "Install with: pip install sentence-transformers"
            )

        if not CHROMADB_AVAILABLE:
            raise ConfigError(
                "chromadb not installed. "
                "Install with: pip install chromadb"
            )

        self.model_name = model_name
        self.persist_directory = persist_directory
        self.collection_name = collection_name

        # Lazy initialization
        self._model: Optional[SentenceTransformer] = None
        self._client: Optional[chromadb.Client] = None
        self._collection: Optional[Any] = None

        # Cache for embeddings
        self._embedding_cache: Dict[str, List[float]] = {}

        logger.info(f"EmbeddingsManager initialized with model: {model_name}")

    @property
    def model(self) -> SentenceTransformer:
        """Lazy load sentence transformer model."""
        if self._model is None:
            logger.info(f"Loading sentence-transformers model: {self.model_name}")
            self._model = SentenceTransformer(self.model_name)
            logger.info("Model loaded successfully")
        return self._model

    @property
    def client(self) -> chromadb.Client:
        """Lazy load ChromaDB client."""
        if self._client is None:
            if self.persist_directory:
                # Persistent storage
                logger.info(f"Initializing persistent ChromaDB at: {self.persist_directory}")
                self._client = chromadb.PersistentClient(
                    path=self.persist_directory,
                    settings=Settings(anonymized_telemetry=False)
                )
            else:
                # In-memory storage
                logger.info("Initializing in-memory ChromaDB")
                self._client = chromadb.Client(
                    Settings(anonymized_telemetry=False)
                )
        return self._client

    @property
    def collection(self) -> Any:
        """Get or create ChromaDB collection."""
        if self._collection is None:
            logger.info(f"Getting or creating collection: {self.collection_name}")
            self._collection = self.client.get_or_create_collection(
                name=self.collection_name,
                metadata={"description": "Grid semantic search collection"}
            )
        return self._collection

    def _get_cache_key(self, text: str) -> str:
        """Generate cache key for text."""
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    def embed_text(self, text: str, use_cache: bool = True) -> List[float]:
        """
        Generate embedding for text.

        Args:
            text: Text to embed
            use_cache: Whether to use cache

        Returns:
            Embedding vector as list of floats
        """
        if not text or not text.strip():
            logger.warning("Empty text provided for embedding")
            return []

        # Check cache
        if use_cache:
            cache_key = self._get_cache_key(text)
            if cache_key in self._embedding_cache:
                logger.debug("Using cached embedding")
                return self._embedding_cache[cache_key]

        # Generate embedding
        try:
            embedding = self.model.encode(text, convert_to_numpy=True)
            embedding_list = embedding.tolist()

            # Cache result
            if use_cache:
                cache_key = self._get_cache_key(text)
                self._embedding_cache[cache_key] = embedding_list

            return embedding_list

        except Exception as e:
            logger.error(f"Error generating embedding: {e}")
            return []

    def embed_texts(self, texts: List[str], use_cache: bool = True) -> List[List[float]]:
        """
        Generate embeddings for multiple texts (batched for efficiency).

        Args:
            texts: List of texts to embed
            use_cache: Whether to use cache

        Returns:
            List of embedding vectors
        """
        if not texts:
            return []

        # Check cache for each text
        embeddings = []
        texts_to_encode = []
        text_indices = []

        for i, text in enumerate(texts):
            if use_cache:
                cache_key = self._get_cache_key(text)
                if cache_key in self._embedding_cache:
                    embeddings.append(self._embedding_cache[cache_key])
                    continue

            texts_to_encode.append(text)
            text_indices.append(i)

        # Encode uncached texts in batch
        if texts_to_encode:
            try:
                new_embeddings = self.model.encode(texts_to_encode, convert_to_numpy=True)

                # Cache and insert results
                for idx, text, embedding in zip(text_indices, texts_to_encode, new_embeddings):
                    embedding_list = embedding.tolist()

                    if use_cache:
                        cache_key = self._get_cache_key(text)
                        self._embedding_cache[cache_key] = embedding_list

                    # Insert at correct position
                    while len(embeddings) <= idx:
                        embeddings.append([])
                    embeddings[idx] = embedding_list

            except Exception as e:
                logger.error(f"Error generating batch embeddings: {e}")
                # Fill with empty embeddings
                for idx in text_indices:
                    while len(embeddings) <= idx:
                        embeddings.append([])
                    if len(embeddings[idx]) == 0:
                        embeddings[idx] = []

        return embeddings

    def add_texts(
        self,
        texts: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
        ids: Optional[List[str]] = None
    ) -> None:
        """
        Add texts to vector store.

        Args:
            texts: Texts to add
            metadatas: Optional metadata for each text
            ids: Optional IDs for each text (auto-generated if not provided)
        """
        if not texts:
            logger.warning("No texts provided to add")
            return

        # Generate embeddings
        embeddings = self.embed_texts(texts)

        # Generate IDs if not provided
        if ids is None:
            ids = [f"doc_{i}_{self._get_cache_key(text)[:8]}" for i, text in enumerate(texts)]

        # Prepare metadatas
        if metadatas is None:
            metadatas = [{"text": text} for text in texts]
        else:
            # Ensure text is in metadata
            for i, meta in enumerate(metadatas):
                if "text" not in meta:
                    meta["text"] = texts[i]

        # Add to collection
        try:
            self.collection.add(
                embeddings=embeddings,
                documents=texts,
                metadatas=metadatas,
                ids=ids
            )
            logger.info(f"Added {len(texts)} texts to collection")
        except Exception as e:
            logger.error(f"Error adding texts to collection: {e}")

    def search(
        self,
        query: str,
        n_results: int = 5,
        where: Optional[Dict[str, Any]] = None,
        where_document: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """
        Semantic search for similar texts.

        Args:
            query: Search query
            n_results: Number of results to return
            where: Metadata filter (e.g., {"source": "code"})
            where_document: Document content filter

        Returns:
            List of search results with text, metadata, and distance
        """
        if not query or not query.strip():
            logger.warning("Empty query provided")
            return []

        # Generate query embedding
        query_embedding = self.embed_text(query)
        if not query_embedding:
            return []

        # Search collection
        try:
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=n_results,
                where=where,
                where_document=where_document
            )

            # Format results
            formatted_results = []
            if results and results.get('documents'):
                documents = results['documents'][0] if results['documents'] else []
                metadatas = results.get('metadatas', [[]])[0]
                distances = results.get('distances', [[]])[0]
                ids = results.get('ids', [[]])[0]

                for i, doc in enumerate(documents):
                    formatted_results.append({
                        'id': ids[i] if i < len(ids) else f"result_{i}",
                        'text': doc,
                        'metadata': metadatas[i] if i < len(metadatas) else {},
                        'distance': distances[i] if i < len(distances) else 1.0,
                        'similarity': 1.0 - (distances[i] if i < len(distances) else 1.0)
                    })

            logger.info(f"Found {len(formatted_results)} results for query")
            return formatted_results

        except Exception as e:
            logger.error(f"Error searching collection: {e}")
            return []

    def clear_collection(self) -> None:
        """Clear all data from collection."""
        try:
            self.client.delete_collection(self.collection_name)
            self._collection = None
            logger.info(f"Cleared collection: {self.collection_name}")
        except Exception as e:
            logger.error(f"Error clearing collection: {e}")

    def get_collection_count(self) -> int:
        """Get number of items in collection."""
        try:
            return self.collection.count()
        except Exception as e:
            logger.error(f"Error getting collection count: {e}")
            return 0

    def clear_cache(self) -> None:
        """Clear embedding cache."""
        self._embedding_cache.clear()
        logger.info("Cleared embedding cache")


class CodeSearchManager(EmbeddingsManager):
    """
    Specialized embeddings manager for code search.

    Adds code-specific features:
    - Code preprocessing
    - Language-aware chunking
    - File metadata tracking
    """

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        persist_directory: Optional[str] = None
    ):
        super().__init__(
            model_name=model_name,
            persist_directory=persist_directory,
            collection_name="grid_code_search"
        )

    def add_code_files(
        self,
        file_paths: List[Path],
        base_path: Optional[Path] = None,
        chunk_size: int = 1000
    ) -> None:
        """
        Add code files to search index.

        Args:
            file_paths: List of file paths to index
            base_path: Base path for relative paths
            chunk_size: Maximum characters per chunk
        """
        texts = []
        metadatas = []
        ids = []

        for file_path in file_paths:
            try:
                # Read file
                content = file_path.read_text(encoding='utf-8', errors='ignore')

                # Get relative path
                if base_path:
                    try:
                        rel_path = file_path.relative_to(base_path)
                    except ValueError:
                        rel_path = file_path
                else:
                    rel_path = file_path

                # Chunk content if needed
                chunks = self._chunk_text(content, chunk_size)

                for i, chunk in enumerate(chunks):
                    texts.append(chunk)
                    metadatas.append({
                        'file_path': str(rel_path),
                        'file_name': file_path.name,
                        'extension': file_path.suffix,
                        'chunk_index': i,
                        'total_chunks': len(chunks),
                        'source': 'code'
                    })
                    ids.append(f"file_{self._get_cache_key(str(file_path))}_{i}")

            except Exception as e:
                logger.error(f"Error processing file {file_path}: {e}")

        if texts:
            self.add_texts(texts, metadatas, ids)
            logger.info(f"Indexed {len(texts)} chunks from {len(file_paths)} files")

    def _chunk_text(self, text: str, chunk_size: int) -> List[str]:
        """
        Chunk text into smaller pieces.

        Args:
            text: Text to chunk
            chunk_size: Maximum chunk size

        Returns:
            List of text chunks
        """
        if len(text) <= chunk_size:
            return [text]

        chunks = []
        lines = text.split('\n')
        current_chunk = []
        current_size = 0

        for line in lines:
            line_size = len(line) + 1  # +1 for newline

            if current_size + line_size > chunk_size and current_chunk:
                # Save current chunk
                chunks.append('\n'.join(current_chunk))
                current_chunk = [line]
                current_size = line_size
            else:
                current_chunk.append(line)
                current_size += line_size

        # Add last chunk
        if current_chunk:
            chunks.append('\n'.join(current_chunk))

        return chunks

    def search_code(
        self,
        query: str,
        n_results: int = 5,
        file_extension: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Search for code matching query.

        Args:
            query: Search query
            n_results: Number of results
            file_extension: Filter by file extension (e.g., ".py")

        Returns:
            List of matching code chunks
        """
        where = {"source": "code"}
        if file_extension:
            where["extension"] = file_extension

        return self.search(query, n_results=n_results, where=where)


def create_embeddings_manager(
    enable_persistence: bool = False,
    persist_directory: Optional[str] = None
) -> Optional[EmbeddingsManager]:
    """
    Factory function to create embeddings manager.

    Args:
        enable_persistence: Whether to enable persistent storage
        persist_directory: Directory for persistence

    Returns:
        EmbeddingsManager instance or None if dependencies not available
    """
    if not SENTENCE_TRANSFORMERS_AVAILABLE or not CHROMADB_AVAILABLE:
        logger.warning(
            "Embeddings features disabled. Install dependencies: "
            "pip install sentence-transformers chromadb"
        )
        return None

    try:
        if enable_persistence and persist_directory:
            return EmbeddingsManager(persist_directory=persist_directory)
        else:
            return EmbeddingsManager()
    except Exception as e:
        logger.error(f"Failed to create embeddings manager: {e}")
        return None


def create_code_search_manager(
    enable_persistence: bool = False,
    persist_directory: Optional[str] = None
) -> Optional[CodeSearchManager]:
    """
    Factory function to create code search manager.

    Args:
        enable_persistence: Whether to enable persistent storage
        persist_directory: Directory for persistence

    Returns:
        CodeSearchManager instance or None if dependencies not available
    """
    if not SENTENCE_TRANSFORMERS_AVAILABLE or not CHROMADB_AVAILABLE:
        logger.warning(
            "Code search features disabled. Install dependencies: "
            "pip install sentence-transformers chromadb"
        )
        return None

    try:
        if enable_persistence and persist_directory:
            return CodeSearchManager(persist_directory=persist_directory)
        else:
            return CodeSearchManager()
    except Exception as e:
        logger.error(f"Failed to create code search manager: {e}")
        return None

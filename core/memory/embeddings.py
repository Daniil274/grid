"""
Embeddings and semantic search module for Grid agents.

Uses OpenRouter (OpenAI-compatible API) for embedding generation and
ChromaDB for vector storage and retrieval.

Supports multimodal input (text + image_url) via the content-array format:
    input = [{"content": [{"type": "text", "text": "..."}, {"type": "image_url", ...}]}]

Configuration is read from Config (config.yaml):
    embeddings:
      model: embed-default        # key from models: section
      request_timeout: 30
      persist: true

    models:
      embed-default:
        name: nvidia/llama-nemotron-embed-vl-1b-v2:free
        provider: openrouter      # api_key resolved from providers.openrouter.api_key_env
"""

import json
import logging
import hashlib
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union
from pathlib import Path

if TYPE_CHECKING:
    from core.config.config import Config

try:
    from openai import OpenAI as _SyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

try:
    import chromadb
    from chromadb.config import Settings
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False

logger = logging.getLogger("core.embeddings")

# Type alias: plain string OR multimodal content block list
EmbedInput = Union[str, List[Dict[str, Any]]]


class EmbeddingsManager:
    """
    Manages text (and multimodal) embeddings via OpenRouter + ChromaDB vector store.

    Features:
    - Lazy initialisation of API client and ChromaDB
    - In-memory MD5-keyed embedding cache
    - Persistent or in-memory ChromaDB storage
    - Batch embedding for efficiency
    - Multimodal input support (text + image_url content blocks)
    """

    def __init__(
        self,
        model_name: str,
        api_key: str,
        base_url: str,
        persist_directory: Optional[str] = None,
        collection_name: str = "grid_embeddings",
        request_timeout: float = 30.0,
    ):
        """
        Args:
            model_name: Exact model name sent to the API (e.g. "nvidia/llama-nemotron-embed-vl-1b-v2:free")
            api_key: Provider API key
            base_url: Provider base URL
            persist_directory: Path for persistent ChromaDB storage (None = in-memory)
            collection_name: ChromaDB collection name
            request_timeout: Timeout in seconds for each API call
        """
        if not OPENAI_AVAILABLE:
            raise RuntimeError("openai package not installed. Run: pip install openai")
        if not CHROMADB_AVAILABLE:
            raise RuntimeError("chromadb not installed. Run: pip install chromadb")
        if not api_key:
            raise RuntimeError("EmbeddingsManager: api_key is required")
        if not model_name:
            raise RuntimeError("EmbeddingsManager: model_name is required")

        self.model_name = model_name
        self.base_url = base_url
        self.api_key = api_key
        self.request_timeout = request_timeout
        self.persist_directory = persist_directory
        self.collection_name = collection_name

        # Lazy-initialised handles
        self._openai_client: Optional[_SyncOpenAI] = None
        self._chroma_client = None
        self._collection = None

        # MD5 → embedding vector cache
        self._cache: Dict[str, List[float]] = {}

        logger.info(
            "EmbeddingsManager initialised: model=%s base_url=%s persist=%s",
            model_name, base_url, persist_directory or "in-memory",
        )

    # ------------------------------------------------------------------
    # Lazy properties
    # ------------------------------------------------------------------

    @property
    def client(self) -> "_SyncOpenAI":
        """Lazy-load synchronous OpenAI client."""
        if self._openai_client is None:
            self._openai_client = _SyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.request_timeout,
            )
            logger.debug(
                "OpenAI sync client created (base_url=%s, timeout=%.0fs)",
                self.base_url, self.request_timeout,
            )
        return self._openai_client

    @property
    def chroma(self):
        """Lazy-load ChromaDB client."""
        if self._chroma_client is None:
            if self.persist_directory:
                logger.info("ChromaDB persistent: %s", self.persist_directory)
                self._chroma_client = chromadb.PersistentClient(
                    path=self.persist_directory,
                    settings=Settings(anonymized_telemetry=False),
                )
            else:
                logger.debug("ChromaDB in-memory")
                self._chroma_client = chromadb.Client(
                    Settings(anonymized_telemetry=False)
                )
        return self._chroma_client

    @property
    def collection(self):
        """Get or create ChromaDB collection."""
        if self._collection is None:
            self._collection = self.chroma.get_or_create_collection(
                name=self.collection_name,
                metadata={"description": "Grid semantic search collection"},
            )
            logger.debug("ChromaDB collection '%s' ready", self.collection_name)
        return self._collection

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_key(self, input_item: EmbedInput) -> str:
        """Stable cache key for any input type."""
        raw = input_item if isinstance(input_item, str) else json.dumps(input_item, sort_keys=True)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Input normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _to_api_input(item: EmbedInput) -> Any:
        """
        Convert an input item to the format expected by OpenRouter.
        - Plain string  → passed as-is
        - Content list  → {"content": [...]} multimodal format
        """
        if isinstance(item, str):
            return item
        return {"content": item}

    # ------------------------------------------------------------------
    # Embedding generation
    # ------------------------------------------------------------------

    def embed_text(self, text: str) -> List[float]:
        """Generate embedding for a single plain-text string. Returns [] on error."""
        return self.embed_input(text)

    def embed_input(self, input_item: EmbedInput) -> List[float]:
        """Generate embedding for text or multimodal content list. Returns [] on error."""
        if not input_item:
            logger.warning("embed_input: empty input")
            return []

        key = self._cache_key(input_item)
        if key in self._cache:
            logger.debug("embed_input: cache hit")
            return self._cache[key]

        try:
            resp = self.client.embeddings.create(
                input=[self._to_api_input(input_item)],
                model=self.model_name,
                encoding_format="float",
            )
            vec: List[float] = resp.data[0].embedding
            self._cache[key] = vec
            logger.debug("embed_input: → %d-dim vector (model=%s)", len(vec), self.model_name)
            return vec
        except Exception as exc:
            logger.error("embed_input failed: %s", exc)
            return []

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Batch-generate embeddings for plain-text strings."""
        return self.embed_inputs(texts)

    def embed_inputs(self, inputs: List[EmbedInput]) -> List[List[float]]:
        """Batch-generate embeddings; cached entries are skipped."""
        if not inputs:
            return []

        result: List[Optional[List[float]]] = [None] * len(inputs)
        to_encode: List[Any] = []
        to_encode_idx: List[int] = []

        for i, item in enumerate(inputs):
            key = self._cache_key(item)
            if key in self._cache:
                result[i] = self._cache[key]
            else:
                to_encode.append(self._to_api_input(item))
                to_encode_idx.append(i)

        if to_encode:
            try:
                resp = self.client.embeddings.create(
                    input=to_encode,
                    model=self.model_name,
                    encoding_format="float",
                )
                for j, emb_obj in enumerate(resp.data):
                    idx = to_encode_idx[j]
                    vec = emb_obj.embedding
                    result[idx] = vec
                    self._cache[self._cache_key(inputs[idx])] = vec
                logger.debug(
                    "embed_inputs: %d API, %d cache",
                    len(to_encode), len(inputs) - len(to_encode),
                )
            except Exception as exc:
                logger.error("embed_inputs batch failed: %s", exc)
                for idx in to_encode_idx:
                    if result[idx] is None:
                        result[idx] = []

        return [r if r is not None else [] for r in result]

    # ------------------------------------------------------------------
    # ChromaDB operations
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Text chunking utilities (language-agnostic)
    # ------------------------------------------------------------------

    @staticmethod
    def _split_text(
        text: str,
        chunk_size: int = 800,
        overlap: int = 150,
    ) -> List[str]:
        """Sliding-window chunking with overlap. Language-agnostic fallback."""
        lines = text.splitlines()
        if not lines:
            return []
        chunks: List[str] = []
        start = 0
        while start < len(lines):
            end, size = start, 0
            while end < len(lines) and size < chunk_size:
                size += len(lines[end]) + 1
                end += 1
            chunks.append("\n".join(lines[start:end]))
            if end >= len(lines):
                break
            # Step back by ≈overlap chars
            back, back_size = end, 0
            while back > start + 1 and back_size < overlap:
                back -= 1
                back_size += len(lines[back]) + 1
            start = back
        return chunks

    @staticmethod
    def _split_markdown(
        text: str,
        chunk_size: int = 800,
        overlap: int = 150,
    ) -> List[str]:
        """
        Markdown-aware chunker: split at headers → paragraphs → overlap.

        Strategy:
          1. If the text contains markdown headers, split into sections at each header.
          2. Sections that fit in chunk_size are kept whole.
          3. Oversized sections fall back to paragraph splitting, then overlap splitting.
        """
        import re

        header_re = re.compile(r'^#{1,4}\s+.+$', re.MULTILINE)
        boundaries = [m.start() for m in header_re.finditer(text)]

        if boundaries:
            boundaries.append(len(text))
            sections: List[str] = []
            for i in range(len(boundaries) - 1):
                section = text[boundaries[i]:boundaries[i + 1]].strip()
                if section:
                    sections.append(section)

            result: List[str] = []
            for section in sections:
                if len(section) <= chunk_size:
                    result.append(section)
                else:
                    result.extend(EmbeddingsManager._split_paragraphs(section, chunk_size, overlap))
            return result or [text]

        return EmbeddingsManager._split_paragraphs(text, chunk_size, overlap)

    @staticmethod
    def _split_paragraphs(
        text: str,
        chunk_size: int,
        overlap: int,
    ) -> List[str]:
        """Split at blank lines, merge small paragraphs, overflow → overlap split."""
        import re
        paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]

        chunks: List[str] = []
        bucket: List[str] = []
        bucket_len = 0

        def _flush() -> None:
            if bucket:
                chunks.append('\n\n'.join(bucket))

        for para in paragraphs:
            if len(para) > chunk_size:
                _flush()
                bucket, bucket_len = [], 0
                chunks.extend(EmbeddingsManager._split_text(para, chunk_size, overlap))
                continue
            if bucket_len + len(para) + 2 > chunk_size and bucket:
                _flush()
                # carry last paragraph for overlap context
                last = bucket[-1]
                bucket = [last] if len(last) <= overlap else []
                bucket_len = len(bucket[0]) + 2 if bucket else 0
            bucket.append(para)
            bucket_len += len(para) + 2

        _flush()
        return chunks or [text]

    # ------------------------------------------------------------------
    # High-level document indexing with chunking
    # ------------------------------------------------------------------

    def add_documents(
        self,
        texts: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
        chunk_size: int = 800,
        overlap: int = 150,
        source: str = "text",
    ) -> int:
        """
        Chunk and index plain-text documents.

        Each text is split with _split_markdown; every chunk inherits the
        caller-supplied metadata plus chunk_index / total_chunks / source.

        Returns the total number of chunks indexed.
        """
        all_texts: List[str] = []
        all_metas: List[Dict[str, Any]] = []
        all_ids:   List[str] = []

        for i, text in enumerate(texts):
            base = dict(metadatas[i]) if metadatas else {}
            base["source"] = source
            chunks = self._split_markdown(text, chunk_size, overlap)
            for j, chunk in enumerate(chunks):
                all_texts.append(chunk)
                all_metas.append({**base, "chunk_index": j, "total_chunks": len(chunks)})
                all_ids.append(f"doc_{self._cache_key(str(i) + text[:32])}_{j}")

        if all_texts:
            self.add_texts(all_texts, all_metas, all_ids)
            logger.info("add_documents: %d chunks from %d texts (source=%s)", len(all_texts), len(texts), source)
        return len(all_texts)

    def add_texts(
        self,
        texts: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
        ids: Optional[List[str]] = None,
    ) -> None:
        """Add plain-text documents to the collection."""
        self.add_inputs(texts, metadatas=metadatas, ids=ids)

    def add_inputs(
        self,
        inputs: List[EmbedInput],
        metadatas: Optional[List[Dict[str, Any]]] = None,
        ids: Optional[List[str]] = None,
    ) -> None:
        """Add text or multimodal inputs to the ChromaDB collection."""
        if not inputs:
            logger.warning("add_inputs: empty list")
            return

        embeddings = self.embed_inputs(inputs)
        documents = [
            item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            for item in inputs
        ]

        if ids is None:
            ids = [f"doc_{i}_{self._cache_key(inp)[:8]}" for i, inp in enumerate(inputs)]
        if metadatas is None:
            metadatas = [{} for _ in inputs]

        try:
            self.collection.add(
                embeddings=embeddings,
                documents=documents,
                metadatas=list(metadatas),
                ids=ids,
            )
            logger.info("add_inputs: %d items → collection '%s'", len(inputs), self.collection_name)
        except Exception as exc:
            logger.error("add_inputs: ChromaDB insert failed: %s", exc)

    def upsert_text(
        self,
        text: str,
        doc_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Insert or update a single text document by ID."""
        vec = self.embed_text(text)
        if not vec:
            return
        try:
            self.collection.upsert(
                ids=[doc_id],
                embeddings=[vec],
                documents=[text],
                metadatas=[metadata or {}],
            )
            logger.debug("upsert_text: id=%s", doc_id)
        except Exception as exc:
            logger.error("upsert_text failed (id=%s): %s", doc_id, exc)

    def search(
        self,
        query: str,
        n_results: int = 5,
        where: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Semantic search. Returns list of {id, text, metadata, distance, similarity}."""
        if not query.strip():
            logger.warning("search: empty query")
            return []

        qvec = self.embed_text(query)
        if not qvec:
            logger.warning("search: failed to embed query")
            return []

        try:
            raw = self.collection.query(
                query_embeddings=[qvec],
                n_results=n_results,
                where=where,
            )
        except Exception as exc:
            logger.error("search: ChromaDB query failed: %s", exc)
            return []

        docs  = raw.get("documents", [[]])[0]
        metas = raw.get("metadatas",  [[]])[0]
        dists = raw.get("distances",  [[]])[0]
        ids   = raw.get("ids",        [[]])[0]

        results = []
        for i, doc in enumerate(docs):
            dist = dists[i] if i < len(dists) else 1.0
            results.append({
                "id":         ids[i] if i < len(ids) else f"r{i}",
                "text":       doc,
                "metadata":   metas[i] if i < len(metas) else {},
                "distance":   dist,
                "similarity": max(0.0, 1.0 - dist),
            })

        logger.info("search: '%s...' → %d results", query[:50], len(results))
        return results

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    def clear_collection(self) -> None:
        """Delete and reset the ChromaDB collection."""
        try:
            self.chroma.delete_collection(self.collection_name)
            self._collection = None
            logger.info("clear_collection: '%s' deleted", self.collection_name)
        except Exception as exc:
            logger.error("clear_collection failed: %s", exc)

    def get_collection_count(self) -> int:
        """Return number of items in the collection."""
        try:
            return self.collection.count()
        except Exception as exc:
            logger.error("get_collection_count failed: %s", exc)
            return 0

    def clear_cache(self) -> None:
        """Clear the in-memory embedding cache."""
        self._cache.clear()
        logger.debug("Embedding cache cleared")


# ======================================================================
# CodeSearchManager — specialised for codebase indexing
# ======================================================================

class CodeSearchManager(EmbeddingsManager):
    """EmbeddingsManager specialised for codebase search."""

    def __init__(
        self,
        model_name: str,
        api_key: str,
        base_url: str,
        persist_directory: Optional[str] = None,
        request_timeout: float = 30.0,
    ):
        super().__init__(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            persist_directory=persist_directory,
            collection_name="grid_code_search",
            request_timeout=request_timeout,
        )

    def add_code_files(
        self,
        file_paths: List[Path],
        base_path: Optional[Path] = None,
        chunk_size: int = 1500,
    ) -> None:
        """Index code files into the collection."""
        texts: List[str] = []
        metadatas: List[Dict[str, Any]] = []
        ids: List[str] = []

        for fp in file_paths:
            try:
                content = fp.read_text(encoding="utf-8", errors="ignore")
                rel = fp.relative_to(base_path) if base_path else fp
                rel_str = str(rel)
                chunks = self._chunk_code(content, rel_str, chunk_size)
                for i, chunk in enumerate(chunks):
                    texts.append(chunk["text"])
                    metadatas.append({
                        "file_path":     rel_str,
                        "file_name":     fp.name,
                        "extension":     fp.suffix,
                        "chunk_index":   i,
                        "total_chunks":  len(chunks),
                        "chunk_name":    chunk.get("name", ""),
                        "chunk_kind":    chunk.get("kind", ""),
                        "line_start":    chunk.get("line_start", 0),
                        "line_end":      chunk.get("line_end", 0),
                        "source":        "code",
                    })
                    ids.append(f"file_{self._cache_key(str(fp))}_{i}")
            except Exception as exc:
                logger.error("add_code_files: error reading %s: %s", fp, exc)

        if texts:
            self.add_texts(texts, metadatas, ids)
            logger.info("add_code_files: %d chunks from %d files", len(texts), len(file_paths))

    @staticmethod
    def _chunk_code(content: str, file_path: str, chunk_size: int = 1500) -> List[Dict[str, Any]]:
        """Dispatch to language-specific chunker."""
        suffix = Path(file_path).suffix.lower()
        if suffix == ".py":
            return CodeSearchManager._chunk_python(content, file_path, chunk_size)
        if suffix in (".js", ".ts", ".jsx", ".tsx"):
            return CodeSearchManager._chunk_js(content, file_path, chunk_size)
        return CodeSearchManager._chunk_overlap(content, chunk_size)

    @staticmethod
    def _chunk_python(content: str, file_path: str, chunk_size: int) -> List[Dict[str, Any]]:
        """AST-based chunking: each top-level function/class is one chunk."""
        import ast as _ast
        try:
            tree = _ast.parse(content)
        except SyntaxError:
            return CodeSearchManager._chunk_overlap(content, chunk_size)

        lines = content.splitlines()
        chunks: List[Dict[str, Any]] = []
        covered: set = set()

        for node in tree.body:
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                continue
            start = node.lineno - 1
            end = node.end_lineno  # type: ignore[attr-defined]
            covered.update(range(start, end))
            body = "\n".join(lines[start:end])
            kind = "class" if isinstance(node, _ast.ClassDef) else "function"
            prefix = f"# file: {file_path}\n# {kind}: {node.name}\n"
            full = prefix + body

            # Large node → secondary overlap split, keep prefix on every sub-chunk
            if len(full) > chunk_size * 2:
                sub = CodeSearchManager._chunk_overlap(body, chunk_size, overlap=200)
                for j, s in enumerate(sub):
                    s["text"] = prefix + s["text"]
                    s["name"] = node.name
                    s["kind"] = kind
                chunks.extend(sub)
            else:
                chunks.append({
                    "text":       full,
                    "name":       node.name,
                    "kind":       kind,
                    "line_start": start + 1,
                    "line_end":   end,
                })

        # Module-level code outside functions/classes
        module_lines = [l for i, l in enumerate(lines) if i not in covered]
        if module_lines:
            body = "\n".join(module_lines).strip()
            if body:
                chunks.append({
                    "text":       f"# file: {file_path}\n# module-level\n{body}",
                    "name":       "__module__",
                    "kind":       "module",
                    "line_start": 1,
                    "line_end":   len(lines),
                })

        return chunks or CodeSearchManager._chunk_overlap(content, chunk_size)

    _JS_BOUNDARY = None  # compiled lazily

    @staticmethod
    def _chunk_js(content: str, file_path: str, chunk_size: int) -> List[Dict[str, Any]]:
        """Regex-based chunking for JS/TS: split at function/class boundaries."""
        import re
        if CodeSearchManager._JS_BOUNDARY is None:
            CodeSearchManager._JS_BOUNDARY = re.compile(
                r'^(?:export\s+)?(?:default\s+)?'
                r'(?:async\s+)?(?:function\s+\w+|class\s+\w+|const\s+\w+\s*=\s*(?:async\s*)?\()',
                re.MULTILINE,
            )

        lines = content.splitlines()
        boundaries = [
            m.start() for m in CodeSearchManager._JS_BOUNDARY.finditer(content)
        ]
        if not boundaries:
            return CodeSearchManager._chunk_overlap(content, chunk_size)

        # Convert byte offsets → line numbers
        byte_to_line: List[int] = []
        pos = 0
        for i, line in enumerate(lines):
            for _ in range(len(line) + 1):
                byte_to_line.append(i)
            pos += len(line) + 1

        split_lines = sorted({byte_to_line[min(b, len(byte_to_line) - 1)] for b in boundaries})
        split_lines.append(len(lines))  # sentinel

        chunks: List[Dict[str, Any]] = []
        prefix = f"// file: {file_path}\n"
        for i in range(len(split_lines) - 1):
            s, e = split_lines[i], split_lines[i + 1]
            body = "\n".join(lines[s:e]).strip()
            if not body:
                continue
            # Extract name from first line
            first = lines[s].strip()
            name_match = re.search(r'(?:function|class|const)\s+(\w+)', first)
            name = name_match.group(1) if name_match else ""
            chunks.append({
                "text":       prefix + body,
                "name":       name,
                "kind":       "class" if "class " in first else "function",
                "line_start": s + 1,
                "line_end":   e,
            })
        return chunks or CodeSearchManager._chunk_overlap(content, chunk_size)

    @staticmethod
    def _chunk_overlap(
        content: str,
        chunk_size: int = 1500,
        overlap: int = 300,
    ) -> List[Dict[str, Any]]:
        """
        Sliding-window chunking with overlap — language-agnostic fallback.

        Wraps EmbeddingsManager._split_text to return code-chunk dicts
        (with line_start / line_end) expected by add_code_files.
        """
        plain = EmbeddingsManager._split_text(content, chunk_size, overlap)
        # Reconstruct line offsets so metadata stays accurate
        result: List[Dict[str, Any]] = []
        line_cursor = 0
        lines = content.splitlines()
        line_set = {i: l for i, l in enumerate(lines)}

        for chunk_text in plain:
            chunk_lines = chunk_text.splitlines()
            start_line = line_cursor + 1
            end_line = line_cursor + len(chunk_lines)
            result.append({
                "text":       chunk_text,
                "name":       "",
                "kind":       "",
                "line_start": start_line,
                "line_end":   end_line,
            })
            # Advance, accounting for overlap (find where this chunk ends in source)
            line_cursor = end_line
        return result

    def search_code(
        self,
        query: str,
        n_results: int = 5,
        file_extension: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search indexed code by semantic similarity."""
        if file_extension:
            ext = file_extension if file_extension.startswith(".") else f".{file_extension}"
            # ChromaDB requires $and when combining multiple conditions
            where: Dict[str, Any] = {"$and": [{"source": {"$eq": "code"}}, {"extension": {"$eq": ext}}]}
        else:
            where = {"source": {"$eq": "code"}}
        return self.search(query, n_results=n_results, where=where)


# ======================================================================
# Factory helpers — read settings from Config
# ======================================================================

def _resolve_settings(config: "Config") -> Optional[Dict[str, Any]]:
    """
    Extract (model_name, api_key, base_url, timeout, persist) from Config.
    Returns None and logs a warning if anything is missing.
    """
    if not OPENAI_AVAILABLE:
        logger.warning("Embeddings disabled: openai not installed (pip install openai)")
        return None
    if not CHROMADB_AVAILABLE:
        logger.warning("Embeddings disabled: chromadb not installed (pip install chromadb)")
        return None

    emb_cfg = getattr(config.config, "embeddings", None)
    if emb_cfg is None:
        logger.warning("Embeddings disabled: no 'embeddings:' section in config.yaml")
        return None

    try:
        model_cfg = config.get_model(emb_cfg.model)
    except Exception:
        logger.warning("Embeddings disabled: model key '%s' not found in models:", emb_cfg.model)
        return None

    api_key = config.get_api_key(model_cfg.provider)
    if not api_key:
        logger.warning(
            "Embeddings disabled: no API key for provider '%s' "
            "(check env var in providers config)",
            model_cfg.provider,
        )
        return None

    try:
        provider_cfg = config.get_provider(model_cfg.provider)
        base_url = provider_cfg.base_url
    except Exception:
        logger.warning("Embeddings disabled: provider '%s' not found", model_cfg.provider)
        return None

    return {
        "model_name":      model_cfg.name,
        "api_key":         api_key,
        "base_url":        base_url,
        "request_timeout": emb_cfg.request_timeout,
        "persist":         emb_cfg.persist,
    }


def create_embeddings_manager(
    config: "Config",
    persist_directory: Optional[str] = None,
    collection_name: str = "grid_embeddings",
) -> Optional[EmbeddingsManager]:
    """
    Create EmbeddingsManager from Config. Returns None if config/deps are missing.

    Args:
        config: Config instance (reads embeddings.model, provider api_key/base_url)
        persist_directory: Override persist path (None = use config + caller-provided path)
        collection_name: ChromaDB collection name
    """
    s = _resolve_settings(config)
    if s is None:
        return None
    try:
        return EmbeddingsManager(
            model_name=s["model_name"],
            api_key=s["api_key"],
            base_url=s["base_url"],
            request_timeout=s["request_timeout"],
            persist_directory=persist_directory if s["persist"] else None,
            collection_name=collection_name,
        )
    except Exception as exc:
        logger.error("create_embeddings_manager failed: %s", exc)
        return None


def create_code_search_manager(
    config: "Config",
    persist_directory: Optional[str] = None,
) -> Optional[CodeSearchManager]:
    """
    Create CodeSearchManager from Config. Returns None if config/deps are missing.

    Args:
        config: Config instance
        persist_directory: Override persist path
    """
    s = _resolve_settings(config)
    if s is None:
        return None
    try:
        return CodeSearchManager(
            model_name=s["model_name"],
            api_key=s["api_key"],
            base_url=s["base_url"],
            request_timeout=s["request_timeout"],
            persist_directory=persist_directory if s["persist"] else None,
        )
    except Exception as exc:
        logger.error("create_code_search_manager failed: %s", exc)
        return None

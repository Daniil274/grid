"""
Semantic search tools for Grid agents.

Isolation model:
    Each unique resolved directory path gets its own CodeSearchManager instance
    backed by a dedicated ChromaDB collection named after the path hash.
    This prevents cross-directory result contamination when multiple agents
    or sessions index different codebases.
"""

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents import function_tool
from utils.logger import Logger

try:
    from core.memory.embeddings import CodeSearchManager, create_code_search_manager
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    CodeSearchManager = None  # type: ignore[assignment,misc]
    EMBEDDINGS_AVAILABLE = False

# Per-directory cache: resolved_path_str → CodeSearchManager instance
_managers: Dict[str, Any] = {}
# Module-level Config singleton — loaded once, avoids re-reading config files
_config_instance = None

_DEFAULT_EXTENSIONS = ['.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.go', '.rs', '.cpp', '.c', '.h']
_log = Logger("tool")


def set_semantic_config(config) -> None:
    """Inject the already-initialised Config instance (called by AgentFactory).
    Clears cached managers so they are re-created with the correct config."""
    global _config_instance, _managers
    _config_instance = config
    _managers = {}
    _log.info(f"semantic_tools: Config injected, working_dir='{config.get_working_directory()}'")


def _get_config():
    """Return the module-level Config singleton, creating it once if needed."""
    global _config_instance
    if _config_instance is None:
        from core.config import Config
        _config_instance = Config()
        _log.info(f"semantic_tools: Config loaded (fallback), working_dir='{_config_instance.get_working_directory()}'")
    return _config_instance


def _collection_name(resolved_path: str) -> str:
    """Stable, filesystem-safe ChromaDB collection name for a directory."""
    digest = hashlib.md5(resolved_path.encode("utf-8")).hexdigest()[:12]
    return f"code_{digest}"


def _get_manager(resolved_path: str) -> Optional[CodeSearchManager]:
    """Return (or lazily create) a per-directory CodeSearchManager."""
    if not EMBEDDINGS_AVAILABLE:
        return None
    if resolved_path not in _managers:
        try:
            config = _get_config()
            mgr = create_code_search_manager(config)
            if mgr is None:
                return None
            # Override the collection name so each directory is isolated
            mgr.collection_name = _collection_name(resolved_path)
            mgr._collection = None  # force collection re-fetch with new name
            _managers[resolved_path] = mgr
            _log.info(f"semantic_tools: new manager for '{resolved_path}' (collection={mgr.collection_name})")
        except Exception as exc:
            _log.error(f"semantic_tools: manager init failed: {exc}")
            return None
    return _managers[resolved_path]


def _resolve(directory: str) -> Optional[Path]:
    """Resolve directory path, using agent working directory as base for relative paths."""
    p = Path(directory)
    if not p.is_absolute():
        try:
            working_dir = _get_config().get_working_directory()
            p = Path(working_dir) / p
        except Exception:
            pass  # fall back to process CWD
    p = p.resolve()
    return p if p.exists() else None


def _parse_extensions(raw: str) -> List[str]:
    if not raw.strip():
        return list(_DEFAULT_EXTENSIONS)
    parts = [e.strip() for e in raw.split(",") if e.strip()]
    return [e if e.startswith(".") else f".{e}" for e in parts]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@function_tool
def semantic_search_code(
    query: str,
    directory: str = ".",
    file_extensions: str = "",
    n_results: int = 5,
    reindex: bool = False,
) -> str:
    """
    Семантический поиск по кодовой базе конкретной директории.

    Ищет код по смыслу запроса — находит релевантные фрагменты, даже если
    точные ключевые слова не совпадают.

    Каждая директория использует изолированный индекс; результаты из других
    директорий никогда не попадают в выдачу.

    Args:
        query: Поисковый запрос на естественном языке или техническом описании
        directory: Корневая директория кодовой базы для поиска
        file_extensions: Фильтр по расширениям, через запятую (например "py,ts").
                         Пусто = все поддерживаемые языки
        n_results: Максимальное количество результатов (по умолчанию 5)
        reindex: True — переиндексировать директорию перед поиском

    Returns:
        str: Найденные фрагменты кода с оценкой релевантности и путём к файлу
    """
    _log.log_tool_call("semantic_search_code", {
        "query": query, "directory": directory,
        "file_extensions": file_extensions, "n_results": n_results,
        "reindex": reindex,
    })

    if not EMBEDDINGS_AVAILABLE:
        msg = "Семантический поиск недоступен. Установите: pip install chromadb openai"
        _log.error(f"TOOL_ERROR | semantic_search_code | {msg}")
        return f"❌ {msg}"

    base_path = _resolve(directory)
    if base_path is None:
        msg = f"Директория не найдена: {directory}"
        _log.error(f"TOOL_ERROR | semantic_search_code | {msg}")
        return f"❌ {msg}"

    resolved = str(base_path)
    manager = _get_manager(resolved)
    if manager is None:
        msg = "Не удалось инициализировать менеджер поиска (проверьте OPENROUTER_API_KEY)"
        _log.error(f"TOOL_ERROR | semantic_search_code | {msg}")
        return f"❌ {msg}"

    try:
        # Index if empty or forced
        count = manager.get_collection_count()
        if reindex or count == 0:
            extensions = _parse_extensions(file_extensions)
            files: List[Path] = []
            for ext in extensions:
                files.extend(base_path.glob(f"**/*{ext}"))

            if not files:
                msg = f"Нет файлов для индексации в {resolved} (расширения: {extensions})"
                _log.info(f"TOOL_RESULT | semantic_search_code | {msg}")
                return f"⚠️ {msg}"

            if reindex:
                manager.clear_collection()

            manager.add_code_files(files, base_path=base_path)
            _log.info(
                f"TOOL_RESULT | semantic_search_code | indexed {len(files)} files "
                f"in '{resolved}'"
            )

        # Search — extension filter is optional
        ext_filter = None
        if file_extensions.strip():
            first = _parse_extensions(file_extensions)[0]
            ext_filter = first

        results = manager.search_code(query=query, n_results=n_results, file_extension=ext_filter)

        if not results:
            msg = f"Ничего не найдено по запросу: «{query}»"
            _log.info(f"TOOL_RESULT | semantic_search_code | {msg}")
            return f"🔍 {msg}"

        lines = [
            f"🔍 Семантический поиск: «{query}»",
            f"Директория: {resolved}",
            f"Найдено {len(results)} фрагментов:",
            "",
        ]
        for i, hit in enumerate(results, 1):
            meta = hit.get("metadata", {})
            file_rel = meta.get("file_path", "unknown")
            similarity = hit.get("similarity", 0.0)
            text = hit.get("text", "")
            if len(text) > 500:
                text = text[:500] + "\n... (усечено)"
            lines += [
                f"[{i}] {file_rel}  (релевантность: {similarity:.0%})",
                "```",
                text,
                "```",
                "",
            ]

        out = "\n".join(lines)
        _log.info(f"TOOL_RESULT | semantic_search_code | found {len(results)} results")
        return out

    except Exception as exc:
        _log.error(f"TOOL_ERROR | semantic_search_code | {exc}")
        return f"❌ Ошибка при поиске: {exc}"


@function_tool
def index_codebase(
    directory: str = ".",
    file_extensions: str = "py,js,ts,jsx,tsx",
    force_reindex: bool = False,
) -> str:
    """
    Индексировать кодовую базу директории для семантического поиска.

    Создаёт изолированный векторный индекс для указанной директории.
    Индекс привязан только к этой директории — другие директории не затрагиваются.

    Args:
        directory: Корневая директория для индексации
        file_extensions: Расширения через запятую (по умолчанию "py,js,ts,jsx,tsx")
        force_reindex: True — очистить и пересоздать индекс полностью

    Returns:
        str: Статистика индексации
    """
    _log.log_tool_call("index_codebase", {
        "directory": directory,
        "file_extensions": file_extensions,
        "force_reindex": force_reindex,
    })

    if not EMBEDDINGS_AVAILABLE:
        msg = "Индексация недоступна. Установите: pip install chromadb openai"
        _log.error(f"TOOL_ERROR | index_codebase | {msg}")
        return f"❌ {msg}"

    base_path = _resolve(directory)
    if base_path is None:
        msg = f"Директория не найдена: {directory}"
        _log.error(f"TOOL_ERROR | index_codebase | {msg}")
        return f"❌ {msg}"

    resolved = str(base_path)
    manager = _get_manager(resolved)
    if manager is None:
        msg = "Не удалось инициализировать менеджер (проверьте OPENROUTER_API_KEY)"
        _log.error(f"TOOL_ERROR | index_codebase | {msg}")
        return f"❌ {msg}"

    try:
        count = manager.get_collection_count()
        if count > 0 and not force_reindex:
            msg = (
                f"Директория уже проиндексирована ({count} фрагментов). "
                f"Передайте force_reindex=True для пересоздания индекса."
            )
            _log.info(f"TOOL_RESULT | index_codebase | {msg}")
            return f"ℹ️ {msg}"

        extensions = _parse_extensions(file_extensions)
        files: List[Path] = []
        for ext in extensions:
            files.extend(base_path.glob(f"**/*{ext}"))

        if not files:
            msg = f"Нет файлов в {resolved} (расширения: {extensions})"
            _log.info(f"TOOL_RESULT | index_codebase | {msg}")
            return f"⚠️ {msg}"

        if force_reindex:
            manager.clear_collection()

        manager.add_code_files(files, base_path=base_path)
        total = manager.get_collection_count()

        out = (
            f"✅ Индексация завершена\n"
            f"Директория:  {resolved}\n"
            f"Коллекция:   {manager.collection_name}\n"
            f"Файлов:      {len(files)}\n"
            f"Фрагментов:  {total}\n"
            f"Расширения:  {', '.join(extensions)}"
        )
        _log.info(f"TOOL_RESULT | index_codebase | indexed {len(files)} files in '{resolved}'")
        return out

    except Exception as exc:
        _log.error(f"TOOL_ERROR | index_codebase | {exc}")
        return f"❌ Ошибка индексации: {exc}"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SEMANTIC_TOOLS: Dict[str, Any] = {
    "semantic_search": semantic_search_code,
    "index_codebase": index_codebase,
}


def get_semantic_tools() -> List[Any]:
    if not EMBEDDINGS_AVAILABLE:
        Logger(__name__).warning("Semantic tools unavailable — install chromadb and openai")
        return []
    return list(SEMANTIC_TOOLS.values())


def get_semantic_tools_by_names(tool_names: List[str]) -> List[Any]:
    if not EMBEDDINGS_AVAILABLE:
        Logger(__name__).warning("Semantic tools unavailable — install chromadb and openai")
        return []
    result = []
    for name in tool_names:
        if name in SEMANTIC_TOOLS:
            result.append(SEMANTIC_TOOLS[name])
        else:
            Logger(__name__).warning(f"Semantic tool '{name}' not found")
    return result

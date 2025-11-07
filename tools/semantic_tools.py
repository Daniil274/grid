"""
Semantic search tools for Grid agents.

Provides semantic search capabilities for code and conversation history.
"""

import os
from pathlib import Path
from typing import List, Any, Optional
from agents import function_tool
from utils.logger import Logger

# Import code search manager
try:
    from core.embeddings import CodeSearchManager, create_code_search_manager
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False

# Global code search manager instance (lazy initialization)
_code_search_manager: Optional[CodeSearchManager] = None


def log_tool_call(tool_name: str, data: dict) -> None:
    Logger("tool").log_tool_call(tool_name, data)


def log_tool_result(tool_name: str, result: str | Exception = "") -> None:
    Logger("tool").info(f"TOOL_RESULT | {tool_name} | {result}")


def log_tool_error(tool_name: str, error: str | Exception) -> None:
    Logger("tool").error(f"TOOL_ERROR | {tool_name} | {error}")


def get_code_search_manager() -> Optional[CodeSearchManager]:
    """Get or create global code search manager."""
    global _code_search_manager

    if not EMBEDDINGS_AVAILABLE:
        return None

    if _code_search_manager is None:
        _code_search_manager = create_code_search_manager(
            enable_persistence=False  # In-memory by default
        )

    return _code_search_manager


@function_tool
def semantic_search_code(
    query: str,
    directory: str = ".",
    file_extensions: str = "",
    n_results: int = 5,
    reindex: bool = False
) -> str:
    """
    Семантический поиск по кодовой базе.

    Находит код, который семантически похож на запрос, даже если точные слова не совпадают.
    Например, запрос "обработка ошибок" найдёт функции с try/except, error handling и т.д.

    Args:
        query: Поисковый запрос (естественный язык или техническое описание)
        directory: Директория для поиска (по умолчанию текущая)
        file_extensions: Фильтр по расширениям файлов, разделенных запятой (например: "py,js,ts")
        n_results: Количество результатов (по умолчанию 5)
        reindex: Переиндексировать файлы перед поиском (по умолчанию False)

    Returns:
        str: Результаты поиска с релевантными фрагментами кода
    """
    log_tool_call("semantic_search_code", {
        "query": query,
        "directory": directory,
        "file_extensions": file_extensions,
        "n_results": n_results,
        "reindex": reindex
    })

    # Check if embeddings available
    if not EMBEDDINGS_AVAILABLE:
        error_msg = (
            "Семантический поиск недоступен. "
            "Установите зависимости: pip install sentence-transformers chromadb"
        )
        log_tool_error("semantic_search_code", error_msg)
        return f"❌ {error_msg}"

    try:
        # Get code search manager
        manager = get_code_search_manager()
        if not manager:
            error_msg = "Не удалось инициализировать менеджер поиска"
            log_tool_error("semantic_search_code", error_msg)
            return f"❌ {error_msg}"

        # Get base path
        base_path = Path(directory)
        if not base_path.exists():
            error_msg = f"Директория {directory} не найдена"
            log_tool_error("semantic_search_code", error_msg)
            return f"❌ {error_msg}"

        # Check if we need to index files
        current_count = manager.get_collection_count()
        if reindex or current_count == 0:
            # Find files to index
            extensions = []
            if file_extensions:
                extensions = [ext.strip() for ext in file_extensions.split(',')]
                extensions = [ext if ext.startswith('.') else f'.{ext}' for ext in extensions]

            # Default code extensions if none specified
            if not extensions:
                extensions = ['.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.go', '.rs', '.cpp', '.c', '.h']

            # Find all code files
            files_to_index = []
            for ext in extensions:
                pattern = f"**/*{ext}"
                files_to_index.extend(base_path.glob(pattern))

            if not files_to_index:
                msg = f"Не найдено файлов для индексации в {directory}"
                log_tool_result("semantic_search_code", msg)
                return f"⚠️ {msg}"

            # Index files
            if reindex:
                manager.clear_collection()

            manager.add_code_files(files_to_index, base_path=base_path)
            indexed_count = len(files_to_index)
            Logger("tool").info(f"Indexed {indexed_count} files for semantic search")

        # Perform search
        extension_filter = None
        if file_extensions:
            # Use first extension for filtering
            ext_list = [ext.strip() for ext in file_extensions.split(',')]
            if ext_list:
                extension_filter = ext_list[0] if ext_list[0].startswith('.') else f'.{ext_list[0]}'

        results = manager.search_code(
            query=query,
            n_results=n_results,
            file_extension=extension_filter
        )

        if not results:
            msg = f"Не найдено результатов для запроса: {query}"
            log_tool_result("semantic_search_code", msg)
            return f"🔍 {msg}"

        # Format results
        output_lines = [
            f"🔍 Семантический поиск: '{query}'",
            f"Найдено {len(results)} релевантных фрагментов:",
            ""
        ]

        for i, result in enumerate(results, 1):
            metadata = result.get('metadata', {})
            file_path = metadata.get('file_path', 'unknown')
            chunk_index = metadata.get('chunk_index', 0)
            similarity = result.get('similarity', 0.0)
            text = result.get('text', '')

            # Truncate long code snippets
            if len(text) > 500:
                text = text[:500] + "\n... (усечено)"

            output_lines.extend([
                f"[{i}] {file_path} (релевантность: {similarity:.2%})",
                "```",
                text,
                "```",
                ""
            ])

        result_text = "\n".join(output_lines)
        log_tool_result("semantic_search_code", f"Found {len(results)} results")
        return result_text

    except Exception as e:
        log_tool_error("semantic_search_code", str(e))
        return f"❌ Ошибка при семантическом поиске: {str(e)}"


@function_tool
def index_codebase(
    directory: str = ".",
    file_extensions: str = "py,js,ts,jsx,tsx",
    force_reindex: bool = False
) -> str:
    """
    Индексировать кодовую базу для семантического поиска.

    Создаёт векторные представления (embeddings) для всех файлов кода в указанной директории.
    После индексации можно использовать semantic_search_code для быстрого поиска.

    Args:
        directory: Директория для индексации (по умолчанию текущая)
        file_extensions: Расширения файлов для индексации (по умолчанию "py,js,ts,jsx,tsx")
        force_reindex: Принудительно переиндексировать даже если уже проиндексировано

    Returns:
        str: Результат индексации
    """
    log_tool_call("index_codebase", {
        "directory": directory,
        "file_extensions": file_extensions,
        "force_reindex": force_reindex
    })

    # Check if embeddings available
    if not EMBEDDINGS_AVAILABLE:
        error_msg = (
            "Индексация недоступна. "
            "Установите зависимости: pip install sentence-transformers chromadb"
        )
        log_tool_error("index_codebase", error_msg)
        return f"❌ {error_msg}"

    try:
        # Get code search manager
        manager = get_code_search_manager()
        if not manager:
            error_msg = "Не удалось инициализировать менеджер поиска"
            log_tool_error("index_codebase", error_msg)
            return f"❌ {error_msg}"

        # Get base path
        base_path = Path(directory)
        if not base_path.exists():
            error_msg = f"Директория {directory} не найдена"
            log_tool_error("index_codebase", error_msg)
            return f"❌ {error_msg}"

        # Check if already indexed
        current_count = manager.get_collection_count()
        if current_count > 0 and not force_reindex:
            msg = f"Кодовая база уже проиндексирована ({current_count} фрагментов). Используйте force_reindex=True для переиндексации."
            log_tool_result("index_codebase", msg)
            return f"ℹ️ {msg}"

        # Parse extensions
        extensions = [ext.strip() for ext in file_extensions.split(',')]
        extensions = [ext if ext.startswith('.') else f'.{ext}' for ext in extensions]

        # Find all code files
        files_to_index = []
        for ext in extensions:
            pattern = f"**/*{ext}"
            files_to_index.extend(base_path.glob(pattern))

        if not files_to_index:
            msg = f"Не найдено файлов для индексации в {directory}"
            log_tool_result("index_codebase", msg)
            return f"⚠️ {msg}"

        # Clear if reindexing
        if force_reindex:
            manager.clear_collection()

        # Index files
        manager.add_code_files(files_to_index, base_path=base_path)
        total_chunks = manager.get_collection_count()

        result_msg = (
            f"✅ Индексация завершена!\n"
            f"Проиндексировано файлов: {len(files_to_index)}\n"
            f"Создано фрагментов: {total_chunks}\n"
            f"Расширения: {', '.join(extensions)}\n"
            f"Теперь можно использовать semantic_search_code для поиска."
        )

        log_tool_result("index_codebase", f"Indexed {len(files_to_index)} files")
        return result_msg

    except Exception as e:
        log_tool_error("index_codebase", str(e))
        return f"❌ Ошибка при индексации: {str(e)}"


# Dictionary of semantic tools
SEMANTIC_TOOLS = {
    "semantic_search": semantic_search_code,
    "index_codebase": index_codebase,
}


def get_semantic_tools() -> List[Any]:
    """Возвращает список всех инструментов семантического поиска."""
    if not EMBEDDINGS_AVAILABLE:
        Logger(__name__).warning("Semantic tools not available - missing dependencies")
        return []
    return list(SEMANTIC_TOOLS.values())


def get_semantic_tools_by_names(tool_names: List[str]) -> List[Any]:
    """Возвращает список инструментов семантического поиска по их именам."""
    if not EMBEDDINGS_AVAILABLE:
        Logger(__name__).warning("Semantic tools not available - missing dependencies")
        return []

    tools = []
    for name in tool_names:
        if name in SEMANTIC_TOOLS:
            tools.append(SEMANTIC_TOOLS[name])
        else:
            Logger(__name__).warning(f"Semantic tool '{name}' not found")
    return tools

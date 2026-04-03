"""
Search Tools — поиск файлов (glob) и текста в файлах (grep).
Все пути ограничены рабочей директорией агента.
"""

import concurrent.futures
import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import function_tool
from utils.path_utils import resolve_agent_path_auto, display_agent_path_auto


MAX_RESULTS = 100
MAX_OUTPUT_LINES = 100

# Directories always skipped during traversal
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", ".mypy_cache"}


def _resolve_dir(directory: str) -> tuple:
    """
    Returns (visible, resolved_path).
    Raises ValueError if directory escapes workspace.
    """
    visible = display_agent_path_auto(directory)
    resolved = resolve_agent_path_auto(directory)  # raises ValueError on escape
    path = Path(resolved)
    if not path.exists():
        raise FileNotFoundError(f"❌ Директория не найдена: {visible}")
    if not path.is_dir():
        raise NotADirectoryError(f"❌ Не является директорией: {visible}")
    return visible, path


# ---------------------------------------------------------------------------
# glob_tool
# ---------------------------------------------------------------------------

@function_tool
def glob_tool(
    pattern: str,
    directory: str = ".",
    max_results: int = MAX_RESULTS,
) -> str:
    """
    Поиск файлов по glob-шаблону внутри рабочей директории.

    Поддерживает:
      *.py              — Python-файлы в корне директории
      src/**/*.js       — все JS-файлы в src рекурсивно
      **/test_*.py      — все тестовые файлы рекурсивно

    Args:
        pattern:     Glob-шаблон
        directory:   Директория поиска (по умолчанию текущая)
        max_results: Максимальное количество результатов

    Returns:
        Список найденных файлов
    """
    try:
        visible, base_path = _resolve_dir(directory)
    except ValueError as exc:
        return str(exc)
    except (FileNotFoundError, NotADirectoryError) as exc:
        return str(exc)

    try:
        results = []
        is_recursive = "**" in pattern

        if is_recursive:
            # Split at the first '**/' to get optional dir prefix and file pattern.
            # e.g. "src/**/*.py"  → dir_prefix="src", file_pat="*.py"
            #      "**/*.py"      → dir_prefix="",    file_pat="*.py"
            #      "**"           → dir_prefix="",    file_pat="*"
            star_idx = pattern.index("**")
            dir_prefix = pattern[:star_idx].rstrip("/\\")
            remainder = pattern[star_idx + 2:].lstrip("/\\")
            file_pat = remainder if remainder else "*"

            walk_root = (base_path / dir_prefix) if dir_prefix else base_path
            if not walk_root.exists():
                return f"❌ Поддиректория не найдена: {dir_prefix or '.'}"

            for root, dirs, files in os.walk(walk_root):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]

                for filename in files:
                    if fnmatch.fnmatch(filename, file_pat):
                        full = Path(root) / filename
                        try:
                            results.append(str(full.relative_to(base_path)))
                        except ValueError:
                            results.append(str(full))
                        if len(results) >= max_results:
                            break

                if len(results) >= max_results:
                    break
        else:
            # Non-recursive: only items directly inside base_path
            for item in sorted(base_path.iterdir()):
                if item.is_file() and fnmatch.fnmatch(item.name, pattern):
                    results.append(item.name)
                elif item.is_dir() and fnmatch.fnmatch(item.name, pattern):
                    results.append(item.name + "/")
                if len(results) >= max_results:
                    break

        if not results:
            return f"🔍 По шаблону '{pattern}' в '{visible}' ничего не найдено"

        results.sort()
        header = f"🔍 Найдено {len(results)} результатов по шаблону '{pattern}':"
        if len(results) >= max_results:
            header += f" (показаны первые {max_results})"

        displayed = results[:MAX_OUTPUT_LINES]
        output = "\n".join(displayed)
        if len(results) > MAX_OUTPUT_LINES:
            output += f"\n\n... и ещё {len(results) - MAX_OUTPUT_LINES} результатов ..."

        return f"{header}\n\n{output}"

    except Exception as exc:
        return f"❌ Ошибка поиска: {exc}"


# ---------------------------------------------------------------------------
# grep_tool
# ---------------------------------------------------------------------------

def _safe_compile(pattern: str, flags: int) -> tuple:
    """Compile regex and return (regex, error_str)."""
    try:
        return re.compile(pattern, flags), None
    except re.error as exc:
        return None, f"❌ Некорректное регулярное выражение: {exc}"


def _search_file(filepath: Path, regex, max_per_file: int = 5, per_file_timeout: float = 10.0):
    """
    Search *filepath* for *regex* matches.
    Runs in a thread so we can apply a timeout (ReDoS protection).
    Returns list of (lineno, content) tuples.
    """
    matches = []

    def _do():
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if regex.search(line):
                        content = line.rstrip()
                        if len(content) > 200:
                            content = content[:200] + "..."
                        matches.append((lineno, content))
                        if len(matches) >= max_per_file:
                            break
        except (IOError, OSError):
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_do)
        try:
            future.result(timeout=per_file_timeout)
        except concurrent.futures.TimeoutError:
            pass  # Return whatever was collected

    return matches


def _grep_with_ripgrep(
    pattern: str,
    base_path: Path,
    file_extensions: str,
    case_sensitive: bool,
    use_regex: bool,
    max_results: int,
) -> str:
    """Use ripgrep with --json output (handles Windows paths correctly)."""
    cmd = ["rg", "--json", "--line-number"]

    if not case_sensitive:
        cmd.append("--ignore-case")
    if not use_regex:
        cmd.append("--fixed-strings")

    # Use --glob instead of --type-add so multiple extensions work correctly
    if file_extensions:
        for ext in file_extensions.split(","):
            ext = ext.strip()
            if ext:
                glob_pat = f"*{ext}" if ext.startswith(".") else f"*.{ext}"
                cmd.extend(["--glob", glob_pat])

    cmd.extend([pattern, str(base_path)])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        encoding="utf-8",
        errors="replace",
    )

    # returncode 0=matches found, 1=no match, 2=error
    if result.returncode == 2:
        raise subprocess.CalledProcessError(result.returncode, cmd)

    results = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if data.get("type") != "match":
            continue

        match_data = data.get("data", {})
        file_path_raw = match_data.get("path", {}).get("text", "")
        lineno = match_data.get("line_number", 0)
        line_text = match_data.get("lines", {}).get("text", "").rstrip()

        if len(line_text) > 200:
            line_text = line_text[:200] + "..."

        try:
            rel = str(Path(file_path_raw).relative_to(base_path))
        except ValueError:
            rel = file_path_raw

        results.append(f"{rel}:{lineno}: {line_text}")
        if len(results) >= max_results:
            break

    if not results:
        return f"🔍 По паттерну '{pattern}' ничего не найдено"

    header = f"🔍 Найдено {len(results)} совпадений по '{pattern}'"
    if len(results) >= max_results:
        header += f" (показаны первые {max_results})"
    return f"{header}\n\n" + "\n".join(results)


def _grep_with_python(
    pattern: str,
    base_path: Path,
    file_extensions: str,
    case_sensitive: bool,
    use_regex: bool,
    max_results: int,
) -> str:
    """Pure-Python fallback grep implementation with ReDoS protection."""
    flags = 0 if case_sensitive else re.IGNORECASE
    if use_regex:
        regex, err = _safe_compile(pattern, flags)
        if err:
            return err
    else:
        regex, err = _safe_compile(re.escape(pattern), flags)
        if err:
            return err

    extensions = []
    if file_extensions:
        for ext in file_extensions.split(","):
            ext = ext.strip().lower()
            if ext:
                extensions.append(ext if ext.startswith(".") else "." + ext)

    results = []

    for root, dirs, files in os.walk(base_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]

        for filename in files:
            if len(results) >= max_results:
                break
            if extensions and Path(filename).suffix.lower() not in extensions:
                continue

            filepath = Path(root) / filename
            for lineno, content in _search_file(filepath, regex):
                try:
                    rel = str(filepath.relative_to(base_path))
                except ValueError:
                    rel = str(filepath)
                results.append(f"{rel}:{lineno}: {content}")
                if len(results) >= max_results:
                    break

        if len(results) >= max_results:
            break

    if not results:
        return f"🔍 По паттерну '{pattern}' ничего не найдено"

    header = f"🔍 Найдено {len(results)} совпадений по '{pattern}'"
    if len(results) >= max_results:
        header += f" (показаны первые {max_results})"
    return f"{header}\n\n" + "\n".join(results)


@function_tool
def grep_tool(
    pattern: str,
    directory: str = ".",
    file_extensions: str = "",
    case_sensitive: bool = False,
    use_regex: bool = False,
    max_results: int = MAX_RESULTS,
) -> str:
    """
    Поиск текста в файлах внутри рабочей директории.

    Args:
        pattern:        Текст или регулярное выражение для поиска
        directory:      Директория для поиска
        file_extensions: Расширения через запятую (например: "py,js,ts")
        case_sensitive: Учитывать регистр
        use_regex:      Использовать регулярное выражение
        max_results:    Максимальное количество совпадений

    Returns:
        Найденные строки с именами файлов и номерами строк
    """
    try:
        visible, base_path = _resolve_dir(directory)
    except ValueError as exc:
        return str(exc)
    except (FileNotFoundError, NotADirectoryError) as exc:
        return str(exc)

    try:
        return _grep_with_ripgrep(
            pattern, base_path, file_extensions, case_sensitive, use_regex, max_results
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass  # ripgrep not available or error — fall through to Python

    return _grep_with_python(
        pattern, base_path, file_extensions, case_sensitive, use_regex, max_results
    )

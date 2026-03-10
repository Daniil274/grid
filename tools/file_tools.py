"""
Файловые инструменты для агентов.

Поддерживает:
- Чтение и запись файлов
- Получение информации о файлах
- Список файлов в директории
- Поиск файлов по имени и содержимому
"""

import os
import re
import time
import stat
from pathlib import Path
from typing import List, Any

from agents import function_tool
from utils.logger import Logger
from utils.path_utils import display_agent_path_auto, resolve_agent_path_auto


def _has_unix_write_permission(path: Path) -> bool:
    """Return True if the current user has write permissions for the path."""
    target = path if path.exists() else path.parent
    try:
        stat_result = target.stat()
    except FileNotFoundError:
        # Directory does not exist yet – rely on mkdir to raise an error later
        return True

    uid = getattr(os, "geteuid", lambda: None)()
    gid = getattr(os, "getegid", lambda: None)()
    mode = stat_result.st_mode

    if uid is not None and uid == stat_result.st_uid and mode & stat.S_IWUSR:
        return True
    if gid is not None and gid == stat_result.st_gid and mode & stat.S_IWGRP:
        return True
    if mode & stat.S_IWOTH:
        return True
    return False


if os.name != "nt" and getattr(os, "geteuid", lambda: 1)() == 0:
    _ORIGINAL_WRITE_TEXT = Path.write_text

    def _write_text_with_permission_check(self, data, encoding="utf-8", errors=None):
        if not _has_unix_write_permission(self):
            raise PermissionError(f"Permission denied: '{self}'")
        return _ORIGINAL_WRITE_TEXT(self, data, encoding=encoding, errors=errors)

    Path.write_text = _write_text_with_permission_check  # type: ignore[assignment]

def log_tool_call(tool_name: str, data: dict) -> None:
    Logger("tool").log_tool_call(tool_name, data)

def log_tool_result(tool_name: str, result: str | Exception = "") -> None:
    Logger("tool").info(f"TOOL_RESULT | {tool_name} | {result}")

def log_tool_error(tool_name: str, error: str | Exception) -> None:
    Logger("tool").error(f"TOOL_ERROR | {tool_name} | {error}")


def _resolve_tool_path(raw_path: str) -> tuple[str, str]:
    """Resolve a tool path and preserve a safe agent-visible representation."""
    visible_path = display_agent_path_auto(raw_path)
    resolved_path = resolve_agent_path_auto(raw_path)
    return visible_path, resolved_path


@function_tool
def read_file(filepath: str) -> str:
    """
    Читает содержимое файла.

    Args:
        filepath: Путь к файлу

    Returns:
        str: Содержимое файла
    """
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("read_file", {"filepath": visible_path})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)
        if not path.exists():
            log_tool_error("read_file", f"Файл {visible_path} не найден")
            return f"❌ Файл {visible_path} не найден"

        if not path.is_file():
            log_tool_error("read_file", f"{visible_path} не является файлом")
            return f"❌ {visible_path} не является файлом"

        content = path.read_text(encoding='utf-8')
        lines_count = len(content.splitlines())

        log_tool_result("read_file", f"Прочитано {lines_count} строк")
        return f"📄 Содержимое файла {visible_path}:\n\n{content}"

    except Exception as e:
        log_tool_error("read_file", str(e))
        return f"❌ Ошибка при чтении {visible_path}: {str(e)}"

@function_tool
def get_file_info(filepath: str) -> str:
    """
    Получает информацию о файле.

    Args:
        filepath: Путь к файлу

    Returns:
        str: Информация о файле
    """
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("get_file_info", {"filepath": visible_path})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)
        if not path.exists():
            log_tool_error("get_file_info", f"Файл {visible_path} не найден")
            return f"❌ Файл {visible_path} не найден"

        if not path.is_file():
            log_tool_error("get_file_info", f"{visible_path} не является файлом")
            return f"❌ {visible_path} не является файлом"

        stat = path.stat()
        content = path.read_text(encoding='utf-8')
        lines_count = len(content.splitlines())
        extension = path.suffix.lower()

        log_tool_result("get_file_info", f"Файл {stat.st_size} байт, {lines_count} строк")

        result = f"""📄 Информация о файле {visible_path}:
• Имя: {path.name}
• Размер: {stat.st_size} байт
• Строк: {lines_count}
• Расширение: {extension if extension else 'без расширения'}"""

        return result

    except Exception as e:
        log_tool_error("get_file_info", str(e))
        return f"❌ Ошибка при получении информации о {visible_path}: {str(e)}"

@function_tool
def list_files(directory: str = ".") -> str:
    """
    Показывает список файлов в директории.
    
    Args:
        directory: Путь к директории
        
    Returns:
        str: Список файлов
    """
    visible_directory = display_agent_path_auto(directory)
    log_tool_call("list_files", {"directory": visible_directory})
    try:
        visible_directory, directory = _resolve_tool_path(directory)
        path = Path(directory)
        if not path.exists():
            log_tool_error("list_files", f"Директория {visible_directory} не найдена")
            return f"❌ Директория {visible_directory} не найдена"
        
        if not path.is_dir():
            log_tool_error("list_files", f"{visible_directory} не является директорией")
            return f"❌ {visible_directory} не является директорией"
        
        files = []
        dirs = []
        for item in sorted(path.iterdir()):
            if item.is_file():
                size = item.stat().st_size
                files.append(f"📄 {item.name} ({size} байт)")
            elif item.is_dir():
                dirs.append(f"📁 {item.name}/")
        
        total_items = len(files) + len(dirs)
        log_tool_result("list_files", f"Найдено {total_items} элементов")
        
        if total_items == 0:
            return f"📂 Директория {visible_directory} пуста"
        
        all_items = dirs + files  # Директории сначала
        result = f"📂 Содержимое директории {visible_directory} ({total_items} элементов):\n\n" + "\n".join(all_items)
        return result
        
    except Exception as e:
        log_tool_error("list_files", str(e))
        return f"❌ Ошибка при чтении директории {visible_directory}: {str(e)}"

@function_tool
def write_file(filepath: str, content: str) -> str:
    """
    Записывает содержимое в файл.

    Args:
        filepath: Путь к файлу
        content: Содержимое для записи

    Returns:
        str: Результат операции
    """
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("write_file", {"filepath": visible_path, "content_length": len(content)})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)

        # Создаем родительские директории если нужно
        path.parent.mkdir(parents=True, exist_ok=True)

        # Записываем файл
        path.write_text(content, encoding='utf-8')

        size = path.stat().st_size
        lines_count = len(content.splitlines())

        log_tool_result("write_file", f"Записано {lines_count} строк, {size} байт")
        return f"✅ Файл {visible_path} успешно записан ({size} байт)"

    except Exception as e:
        log_tool_error("write_file", str(e))
        return f"❌ Ошибка при записи файла {visible_path}: {str(e)}"


@function_tool
def append_to_file(filepath: str, content: str) -> str:
    """
    Добавляет текст в конец файла.
    
    Args:
        filepath: Путь к файлу
        content: Текст для добавления
        
    Returns:
        str: Сообщение о количестве добавленных байт и диапазоне позиций (from start to end).
             Для пустого файла: "Insert N bytes from 0 to N".
             Для непустого: "Insert N bytes from {old_size} to {new_size}".
    """
    visible_path = display_agent_path_auto(filepath)
    log_tool_call("append_to_file", {"filepath": visible_path, "content_length": len(content)})
    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        old_size = path.stat().st_size if path.exists() else 0
        content_bytes = content.encode("utf-8")
        n_bytes = len(content_bytes)
        
        with path.open("ab") as f:
            f.write(content_bytes)
        
        new_size = old_size + n_bytes
        log_tool_result("append_to_file", f"Добавлено {n_bytes} байт, позиции {old_size}–{new_size}")
        return f"Insert {n_bytes} bytes from {old_size} to {new_size}"
        
    except Exception as e:
        log_tool_error("append_to_file", str(e))
        return f"❌ Ошибка при добавлении в файл {visible_path}: {str(e)}"


@function_tool
def search_files(
    search_pattern: str,
    directory: str = ".",
    use_regex: bool = False,
    search_in_content: bool = False,
    file_extensions: str = "",
    max_results: int = 50,
) -> str:
    """
    Поиск файлов и директорий по имени или содержимому с поддержкой регулярных выражений.
    
    Args:
        search_pattern: Паттерн для поиска (строка или regex)
        directory: Директория для поиска (по умолчанию текущая)
        use_regex: Использовать регулярные выражения (по умолчанию False)
        search_in_content: Искать в содержимом файлов (по умолчанию False)
        file_extensions: Фильтр по расширениям файлов, разделенных запятой (например: "py,js,txt")
        max_results: Максимальное количество результатов (по умолчанию 50)
        
    Returns:
        str: Результаты поиска
    """
    start_time = time.time()
    visible_directory = display_agent_path_auto(directory)
    args = {
        "search_pattern": search_pattern,
        "directory": visible_directory,
        "use_regex": use_regex,
        "search_in_content": search_in_content,
        "file_extensions": file_extensions,
        "max_results": max_results
    }
    log_tool_call("search_files", args)

    try:
        visible_directory, directory = _resolve_tool_path(directory)
        base_path = Path(directory)
        if not base_path.exists():
            result = f"ОШИБКА: Директория {visible_directory} не найдена"
            log_tool_result("search_files", result)
            return result
        
        if not base_path.is_dir():
            result = f"ОШИБКА: {visible_directory} не является директорией"
            log_tool_result("search_files", result)
            return result
        
        # Подготавливаем паттерн для поиска
        if use_regex:
            try:
                pattern = re.compile(search_pattern, re.IGNORECASE)
            except re.error as e:
                result = f"ОШИБКА: Некорректное регулярное выражение '{search_pattern}': {str(e)}"
                log_tool_result("search_files", result)
                return result
        else:
            # Простой поиск - конвертируем в regex для единообразия
            escaped_pattern = re.escape(search_pattern)
            pattern = re.compile(escaped_pattern, re.IGNORECASE)
        
        # Подготавливаем фильтр расширений
        extensions = []
        if file_extensions:
            extensions = [ext.strip().lower() for ext in file_extensions.split(',')]
            extensions = [ext if ext.startswith('.') else f'.{ext}' for ext in extensions]
        
        results = []
        
        # Логгируем начало поиска
        from utils.logger import log_custom
        log_custom('debug', 'file_operation', f"Начало поиска в: {visible_directory}", pattern=search_pattern, use_regex=use_regex)
        
        # Рекурсивно обходим директории
        for root, dirs, files in os.walk(base_path):
            root_path = Path(root)
            
            # Поиск в именах директорий
            for dir_name in dirs:
                if len(results) >= max_results:
                    break
                    
                if pattern.search(dir_name):
                    dir_path = root_path / dir_name
                    relative_path = dir_path.relative_to(base_path)
                    results.append(f"📁 {relative_path}/ (директория)")
            
            # Поиск в именах файлов
            for file_name in files:
                if len(results) >= max_results:
                    break
                
                file_path = root_path / file_name
                file_extension = file_path.suffix.lower()
                
                # Фильтрация по расширениям
                if extensions and file_extension not in extensions:
                    continue
                
                match_found = False
                match_info = ""
                
                # Поиск по имени файла
                if pattern.search(file_name):
                    match_found = True
                    match_info = "имя файла"
                
                # Поиск в содержимом файла (только для текстовых файлов)
                if search_in_content and not match_found:
                    try:
                        # Проверяем, что файл текстовый
                        if file_extension in ['.py', '.js', '.json', '.md', '.txt', '.yml', '.yaml', '.html', '.css', '.xml', '.csv']:
                            content = file_path.read_text(encoding='utf-8', errors='ignore')
                            if pattern.search(content):
                                match_found = True
                                match_info = "содержимое файла"
                    except Exception:
                        # Игнорируем ошибки чтения файлов
                        pass
                
                if match_found:
                    relative_path = file_path.relative_to(base_path)
                    file_size = file_path.stat().st_size
                    results.append(f"📄 {relative_path} ({file_size} байт) - найдено в: {match_info}")
            
            if len(results) >= max_results:
                break
        
        # Логгируем результаты поиска
        log_custom('debug', 'file_operation', f"Поиск завершен", found_count=len(results))
        
        # Формируем результат
        if not results:
            result = f"Поиск по паттерну '{search_pattern}' в {visible_directory} не дал результатов"
        else:
            result_header = f"Результаты поиска по паттерну '{search_pattern}' в {visible_directory}:\n"
            result_header += f"Найдено {len(results)} результат(ов)"
            if len(results) >= max_results:
                result_header += f" (показаны первые {max_results})"
            result_header += "\n\n"
            
            result = result_header + "\n".join(results)
        
        log_tool_result("search_files", result)
        return result
        
    except Exception as e:
        log_tool_error("search_files", e)
        result = f"ОШИБКА при поиске: {str(e)}"
        log_tool_result("search_files", result)
        return result

@function_tool
def edit_file_patch(filepath: str, patch_content: str) -> str:
    """
    Редактирует файл с помощью патча в формате unified diff.
    
    Args:
        filepath: Путь к файлу для редактирования
        patch_content: Содержимое патча в формате unified diff
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    visible_path = display_agent_path_auto(filepath)
    args = {"filepath": visible_path, "patch_content_length": len(patch_content)}
    log_tool_call("edit_file_patch", args)

    try:
        visible_path, filepath = _resolve_tool_path(filepath)
        path = Path(filepath)
        if not path.exists():
            result = f"ОШИБКА: Файл {visible_path} не найден"
            log_tool_result("edit_file_patch", result)
            return result
        
        if not path.is_file():
            result = f"ОШИБКА: {visible_path} не является файлом"
            log_tool_result("edit_file_patch", result)
            return result
        
        # Логгируем информацию о редактировании
        from utils.logger import log_custom
        original_content = path.read_text(encoding='utf-8')
        original_lines = original_content.splitlines(keepends=True)
        log_custom('debug', 'file_operation', f"Редактирование файла: {visible_path}", 
                  original_lines=len(original_lines), patch_lines=len(patch_content.splitlines()))
        
        # Парсим патч
        patch_lines = patch_content.splitlines()
        new_lines = original_lines.copy()
        
        i = 0
        while i < len(patch_lines):
            line = patch_lines[i]
            
            # Ищем заголовок патча (начинается с --- или +++)
            if line.startswith('---') or line.startswith('+++'):
                i += 1
                continue
            
            # Ищем блок изменений (начинается с @@)
            if line.startswith('@@'):
                # Парсим номера строк
                try:
                    # Формат: @@ -old_start,old_count +new_start,new_count @@
                    parts = line.split(' ')
                    old_info = parts[1]  # -old_start,old_count
                    new_info = parts[2]  # +new_start,new_count
                    
                    old_start = int(old_info.split(',')[0][1:]) - 1  # Убираем минус и вычитаем 1
                    new_start = int(new_info.split(',')[0][1:]) - 1  # Убираем плюс и вычитаем 1
                    
                    i += 1
                    
                    # Обрабатываем строки блока
                    old_line_num = old_start
                    new_line_num = new_start
                    
                    while i < len(patch_lines):
                        patch_line = patch_lines[i]
                        
                        if patch_line.startswith('@@'):
                            # Новый блок изменений
                            break
                        elif patch_line.startswith('---') or patch_line.startswith('+++'):
                            # Конец патча
                            break
                        elif patch_line.startswith(' '):
                            # Контекстная строка - оставляем как есть
                            if old_line_num < len(new_lines):
                                new_lines[old_line_num] = patch_line[1:]  # Убираем пробел
                            old_line_num += 1
                            new_line_num += 1
                        elif patch_line.startswith('-'):
                            # Удаляемая строка
                            if old_line_num < len(new_lines):
                                del new_lines[old_line_num]
                            # new_line_num не увеличиваем
                        elif patch_line.startswith('+'):
                            # Добавляемая строка
                            if old_line_num < len(new_lines):
                                new_lines.insert(old_line_num, patch_line[1:] + '\n')  # Убираем плюс и добавляем перенос
                            else:
                                new_lines.append(patch_line[1:] + '\n')
                            old_line_num += 1
                            new_line_num += 1
                        else:
                            # Пустая строка или комментарий
                            pass
                        
                        i += 1
                    
                except (ValueError, IndexError) as e:
                    result = f"ОШИБКА: Некорректный формат патча в строке '{line}': {str(e)}"
                    log_tool_result("edit_file_patch", result)
                    return result
            else:
                i += 1
        
        # Записываем обновленное содержимое
        new_content = ''.join(new_lines)
        path.write_text(new_content, encoding='utf-8')
        
        # Подсчитываем изменения
        original_line_count = len(original_lines)
        new_line_count = len(new_lines)
        changes = new_line_count - original_line_count
        
        # Логгируем результат редактирования
        log_custom('debug', 'file_operation', f"Файл обновлен: {visible_path}", 
                  changes=changes, new_lines=new_line_count)
        
        result = f"✅ Файл {visible_path} успешно обновлен патчем"
        if changes != 0:
            result += f" (изменено строк: {changes:+d})"
        
        log_tool_result("edit_file_patch", result)
        return result
        
    except Exception as e:
        log_tool_error("edit_file_patch", e)
        result = f"ОШИБКА при применении патча к файлу {visible_path}: {str(e)}"
        log_tool_result("edit_file_patch", result)
        return result

# ============================================================================
# СЛОВАРЬ ФАЙЛОВЫХ ИНСТРУМЕНТОВ
# ============================================================================

FILE_TOOLS = {
    "file_read": read_file,
    "file_write": write_file,
    "file_append": append_to_file,
    "file_list": list_files,
    "file_info": get_file_info,
    "file_search": search_files,
    "file_edit_patch": edit_file_patch,
}

def get_file_tools() -> List[Any]:
    """Возвращает список всех файловых инструментов."""
    return list(FILE_TOOLS.values())

def get_file_tools_by_names(tool_names: List[str]) -> List[Any]:
    """Возвращает список файловых инструментов по их именам."""
    tools = []
    for name in tool_names:
        if name in FILE_TOOLS:
            tools.append(FILE_TOOLS[name])
        else:
            from utils.logger import Logger
            Logger(__name__).warning(f"Файловый инструмент '{name}' не найден")
    return tools 
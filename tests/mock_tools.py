"""
Мок инструменты для тестирования системы Grid.
Предоставляют изолированные реализации инструментов без внешних зависимостей.
"""

import asyncio
import json
import os
import re
from typing import Dict, List, Any, Optional
from unittest.mock import AsyncMock


class MockFileSystem:
    """Мок файловой системы для тестирования."""
    
    def __init__(self):
        self.files: Dict[str, str] = {}
        self.directories: Dict[str, List[str]] = {}
    
    def create_file(self, path: str, content: str):
        """Создает файл в мок файловой системе."""
        self.files[path] = content
        
        # Обновляем директории
        dir_path = os.path.dirname(path)
        filename = os.path.basename(path)
        
        if dir_path not in self.directories:
            self.directories[dir_path] = []
        
        if filename not in self.directories[dir_path]:
            self.directories[dir_path].append(filename)
    
    def file_exists(self, path: str) -> bool:
        """Проверяет существование файла."""
        return path in self.files
    
    def read_file(self, path: str) -> Optional[str]:
        """Читает содержимое файла."""
        return self.files.get(path)
    
    def list_directory(self, path: str) -> List[str]:
        """Возвращает список файлов в директории."""
        return self.directories.get(path, [])
    
    def search_content(self, pattern: str, directory: str = None) -> List[str]:
        """Ищет паттерн в содержимом файлов."""
        results = []
        
        for file_path, content in self.files.items():
            if directory and not file_path.startswith(directory):
                continue
            
            if pattern.lower() in content.lower():
                results.append(file_path)
        
        return results


# Глобальная мок файловая система для тестов
mock_fs = MockFileSystem()


async def mock_read_file(filepath: str) -> str:
    """Мок функция чтения файла."""
    await asyncio.sleep(0.01)  # Имитация задержки
    
    if not mock_fs.file_exists(filepath):
        return f"❌ Ошибка: файл '{filepath}' не найден в тестовой файловой системе."
    
    content = mock_fs.read_file(filepath)
    return f"📄 Содержимое файла '{filepath}':\n{content}"


async def mock_write_file(filepath: str, content: str) -> str:
    """Мок функция записи файла."""
    await asyncio.sleep(0.01)  # Имитация задержки
    
    mock_fs.create_file(filepath, content)
    return f"✅ Файл '{filepath}' успешно записан в тестовую файловую систему. Размер: {len(content)} символов."


async def mock_list_files(directory: str) -> str:
    """Мок функция получения списка файлов."""
    await asyncio.sleep(0.01)
    
    files = mock_fs.list_directory(directory)
    
    if not files:
        return f"📁 Директория '{directory}' пуста или не существует в тестовой системе."
    
    files_list = "\n".join(f"  - {file}" for file in files)
    return f"📁 Файлы в директории '{directory}':\n{files_list}"


async def mock_get_file_info(filepath: str) -> str:
    """Мок функция получения информации о файле."""
    await asyncio.sleep(0.01)
    
    if not mock_fs.file_exists(filepath):
        return f"❌ Файл '{filepath}' не найден в тестовой системе."
    
    content = mock_fs.read_file(filepath)
    size = len(content)
    lines = content.count('\n') + 1 if content else 0
    
    return f"""📊 Информация о файле '{filepath}':
  - Размер: {size} символов
  - Строк: {lines}
  - Тип: текстовый файл (тестовый)"""


async def mock_search_files(search_pattern: str, directory: str, use_regex: bool = False, search_in_content: bool = False) -> str:
    """Мок функция поиска файлов."""
    await asyncio.sleep(0.01)
    
    if search_in_content:
        # Поиск по содержимому
        results = mock_fs.search_content(search_pattern, directory)
        if results:
            files_list = "\n".join(f"  - {file}" for file in results)
            return f"🔍 Найдены файлы с содержимым '{search_pattern}':\n{files_list}"
        else:
            return f"❌ Файлы с содержимым '{search_pattern}' не найдены."
    else:
        # Поиск по именам файлов
        all_files = mock_fs.list_directory(directory)
        if use_regex:
            try:
                pattern = re.compile(search_pattern, re.IGNORECASE)
                results = [f for f in all_files if pattern.search(f)]
            except re.error:
                return f"❌ Некорректное регулярное выражение: {search_pattern}"
        else:
            results = [f for f in all_files if search_pattern.lower() in f.lower()]
        
        if results:
            files_list = "\n".join(f"  - {file}" for file in results)
            return f"🔍 Найдены файлы по паттерну '{search_pattern}':\n{files_list}"
        else:
            return f"❌ Файлы по паттерну '{search_pattern}' не найдены."


async def mock_edit_file_patch(filepath: str, patch_content: str) -> str:
    """Мок функция редактирования файла патчем."""
    await asyncio.sleep(0.02)
    
    if not mock_fs.file_exists(filepath):
        return f"❌ Файл '{filepath}' не найден для редактирования."
    
    # Упрощенная обработка патча для тестов
    current_content = mock_fs.read_file(filepath)
    
    # Ищем строки для замены в патче
    patch_lines = patch_content.split('\n')
    old_line = None
    new_line = None
    
    for line in patch_lines:
        if line.startswith('-') and not line.startswith('---'):
            old_line = line[1:]  # Убираем префикс '-'
        elif line.startswith('+') and not line.startswith('+++'):
            new_line = line[1:]  # Убираем префикс '+'
    
    if old_line and new_line:
        # Применяем простую замену
        updated_content = current_content.replace(old_line, new_line)
        mock_fs.create_file(filepath, updated_content)
        return f"✅ Патч успешно применен к файлу '{filepath}'. Заменено: '{old_line}' → '{new_line}'"
    else:
        return f"⚠️ Патч применен к файлу '{filepath}' (упрощенная обработка для тестов)."


# Git операции (моки)

class MockGitRepository:
    """Мок Git репозитория для тестирования."""
    
    def __init__(self):
        self.staged_files: List[str] = []
        self.commits: List[Dict] = []
        self.branches: List[str] = ["main"]
        self.current_branch = "main"
        self.status_message = "working tree clean"
    
    def add_file(self, filepath: str):
        """Добавляет файл в индекс."""
        if filepath not in self.staged_files:
            self.staged_files.append(filepath)
    
    def add_all(self):
        """Добавляет все файлы в индекс."""
        # В реальном тесте здесь были бы все измененные файлы
        self.staged_files = ["test_file.txt", "another_file.py"]
    
    def commit(self, message: str, author_name: str, author_email: str):
        """Создает коммит."""
        commit = {
            "message": message,
            "author": f"{author_name} <{author_email}>",
            "files": self.staged_files.copy(),
            "timestamp": "2023-01-01T10:00:00Z"
        }
        self.commits.append(commit)
        self.staged_files.clear()
        return commit


# Глобальный мок репозиторий
mock_git = MockGitRepository()


async def mock_git_status(directory: str) -> str:
    """Мок функция статуса Git."""
    await asyncio.sleep(0.01)
    
    staged_count = len(mock_git.staged_files)
    branch = mock_git.current_branch
    
    if staged_count > 0:
        staged_list = "\n".join(f"  - {file}" for file in mock_git.staged_files)
        return f"""📋 Git статус (тестовый репозиторий):
Ветка: {branch}
Файлы в индексе ({staged_count}):
{staged_list}"""
    else:
        return f"📋 Git статус: ветка '{branch}', рабочая директория чистая (тестовый репозиторий)."


async def mock_git_add_file(directory: str, filename: str) -> str:
    """Мок функция добавления файла в Git."""
    await asyncio.sleep(0.01)
    
    mock_git.add_file(filename)
    return f"✅ Файл '{filename}' добавлен в индекс Git (тестовый репозиторий)."


async def mock_git_add_all(directory: str) -> str:
    """Мок функция добавления всех файлов в Git."""
    await asyncio.sleep(0.01)
    
    mock_git.add_all()
    files_count = len(mock_git.staged_files)
    return f"✅ Все файлы добавлены в индекс Git. Добавлено файлов: {files_count} (тестовый репозиторий)."


async def mock_git_commit(directory: str, message: str, author_name: str, author_email: str) -> str:
    """Мок функция создания коммита Git."""
    await asyncio.sleep(0.02)
    
    if not mock_git.staged_files:
        return "❌ Нет файлов в индексе для коммита (тестовый репозиторий)."
    
    commit = mock_git.commit(message, author_name, author_email)
    files_count = len(commit["files"])
    
    return f"""✅ Коммит создан успешно (тестовый репозиторий):
  - Сообщение: {message}
  - Автор: {author_name} <{author_email}>
  - Файлов: {files_count}
  - ID: test-commit-{len(mock_git.commits)}"""


async def mock_git_log(directory: str, max_commits: int = 10) -> str:
    """Мок функция истории коммитов Git."""
    await asyncio.sleep(0.01)
    
    if not mock_git.commits:
        return "📜 История коммитов пуста (тестовый репозиторий)."
    
    recent_commits = mock_git.commits[-max_commits:]
    
    log_lines = []
    for i, commit in enumerate(reversed(recent_commits)):
        commit_id = f"test-commit-{len(mock_git.commits) - i}"
        log_lines.append(f"Коммит {commit_id}: {commit['message']} ({commit['author']})")
    
    return f"📜 История коммитов (тестовый репозиторий):\n" + "\n".join(log_lines)


# Калькулятор (мок)

async def mock_calculate(expression: str) -> str:
    """Мок функция калькулятора."""
    await asyncio.sleep(0.01)
    
    # Простая обработка математических выражений для тестов
    try:
        # Безопасная оценка простых выражений
        allowed_chars = set('0123456789+-*/().= ')
        if all(c in allowed_chars for c in expression):
            # Убираем знак равенства если есть
            clean_expr = expression.replace('=', '').strip()
            result = eval(clean_expr)
            return f"🧮 Результат вычисления '{expression}': {result}"
        else:
            return f"❌ Некорректное выражение для вычисления: '{expression}'"
    except Exception as e:
        return f"❌ Ошибка вычисления '{expression}': {str(e)}"


# Регистрация мок инструментов

MOCK_TOOLS = {
    "test_file_read": mock_read_file,
    "test_file_write": mock_write_file,
    "test_file_list": mock_list_files,
    "test_file_info": mock_get_file_info,
    "test_file_search": mock_search_files,
    "test_file_edit": mock_edit_file_patch,
    "test_git_status": mock_git_status,
    "test_git_add_file": mock_git_add_file,
    "test_git_add_all": mock_git_add_all,
    "test_git_commit": mock_git_commit,
    "test_git_log": mock_git_log,
    "test_calculate": mock_calculate,
}


def get_mock_tool(tool_name: str):
    """Получает мок инструмент по имени."""
    return MOCK_TOOLS.get(tool_name)


def reset_mock_state():
    """Сбрасывает состояние всех моков."""
    global mock_fs, mock_git
    mock_fs = MockFileSystem()
    mock_git = MockGitRepository()


def setup_mock_data():
    """Настройка тестовых данных для моков."""
    # Создаем несколько тестовых файлов
    mock_fs.create_file("/tmp/test/config.json", '{"setting": "value"}')
    mock_fs.create_file("/tmp/test/data.txt", "Sample data\nLine 2\nLine 3")
    mock_fs.create_file("/tmp/test/readme.md", "# Test Project\nDescription")
    
    # Настройка Git репозитория
    mock_git.status_message = "На ветке main. Нет коммитов для push."
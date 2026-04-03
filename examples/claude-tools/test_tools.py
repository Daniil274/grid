#!/usr/bin/env python3
"""
Тестовый скрипт для проверки инструментов Claude Tools.

Запуск:
    cd examples/claude-tools
    python test_tools.py
"""

import sys
from pathlib import Path

# Добавляем родительскую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tools import (
    bash_tool,
    file_read, file_write, file_edit, file_append,
    glob_tool, grep_tool,
    web_fetch,
    notebook_create, notebook_read, notebook_edit,
    todo_write, todo_list, todo_clear
)


def test_bash():
    print("=" * 60)
    print("Тест: bash_tool")
    print("=" * 60)
    
    # Простая команда
    result = bash_tool("echo 'Hello, World!'")
    print(result)
    print()
    
    # Проверка безопасности
    result = bash_tool("rm -rf /")
    print(result)
    print()
    
    # С таймаутом
    result = bash_tool("pwd", description="Текущая директория")
    print(result)
    print()


def test_file():
    print("=" * 60)
    print("Тест: file_tools")
    print("=" * 60)
    
    test_file_path = "/tmp/test_claude_tools.txt"
    
    # Запись
    result = file_write(test_file_path, "Line 1\nLine 2\nLine 3\n")
    print(result)
    print()
    
    # Чтение
    result = file_read(test_file_path)
    print(result)
    print()
    
    # Добавление
    result = file_append(test_file_path, "Line 4\n")
    print(result)
    print()
    
    # Чтение с offset
    result = file_read(test_file_path, offset=1, limit_lines=2)
    print(result)
    print()
    
    # Редактирование
    patch = """--- a/test_claude_tools.txt
+++ b/test_claude_tools.txt
@@ -1,4 +1,4 @@
 Line 1
-Line 2
+Line 2 modified
 Line 3
 Line 4
"""
    result = file_edit(test_file_path, patch)
    print(result)
    print()
    
    # Проверяем результат
    result = file_read(test_file_path)
    print(result)
    print()
    
    # Очистка
    import os
    os.remove(test_file_path)
    print("✅ Тестовый файл удалён")
    print()


def test_search():
    print("=" * 60)
    print("Тест: search_tools")
    print("=" * 60)
    
    # Glob
    result = glob_tool("*.py", directory="tools")
    print(result)
    print()
    
    # Grep
    result = grep_tool("function_tool", directory="tools", file_extensions="py")
    print(result)
    print()


def test_todo():
    print("=" * 60)
    print("Тест: todo_tools")
    print("=" * 60)
    
    # Очистка перед тестом
    todo_clear()
    
    # Создание задач
    result = todo_write("Первая задача", priority=3)
    print(result)
    
    result = todo_write("Вторая задача", priority=5)
    print(result)
    
    result = todo_write("Третья задача", status="in_progress")
    print(result)
    print()
    
    # Список
    result = todo_list()
    print(result)
    print()
    
    # Обновление
    result = todo_write("Первая задача (обновлена)", todo_id="todo_1", status="done")
    print(result)
    print()
    
    # Список с фильтром
    result = todo_list(status_filter="pending")
    print(result)
    print()
    
    # Очистка
    result = todo_clear()
    print(result)
    print()


def test_notebook():
    print("=" * 60)
    print("Тест: notebook_tools")
    print("=" * 60)
    
    test_nb_path = "/tmp/test_claude_tools.ipynb"
    
    # Создание
    result = notebook_create(test_nb_path)
    print(result)
    print()
    
    # Чтение
    result = notebook_read(test_nb_path)
    print(result)
    print()
    
    # Редактирование
    result = notebook_edit(
        test_nb_path,
        cell_index=1,
        new_source="# Новый заголовок\n\nЭто markdown ячейка"
    )
    print(result)
    print()
    
    # Проверка
    result = notebook_read(test_nb_path)
    print(result)
    print()
    
    # Очистка
    import os
    os.remove(test_nb_path)
    print("✅ Тестовый notebook удалён")
    print()


def main():
    print("\n" + "=" * 60)
    print("Тестирование инструментов Claude Tools")
    print("=" * 60 + "\n")
    
    try:
        test_bash()
    except Exception as e:
        print(f"❌ Ошибка в test_bash: {e}")
    
    try:
        test_file()
    except Exception as e:
        print(f"❌ Ошибка в test_file: {e}")
    
    try:
        test_search()
    except Exception as e:
        print(f"❌ Ошибка в test_search: {e}")
    
    try:
        test_todo()
    except Exception as e:
        print(f"❌ Ошибка в test_todo: {e}")
    
    try:
        test_notebook()
    except Exception as e:
        print(f"❌ Ошибка в test_notebook: {e}")
    
    print("\n" + "=" * 60)
    print("Тестирование завершено")
    print("=" * 60)


if __name__ == "__main__":
    main()

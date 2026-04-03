"""
Todo Tool — управление задачами с хранением в рабочей директории агента.
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import function_tool
from utils.path_utils import get_current_factory, resolve_agent_path_auto


# Values that LLMs mistakenly pass instead of omitting the optional parameter
_NULL_VALUES = {"null", "none", "undefined", "nil", ""}


def _get_todo_file() -> Path:
    """
    Resolves the todo file path.

    Priority:
      1. CLAUDE_TODO_FILE env var (validated to stay within workspace)
      2. <workspace>/data/todos.json  (if factory available)
      3. ~/.claude_tools_todos.json   (last resort fallback)
    """
    import os
    env_path = os.environ.get("CLAUDE_TODO_FILE")
    if env_path:
        try:
            resolved = Path(resolve_agent_path_auto(env_path))
            resolved.parent.mkdir(parents=True, exist_ok=True)
            return resolved
        except (ValueError, Exception):
            pass  # Escaped workspace or other error — ignore and use default

    factory = get_current_factory()
    if factory is not None:
        try:
            workspace = Path(factory.config.get_working_directory())
            data_dir = workspace / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            return data_dir / "todos.json"
        except Exception:
            pass

    # Final fallback: user home
    return Path.home() / ".claude_tools_todos.json"


def _load() -> dict:
    todo_file = _get_todo_file()
    if todo_file.exists():
        try:
            with open(todo_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"todos": [], "version": 1}


def _save(data: dict) -> None:
    todo_file = _get_todo_file()
    todo_file.parent.mkdir(parents=True, exist_ok=True)
    with open(todo_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _new_id() -> str:
    return f"todo_{int(time.time() * 1000)}"


def _is_null(value: Optional[str]) -> bool:
    """True if value is None or a string the LLM uses to mean 'not set'."""
    if value is None:
        return True
    return str(value).strip().lower() in _NULL_VALUES


# ---------------------------------------------------------------------------
# todo_write
# ---------------------------------------------------------------------------

@function_tool
def todo_write(
    content: str,
    todo_id: Optional[str] = None,
    status: Optional[str] = None,
    priority: int = 1,
) -> str:
    """
    Создаёт или обновляет задачу в todo-списке.

    Args:
        content:  Текст задачи
        todo_id:  ID задачи для обновления (оставь пустым для создания новой)
        status:   Статус: 'pending', 'in_progress', 'done'
        priority: Приоритет 1–5 (5 = высший)

    Returns:
        Результат операции
    """
    try:
        data = _load()
        todos = data.get("todos", [])
        valid_statuses = ("pending", "in_progress", "done")

        # CREATE a new task when todo_id is absent or a null-like value
        if _is_null(todo_id):
            if not content:
                return "❌ Укажите content для создания задачи"

            if status and status not in valid_statuses:
                return f"❌ Некорректный статус: {status}. Допустимо: {', '.join(valid_statuses)}"

            new_todo = {
                "id": _new_id(),
                "content": content,
                "status": status or "pending",
                "priority": max(1, min(5, int(priority))),
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            todos.append(new_todo)
            data["todos"] = todos
            _save(data)
            short = content[:50] + ("..." if len(content) > 50 else "")
            return f"➕ Создана задача [{new_todo['id']}]: {short}"

        # UPDATE an existing task
        for todo in todos:
            if todo.get("id") == todo_id:
                if content:
                    todo["content"] = content
                if status:
                    if status not in valid_statuses:
                        return f"❌ Некорректный статус: {status}. Допустимо: {', '.join(valid_statuses)}"
                    todo["status"] = status
                    if status == "done":
                        todo["completed_at"] = datetime.now().isoformat()
                if priority:
                    todo["priority"] = max(1, min(5, int(priority)))
                todo["updated_at"] = datetime.now().isoformat()
                _save(data)

                emoji = {"pending": "⏳", "in_progress": "🔄", "done": "✅"}.get(todo["status"], "⏳")
                return f"{emoji} Задача обновлена: {todo['content'][:50]}"

        return f"❌ Задача с ID '{todo_id}' не найдена"

    except Exception as exc:
        return f"❌ Ошибка: {exc}"


# ---------------------------------------------------------------------------
# todo_list
# ---------------------------------------------------------------------------

@function_tool
def todo_list(
    status_filter: Optional[str] = None,
    sort_by: str = "priority",
) -> str:
    """
    Показывает список задач.

    Args:
        status_filter: Фильтр ('pending', 'in_progress', 'done', 'all' или None)
        sort_by:       Сортировка: 'priority', 'created', 'updated'

    Returns:
        Список задач
    """
    try:
        data = _load()
        all_todos = data.get("todos", [])

        if not all_todos:
            return "📋 Задач нет. Используй todo_write для создания."

        valid_statuses = ("pending", "in_progress", "done")
        todos = all_todos

        if status_filter and status_filter != "all":
            if status_filter not in valid_statuses:
                return f"❌ Некорректный фильтр: {status_filter}"
            todos = [t for t in todos if t.get("status") == status_filter]

        if not todos:
            return f"📋 Нет задач со статусом '{status_filter}'"

        if sort_by == "priority":
            todos = sorted(todos, key=lambda x: (-x.get("priority", 1), x.get("created_at", "")))
        elif sort_by == "created":
            todos = sorted(todos, key=lambda x: x.get("created_at", ""))
        elif sort_by == "updated":
            todos = sorted(todos, key=lambda x: x.get("updated_at", ""))

        pending = sum(1 for t in all_todos if t.get("status") == "pending")
        in_progress = sum(1 for t in all_todos if t.get("status") == "in_progress")
        done = sum(1 for t in all_todos if t.get("status") == "done")

        lines = [
            f"📋 Задачи (всего: {len(all_todos)}, ⏳{pending} 🔄{in_progress} ✅{done})",
            "",
        ]
        emoji_map = {"pending": "⏳", "in_progress": "🔄", "done": "✅"}

        for i, todo in enumerate(todos, 1):
            emoji = emoji_map.get(todo.get("status", ""), "⏳")
            stars = "★" * todo.get("priority", 1) + "☆" * (5 - todo.get("priority", 1))
            text = todo.get("content", "")
            if len(text) > 60:
                text = text[:60] + "..."
            lines.append(f"{i}. {emoji} [{stars}] {text}")
            lines.append(f"   ID: {todo.get('id', '?')}")

        return "\n".join(lines)

    except Exception as exc:
        return f"❌ Ошибка: {exc}"


# ---------------------------------------------------------------------------
# todo_delete
# ---------------------------------------------------------------------------

@function_tool
def todo_delete(todo_id: str) -> str:
    """
    Удаляет задачу по ID.

    Args:
        todo_id: ID задачи

    Returns:
        Результат операции
    """
    try:
        data = _load()
        todos = data.get("todos", [])
        for i, todo in enumerate(todos):
            if todo.get("id") == todo_id:
                text = todo.get("content", "")[:50]
                todos.pop(i)
                data["todos"] = todos
                _save(data)
                return f"🗑️ Удалена задача: {text}"
        return f"❌ Задача с ID '{todo_id}' не найдена"

    except Exception as exc:
        return f"❌ Ошибка: {exc}"


# ---------------------------------------------------------------------------
# todo_clear
# ---------------------------------------------------------------------------

@function_tool
def todo_clear(status: Optional[str] = None) -> str:
    """
    Очищает задачи (все или только с указанным статусом).

    Args:
        status: Статус для очистки ('done', 'pending', 'in_progress') или None для всех

    Returns:
        Результат операции
    """
    try:
        data = _load()
        todos = data.get("todos", [])

        if not todos:
            return "📋 Задач нет"

        if status:
            original = len(todos)
            todos = [t for t in todos if t.get("status") != status]
            removed = original - len(todos)
            data["todos"] = todos
            _save(data)
            return f"🗑️ Удалено {removed} задач со статусом '{status}'"
        else:
            count = len(todos)
            data["todos"] = []
            _save(data)
            return f"🗑️ Удалены все задачи ({count} шт.)"

    except Exception as exc:
        return f"❌ Ошибка: {exc}"

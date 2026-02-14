"""
Git инструменты для агентов.

Поддерживает:
- Статус репозитория
- История коммитов
- Различия в файлах
- Управление ветками
- Добавление файлов и создание коммитов
- Синхронизация с удаленными репозиториями
- Поддержка кириллицы в именах авторов и веток
"""

import re
import subprocess
import time
import os
from pathlib import Path
from typing import List, Dict, Any, Optional
from agents import function_tool, RunContextWrapper
from utils.logger import Logger

class _PrettyShim:
    def __init__(self):
        self._logger = Logger("git_tool")
    def tool_start(self, name: str, **kwargs):
        self._logger.log_tool_call(f"GIT:{name}", kwargs)
        return {"name": name, "args": kwargs}
    def tool_result(self, operation, result: str | None = None, error: str | None = None):
        if error:
            self._logger.error(f"{operation.get('name')} failed", error=error)
        else:
            self._logger.info(f"{operation.get('name')} ok", result=result)

pretty_logger = _PrettyShim()

def log_tool_start(name: str, **kwargs):
    return pretty_logger.tool_start(name, **kwargs)

def log_tool_result(name_or_operation, *, result: str | None = None, error: str | None = None):
    if isinstance(name_or_operation, dict):
        pretty_logger.tool_result(name_or_operation, result=result, error=error)
    else:
        pretty_logger.tool_result({"name": name_or_operation, "args": {}}, result=result, error=error)

def _map_path_to_container(path: Optional[str], context: Any) -> str:
    """Map a host path to a container path (/workspace)."""
    if not path or path == ".":
        return "/workspace"
    
    # If it's already a container path, return it
    if path.startswith("/workspace"):
        return path
        
    # If it's an absolute host path, try to map it
    if os.path.isabs(path):
        try:
            # We need the host working directory to calculate relative path
            # This is a bit tricky since we don't have direct access to factory.config here
            # but we can try to get it from context
            if hasattr(context, 'context') and hasattr(context.context, 'factory'):
                host_wd = context.context.factory.config.get_working_directory()
                # Ensure paths are normalized (resolve symlinks, etc.)
                norm_path = os.path.normpath(path)
                norm_host_wd = os.path.normpath(host_wd)
                
                if norm_path.startswith(norm_host_wd):
                    rel = os.path.relpath(norm_path, norm_host_wd)
                    # Handle root case
                    if rel == ".":
                        return "/workspace"
                    return (Path("/workspace") / rel).as_posix()
        except Exception:
            pass
            
    # Fallback: if it's relative, assume it's relative to /workspace
    if not os.path.isabs(path):
        return (Path("/workspace") / path).as_posix()
        
    # If path is already /workspace or inside it, return it
    if path.startswith("/workspace"):
        return path

    # If we can't map it, default to /workspace to be safe
    return "/workspace"

def _run_git_command(command: List[str], cwd: Optional[str] = None, container_id: Optional[str] = None, context: Any = None) -> Dict[str, Any]:
    """
    Безопасный запуск Git команды с валидацией.
    
    Args:
        command: Список аргументов команды
        cwd: Рабочая директория
        container_id: ID контейнера для выполнения команды (опционально)
        context: Context for path mapping
        
    Returns:
        Dict с результатом: {"success": bool, "output": str, "error": str}
    """
    try:
        # Проверяем, что команда начинается с git
        if not command or command[0] != "git":
            return {"success": False, "output": "", "error": "Команда должна начинаться с 'git'"}
        
        # Валидация опасных команд - проверяем только аргументы команды
        dangerous_commands = ["rm", "clean", "reset --hard", "push --force", "rebase -i"]
        cmd_str = " ".join(command)
        for dangerous in dangerous_commands:
            # Проверяем, что опасная команда является отдельным аргументом, а не частью другого
            if f" {dangerous} " in f" {cmd_str} " or cmd_str.endswith(f" {dangerous}") or cmd_str.startswith(f"{dangerous} "):
                return {"success": False, "output": "", "error": f"Опасная команда заблокирована: {dangerous}"}
        
        # Логгируем выполнение команды
        from utils.logger import log_custom
        log_custom('debug', 'git_command', f"Выполнение: {' '.join(command)}", cwd=cwd, container_id=container_id, context=context)
        
        # Выполняем команду
        if container_id:
            # Конструируем команду docker exec
            # docker exec -i -w /workspace <container_id> <command>
            # Используем /workspace как рабочую директорию в контейнере, если cwd не указан или относительный
            
            workdir = _map_path_to_container(cwd, context)

            docker_cmd = ["docker", "exec", "-i", "-w", workdir, container_id] + command
            
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=30
            )
        else:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                timeout=30
            )
        
        # Логгируем результат
        if result.returncode == 0:
            log_custom('debug', 'git_command', f"Успешно выполнено: {' '.join(command)}")
        else:
            log_custom('debug', 'git_command', f"Ошибка выполнения: {' '.join(command)}", error=result.stderr)
        
        return {
            "success": result.returncode == 0,
            "output": result.stdout.strip(),
            "error": result.stderr.strip()
        }
        
    except subprocess.TimeoutExpired:
        from utils.logger import log_custom
        log_custom('error', 'git_command', f"Таймаут команды: {' '.join(command)}")
        return {"success": False, "output": "", "error": "Команда превысила лимит времени выполнения"}
    except FileNotFoundError as e:
        from utils.logger import log_custom
        log_custom('error', 'git_command', f"Executable not found: {e}. command={command if not container_id else docker_cmd}")
        return {"success": False, "output": "", "error": f"Executable not found: {e}"}

def _get_container_id(context: Any) -> Optional[str]:
    """Extract container_id from context."""
    if hasattr(context, 'context') and hasattr(context.context, 'container_id'):
        return context.context.container_id
    return None

@function_tool
def git_status(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Показывает статус Git репозитория.
    
    Args:
        directory: Путь к репозиторию
        
    Returns:
        str: Статус репозитория
    """

    
    operation = pretty_logger.tool_start("GitStatus", directory=directory)
    
    try:
        container_id = _get_container_id(context)
        
        # Если не в контейнере, проверяем путь локально
        if not container_id:
            path = Path(directory)
            if not path.exists():
                pretty_logger.tool_result(operation, error=f"Директория {directory} не найдена")
                return f"❌ Директория {directory} не найдена"
            cwd = str(path)
        else:
            cwd = None # В контейнере используем дефолтную рабочую директорию /workspace
        
        cmd_result = _run_git_command(["git", "status", "--porcelain"], cwd=cwd, container_id=container_id, context=context)
        
        if not cmd_result["success"]:
            error_text = cmd_result.get("error", "")
            error_text_lower = error_text.lower()
            if "not a git repository" in error_text_lower:
                pretty_logger.tool_result(operation, error=error_text)
                return f"❌ Не Git репозиторий: {error_text}"
            pretty_logger.tool_result(operation, error=error_text)
            return f"❌ Ошибка Git: {error_text}"
        
        
        # Форматируем вывод
        if not cmd_result["output"]:
            pretty_logger.tool_result(operation, result="Репозиторий чистый")
            return "✅ Рабочая директория чистая - нет изменений"
        else:
            lines = cmd_result["output"].split('\n')
            status_map = {
                'M': 'изменен',
                'A': 'добавлен', 
                'D': 'удален',
                'R': 'переименован',
                'C': 'скопирован',
                '??': 'неотслеживаемый'
            }
            
            formatted_lines = []
            for line in lines:
                if len(line) < 3:
                    continue
                status_code = line[:2].strip()
                filename_start = 2
                while filename_start < len(line) and line[filename_start] == ' ':
                    filename_start += 1
                filename = line[filename_start:].strip()
                status_text = status_map.get(status_code, status_code)
                formatted_lines.append(f"  📝 {status_text}: {filename}")
            
            changes_count = len(formatted_lines)
            pretty_logger.tool_result(operation, result=f"Найдено {changes_count} изменений")
            result = f"📋 Статус Git репозитория в {directory} ({changes_count} изменений):\n\n" + "\n".join(formatted_lines)
        
        return result
        
    except Exception as e:
        pretty_logger.tool_result(operation, error=str(e))
        return f"❌ Ошибка при получении статуса Git: {str(e)}"

@function_tool
def git_log(context: RunContextWrapper, directory: str = ".", max_commits: int = 10) -> str:
    """
    Показывает историю коммитов.
    
    Args:
        directory: Путь к репозиторию
        max_commits: Максимальное количество коммитов для отображения
        
    Returns:
        str: История коммитов
    """
    start_time = time.time()
    args = {"directory": directory, "max_commits": max_commits}
    operation = log_tool_start("git_log", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path)
        
        # Ограничиваем количество коммитов для безопасности
        max_commits = min(max_commits, 50)
        
        cmd_result = _run_git_command([
            "git", "log", 
            f"--max-count={max_commits}",
            "--pretty=format:%h|%an|%ad|%s",
            "--date=short"
        ], cwd=cwd, container_id=container_id, context=context)
        
        if not cmd_result["success"]:
            result = f"ОШИБКА: {cmd_result['error']}"
            log_tool_result(operation, error=result)
            return result
        
        if not cmd_result["output"]:
            result = "История коммитов пуста"
        else:
            lines = cmd_result["output"].split('\n')
            formatted_lines = [f"История коммитов в {directory}:\n"]
            
            for line in lines:
                parts = line.split('|')
                if len(parts) == 4:
                    hash_short, author, date, message = parts
                    formatted_lines.append(f"  {hash_short} - {author} ({date}): {message}")
            
            result = "\n".join(formatted_lines)
        
        duration = time.time() - start_time
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при получении истории: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_diff(context: RunContextWrapper, directory: str = ".", filename: str = "") -> str:
    """
    Показывает различия в файлах.
    
    Args:
        directory: Путь к репозиторию
        filename: Имя конкретного файла (опционально)
        
    Returns:
        str: Различия в файлах
    """
    start_time = time.time()
    args = {"directory": directory, "filename": filename}
    operation = log_tool_start("git_diff", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path)
        
        # Формируем команду
        command = ["git", "diff"]
        if filename:
            # Валидируем имя файла
            if not re.match(r'^[a-zA-Z0-9._/-]+$', filename):
                result = f"ОШИБКА: Недопустимое имя файла: {filename}"
                log_tool_result(operation, error=result)
                return result
            command.append(filename)
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if not cmd_result["success"]:
            result = f"ОШИБКА: {cmd_result['error']}"
            log_tool_result(operation, error=result)
            return result
        
        if not cmd_result["output"]:
            result = "Нет изменений для отображения"
        else:
            # Показываем полный вывод
            result = f"Различия в {directory}" + (f" для файла {filename}" if filename else "") + ":\n\n" + cmd_result["output"]
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при получении различий: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_branch_list(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Показывает список веток.
    
    Args:
        directory: Путь к репозиторию
        
    Returns:
        str: Список веток
    """
    start_time = time.time()
    args = {"directory": directory}
    operation = log_tool_start("git_branch_list", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, result=result)
                return result
            cwd = str(path)
        
        cmd_result = _run_git_command(["git", "branch", "-a"], cwd=cwd, container_id=container_id, context=context)
        
        if not cmd_result["success"]:
            result = f"ОШИБКА: {cmd_result['error']}"
            log_tool_result(operation, result=result)
            return result
        
        if not cmd_result["output"]:
            result = "Нет веток для отображения"
        else:
            lines = cmd_result["output"].split('\n')
            formatted_lines = [f"Ветки в репозитории {directory}:\n"]
            
            for line in lines:
                line = line.strip()
                if line:
                    if line.startswith('*'):
                        formatted_lines.append(f"  ➤ {line[1:].strip()} (текущая)")
                    else:
                        formatted_lines.append(f"    {line}")
            
            result = "\n".join(formatted_lines)
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при получении списка веток: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_add_file(context: RunContextWrapper, directory: str, filename: str) -> str:
    """
    Добавляет файл в индекс Git.
    
    Args:
        directory: Путь к репозиторию
        filename: Имя файла для добавления
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory, "filename": filename}
    operation = log_tool_start("git_add_file", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, result=result)
                return result
            cwd = str(path)
            
            # Проверяем, что файл существует (локально)
            file_path = path / filename
            if not file_path.exists():
                result = f"ОШИБКА: Файл {filename} не найден"
                log_tool_result(operation, result=result)
                return result
        
        # Валидация имени файла
        if not re.match(r'^[a-zA-Z0-9._/-]+$', filename):
            result = f"ОШИБКА: Недопустимое имя файла: {filename}"
            log_tool_result(operation, result=result)
            return result
        
        cmd_result = _run_git_command(["git", "add", filename], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Файл {filename} успешно добавлен в индекс"
        else:
            result = f"ОШИБКА при добавлении файла: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при добавлении файла: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_commit(context: RunContextWrapper, directory: str, message: str, author_name: str = "", author_email: str = "") -> str:
    """
    Создает коммит с указанным сообщением.
    
    Args:
        directory: Путь к репозиторию
        message: Сообщение коммита
        author_name: Имя автора (опционально)
        author_email: Email автора (опционально)
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory, "message": message, "author_name": author_name, "author_email": author_email}
    operation = log_tool_start("git_commit", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, result=result)
                return result
            cwd = str(path)
        
        # Валидация сообщения коммита
        if not message or len(message.strip()) < 3:
            result = "ОШИБКА: Сообщение коммита должно содержать минимум 3 символа"
            log_tool_result(operation, result=result)
            return result
        
        if len(message) > 500:
            result = "ОШИБКА: Сообщение коммита слишком длинное (максимум 500 символов)"
            log_tool_result(operation, result=result)
            return result
        
        # Формируем команду
        command = ["git", "commit", "-m", message.strip()]
        
        # Добавляем автора если указан
        if author_name and author_email:
            # Валидация имени автора - поддерживаем кириллицу, латиницу, цифры, пробелы и основные символы
            if not re.match(r'^[\w\sа-яёА-ЯЁ\-\.]+$', author_name):
                result = "ОШИБКА: Недопустимое имя автора (поддерживаются буквы, цифры, пробелы, дефисы и точки)"
                log_tool_result(operation, result=result)
                return result
            
            # Более гибкая валидация email - поддерживаем локальные адреса
            if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+(\.[a-zA-Z]{2,})?$', author_email):
                result = "ОШИБКА: Недопустимый email автора (формат: user@domain.com или user@local)"
                log_tool_result(operation, result=result)
                return result
            
            command.extend(["--author", f"{author_name} <{author_email}>"])
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Коммит успешно создан: {message}"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при создании коммита: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_checkout_branch(context: RunContextWrapper, directory: str, branch_name: str, create_new: bool = False) -> str:
    """
    Переключается на ветку или создает новую.
    
    Args:
        directory: Путь к репозиторию
        branch_name: Имя ветки
        create_new: Создать новую ветку если не существует
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory, "branch_name": branch_name, "create_new": create_new}
    operation = log_tool_start("git_checkout_branch", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, result=result)
                return result
            cwd = str(path)
        
        # Валидация имени ветки - поддерживаем кириллицу и основные символы
        if not re.match(r'^[\wа-яёА-ЯЁ._/-]+$', branch_name):
            result = f"ОШИБКА: Недопустимое имя ветки: {branch_name} (поддерживаются буквы, цифры, точки, дефисы, подчеркивания и слеши)"
            log_tool_result(operation, result=result)
            return result
        
        # Формируем команду
        command = ["git", "checkout"]
        if create_new:
            command.append("-b")
        command.append(branch_name)
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            action = "создана и выбрана" if create_new else "выбрана"
            result = f"✅ Ветка {branch_name} {action}"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при переключении на ветку: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_pull(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Получает и объединяет изменения из удаленного репозитория.
    
    Args:
        directory: Путь к репозиторию
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory}
    operation = log_tool_start("git_pull", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, result=result)
                return result
            cwd = str(path)
        
        cmd_result = _run_git_command(["git", "pull"], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = "✅ Изменения успешно получены из удаленного репозитория"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при получении изменений: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_remote_info(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Показывает информацию о удаленных репозиториях.
    
    Args:
        directory: Путь к репозиторию
        
    Returns:
        str: Информация об удаленных репозиториях
    """
    start_time = time.time()
    args = {"directory": directory}
    operation = log_tool_start("git_remote_info", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, result=result)
                return result
            cwd = str(path)
        
        cmd_result = _run_git_command(["git", "remote", "-v"], cwd=cwd, container_id=container_id, context=context)
        
        if not cmd_result["success"]:
            result = f"ОШИБКА: {cmd_result['error']}"
            log_tool_result(operation, result=result)
            return result
        
        if not cmd_result["output"]:
            result = "Удаленные репозитории не настроены"
        else:
            result = f"Удаленные репозитории для {directory}:\n\n{cmd_result['output']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_init(context: RunContextWrapper, directory: str = ".", bare: bool = False) -> str:
    """
    Инициализирует новый Git репозиторий.
    
    Args:
        directory: Путь к директории для инициализации
        bare: Создать bare репозиторий (без рабочей директории)
        
    Returns:
        str: Результат инициализации
    """
    start_time = time.time()
    args = {"directory": directory, "bare": bare}
    operation = log_tool_start("git_init", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists():
                result = f"ОШИБКА: Директория {directory} не существует"
                log_tool_result(operation, error=result)
                return result
            
            # Проверяем, что это не уже Git репозиторий
            if (path / ".git").exists():
                result = f"ОШИБКА: {directory} уже является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path)
        
        # Формируем команду
        command = ["git", "init"]
        if bare:
            command.append("--bare")
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            repo_type = "bare" if bare else "обычный"
            result = f"✅ Git репозиторий ({repo_type}) успешно инициализирован в {directory}"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при инициализации: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_config(context: RunContextWrapper, directory: str = ".", name: str = "", email: str = "", global_config: bool = False) -> str:
    """
    Настраивает Git конфигурацию (имя пользователя и email).
    
    Args:
        directory: Путь к репозиторию
        name: Имя пользователя
        email: Email пользователя
        global_config: Применить глобально (--global)
        
    Returns:
        str: Результат настройки
    """
    start_time = time.time()
    args = {"directory": directory, "name": name, "email": email, "global_config": global_config}
    operation = log_tool_start("git_config", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not global_config and (not path.exists() or not (path / ".git").exists()):
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path) if not global_config else None
        else:
            if not global_config:
                cwd = None # /workspace
        
        results = []
        
        # Настраиваем имя пользователя
        if name:
            command = ["git", "config"]
            if global_config:
                command.append("--global")
            command.extend(["user.name", name])
            
            cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
            if cmd_result["success"]:
                results.append(f"✅ Имя пользователя установлено: {name}")
            else:
                results.append(f"❌ Ошибка установки имени: {cmd_result['error']}")
        
        # Настраиваем email
        if email:
            command = ["git", "config"]
            if global_config:
                command.append("--global")
            command.extend(["user.email", email])
            
            cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
            if cmd_result["success"]:
                results.append(f"✅ Email установлен: {email}")
            else:
                results.append(f"❌ Ошибка установки email: {cmd_result['error']}")
        
        if not name and not email:
            # Показываем текущую конфигурацию
            command = ["git", "config"]
            if global_config:
                command.append("--global")
            command.extend(["--list"])
            
            cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
            if cmd_result["success"]:
                result = f"Текущая конфигурация Git:\n{cmd_result['output']}"
            else:
                result = f"ОШИБКА при получении конфигурации: {cmd_result['error']}"
        else:
            result = "\n".join(results)
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_add_all(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Добавляет все измененные файлы в индекс Git.
    
    Args:
        directory: Путь к репозиторию
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory}
    operation = log_tool_start("git_add_all", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path)
        
        cmd_result = _run_git_command(["git", "add", "."], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = "✅ Все измененные файлы добавлены в индекс"
        else:
            result = f"ОШИБКА при добавлении файлов: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_push(context: RunContextWrapper, directory: str = ".", remote: str = "origin", branch: str = "") -> str:
    """
    Отправляет изменения в удаленный репозиторий.
    
    Args:
        directory: Путь к репозиторию
        remote: Имя удаленного репозитория
        branch: Имя ветки (если не указана, используется текущая)
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory, "remote": remote, "branch": branch}
    operation = log_tool_start("git_push", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path)
        
        # Формируем команду
        command = ["git", "push", remote]
        if branch:
            command.append(branch)
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Изменения успешно отправлены в {remote}"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при отправке изменений: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_remote_add(context: RunContextWrapper, directory: str, name: str, url: str) -> str:
    """
    Добавляет удаленный репозиторий.
    
    Args:
        directory: Путь к репозиторию
        name: Имя удаленного репозитория
        url: URL удаленного репозитория
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory, "name": name, "url": url}
    operation = log_tool_start("git_remote_add", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path)
        
        # Валидация URL
        if not url.startswith(('http://', 'https://', 'git://', 'ssh://', 'git@')):
            result = f"ОШИБКА: Недопустимый URL репозитория: {url}"
            log_tool_result(operation, error=result)
            return result
        
        cmd_result = _run_git_command(["git", "remote", "add", name, url], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Удаленный репозиторий '{name}' добавлен: {url}"
        else:
            result = f"ОШИБКА при добавлении удаленного репозитория: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_remote_remove(context: RunContextWrapper, directory: str, name: str) -> str:
    """
    Удаляет удаленный репозиторий.
    
    Args:
        directory: Путь к репозиторию
        name: Имя удаленного репозитория
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory, "name": name}
    operation = log_tool_start("git_remote_remove", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path)
        
        cmd_result = _run_git_command(["git", "remote", "remove", name], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Удаленный репозиторий '{name}' удален"
        else:
            result = f"ОШИБКА при удалении удаленного репозитория: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_merge(context: RunContextWrapper, directory: str, branch_name: str, message: str = "") -> str:
    """
    Сливает указанную ветку в текущую.
    
    Args:
        directory: Путь к репозиторию
        branch_name: Имя ветки для слияния
        message: Сообщение коммита слияния (опционально)
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory, "branch_name": branch_name, "message": message}
    operation = log_tool_start("git_merge", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path)
        
        # Формируем команду
        command = ["git", "merge"]
        if message:
            command.extend(["-m", message])
        command.append(branch_name)
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Ветка '{branch_name}' успешно слита"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при слиянии ветки: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_reset(context: RunContextWrapper, directory: str, mode: str = "soft", commit_hash: str = "HEAD~1") -> str:
    """
    Сбрасывает состояние репозитория к указанному коммиту.
    
    Args:
        directory: Путь к репозиторию
        mode: Режим сброса (soft, mixed, hard)
        commit_hash: Хеш коммита для сброса (по умолчанию HEAD~1)
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory, "mode": mode, "commit_hash": commit_hash}
    operation = log_tool_start("git_reset", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path)
        
        # Валидация режима
        valid_modes = ["soft", "mixed", "hard"]
        if mode not in valid_modes:
            result = f"ОШИБКА: Недопустимый режим сброса '{mode}'. Допустимые: {', '.join(valid_modes)}"
            log_tool_result(operation, error=result)
            return result
        
        # Проверяем, что это не hard reset (опасная операция)
        if mode == "hard":
            result = "⚠️  ВНИМАНИЕ: Hard reset может привести к потере данных. Операция заблокирована для безопасности."
            log_tool_result(operation, error=result)
            return result
        
        cmd_result = _run_git_command(["git", "reset", f"--{mode}", commit_hash], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Репозиторий сброшен к коммиту {commit_hash} (режим: {mode})"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при сбросе: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_stash(context: RunContextWrapper, directory: str = ".", action: str = "save", message: str = "") -> str:
    """
    Управляет stash (временным сохранением изменений).
    
    Args:
        directory: Путь к репозиторию
        action: Действие (save, list, pop, apply, drop)
        message: Сообщение для stash (только для save)
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory, "action": action, "message": message}
    operation = log_tool_start("git_stash", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path)
        
        # Формируем команду
        command = ["git", "stash"]
        
        if action == "save":
            if message:
                command.extend(["save", message])
            else:
                command.append("save")
        elif action == "list":
            command.append("list")
        elif action == "pop":
            command.append("pop")
        elif action == "apply":
            command.append("apply")
        elif action == "drop":
            command.append("drop")
        else:
            result = f"ОШИБКА: Недопустимое действие '{action}'. Допустимые: save, list, pop, apply, drop"
            log_tool_result(operation, error=result)
            return result
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            if action == "list":
                if cmd_result["output"]:
                    result = f"Список stash:\n{cmd_result['output']}"
                else:
                    result = "Список stash пуст"
            else:
                result = f"✅ Stash операция '{action}' выполнена успешно"
                if cmd_result["output"]:
                    result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при выполнении stash: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_tag(context: RunContextWrapper, directory: str, tag_name: str, message: str = "", commit_hash: str = "HEAD") -> str:
    """
    Создает тег в репозитории.
    
    Args:
        directory: Путь к репозиторию
        tag_name: Имя тега
        message: Сообщение тега (опционально)
        commit_hash: Хеш коммита для тегирования (по умолчанию HEAD)
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory, "tag_name": tag_name, "message": message, "commit_hash": commit_hash}
    operation = log_tool_start("git_tag", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path)
        
        # Валидация имени тега
        if not re.match(r'^[\wа-яёА-ЯЁ._/-]+$', tag_name):
            result = f"ОШИБКА: Недопустимое имя тега: {tag_name}"
            log_tool_result(operation, error=result)
            return result
        
        # Формируем команду
        command = ["git", "tag"]
        if message:
            command.extend(["-a", tag_name, "-m", message, commit_hash])
        else:
            command.extend([tag_name, commit_hash])
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Тег '{tag_name}' создан для коммита {commit_hash}"
            if message:
                result += f" с сообщением: {message}"
        else:
            result = f"ОШИБКА при создании тега: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_tag_list(context: RunContextWrapper, directory: str = ".") -> str:
    """
    Показывает список тегов в репозитории.
    
    Args:
        directory: Путь к репозиторию
        
    Returns:
        str: Список тегов
    """
    start_time = time.time()
    args = {"directory": directory}
    operation = log_tool_start("git_tag_list", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path)
        
        cmd_result = _run_git_command(["git", "tag", "-l"], cwd=cwd, container_id=container_id, context=context)
        
        if not cmd_result["success"]:
            result = f"ОШИБКА: {cmd_result['error']}"
            log_tool_result(operation, error=result)
            return result
        
        if not cmd_result["output"]:
            result = "Теги не найдены"
        else:
            tags = cmd_result["output"].split('\n')
            formatted_tags = [f"Теги в репозитории {directory}:\n"]
            for tag in tags:
                if tag.strip():
                    formatted_tags.append(f"  🏷️  {tag.strip()}")
            result = "\n".join(formatted_tags)
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_clone(context: RunContextWrapper, directory: str, repository_url: str, branch: str = "") -> str:
    """
    Клонирует удаленный репозиторий.
    
    Args:
        directory: Директория для клонирования
        repository_url: URL репозитория
        branch: Ветка для клонирования (опционально)
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory, "repository_url": repository_url, "branch": branch}
    operation = log_tool_start("git_clone", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if path.exists():
                result = f"ОШИБКА: Директория {directory} уже существует"
                log_tool_result(operation, error=result)
                return result
            cwd = None # clone uses current dir, but here we specify target dir in command
        else:
             # In container, we assume directory is relative to /workspace or absolute
             pass
        
        # Валидация URL
        if not repository_url.startswith(('http://', 'https://', 'git://', 'ssh://', 'git@')):
            result = f"ОШИБКА: Недопустимый URL репозитория: {repository_url}"
            log_tool_result(operation, error=result)
            return result
        
        # Формируем команду
        command = ["git", "clone"]
        if branch:
            command.extend(["-b", branch])
        command.extend([repository_url, directory])
        
        cmd_result = _run_git_command(command, cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Репозиторий успешно клонирован в {directory}"
            if branch:
                result += f" (ветка: {branch})"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при клонировании: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

@function_tool
def git_fetch(context: RunContextWrapper, directory: str = ".", remote: str = "origin") -> str:
    """
    Получает изменения из удаленного репозитория без слияния.
    
    Args:
        directory: Путь к репозиторию
        remote: Имя удаленного репозитория
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory, "remote": remote}
    operation = log_tool_start("git_fetch", **args)
    
    try:
        container_id = _get_container_id(context)
        cwd = None
        if not container_id:
            path = Path(directory)
            if not path.exists() or not (path / ".git").exists():
                result = f"ОШИБКА: {directory} не является Git репозиторием"
                log_tool_result(operation, error=result)
                return result
            cwd = str(path)
        
        cmd_result = _run_git_command(["git", "fetch", remote], cwd=cwd, container_id=container_id, context=context)
        
        if cmd_result["success"]:
            result = f"✅ Изменения получены из {remote}"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при получении изменений: {cmd_result['error']}"
        
        log_tool_result(operation, result=result)
        return result
        
    except Exception as e:
        result = f"ОШИБКА при выполнении операции: {str(e)}"
        log_tool_result(operation, error=result)
        return result

# ============================================================================
# СЛОВАРЬ GIT ИНСТРУМЕНТОВ
# ============================================================================

GIT_TOOLS = {
    # Основные операции
    "git_status": git_status,
    "git_log": git_log,
    "git_diff": git_diff,
    "git_branch_list": git_branch_list,
    "git_add_file": git_add_file,
    "git_add_all": git_add_all,
    "git_commit": git_commit,
    "git_checkout_branch": git_checkout_branch,
    
    # Инициализация и настройка
    "git_init": git_init,
    "git_config": git_config,
    "git_clone": git_clone,
    
    # Удаленные репозитории
    "git_remote_info": git_remote_info,
    "git_remote_add": git_remote_add,
    "git_remote_remove": git_remote_remove,
    "git_fetch": git_fetch,
    "git_pull": git_pull,
    "git_push": git_push,
    
    # Управление ветками и слияние
    "git_merge": git_merge,
    "git_reset": git_reset,
    "git_stash": git_stash,
    
    # Теги
    "git_tag": git_tag,
    "git_tag_list": git_tag_list,
}

def get_git_tools() -> List[Any]:
    """Возвращает список всех Git инструментов."""
    return list(GIT_TOOLS.values())

def get_git_tools_by_names(tool_names: List[str]) -> List[Any]:
    """Возвращает список Git инструментов по их именам."""
    tools = []
    for name in tool_names:
        if name in GIT_TOOLS:
            tools.append(GIT_TOOLS[name])
        else:
            from utils.logger import Logger
            Logger(__name__).warning(f"Git инструмент '{name}' не найден")
    return tools 
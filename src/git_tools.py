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
from pathlib import Path
from typing import List, Dict, Any, Optional
from agents import function_tool
from utils.logger import log_tool_start, log_tool_end, log_tool_error, log_tool_usage

def _run_git_command(command: List[str], cwd: Optional[str] = None) -> Dict[str, Any]:
    """
    Безопасный запуск Git команды с валидацией.
    
    Args:
        command: Список аргументов команды
        cwd: Рабочая директория
        
    Returns:
        Dict с результатом: {"success": bool, "output": str, "error": str}
    """
    try:
        # Проверяем, что команда начинается с git
        if not command or command[0] != "git":
            return {"success": False, "output": "", "error": "Команда должна начинаться с 'git'"}
        
        # Валидация опасных команд
        dangerous_commands = ["rm", "clean", "reset --hard", "push --force", "rebase -i"]
        cmd_str = " ".join(command)
        for dangerous in dangerous_commands:
            # Ищем точные слова, а не подстроки
            if f" {dangerous} " in f" {cmd_str} " or cmd_str.endswith(f" {dangerous}") or cmd_str.startswith(f"{dangerous} "):
                return {"success": False, "output": "", "error": f"Опасная команда заблокирована: {dangerous}"}
        
        # Логгируем выполнение команды
        from .utils.logger import log_custom
        log_custom('debug', 'git_command', f"Выполнение: {' '.join(command)}", cwd=cwd)
        
        # Выполняем команду
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
        from .utils.logger import log_custom
        log_custom('error', 'git_command', f"Таймаут команды: {' '.join(command)}")
        return {"success": False, "output": "", "error": "Команда превысила лимит времени выполнения"}
    except Exception as e:
        from .utils.logger import log_custom
        log_custom('error', 'git_command', f"Ошибка выполнения: {' '.join(command)}", error=str(e))
        return {"success": False, "output": "", "error": f"Ошибка выполнения команды: {str(e)}"}

@function_tool
def git_status(directory: str = ".") -> str:
    """
    Показывает статус Git репозитория.
    
    Args:
        directory: Путь к репозиторию
        
    Returns:
        str: Статус репозитория
    """
    start_time = time.time()
    args = {"directory": directory}
    log_tool_start("git_status", args)
    
    try:
        path = Path(directory)
        if not path.exists():
            result = f"ОШИБКА: Директория {directory} не найдена"
            log_tool_end("git_status", result, time.time() - start_time)
            return result
        
        # Проверяем, что это Git репозиторий
        git_dir = path / ".git"
        if not git_dir.exists():
            result = f"ОШИБКА: {directory} не является Git репозиторием"
            log_tool_end("git_status", result, time.time() - start_time)
            return result
        
        cmd_result = _run_git_command(["git", "status", "--porcelain"], cwd=str(path))
        
        if not cmd_result["success"]:
            result = f"ОШИБКА: {cmd_result['error']}"
            log_tool_end("git_status", result, time.time() - start_time)
            return result
        
        # Форматируем вывод
        if not cmd_result["output"]:
            result = "Рабочая директория чистая - нет изменений"
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
                # Более надежный парсинг - ищем первый пробел после статуса
                status_code = line[:2].strip()
                # Находим начало имени файла после статуса и пробелов
                filename_start = 2
                while filename_start < len(line) and line[filename_start] == ' ':
                    filename_start += 1
                filename = line[filename_start:].strip()
                status_text = status_map.get(status_code, status_code)
                formatted_lines.append(f"  {status_text}: {filename}")
            
            result = f"Статус Git репозитория в {directory}:\n\n" + "\n".join(formatted_lines)
        
        duration = time.time() - start_time
        log_tool_end("git_status", result, duration)
        log_tool_usage("git_status", args, True, duration)
        return result
        
    except Exception as e:
        log_tool_error("git_status", e)
        result = f"ОШИБКА при получении статуса: {str(e)}"
        duration = time.time() - start_time
        log_tool_end("git_status", result, duration)
        log_tool_usage("git_status", args, False, duration)
        return result

@function_tool
def git_log(directory: str = ".", max_commits: int = 10) -> str:
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
    log_tool_start("git_log", args)
    
    try:
        path = Path(directory)
        if not path.exists() or not (path / ".git").exists():
            result = f"ОШИБКА: {directory} не является Git репозиторием"
            log_tool_end("git_log", result, time.time() - start_time)
            return result
        
        # Ограничиваем количество коммитов для безопасности
        max_commits = min(max_commits, 50)
        
        cmd_result = _run_git_command([
            "git", "log", 
            f"--max-count={max_commits}",
            "--pretty=format:%h|%an|%ad|%s",
            "--date=short"
        ], cwd=str(path))
        
        if not cmd_result["success"]:
            result = f"ОШИБКА: {cmd_result['error']}"
            log_tool_end("git_log", result, time.time() - start_time)
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
        log_tool_end("git_log", result, duration)
        log_tool_usage("git_log", args, True, duration)
        return result
        
    except Exception as e:
        log_tool_error("git_log", e)
        result = f"ОШИБКА при получении истории: {str(e)}"
        duration = time.time() - start_time
        log_tool_end("git_log", result, duration)
        log_tool_usage("git_log", args, False, duration)
        return result

@function_tool
def git_diff(directory: str = ".", filename: str = "") -> str:
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
    log_tool_start("git_diff", args)
    
    try:
        path = Path(directory)
        if not path.exists() or not (path / ".git").exists():
            result = f"ОШИБКА: {directory} не является Git репозиторием"
            log_tool_end("git_diff", result, time.time() - start_time)
            return result
        
        # Формируем команду
        command = ["git", "diff"]
        if filename:
            # Валидируем имя файла
            if not re.match(r'^[a-zA-Z0-9._/-]+$', filename):
                result = f"ОШИБКА: Недопустимое имя файла: {filename}"
                log_tool_end("git_diff", result, time.time() - start_time)
                return result
            command.append(filename)
        
        cmd_result = _run_git_command(command, cwd=str(path))
        
        if not cmd_result["success"]:
            result = f"ОШИБКА: {cmd_result['error']}"
            log_tool_end("git_diff", result, time.time() - start_time)
            return result
        
        if not cmd_result["output"]:
            result = "Нет изменений для отображения"
        else:
            # Ограничиваем размер вывода
            output_lines = cmd_result["output"].split('\n')
            if len(output_lines) > 100:
                truncated_output = '\n'.join(output_lines[:100])
                result = f"Различия в {directory}" + (f" для файла {filename}" if filename else "") + ":\n\n" + truncated_output + "\n\n[Вывод обрезан, показаны первые 100 строк]"
            else:
                result = f"Различия в {directory}" + (f" для файла {filename}" if filename else "") + ":\n\n" + cmd_result["output"]
        
        log_tool_end("git_diff", result, time.time() - start_time)
        return result
        
    except Exception as e:
        log_tool_error("git_diff", e)
        result = f"ОШИБКА при получении различий: {str(e)}"
        log_tool_end("git_diff", result, time.time() - start_time)
        return result

@function_tool
def git_branch_list(directory: str = ".") -> str:
    """
    Показывает список веток.
    
    Args:
        directory: Путь к репозиторию
        
    Returns:
        str: Список веток
    """
    start_time = time.time()
    args = {"directory": directory}
    log_tool_start("git_branch_list", args)
    
    try:
        path = Path(directory)
        if not path.exists() or not (path / ".git").exists():
            result = f"ОШИБКА: {directory} не является Git репозиторием"
            log_tool_end("git_branch_list", result, time.time() - start_time)
            return result
        
        cmd_result = _run_git_command(["git", "branch", "-a"], cwd=str(path))
        
        if not cmd_result["success"]:
            result = f"ОШИБКА: {cmd_result['error']}"
            log_tool_end("git_branch_list", result, time.time() - start_time)
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
        
        log_tool_end("git_branch_list", result, time.time() - start_time)
        return result
        
    except Exception as e:
        log_tool_error("git_branch_list", e)
        result = f"ОШИБКА при получении списка веток: {str(e)}"
        log_tool_end("git_branch_list", result, time.time() - start_time)
        return result

@function_tool
def git_add_file(directory: str, filename: str) -> str:
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
    log_tool_start("git_add_file", args)
    
    try:
        path = Path(directory)
        if not path.exists() or not (path / ".git").exists():
            result = f"ОШИБКА: {directory} не является Git репозиторием"
            log_tool_end("git_add_file", result, time.time() - start_time)
            return result
        
        # Валидация имени файла
        if not re.match(r'^[a-zA-Z0-9._/-]+$', filename):
            result = f"ОШИБКА: Недопустимое имя файла: {filename}"
            log_tool_end("git_add_file", result, time.time() - start_time)
            return result
        
        # Проверяем, что файл существует
        file_path = path / filename
        if not file_path.exists():
            result = f"ОШИБКА: Файл {filename} не найден"
            log_tool_end("git_add_file", result, time.time() - start_time)
            return result
        
        cmd_result = _run_git_command(["git", "add", filename], cwd=str(path))
        
        if cmd_result["success"]:
            result = f"✅ Файл {filename} успешно добавлен в индекс"
        else:
            result = f"ОШИБКА при добавлении файла: {cmd_result['error']}"
        
        log_tool_end("git_add_file", result, time.time() - start_time)
        return result
        
    except Exception as e:
        log_tool_error("git_add_file", e)
        result = f"ОШИБКА при добавлении файла: {str(e)}"
        log_tool_end("git_add_file", result, time.time() - start_time)
        return result

@function_tool
def git_commit(directory: str, message: str, author_name: str = "", author_email: str = "") -> str:
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
    log_tool_start("git_commit", args)
    
    try:
        path = Path(directory)
        if not path.exists() or not (path / ".git").exists():
            result = f"ОШИБКА: {directory} не является Git репозиторием"
            log_tool_end("git_commit", result, time.time() - start_time)
            return result
        
        # Валидация сообщения коммита
        if not message or len(message.strip()) < 3:
            result = "ОШИБКА: Сообщение коммита должно содержать минимум 3 символа"
            log_tool_end("git_commit", result, time.time() - start_time)
            return result
        
        if len(message) > 500:
            result = "ОШИБКА: Сообщение коммита слишком длинное (максимум 500 символов)"
            log_tool_end("git_commit", result, time.time() - start_time)
            return result
        
        # Формируем команду
        command = ["git", "commit", "-m", message.strip()]
        
        # Добавляем автора если указан
        if author_name and author_email:
            # Валидация имени автора - поддерживаем кириллицу, латиницу, цифры, пробелы и основные символы
            if not re.match(r'^[\w\sа-яёА-ЯЁ\-\.]+$', author_name):
                result = "ОШИБКА: Недопустимое имя автора (поддерживаются буквы, цифры, пробелы, дефисы и точки)"
                log_tool_end("git_commit", result, time.time() - start_time)
                return result
            
            # Более гибкая валидация email
            if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', author_email):
                result = "ОШИБКА: Недопустимый email автора (формат: user@domain.com)"
                log_tool_end("git_commit", result, time.time() - start_time)
                return result
            
            command.extend(["--author", f"{author_name} <{author_email}>"])
        
        cmd_result = _run_git_command(command, cwd=str(path))
        
        if cmd_result["success"]:
            result = f"✅ Коммит успешно создан: {message[:50]}..."
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при создании коммита: {cmd_result['error']}"
        
        log_tool_end("git_commit", result, time.time() - start_time)
        return result
        
    except Exception as e:
        log_tool_error("git_commit", e)
        result = f"ОШИБКА при создании коммита: {str(e)}"
        log_tool_end("git_commit", result, time.time() - start_time)
        return result

@function_tool
def git_checkout_branch(directory: str, branch_name: str, create_new: bool = False) -> str:
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
    log_tool_start("git_checkout_branch", args)
    
    try:
        path = Path(directory)
        if not path.exists() or not (path / ".git").exists():
            result = f"ОШИБКА: {directory} не является Git репозиторием"
            log_tool_end("git_checkout_branch", result, time.time() - start_time)
            return result
        
        # Валидация имени ветки - поддерживаем кириллицу и основные символы
        if not re.match(r'^[\wа-яёА-ЯЁ._/-]+$', branch_name):
            result = f"ОШИБКА: Недопустимое имя ветки: {branch_name} (поддерживаются буквы, цифры, точки, дефисы, подчеркивания и слеши)"
            log_tool_end("git_checkout_branch", result, time.time() - start_time)
            return result
        
        # Формируем команду
        command = ["git", "checkout"]
        if create_new:
            command.append("-b")
        command.append(branch_name)
        
        cmd_result = _run_git_command(command, cwd=str(path))
        
        if cmd_result["success"]:
            action = "создана и выбрана" if create_new else "выбрана"
            result = f"✅ Ветка {branch_name} {action}"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при переключении на ветку: {cmd_result['error']}"
        
        log_tool_end("git_checkout_branch", result, time.time() - start_time)
        return result
        
    except Exception as e:
        log_tool_error("git_checkout_branch", e)
        result = f"ОШИБКА при переключении на ветку: {str(e)}"
        log_tool_end("git_checkout_branch", result, time.time() - start_time)
        return result

@function_tool
def git_pull(directory: str = ".") -> str:
    """
    Получает и объединяет изменения из удаленного репозитория.
    
    Args:
        directory: Путь к репозиторию
        
    Returns:
        str: Результат операции
    """
    start_time = time.time()
    args = {"directory": directory}
    log_tool_start("git_pull", args)
    
    try:
        path = Path(directory)
        if not path.exists() or not (path / ".git").exists():
            result = f"ОШИБКА: {directory} не является Git репозиторием"
            log_tool_end("git_pull", result, time.time() - start_time)
            return result
        
        cmd_result = _run_git_command(["git", "pull"], cwd=str(path))
        
        if cmd_result["success"]:
            result = "✅ Изменения успешно получены из удаленного репозитория"
            if cmd_result["output"]:
                result += f"\n{cmd_result['output']}"
        else:
            result = f"ОШИБКА при получении изменений: {cmd_result['error']}"
        
        log_tool_end("git_pull", result, time.time() - start_time)
        return result
        
    except Exception as e:
        log_tool_error("git_pull", e)
        result = f"ОШИБКА при получении изменений: {str(e)}"
        log_tool_end("git_pull", result, time.time() - start_time)
        return result

@function_tool
def git_remote_info(directory: str = ".") -> str:
    """
    Показывает информацию о удаленных репозиториях.
    
    Args:
        directory: Путь к репозиторию
        
    Returns:
        str: Информация об удаленных репозиториях
    """
    start_time = time.time()
    args = {"directory": directory}
    log_tool_start("git_remote_info", args)
    
    try:
        path = Path(directory)
        if not path.exists() or not (path / ".git").exists():
            result = f"ОШИБКА: {directory} не является Git репозиторием"
            log_tool_end("git_remote_info", result, time.time() - start_time)
            return result
        
        cmd_result = _run_git_command(["git", "remote", "-v"], cwd=str(path))
        
        if not cmd_result["success"]:
            result = f"ОШИБКА: {cmd_result['error']}"
            log_tool_end("git_remote_info", result, time.time() - start_time)
            return result
        
        if not cmd_result["output"]:
            result = "Удаленные репозитории не настроены"
        else:
            result = f"Удаленные репозитории для {directory}:\n\n{cmd_result['output']}"
        
        log_tool_end("git_remote_info", result, time.time() - start_time)
        return result
        
    except Exception as e:
        log_tool_error("git_remote_info", e)
        result = f"ОШИБКА при получении информации об удаленных репозиториях: {str(e)}"
        log_tool_end("git_remote_info", result, time.time() - start_time)
        return result

# ============================================================================
# СЛОВАРЬ GIT ИНСТРУМЕНТОВ
# ============================================================================

GIT_TOOLS = {
    "git_status": git_status,
    "git_log": git_log,
    "git_diff": git_diff,
    "git_branch_list": git_branch_list,
    "git_add_file": git_add_file,
    "git_commit": git_commit,
    "git_checkout_branch": git_checkout_branch,
    "git_pull": git_pull,
    "git_remote_info": git_remote_info,
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
            print(f"⚠️  Git инструмент '{name}' не найден")
    return tools 
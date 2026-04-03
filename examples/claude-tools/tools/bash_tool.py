"""
BashTool — выполнение shell-команд с изоляцией по рабочей директории.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import function_tool
from utils.path_utils import resolve_agent_path_auto, display_agent_path_auto


# ---------------------------------------------------------------------------
# Dangerous-command patterns (Linux + Windows)
# ---------------------------------------------------------------------------
DANGEROUS_PATTERNS = [
    # Linux: удаление системных директорий
    r"rm\s+-[rf]{1,2}\s+/\s*$",
    r"rm\s+-[rf]{1,2}\s+/\s+",
    r"rm\s+-[rf]{1,2}\s+/(bin|boot|etc|lib|sbin|sys|usr|var)\b",
    r"rm\s+-[rf]{1,2}\s+~",
    r"rm\s+-[rf]{1,2}\s+\$HOME",
    # Fork bomb
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    # Перезапись критических файлов
    r">\s*/etc/(passwd|shadow|sudoers|crontab)",
    # dd в устройства
    r"dd\s+.*of=/dev/(sd|hd|nvme|disk)",
    # mkfs на разделах
    r"mkfs\b.*\s+/dev/",
    # Windows: опасные команды
    r"format\s+[a-zA-Z]:\s*(/[a-zA-Z])*\s*$",
    r"del\s+/[fFsS].*\s+[a-zA-Z]:\\",
    r"rd\s+/[sS]\s+/[qQ]\s+[a-zA-Z]:\\",
    r"rmdir\s+/[sS]\s+/[qQ]\s+[a-zA-Z]:\\",
    r"reg\s+(delete|add)\s+HKLM",
    r"bcdedit",
    r"diskpart",
]

DANGEROUS_REGEX = [re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS]

CAUTION_COMMANDS = {
    "rm", "rmdir", "mv", "cp", "dd", "mkfs", "fdisk", "parted",
    # Windows
    "del", "rd", "format", "move", "xcopy", "robocopy", "reg",
}

# sudo/doas prefixes that should be stripped before extracting the base command
PRIVILEGE_ESCALATION = {"sudo", "su", "doas", "run-as", "runas"}


def _extract_base_command(command: str) -> str:
    """Extract actual command name, skipping any privilege-escalation prefix."""
    parts = command.strip().lower().split()
    if not parts:
        return ""
    idx = 0
    while idx < len(parts) and parts[idx] in PRIVILEGE_ESCALATION:
        idx += 1
    cmd = parts[idx] if idx < len(parts) else ""
    # Strip directory path component (e.g. /bin/rm → rm, C:\Windows\rm.exe → rm)
    cmd = re.split(r"[/\\]", cmd)[-1]
    # Strip .exe/.bat suffix on Windows
    cmd = re.sub(r"\.(exe|bat|cmd|ps1)$", "", cmd)
    return cmd


def _is_dangerous(command: str) -> Optional[str]:
    """Return reason string if command is dangerous, else None."""
    for pattern in DANGEROUS_REGEX:
        if pattern.search(command):
            return f"Обнаружен опасный паттерн: {pattern.pattern}"
    return None


def _is_caution(command: str) -> bool:
    return _extract_base_command(command) in CAUTION_COMMANDS


def _truncate(text: str, max_chars: int = 10000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n... [обрезано {len(text) - max_chars} символов] ..."


def _detect_encoding() -> str:
    """Return the appropriate encoding for subprocess output on this platform."""
    if sys.platform == "win32":
        # cmd.exe outputs in the OEM code page (CP866 for Russian Windows)
        return "oem"
    return "utf-8"


def _run_command(
    command: str, work_path: Path, timeout: int
) -> tuple:
    """
    Run *command* inside *work_path* and return (returncode, stdout, stderr).
    Uses subprocess.run so the approach is simple and encoding-correct.
    """
    encoding = _detect_encoding()

    if sys.platform == "win32":
        # cmd.exe needs double-quoted paths and /d to change drives.
        # Strip embedded double-quotes from the path to avoid injection.
        safe_path = str(work_path).replace('"', "")
        shell_cmd = f'cd /d "{safe_path}" && {command}'
    else:
        # shlex.quote produces single-quoted strings, valid for sh/bash.
        import shlex
        shell_cmd = f"cd {shlex.quote(str(work_path))} && {command}"

    try:
        result = subprocess.run(
            shell_cmd,
            shell=True,
            capture_output=True,
            timeout=timeout,
            encoding=encoding,
            errors="replace",
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"Команда превысила таймаут {timeout} секунд")


@function_tool
def bash_tool(
    command: str,
    working_dir: str = ".",
    timeout: int = 60,
    description: str = "",
) -> str:
    """
    Выполняет shell-команду в рабочей директории с проверкой безопасности.

    Args:
        command:     Команда для выполнения
        working_dir: Рабочая директория (по умолчанию текущая)
        timeout:     Таймаут в секундах (1–300)
        description: Описание команды для вывода

    Returns:
        Результат выполнения (stdout + stderr)
    """
    # --- Validate timeout --------------------------------------------------
    timeout = max(1, min(int(timeout), 300))

    # --- Security check ----------------------------------------------------
    danger_reason = _is_dangerous(command)
    if danger_reason:
        return f"❌ Команда заблокирована: {danger_reason}"

    caution_prefix = "⚠️ Потенциально опасная команда\n" if _is_caution(command) else ""

    # --- Resolve working directory (sandbox enforced) ----------------------
    visible_dir = display_agent_path_auto(working_dir)
    try:
        resolved_dir = resolve_agent_path_auto(working_dir)
    except ValueError as exc:
        return f"❌ {exc}"

    work_path = Path(resolved_dir)
    if not work_path.exists():
        return f"❌ Рабочая директория не существует: {visible_dir}"
    if not work_path.is_dir():
        return f"❌ Путь не является директорией: {visible_dir}"

    # --- Execute -----------------------------------------------------------
    try:
        returncode, stdout, stderr = _run_command(command, work_path, timeout)
    except TimeoutError as exc:
        return f"⏱️ {exc}"
    except Exception as exc:
        return f"❌ Ошибка выполнения: {exc}"

    # --- Format output -----------------------------------------------------
    parts = []
    if description:
        parts.append(f"📋 {description}")
    parts.append(f"$ {command}")
    if stdout:
        parts.append(_truncate(stdout))
    if stderr:
        parts.append(f"stderr:\n{_truncate(stderr)}")
    if returncode != 0:
        parts.append(f"⚠️ Exit code: {returncode}")

    return caution_prefix + "\n\n".join(parts)

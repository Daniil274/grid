"""
BashTool — executes shell commands with isolation by working directory.
"""

import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agents import function_tool
from utils.path_utils import display_agent_path_auto, get_current_factory, resolve_agent_path_auto


# ---------------------------------------------------------------------------
# Dangerous-command patterns (Linux + Windows)
# ---------------------------------------------------------------------------
DANGEROUS_PATTERNS = [
    # Linux: deleting system directories
    r"rm\s+-[rf]{1,2}\s+/\s*$",
    r"rm\s+-[rf]{1,2}\s+/\s+",
    r"rm\s+-[rf]{1,2}\s+/(bin|boot|etc|lib|sbin|sys|usr|var)\b",
    r"rm\s+-[rf]{1,2}\s+~",
    r"rm\s+-[rf]{1,2}\s+\$HOME",
    # Fork bomb
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    # Overwriting critical files
    r">\s*/etc/(passwd|shadow|sudoers|crontab)",
    # dd to devices
    r"dd\s+.*of=/dev/(sd|hd|nvme|disk)",
    # mkfs on partitions
    r"mkfs\b.*\s+/dev/",
    # Windows: dangerous commands
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
            return f"Dangerous pattern detected: {pattern.pattern}"
    return None


def _is_caution(command: str) -> bool:
    return _extract_base_command(command) in CAUTION_COMMANDS


def _truncate(text: str, max_chars: int = 10000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n... [truncated {len(text) - max_chars} characters] ..."


def _detect_encoding() -> str:
    """Return the appropriate encoding for subprocess output on this platform."""
    if sys.platform == "win32":
        # cmd.exe outputs in the OEM code page (CP866 for Russian Windows)
        return "oem"
    return "utf-8"


#: Where the host workspace is mounted in a run's container.
CONTAINER_ROOT = "/workspace"


def _container_command(command: str, container_id: str, relative_dir: str, timeout: int) -> list:
    """``docker exec`` of *command* in the container, in the workspace
    directory *relative_dir*; ``timeout`` ends it inside the container too."""
    workdir = CONTAINER_ROOT if relative_dir in ("", ".") else f"{CONTAINER_ROOT}/{relative_dir}"
    return [
        "docker", "exec", "-i", "-w", workdir, container_id,
        "timeout", "-k", "5", str(timeout), "sh", "-c", command,
    ]


def _run_in_container(command: str, container_id: str, relative_dir: str, timeout: int) -> tuple:
    """Run *command* in the run's container; (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            _container_command(command, container_id, relative_dir, timeout),
            capture_output=True,
            timeout=timeout + 15,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"Command exceeded timeout of {timeout} seconds")
    if result.returncode == 124:
        raise TimeoutError(f"Command exceeded timeout of {timeout} seconds")
    return result.returncode, result.stdout, result.stderr


def _run_command(
    command: str, work_path: Path, timeout: int
) -> tuple:
    """
    Run *command* inside *work_path* on this machine and return (returncode, stdout, stderr).
    Only for runs without a container (see bash_tool).
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
        raise TimeoutError(f"Command exceeded timeout of {timeout} seconds")


@function_tool
def bash_tool(
    command: str,
    working_dir: str = ".",
    timeout: int = 60,
    description: str = "",
) -> str:
    """
    Executes shell commands in the working directory with security checks.

    Args:
        command:     Command to execute
        working_dir: Working directory (default current)
        timeout:     Timeout in seconds (1–300)
        description: Command description for display

    Returns:
        Execution result (stdout + stderr)
    """
    # --- Validate timeout --------------------------------------------------
    timeout = max(1, min(int(timeout), 300))

    # --- Security check ----------------------------------------------------
    danger_reason = _is_dangerous(command)
    if danger_reason:
        return f"❌ Command blocked: {danger_reason}"

    caution_prefix = "⚠️ Potentially dangerous command\n" if _is_caution(command) else ""

    # --- Resolve working directory (sandbox enforced) ----------------------
    visible_dir = display_agent_path_auto(working_dir)
    try:
        resolved_dir = resolve_agent_path_auto(working_dir)
    except ValueError as exc:
        return f"❌ {exc}"

    work_path = Path(resolved_dir)
    if not work_path.exists():
        return f"❌ Working directory does not exist: {visible_dir}"
    if not work_path.is_dir():
        return f"❌ Path is not a directory: {visible_dir}"

    # --- Execute: in the run's container when it has one, else here -------
    container_id = getattr(get_current_factory(), "container_id", None)
    try:
        if container_id:
            relative_dir = display_agent_path_auto(working_dir)
            returncode, stdout, stderr = _run_in_container(command, container_id, relative_dir, timeout)
        else:
            returncode, stdout, stderr = _run_command(command, work_path, timeout)
    except TimeoutError as exc:
        return f"⏱️ {exc}"
    except Exception as exc:
        return f"❌ Execution error: {exc}"

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


# Where these tools act (utils.tool_isolation): commands run in the run's container when there is one
from utils.tool_isolation import CONTAINER as _CONTAINER  # noqa: E402

TOOL_ISOLATION = {
    "bash_tool": _CONTAINER,
}

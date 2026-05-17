"""
Shell command execution. Token-aware output truncation per research recommendations.
"""

import subprocess
from agents import function_tool

_MAX_CHARS = 6000
_PS_ENCODING_PREFIX = (
    "$utf8 = [System.Text.UTF8Encoding]::new($false); "
    "[Console]::OutputEncoding = $utf8; "
    "$OutputEncoding = $utf8; "
)

_DESTRUCTIVE = {
    "remove-item", "del ", "rmdir", "rd ", "format ",
    "reg delete", "stop-service", "stop-process", "taskkill",
    "net stop", "clear-content", "disable-", "uninstall-",
}


@function_tool
def shell(command: str, interpreter: str = "powershell", timeout: int = 30) -> str:
    """
    Execute a shell command and return its output.

    Use for: listing files, reading text files, querying processes, running scripts,
    launching applications (Start-Process), checking environment state.

    Safety: Confirm with the user before running any destructive command
    (remove-item, del, format, reg delete, stop-process, etc.).

    Args:
        command:     The command to execute.
        interpreter: "powershell" (default) or "cmd".
        timeout:     Max seconds to wait (default 30, max 120).

    Examples:
        shell("Get-ChildItem $env:USERPROFILE\\Desktop")
        shell("Get-Content C:\\path\\to\\file.txt")
        shell("Start-Process notepad")
        shell("Get-Process | Sort-Object CPU -Descending | Select-Object -First 10")
        shell("dir /b C:\\Users", interpreter="cmd")
    """
    timeout = min(max(timeout, 1), 120)

    if any(kw in command.lower() for kw in _DESTRUCTIVE):
        return (
            "SAFETY STOP: this command looks destructive. "
            "Explain what it will do and get user confirmation before proceeding."
        )

    args = (
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_ENCODING_PREFIX + command]
        if interpreter == "powershell"
        else ["cmd", "/c", command]
    )

    try:
        result = subprocess.run(
            args, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        out = ((result.stdout or "") + (result.stderr or "")).strip()
        if not out:
            return f"Command completed (exit {result.returncode}), no output."
        if len(out) > _MAX_CHARS:
            out = out[:_MAX_CHARS] + f"\n[...output truncated at {_MAX_CHARS} chars]"
        return out
    except subprocess.TimeoutExpired:
        return f"Error: timed out after {timeout}s."
    except Exception as e:
        return f"Error: {e}"

"""Shared helpers for the video-editor example tools."""

from __future__ import annotations

import base64
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.path_utils import display_agent_path_auto, resolve_agent_path_auto

from agents.tool import ToolOutputFileContent


class VideoToolError(RuntimeError):
    """Expected tool-level failure that should be shown to the agent."""


def resolve_path(path: str, *, must_exist: bool = False) -> Path:
    """Resolve an agent path inside the configured working directory."""
    try:
        resolved = Path(resolve_agent_path_auto(path)).resolve()
    except Exception as exc:
        raise VideoToolError(f"Path error for {path!r}: {exc}") from exc

    if must_exist and not resolved.exists():
        raise VideoToolError(f"File does not exist: {display_path(path)}")
    return resolved


def display_path(path: str | Path) -> str:
    """Return an agent-visible path without leaking the host workspace."""
    try:
        return display_agent_path_auto(str(path))
    except Exception:
        return str(path).replace("\\", "/")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def which(binary: str) -> str:
    exe = shutil.which(binary)
    if not exe:
        raise VideoToolError(
            f"Required executable not found: {binary}. Install FFmpeg and ensure it is on PATH."
        )
    return exe


def run_process(args: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    """Run ffmpeg/ffprobe and return a completed process or raise a concise error."""
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout)),
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoToolError(f"Command timed out after {timeout}s: {' '.join(args[:3])}") from exc
    except OSError as exc:
        raise VideoToolError(f"Command failed to start: {exc}") from exc


def require_success(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode == 0:
        return
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    message = stderr or stdout or f"exit code {result.returncode}"
    if len(message) > 4000:
        message = message[-4000:]
    raise VideoToolError(f"{action} failed: {message}")


def probe_media(path: Path, *, timeout: int = 60) -> dict[str, Any]:
    ffprobe = which("ffprobe")
    result = run_process(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=timeout,
    )
    require_success(result, "ffprobe")
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise VideoToolError(f"ffprobe returned invalid JSON: {exc}") from exc


def duration_seconds(probe: dict[str, Any]) -> float:
    raw = (probe.get("format") or {}).get("duration")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def video_stream(probe: dict[str, Any]) -> dict[str, Any]:
    for stream in probe.get("streams") or []:
        if stream.get("codec_type") == "video":
            return stream
    return {}


def audio_streams(probe: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in (probe.get("streams") or []) if s.get("codec_type") == "audio"]


def parse_time(value: str | int | float) -> float:
    """Parse seconds, MM:SS, or HH:MM:SS.mmm into seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        raise VideoToolError("Empty time value")
    try:
        return float(text)
    except ValueError:
        pass

    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise VideoToolError(f"Invalid time value: {value!r}")
    try:
        nums = [float(part) for part in parts]
    except ValueError as exc:
        raise VideoToolError(f"Invalid time value: {value!r}") from exc
    if len(nums) == 2:
        minutes, seconds = nums
        return minutes * 60 + seconds
    hours, minutes, seconds = nums
    return hours * 3600 + minutes * 60 + seconds


def format_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - hours * 3600 - minutes * 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def format_srt_time(seconds: float) -> str:
    text = format_time(seconds)
    return text.replace(".", ",")


def read_json_arg(value: str, *, default_name: str = "input") -> Any:
    """Read JSON either directly from a string or from a workspace path."""
    raw = (value or "").strip()
    if not raw:
        raise VideoToolError(f"{default_name} is empty")

    if raw[0] not in "[{":
        candidate = resolve_path(raw, must_exist=True)
        try:
            raw = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise VideoToolError(f"Cannot read JSON file {display_path(candidate)}: {exc}") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VideoToolError(f"Invalid JSON in {default_name}: {exc}") from exc


def write_json(path: Path, payload: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def encode_image_data_uri(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def make_video_output(path: Path, *, max_inline_mb: int = 20) -> Any | None:
    """
    Return a model-visible file output for rendered video media.

    The current OpenAI Agents SDK has no ToolOutputVideo type. Its structured
    tool-output union accepts video artifacts as ToolOutputFileContent.
    """
    if not path.exists() or not path.is_file():
        return None

    max_bytes = max(1, int(max_inline_mb)) * 1024 * 1024
    if path.stat().st_size > max_bytes:
        return None

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return ToolOutputFileContent(file_data=encoded, filename=path.name)


def choose_tile(count: int) -> tuple[int, int]:
    count = max(1, int(count))
    cols = min(5, max(1, math.ceil(math.sqrt(count))))
    rows = math.ceil(count / cols)
    return cols, rows


def clean_filename(value: str, fallback: str = "item") -> str:
    text = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)
    text = text.strip("._")
    return text or fallback

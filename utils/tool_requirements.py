"""What a tool needs from its environment, declared next to the tool.

A tool module lists its needs in a module-level ``TOOL_REQUIREMENTS`` dict:

    TOOL_REQUIREMENTS = {
        "video_probe": Requires(programs=("ffprobe",), hint="Install FFmpeg"),
    }

The health check (``core.tool_check``) reads these before any agent runs and
explains what is missing. Nothing here disables a tool: an agent keeps every
tool it was given, the person is told in advance which ones will fail and why.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

#: A reachability probe must not hold up startup.
SERVICE_PROBE_TIMEOUT_SECONDS = 1.5
#: Probe results are reused for this long, so several tools sharing one service cost one request.
SERVICE_PROBE_TTL_SECONDS = 60.0

_PLATFORM_NAMES = {"win32": "Windows", "linux": "Linux", "darwin": "macOS"}
_service_cache: dict[str, Tuple[float, Optional[str]]] = {}


@dataclass(frozen=True)
class Requires:
    """Environment a tool needs. Every field is optional; all given ones must hold."""

    #: Programs that must all be on PATH.
    programs: Tuple[str, ...] = ()
    #: Alternatives: at least one of these programs must be on PATH.
    any_program: Tuple[str, ...] = ()
    #: Environment variables that must be set and non-empty.
    env: Tuple[str, ...] = ()
    #: Python modules that must be importable.
    modules: Tuple[str, ...] = ()
    #: ``sys.platform`` prefix the tool works on, e.g. ``"win32"``.
    platform: str = ""
    #: HTTP service the tool calls: (env var holding its URL, default URL).
    service: Optional[Tuple[str, str]] = None
    #: Custom probe returning a reason when the tool cannot work, else None.
    check: Optional[Callable[[], Optional[str]]] = None
    #: How to make the tool available; shown next to every reason.
    hint: str = ""


def unmet(requires: Requires) -> List[str]:
    """Reasons *requires* does not hold in this process; empty when it does."""
    reasons: List[str] = []
    if requires.platform and not sys.platform.startswith(requires.platform):
        wanted = _PLATFORM_NAMES.get(requires.platform, requires.platform)
        current = _PLATFORM_NAMES.get(sys.platform, sys.platform)
        reasons.append(f"works only on {wanted}, this is {current}")
    missing_programs = [name for name in requires.programs if shutil.which(name) is None]
    if missing_programs:
        reasons.append(f"program not on PATH: {', '.join(missing_programs)}")
    if requires.any_program and not any(shutil.which(name) for name in requires.any_program):
        reasons.append(f"none of these programs is on PATH: {', '.join(requires.any_program)}")
    missing_env = [name for name in requires.env if not os.environ.get(name)]
    if missing_env:
        reasons.append(f"environment variable not set: {', '.join(missing_env)}")
    missing_modules = [name for name in requires.modules if not _importable(name)]
    if missing_modules:
        reasons.append(f"Python package not installed: {', '.join(missing_modules)}")
    if requires.service:
        env_name, default_url = requires.service
        url = os.environ.get(env_name) or default_url
        problem = probe_service(url)
        if problem:
            reasons.append(f"service {url} ({env_name}) is not reachable: {problem}")
    if requires.check:
        try:
            problem = requires.check()
        except Exception as exc:  # a broken probe is itself worth reporting
            problem = f"availability check failed: {exc}"
        if problem:
            reasons.append(problem)
    return reasons


def probe_service(url: str) -> Optional[str]:
    """Why *url* does not answer over HTTP, or None when it does (any status counts)."""
    now = time.monotonic()
    cached = _service_cache.get(url)
    if cached and now - cached[0] < SERVICE_PROBE_TTL_SECONDS:
        return cached[1]
    problem: Optional[str] = None
    try:
        # A direct connection: a local service must not be looked up through a proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=SERVICE_PROBE_TIMEOUT_SECONDS):
            pass
    except urllib.error.HTTPError:
        problem = None  # it answered; the status is the tool's business
    except Exception as exc:
        problem = str(getattr(exc, "reason", None) or exc) or type(exc).__name__
    _service_cache[url] = (now, problem)
    return problem


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False

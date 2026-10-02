"""Tool packages: tools written as plain Python, run only where the agents' commands run.

A tool of a Grid system usually is Python loaded into the server (``tools/``,
``project_tools``): it runs in the server process, on the host. That is fine for
tools a person wrote and reviewed, and wrong for tools a system writes for
itself. A *tool package* is how such tools are made safely:

- it is a directory of modules whose ``@tool`` functions are the tools
  (the runtime, :mod:`core.tool_packages.grid_tool`, documents the format),
  with ``test_*.py`` beside them and an optional ``requirements.txt`` of pinned
  packages;
- a config declares it on an MCP tool, relative to the config file::

      tools:
        words:
          type: mcp
          tool_package: tools/words
          description: Count and compare words in workspace files

- the server never imports it. It reads it with :mod:`ast` to check it and to
  show it (:func:`analyze`), and copies it, with the runtime, into the user's
  container, where it installs the requirements and starts it as an MCP server
  (:func:`ensure_in_container`) - so its code runs in the sandbox that already
  runs the agents' shell commands, and every call passes the action policy
  like any MCP call;
- its tests run in a throwaway container without network (:func:`sandbox_test`).

Only a one-user server without isolation runs a package on the host
(:func:`ensure_on_host`), as it runs every other command of its agents.

A deployed package lives in a directory named by the hash of its content, so a
changed package is a new directory and an unchanged one is copied once.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("grid.tool_packages")

RUNTIME = Path(__file__).with_name("grid_tool.py")
#: Where packages are deployed inside a container; its own directory per content hash.
CONTAINER_ROOT = "/opt/grid-tools"
#: The container user the tools run as (docker/agent/Dockerfile).
CONTAINER_USER = "agent"
HOST_CACHE = Path.home() / ".grid" / "tool-packages"

MAX_FILES = 60
MAX_FILE_BYTES = 512 * 1024
MAX_PACKAGE_BYTES = 2 * 1024 * 1024
INSTALL_TIMEOUT = 300.0
TEST_TIMEOUT = 300.0
TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
#: A requirement pinned to one version, from the package index: nothing else is installed.
REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9.!+_-]+$")
SKIPPED_DIRS = {"__pycache__", ".pytest_cache"}


class PackageError(RuntimeError):
    """A package that cannot be deployed or tested; the message is for a person."""


# -- where a package is -----------------------------------------------------------------
def package_dir(config_dir: Path, relative: str) -> Path:
    """The package a config names, which must stay inside the config's directory.

    A system carries its tools with it: copying or publishing it moves them too.
    """
    base = Path(config_dir).resolve()
    path = (base / relative).resolve()
    if path != base and base not in path.parents:
        raise PackageError(f"tool_package '{relative}' leaves the system's directory")
    return path


def files_of(package: Path) -> List[Path]:
    """The package's files, relative, sorted; links and caches left out."""
    found = []
    for path in sorted(package.rglob("*")):
        relative = path.relative_to(package)
        if any(part in SKIPPED_DIRS for part in relative.parts) or path.is_dir():
            continue
        found.append(relative)
    return found


def digest(package: Path) -> str:
    """The hash of the package and the runtime: a deployment's name."""
    sha = hashlib.sha256(RUNTIME.read_bytes())
    for relative in files_of(package):
        path = package / relative
        if path.is_symlink():
            continue
        sha.update(relative.as_posix().encode())
        sha.update(b"\0")
        sha.update(path.read_bytes())
    return sha.hexdigest()[:20]


# -- reading a package without running it ------------------------------------------------
@dataclass
class ToolInfo:
    name: str
    module: str
    description: str
    params: List[Dict[str, Any]]
    read_only: bool = False
    destructive: bool = False
    open_world: bool = False


@dataclass
class PackageInfo:
    path: Path
    files: List[Dict[str, Any]] = field(default_factory=list)
    tools: List[ToolInfo] = field(default_factory=list)
    requirements: List[str] = field(default_factory=list)
    tests: int = 0
    issues: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": str(self.path),
            "files": self.files,
            "tools": [tool.__dict__ for tool in self.tools],
            "requirements": self.requirements,
            "tests": self.tests,
            "issues": self.issues,
        }


def _decorator(node: ast.expr) -> Optional[ast.expr]:
    """The ``tool`` decorator among a function's, as written: ``tool``, ``tool(...)``, ``grid_tool.tool``."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name) and target.id == "tool":
        return node
    if isinstance(target, ast.Attribute) and target.attr == "tool" and isinstance(target.value, ast.Name) and target.value.id == "grid_tool":
        return node
    return None


def _flags(decorator: ast.expr) -> Dict[str, Any]:
    if not isinstance(decorator, ast.Call):
        return {}
    flags = {}
    for keyword in decorator.keywords:
        if keyword.arg and isinstance(keyword.value, ast.Constant):
            flags[keyword.arg] = keyword.value.value
    return flags


def analyze(package: Path) -> PackageInfo:
    """What a package holds and what is wrong with it, read with :mod:`ast` only."""
    info = PackageInfo(path=Path(package))
    if not info.path.is_dir():
        info.issues.append(f"the package directory {info.path} does not exist")
        return info
    files = files_of(info.path)
    total = 0
    for relative in files:
        path = info.path / relative
        size = path.stat().st_size if not path.is_symlink() else 0
        total += size
        info.files.append({"path": relative.as_posix(), "bytes": size})
        if path.is_symlink():
            info.issues.append(f"{relative}: links are not copied; put the file itself in the package")
        elif size > MAX_FILE_BYTES:
            info.issues.append(f"{relative}: {size} bytes, over the {MAX_FILE_BYTES} a file may have")
    if len(files) > MAX_FILES:
        info.issues.append(f"{len(files)} files, over the {MAX_FILES} a package may have")
    if total > MAX_PACKAGE_BYTES:
        info.issues.append(f"{total} bytes in all, over the {MAX_PACKAGE_BYTES} a package may have")

    names: Dict[str, str] = {}
    unreadable = False
    for relative in files:
        if relative.suffix != ".py" or len(relative.parts) != 1 or relative.name.startswith("_"):
            continue
        try:
            tree = ast.parse((info.path / relative).read_text(encoding="utf-8"), filename=str(relative))
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            info.issues.append(f"{relative}: {exc}")
            unreadable = True
            continue
        if relative.name.startswith("test_"):
            info.tests += sum(
                1
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
            )
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorator = next((found for found in map(_decorator, node.decorator_list) if found is not None), None)
            if decorator is None:
                continue
            flags = _flags(decorator)
            name = flags.get("name") or node.name
            where = f"{relative}: tool '{name}'"
            if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
                info.issues.append(f"{where}: a name is letters, digits and _, starting with a letter")
                continue
            if name in names:
                info.issues.append(f"{where}: also defined in {names[name]}")
                continue
            names[name] = relative.as_posix()
            docstring = ast.get_docstring(node) or ""
            if not docstring.strip():
                info.issues.append(f"{where}: no docstring - the agent reads it to know what the tool does")
            if node.args.vararg or node.args.kwarg:
                info.issues.append(f"{where}: *args and **kwargs cannot be described to an agent")
            positional = node.args.posonlyargs + node.args.args
            defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
            params = [
                {
                    "name": arg.arg,
                    "annotation": ast.unparse(arg.annotation) if arg.annotation is not None else "",
                    "required": default is None,
                }
                for arg, default in zip(positional, defaults)
            ] + [
                {
                    "name": arg.arg,
                    "annotation": ast.unparse(arg.annotation) if arg.annotation is not None else "",
                    "required": default is None,
                }
                for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults)
            ]
            info.tools.append(
                ToolInfo(
                    name=name,
                    module=relative.stem,
                    description=docstring.strip().split("\n\n")[0],
                    params=params,
                    read_only=bool(flags.get("read_only")),
                    destructive=bool(flags.get("destructive")),
                    open_world=bool(flags.get("open_world")),
                )
            )
    if not info.tools and not unreadable:
        info.issues.append("no @tool functions: decorate the functions the agent may call with @tool")

    requirements = info.path / "requirements.txt"
    if requirements.is_file():
        for number, line in enumerate(requirements.read_text(encoding="utf-8").splitlines(), 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if REQUIREMENT.fullmatch(line):
                info.requirements.append(line)
            else:
                info.issues.append(
                    f"requirements.txt:{number}: '{line}' - pin one version from the package index, like name==1.2.3"
                )
    return info


def _archive(package: Path, name: str) -> bytes:
    """A tar of the runtime and the package under *name*/, owned by root, read-only to others."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:

        def add_dir(arcname: str) -> None:
            entry = tarfile.TarInfo(arcname)
            entry.type, entry.mode, entry.mtime = tarfile.DIRTYPE, 0o755, int(time.time())
            tar.addfile(entry)

        def add_file(arcname: str, data: bytes) -> None:
            entry = tarfile.TarInfo(arcname)
            entry.size, entry.mode, entry.mtime = len(data), 0o644, int(time.time())
            tar.addfile(entry, io.BytesIO(data))

        add_dir(name)
        add_dir(f"{name}/pkg")
        add_file(f"{name}/grid_tool.py", RUNTIME.read_bytes())
        made = set()
        for relative in files_of(package):
            path = package / relative
            if path.is_symlink():
                continue
            for parent in reversed(relative.parents[:-1]):
                if parent not in made:
                    made.add(parent)
                    add_dir(f"{name}/pkg/{parent.as_posix()}")
            add_file(f"{name}/pkg/{relative.as_posix()}", path.read_bytes())
    return buffer.getvalue()


# -- deployed ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Deployment:
    """Where a deployed package is, and how to start its server."""

    root: str
    python: str
    #: In a container (Linux paths) rather than on the host.
    in_container: bool = True

    @property
    def script(self) -> str:
        return f"{self.root}/grid_tool.py"

    @property
    def package(self) -> str:
        return f"{self.root}/pkg"

    @property
    def pythonpath(self) -> str:
        separator = ":" if self.in_container else os.pathsep
        return separator.join([self.root, f"{self.root}/deps", self.package])

    def command(self, mode: str = "serve") -> List[str]:
        return [self.python, self.script, mode, self.package]

    def env(self) -> Dict[str, str]:
        return {"PYTHONPATH": self.pythonpath, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}


async def _run(args: Sequence[str], *, stdin: Optional[bytes] = None, timeout: float = 60.0) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(stdin), timeout)
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise PackageError(f"'{' '.join(args[:4])} …' took longer than {timeout:.0f}s") from None
    return process.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


_deploying: Dict[tuple, asyncio.Lock] = {}


async def ensure_in_container(container: str, package: Path, *, timeout: float = INSTALL_TIMEOUT) -> Deployment:
    """Copy *package* into the container and install its requirements, once per content."""
    info = analyze(package)
    if info.issues:
        raise PackageError("; ".join(info.issues[:3]))
    name = digest(package)
    deployment = Deployment(root=f"{CONTAINER_ROOT}/{name}", python="python3")
    lock = _deploying.setdefault((container, name), asyncio.Lock())
    async with lock:
        code, _, _ = await _run(["docker", "exec", container, "test", "-f", f"{deployment.root}/.ready"])
        if code == 0:
            return deployment
        started = time.monotonic()
        code, _, err = await _run(["docker", "exec", "-u", "0", container, "mkdir", "-p", CONTAINER_ROOT])
        if code != 0:
            raise PackageError(f"cannot prepare {CONTAINER_ROOT} in the container: {err.strip()}")
        code, _, err = await _run(
            ["docker", "cp", "-", f"{container}:{CONTAINER_ROOT}"], stdin=_archive(package, name), timeout=120
        )
        if code != 0:
            raise PackageError(f"cannot copy the package into the container: {err.strip()}")
        if info.requirements:
            deps = f"{deployment.root}/deps"
            # The runtime drops CAP_CHOWN even for uid 0. Only the unprivileged
            # agent runs in this container, so make this one deps directory
            # writable without changing the ownership of the package code.
            code, _, err = await _run([
                "docker", "exec", "-u", "0", container, "sh", "-c",
                f"mkdir -p {deps} && chmod 0777 {deps}",
            ])
            if code != 0:
                raise PackageError(f"cannot prepare dependencies in the container: {err.strip()}")
            code, out, err = await _run(
                [
                    "docker", "exec", "-u", CONTAINER_USER, container,
                    "python3", "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check",
                    "--no-input", "--target", deps, *info.requirements,
                ],
                timeout=timeout,
            )
            if code != 0:
                raise PackageError(f"installing {', '.join(info.requirements)} failed: {(err or out).strip()[-1500:]}")
        await _run(["docker", "exec", "-u", "0", container, "touch", f"{deployment.root}/.ready"])
        logger.info("Tool package %s deployed in %s in %.1fs", name, container, time.monotonic() - started)
    return deployment


async def ensure_on_host(package: Path, *, timeout: float = INSTALL_TIMEOUT) -> Deployment:
    """A one-user server without isolation: the package in a cache on the host."""
    info = analyze(package)
    if info.issues:
        raise PackageError("; ".join(info.issues[:3]))
    name = digest(package)
    root = HOST_CACHE / name
    deployment = Deployment(root=root.as_posix(), python=sys.executable, in_container=False)
    lock = _deploying.setdefault(("host", name), asyncio.Lock())
    async with lock:
        if (root / ".ready").is_file():
            return deployment
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(_archive(package, name))) as tar:
            tar.extractall(HOST_CACHE, filter="data")
        if info.requirements:
            code, out, err = await _run(
                [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check",
                 "--no-input", "--target", str(root / "deps"), *info.requirements],
                timeout=timeout,
            )
            if code != 0:
                raise PackageError(f"installing {', '.join(info.requirements)} failed: {(err or out).strip()[-1500:]}")
        (root / ".ready").touch()
    return deployment


# -- tests in a sandbox ---------------------------------------------------------------------
async def sandbox_test(image: str, package: Path, *, timeout: float = TEST_TIMEOUT, memory: str = "512m") -> Dict[str, Any]:
    """Deploy *package* in a throwaway container of *image*, cut its network, and run
    ``describe`` and the tests there. The package's code never runs on the host.
    """
    info = analyze(package)
    if info.issues:
        return {"ok": False, "stage": "check", "error": "; ".join(info.issues), "describe": None, "tests": None}
    name = f"grid-tooltest-{uuid.uuid4().hex[:10]}"
    started = time.monotonic()
    code, _, err = await _run(
        ["docker", "run", "-d", "--rm", "--name", name, "--memory", memory, "--pids-limit", "256", "--cpus", "1",
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--init",
         "--user", CONTAINER_USER, "--entrypoint", "sleep", image, str(int(timeout) + 120)],
        timeout=120,
    )
    if code != 0:
        return {"ok": False, "stage": "start", "error": f"cannot start a sandbox of {image}: {err.strip()}", "describe": None, "tests": None}
    try:
        try:
            deployment = await ensure_in_container(name, package, timeout=timeout)
        except PackageError as exc:
            return {"ok": False, "stage": "install", "error": str(exc), "describe": None, "tests": None}
        code, _, err = await _run(["docker", "network", "disconnect", "-f", "bridge", name])
        if code != 0:
            return {"ok": False, "stage": "network", "error": f"cannot isolate sandbox network: {err.strip()}",
                    "offline": False, "describe": None, "tests": None}
        offline = True
        env = [part for key, value in deployment.env().items() for part in ("-e", f"{key}={value}")]
        results: Dict[str, Any] = {"offline": offline}
        for mode, workdir in (("describe", "/workspace"), ("test", "/tmp")):
            code, out, err = await _run(
                ["docker", "exec", "-w", workdir, *env, name, *deployment.command(mode)], timeout=timeout
            )
            try:
                results[mode] = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
            except ValueError:
                results[mode] = None
            if results[mode] is None:
                return {**results, "ok": False, "stage": mode, "error": (err or out).strip()[-2000:] or f"exit {code}",
                        "describe": results.get("describe"), "tests": results.get("test")}
        describe, tests = results["describe"], results["test"]
        ok = offline and not describe["errors"] and bool(describe["tools"]) and tests["failed"] == 0 and tests["passed"] > 0
        return {
            "ok": ok,
            "stage": "done",
            "error": "" if ok else "Package needs at least one passing test, no failures, and confirmed network isolation.",
            "offline": offline,
            "describe": describe,
            "tests": tests,
            "seconds": round(time.monotonic() - started, 1),
        }
    finally:
        await _run(["docker", "rm", "-f", name], timeout=60)

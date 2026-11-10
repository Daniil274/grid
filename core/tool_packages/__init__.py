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

  or, to give each ``@function_tool`` of it to agents as a tool of its own,
  on one ``type: function`` tool per function (``name`` is the function)::

      tools:
        word_count:
          type: function
          name: word_count
          tool_package: tools/words

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
import gzip
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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils.process_limits import sandbox_run_args

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
CALL_TIMEOUT = 300.0
TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
#: A requirement pinned to one version, from the package index: nothing else is installed.
REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9.!+_-]+$")
SKIPPED_DIRS = {"__pycache__", ".pytest_cache"}
DECORATORS = ("tool", "function_tool")
SECTIONS_AFTER_SUMMARY = ("Returns", "Raises", "Examples", "Example", "Notes")


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


def package_archive(package: Path) -> Tuple[bytes, str]:
    """The ``.tar.gz`` a device downloads for *package*, and its sha256.

    The same tree always gives the same bytes - times, owners and modes are
    fixed and the gzip header carries no name - so the hash names the content a
    device is about to run (``GET /node/tools/{pkg}/{hash}.tar.gz``,
    web_chat.devices_api, which serves exactly these bytes).
    """
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as packed:
        with tarfile.open(fileobj=packed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(package.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                info = archive.gettarinfo(str(path), arcname=str(path.relative_to(package.parent)))
                info.mtime, info.uid, info.gid = 0, 0, 0
                info.uname = info.gname = ""
                info.mode = 0o755 if info.mode & 0o111 else 0o644
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest()


#: The archive hashes already computed, by package directory and its content stamp.
_archive_digests: Dict[Tuple[str, tuple], str] = {}


def archive_digest(package: Path) -> str:
    """The hash a device fetches *package* by; computed once per content."""
    stamp = tuple(
        (relative.as_posix(), (package / relative).stat().st_mtime_ns, (package / relative).stat().st_size)
        for relative in files_of(package)
        if not (package / relative).is_symlink()
    )
    key = (str(package), stamp)
    found = _archive_digests.get(key)
    if found is None:
        if len(_archive_digests) > 64:  # a long-lived server, few packages
            _archive_digests.clear()
        found = _archive_digests[key] = package_archive(package)[1]
    return found


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
    #: JSON schema of the arguments, built from the source as grid_tool.describe_function does.
    input_schema: Dict[str, Any] = field(default_factory=dict)


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
    """The ``tool`` decorator among a function's, as written: ``tool``, ``function_tool``,
    either called, or ``grid_tool.tool`` / ``grid_tool.function_tool``."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name) and target.id in DECORATORS:
        return node
    if (
        isinstance(target, ast.Attribute)
        and target.attr in DECORATORS
        and isinstance(target.value, ast.Name)
        and target.value.id == "grid_tool"
    ):
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


def _annotation_schema(node: Optional[ast.expr]) -> Dict[str, Any]:
    """JSON schema of a parameter annotation as written, like grid_tool._schema."""
    if node is None:
        return {}
    if isinstance(node, ast.Constant):
        if node.value is None:
            return {}
        if isinstance(node.value, str):  # a quoted annotation
            try:
                return _annotation_schema(ast.parse(node.value, mode="eval").body)
            except SyntaxError:
                return {}
        return {}
    simple = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object"}
    if isinstance(node, ast.Attribute):
        node_name = node.attr
        if node_name == "Any":
            return {}
        return {"type": simple[node_name]} if node_name in simple else {}
    if isinstance(node, ast.Name):
        if node.id == "Any":
            return {}
        return {"type": simple[node.id]} if node.id in simple else {}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):  # X | Y
        return _union_schema([node.left, node.right])
    if isinstance(node, ast.Subscript):
        base = node.value.id if isinstance(node.value, ast.Name) else getattr(node.value, "attr", "")
        index = node.slice
        items = list(index.elts) if isinstance(index, ast.Tuple) else [index]
        if base == "Optional":
            return _union_schema(items)
        if base == "Union":
            return _union_schema(items)
        if base == "Literal":
            return {"enum": [item.value for item in items if isinstance(item, ast.Constant)]}
        if base in ("List", "list", "Sequence", "Set", "set", "FrozenSet", "frozenset", "Tuple", "tuple"):
            return {"type": "array", "items": _annotation_schema(items[0]) if items else {}}
        if base in ("Dict", "dict", "Mapping"):
            return {"type": "object"}
    return {}


def _union_schema(members: List[ast.expr]) -> Dict[str, Any]:
    others = [
        member for member in members if not (isinstance(member, ast.Constant) and member.value is None)
    ]
    if len(others) == 1:
        return _annotation_schema(others[0])
    return {"anyOf": [_annotation_schema(member) for member in others]}


def _docstring_parts(text: str) -> Tuple[str, Dict[str, str]]:
    """The summary and ``Args:`` descriptions of a Google-style docstring, like grid_tool._docstring."""
    summary: List[str] = []
    params: Dict[str, str] = {}
    section: Optional[str] = None
    current: Optional[str] = None
    indent: Optional[int] = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in ("Args:", "Arguments:", "Parameters:"):
            section, current, indent = "args", None, None
            continue
        if stripped.endswith(":") and stripped[:-1] in SECTIONS_AFTER_SUMMARY:
            section = "other"
            continue
        if section == "args" and stripped:
            depth = len(line) - len(line.lstrip())
            match = re.match(r"^(\w+)\s*(\([^)]*\))?\s*:\s*(.*)$", stripped)
            if match and (indent is None or depth <= indent):
                indent = depth
                current = match.group(1)
                params[current] = match.group(3)
            elif current:
                params[current] += " " + stripped
        elif section is None:
            summary.append(line)
    return "\n".join(summary).strip(), params


def _input_schema(node: ast.AST, param_docs: Dict[str, str]) -> Dict[str, Any]:
    args = node.args  # type: ignore[attr-defined]
    positional = args.posonlyargs + args.args
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    pairs = list(zip(positional, defaults)) + list(zip(args.kwonlyargs, args.kw_defaults))
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for arg, default in pairs:
        schema = dict(_annotation_schema(arg.annotation))
        if param_docs.get(arg.arg):
            schema["description"] = param_docs[arg.arg]
        if default is None:
            required.append(arg.arg)
        elif isinstance(default, ast.Constant) and default.value is not None and isinstance(
            default.value, (str, int, float, bool)
        ):
            schema["default"] = default.value
        properties[arg.arg] = schema
    return {"type": "object", "properties": properties, "required": required}


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
            summary, param_docs = _docstring_parts(docstring)
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
                    description=summary or docstring.strip().split("\n\n")[0],
                    params=params,
                    read_only=bool(flags.get("read_only")),
                    destructive=bool(flags.get("destructive")),
                    open_world=bool(flags.get("open_world")),
                    input_schema=_input_schema(node, param_docs),
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

    def call_command(self, tool_name: str) -> List[str]:
        """One call of one tool: arguments as JSON on stdin, a JSON line on stdout."""
        return [self.python, self.script, "call", self.package, tool_name]

    def env(self) -> Dict[str, str]:
        return {"PYTHONPATH": self.pythonpath, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}


async def _run(
    args: Sequence[str], *, stdin: Optional[bytes] = None, timeout: float = 60.0, raw: bool = False
) -> tuple[int, Any, str]:
    """Run a command; with *raw* its stdout stays bytes (a tar stream from ``docker cp``)."""
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
    return process.returncode, (out if raw else out.decode("utf-8", "replace")), err.decode("utf-8", "replace")


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


# -- one @function_tool as an agent's tool ---------------------------------------------------
def _device_target(factory: Any) -> Tuple[Any, Optional[str]]:
    """(target, error): the device this call runs on, or None when it runs here.

    A chat pointed at a device (``execution_target`` in its metadata,
    core.devices.execution) runs a package's function on that device instead of
    in the server's container: the code, the interpreter and the files are the
    user's, and the answer is the function's own result.
    """
    try:
        from core.devices import execution
    except Exception:  # pragma: no cover - a deployment without devices
        return None, None
    if factory is None:
        from utils.path_utils import get_current_factory

        factory = get_current_factory()
    try:
        return execution.chosen(factory), None
    except execution.DeviceError as exc:
        return None, f"❌ Tool error: {exc}"


async def _call_on_device(target: Any, package: Path, package_name: str, fn: str, raw_args: Any) -> str:
    """One call of *package*'s function *fn* on the device: ``tool_exec``, its own answer."""
    from core.devices import execution

    answer = await execution.call_in_device(
        target,
        "tool_exec",
        {
            "package": package_name,
            "hash": await asyncio.to_thread(archive_digest, package),
            "fn": fn,
            "arguments": execution.arguments_of(raw_args),
        },
        timeout=CALL_TIMEOUT,
    )
    return answer


def build_package_function_tool(
    config_dir: Path,
    package_rel: str,
    tool_key: str,
    func_name: str = "",
    description_override: str = "",
    default_container_id: Optional[str] = None,
    default_workdir: str = "/workspace",
    host_workdir: Optional[str] = None,
) -> Tuple[Any, Any]:
    """An agents ``FunctionTool`` for one function of a package, and its policy effect.

    The package is read with :mod:`ast`, never imported here. Each call deploys
    it (once per content) into the container the call's factory runs in - or the
    host cache without one - and runs ``grid_tool.py call`` there, so the
    function's code runs where the agents' own commands do. ``(None, None)``
    when the package has issues or has no such function.
    """
    from agents.tool import FunctionTool

    from utils.tool_effects import EXEC_ANY, egress, read

    target_name = func_name or tool_key
    try:
        pkg_path = package_dir(config_dir, package_rel)
    except PackageError as exc:
        logger.error("Tool '%s': %s", tool_key, exc)
        return None, None
    info = analyze(pkg_path)
    if info.issues:
        logger.error("Tool package '%s' of tool '%s' has issues: %s", package_rel, tool_key, "; ".join(info.issues[:3]))
        return None, None
    tool_info = next((found for found in info.tools if found.name == target_name), None)
    if tool_info is None:
        logger.error("Tool package '%s' has no @function_tool named '%s'", package_rel, target_name)
        return None, None

    async def on_invoke_tool(ctx: Any, raw_args: Any) -> Any:
        raw_ctx = getattr(ctx, "context", None)
        factory = getattr(raw_ctx, "factory", None)
        target, failure = _device_target(factory)
        if failure:
            return failure
        if target is not None:
            # The chat works on a device: its node runs the package (tool_exec),
            # with the same function and the same arguments.
            return await _call_on_device(target, pkg_path, pkg_path.name or package_rel, target_name, raw_args)
        container_id = (
            getattr(raw_ctx, "container_id", None)
            or getattr(factory, "container_id", None)
            or default_container_id
        )
        if isinstance(raw_args, (bytes, bytearray)):
            stdin_bytes = bytes(raw_args) or b"{}"
        elif isinstance(raw_args, str):
            stdin_bytes = raw_args.encode("utf-8") if raw_args.strip() else b"{}"
        elif isinstance(raw_args, dict):
            stdin_bytes = json.dumps(raw_args, ensure_ascii=False).encode("utf-8")
        else:
            stdin_bytes = b"{}"
        deployment = (
            await ensure_in_container(container_id, pkg_path) if container_id else await ensure_on_host(pkg_path)
        )
        env = deployment.env()
        if container_id:
            workdir = getattr(factory, "_container_workdir", None) or default_workdir or "/workspace"
            flags = [part for key, value in env.items() for part in ("-e", f"{key}={value}")]
            command = ["docker", "exec", "-i", "-w", workdir, *flags, container_id,
                       *deployment.call_command(target_name)]
            code, out, err = await _run(command, stdin=stdin_bytes, timeout=CALL_TIMEOUT)
        else:
            config = getattr(factory, "config", None)
            cwd = (config.get_working_directory() if config is not None else host_workdir) or os.getcwd()
            code, out, err = await _run_host(
                deployment.call_command(target_name), stdin=stdin_bytes, cwd=cwd,
                env={**os.environ, **env}, timeout=CALL_TIMEOUT,
            )
        lines = [line for line in out.splitlines() if line.strip()]
        try:
            payload = json.loads(lines[-1]) if lines else None
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or "ok" not in payload:
            detail = (err or out).strip()[-1500:] or f"exit code {code}"
            raise RuntimeError(f"Tool '{tool_key}' gave no answer: {detail}")
        if payload.get("ok"):
            return payload.get("result", "")
        return payload.get("error") or "Error"

    tool = FunctionTool(
        name=tool_key,
        description=description_override or tool_info.description,
        params_json_schema=tool_info.input_schema,
        on_invoke_tool=on_invoke_tool,
        strict_json_schema=False,
    )
    # The policy gate reads these hints with the call (ActionGate.describe_tool).
    tool.annotations = {
        "readOnlyHint": tool_info.read_only,
        "destructiveHint": tool_info.destructive,
        "openWorldHint": tool_info.open_world,
    }
    if tool_info.read_only and not tool_info.destructive and not tool_info.open_world:
        effect = read()
    elif tool_info.open_world and tool_info.read_only and not tool_info.destructive:
        effect = egress(untrusted=True)
    else:
        effect = EXEC_ANY
    return tool, effect


async def _run_host(
    args: Sequence[str], *, stdin: bytes, cwd: str, env: Dict[str, str], timeout: float
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
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
        raise PackageError(f"a tool call took longer than {timeout:.0f}s") from None
    return process.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


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
         *sandbox_run_args(memory, 1), "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--init",
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

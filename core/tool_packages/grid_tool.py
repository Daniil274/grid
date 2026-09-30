"""Runtime of a Grid tool package: run where the tools run, never in the Grid server.

A *tool package* is a directory of Python modules. Every function decorated
with :func:`tool` is a tool an agent can call::

    from grid_tool import tool

    @tool(read_only=True)
    def word_count(path: str, unique: bool = False) -> dict:
        '''Count the words of a text file of the workspace.

        Args:
            path: the file, relative to the workspace
            unique: count distinct words instead
        '''

Grid copies the package and this file into the user's container (or, on a
one-user server without isolation, a cache on the host) and starts::

    python grid_tool.py serve <package>       an MCP server on stdio: the agent's tools
    python grid_tool.py describe <package>    the tools as JSON, for a check
    python grid_tool.py test <package>        run the test_* functions of test_*.py

The tools run with the workspace as their working directory. This file needs
only the standard library: it must run in any image with a Python 3.9+.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import io
import json
import os
import re
import sys
import time
import traceback
import typing
from pathlib import Path

try:
    from typing import get_args, get_origin, get_type_hints
except ImportError:  # pragma: no cover - Python < 3.8
    raise SystemExit("grid_tool needs Python 3.8 or newer")

SERVER_NAME = "grid-tool-package"
SERVER_VERSION = "1"
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
DEFAULT_PROTOCOL = "2025-06-18"
#: The attribute :func:`tool` sets; found on the function whichever copy of this module decorated it.
MARK = "__grid_tool__"
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
#: A tool result longer than this is cut: the agent's context is not a file.
MAX_RESULT_CHARS = 60_000


# -- declaring tools ------------------------------------------------------------------
def tool(fn=None, *, name=None, read_only=False, destructive=False, open_world=False):
    """Mark a function as a tool.

    ``read_only``: it changes nothing. ``destructive``: it may delete or
    overwrite. ``open_world``: it reaches outside the workspace (the internet).
    The action policy reads these hints with the call.
    """

    def mark(function):
        setattr(
            function,
            MARK,
            {
                "name": name or function.__name__,
                "read_only": bool(read_only),
                "destructive": bool(destructive),
                "open_world": bool(open_world),
            },
        )
        return function

    return mark(fn) if fn is not None else mark


# -- describing tools -----------------------------------------------------------------
def _schema(annotation):
    """JSON schema of a parameter annotation; plain types only."""
    if annotation is inspect.Parameter.empty or annotation is typing.Any:
        return {}
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is typing.Union or (origin is not None and getattr(origin, "__name__", "") == "UnionType"):
        others = [arg for arg in args if arg is not type(None)]
        if len(others) == 1:
            return _schema(others[0])
        return {"anyOf": [_schema(arg) for arg in others]}
    if origin is typing.Literal:
        return {"enum": list(args)}
    if origin in (list, tuple, set, frozenset):
        return {"type": "array", "items": _schema(args[0]) if args else {}}
    if origin is dict:
        return {"type": "object"}
    simple = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}
    if annotation in simple:
        return {"type": simple[annotation]}
    raise TypeError(f"unsupported parameter type {annotation!r}: use str, int, float, bool, list, dict, Optional or Literal")


def _docstring(function):
    """The summary and the ``Args:`` descriptions of a Google-style docstring."""
    text = inspect.getdoc(function) or ""
    summary, params, section = [], {}, None
    current, indent = None, None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in ("Args:", "Arguments:", "Parameters:"):
            section, current, indent = "args", None, None
            continue
        if stripped.endswith(":") and stripped[:-1] in ("Returns", "Raises", "Examples", "Example", "Notes"):
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


def describe_function(function):
    """The MCP description of one tool function."""
    meta = getattr(function, MARK)
    if not NAME.fullmatch(meta["name"]):
        raise ValueError(f"tool name {meta['name']!r}: letters, digits and _, starting with a letter")
    summary, param_docs = _docstring(function)
    if not summary:
        raise ValueError(f"tool {meta['name']!r} has no docstring: the agent reads it to know what the tool does")
    hints = get_type_hints(function)
    properties, required = {}, []
    for parameter in inspect.signature(function).parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise TypeError(f"tool {meta['name']!r}: *args and **kwargs cannot be described to an agent")
        schema = dict(_schema(hints.get(parameter.name, parameter.annotation)))
        if param_docs.get(parameter.name):
            schema["description"] = param_docs[parameter.name]
        if parameter.default is parameter.empty:
            required.append(parameter.name)
        elif parameter.default is not None and isinstance(parameter.default, (str, int, float, bool)):
            schema["default"] = parameter.default
        properties[parameter.name] = schema
    return {
        "name": meta["name"],
        "description": summary,
        "inputSchema": {"type": "object", "properties": properties, "required": required},
        "annotations": {
            "readOnlyHint": meta["read_only"],
            "destructiveHint": meta["destructive"],
            "openWorldHint": meta["open_world"],
        },
    }


# -- loading a package ----------------------------------------------------------------
def _import(path):
    spec = importlib.util.spec_from_file_location(f"grid_tool_package.{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load(package):
    """The package's tools by name, and why a module could not give its tools."""
    package = str(Path(package).resolve())
    if package not in sys.path:
        sys.path.insert(0, package)
    tools, errors = {}, []
    for path in sorted(Path(package).glob("*.py")):
        if path.name.startswith(("test_", "_")):
            continue
        try:
            module = _import(path)
        except Exception:
            errors.append(f"{path.name}: {traceback.format_exc(limit=3).strip()}")
            continue
        for _, function in inspect.getmembers(module, callable):
            meta = getattr(function, MARK, None)
            if meta is None or getattr(function, "__module__", None) != module.__name__:
                continue
            if meta["name"] in tools:
                errors.append(f"{path.name}: tool {meta['name']!r} is defined twice")
                continue
            tools[meta["name"]] = function
    return tools, errors


def describe(package):
    tools, errors = load(package)
    described = []
    for name, function in tools.items():
        try:
            described.append(describe_function(function))
        except Exception as exc:
            errors.append(f"tool {name!r}: {exc}")
    return {"tools": described, "errors": errors}


# -- calling a tool -------------------------------------------------------------------
def _coerce(value, schema):
    """Arguments come as JSON; turn the obvious strings into what the schema wants."""
    kind = schema.get("type")
    if isinstance(value, str):
        if kind == "integer" and re.fullmatch(r"-?\d+", value.strip()):
            return int(value)
        if kind == "number":
            try:
                return float(value)
            except ValueError:
                return value
        if kind == "boolean" and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
    return value


def call(function, arguments):
    schema = describe_function(function)["inputSchema"]
    known = schema["properties"]
    unknown = set(arguments) - set(known)
    if unknown:
        raise TypeError(f"unknown arguments: {', '.join(sorted(unknown))}")
    missing = [name for name in schema["required"] if name not in arguments]
    if missing:
        raise TypeError(f"missing arguments: {', '.join(missing)}")
    kwargs = {name: _coerce(value, known[name]) for name, value in arguments.items()}
    result = function(**kwargs)
    if inspect.isawaitable(result):
        result = asyncio.run(result) if not _running_loop() else result
    return result


def _running_loop():
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _text(result):
    if result is None:
        text = "done"
    elif isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n[cut: {len(text) - MAX_RESULT_CHARS} more characters]"
    return text


# -- the MCP server -------------------------------------------------------------------
class Server:
    """MCP over stdio: one JSON-RPC message per line.

    The protocol owns the process's original stdout. Everything a tool prints -
    Python's or a subprocess's - goes to stderr, or it would break the stream.
    """

    def __init__(self, package):
        self.tools, self.errors = load(package)
        self.described = {}
        for name, function in list(self.tools.items()):
            try:
                self.described[name] = describe_function(function)
            except Exception as exc:
                self.errors.append(f"tool {name!r}: {exc}")
                del self.tools[name]
        for error in self.errors:
            print(f"grid_tool: {error}", file=sys.stderr)

    def handle(self, message):
        method, params = message.get("method"), message.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {
                "protocolVersion": requested if requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": list(self.described.values())}
        if method == "tools/call":
            name = params.get("name")
            function = self.tools.get(name)
            if function is None:
                return {"content": [{"type": "text", "text": f"No tool {name!r}"}], "isError": True}
            try:
                result = call(function, params.get("arguments") or {})
            except Exception as exc:
                detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                return {"content": [{"type": "text", "text": f"Error: {detail}"}], "isError": True}
            return {"content": [{"type": "text", "text": _text(result)}], "isError": False}
        raise LookupError(method)

    def serve(self, stdin, stdout):
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._send(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if "id" not in message:
                continue  # a notification: nothing to answer
            try:
                response = {"jsonrpc": "2.0", "id": message["id"], "result": self.handle(message)}
            except LookupError:
                response = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": f"Method not found: {message.get('method')}"},
                }
            except Exception as exc:
                response = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": str(exc)}}
            self._send(stdout, response)

    @staticmethod
    def _send(stdout, payload):
        stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stdout.flush()


def _claim_stdout():
    """The process's stdout for the answer alone; everything else printed goes to stderr."""
    answer = io.TextIOWrapper(os.fdopen(os.dup(1), "wb", buffering=0), encoding="utf-8", write_through=True)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return answer


def serve(package):
    protocol = _claim_stdout()
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    Server(package).serve(stdin, protocol)


# -- tests ----------------------------------------------------------------------------
def test(package):
    """Run every ``test_*`` function of the ``test_*.py`` modules; a result per test."""
    package = str(Path(package).resolve())
    if package not in sys.path:
        sys.path.insert(0, package)
    results = []
    for path in sorted(Path(package).glob("test_*.py")):
        try:
            module = _import(path)
        except Exception:
            results.append({"test": path.name, "ok": False, "error": traceback.format_exc(limit=3).strip(), "ms": 0})
            continue
        for name, function in inspect.getmembers(module, inspect.isfunction):
            if not name.startswith("test_") or function.__module__ != module.__name__:
                continue
            started = time.monotonic()
            try:
                outcome = function()
                if inspect.isawaitable(outcome):
                    asyncio.run(outcome)
                results.append({"test": f"{path.stem}.{name}", "ok": True, "error": "", "ms": _ms(started)})
            except Exception:
                results.append(
                    {"test": f"{path.stem}.{name}", "ok": False, "error": traceback.format_exc(limit=4).strip(), "ms": _ms(started)}
                )
    return {"tests": results, "passed": sum(result["ok"] for result in results), "failed": sum(not result["ok"] for result in results)}


def _ms(started):
    return round((time.monotonic() - started) * 1000)


def main(argv):
    if len(argv) != 3 or argv[1] not in ("serve", "describe", "test"):
        print("usage: grid_tool.py serve|describe|test <package directory>", file=sys.stderr)
        return 2
    command, package = argv[1], argv[2]
    if command == "serve":
        serve(package)
        return 0
    answer = _claim_stdout()
    output = describe(package) if command == "describe" else test(package)
    answer.write(json.dumps(output, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    # Tool modules import this file as `grid_tool`; run as a script it is
    # `__main__`, so make the name point here and not at a second copy.
    sys.modules.setdefault("grid_tool", sys.modules[__name__])
    sys.exit(main(sys.argv))

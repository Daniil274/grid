"""The system builder's tools: make and change created systems, test their tool packages.

The builder (examples/system-builder) is the web chat's system for creating
systems. It writes a new system from scratch - its config, skills and tool
packages - into the store of created systems (core.system_store), checks it the
way the router does, and runs the tests of its tool packages in a throwaway
container (core.tool_packages). It never publishes: a person does, on the
systems page, after reading and trying the draft.

Every tool uses the store granted by the user's BuilderAccess: the shared
store for admins, a private store for other users. Shared published systems
are read-only here: a change to one goes through a draft.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from agents import RunContextWrapper, function_tool

from core.system_store import SYSTEMS_README_HINT, BuilderAccess, StoreError, SystemManifest, SystemOrigin, check_key, inspect_system
from utils.tool_isolation import SYSTEMS

MAX_FILE_BYTES = 512 * 1024
#: Files of a catalog system the builder may read, as examples.
READABLE_SUFFIXES = {".yaml", ".yml", ".md", ".py", ".txt", ".json", ".toml"}
#: Never read, whatever directory: credentials and the like.
SECRET_NAMES = {".env", ".env.local", "secrets.yaml", "credentials.json"}


class _Refused(Exception):
    pass


def _access(context: RunContextWrapper) -> BuilderAccess:
    factory = getattr(getattr(context, "context", None), "factory", None)
    access = getattr(factory, "system_builder", None)
    if access is None:
        raise _Refused("The system builder needs access to a web chat user's system store.")
    return access


def _answer(payload: Any) -> str:
    return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)


def _inside(root: Path, relative: str) -> Path:
    path = (root / (relative or ".")).resolve()
    if path != root.resolve() and root.resolve() not in path.parents:
        raise _Refused(f"'{relative}' is outside the system's directory")
    if path.name in SECRET_NAMES:
        raise _Refused(f"'{relative}' is not for reading")
    return path


def _validate_config(text: str) -> None:
    from schemas import GridConfig

    document = yaml.safe_load(text) or {}
    if not isinstance(document, dict):
        raise _Refused("A config is a YAML mapping.")
    GridConfig(**document)


def _created(access: BuilderAccess, key: str, *, writable: bool) -> SystemManifest:
    try:
        manifest = access.store.get(check_key(key))
    except StoreError as exc:
        raise _Refused(str(exc)) from None
    if writable and manifest.status == "published":
        raise _Refused(
            f"'{key}' is published: use builder_fork to prepare and test a new draft, "
            "or withdraw it on the systems page first."
        )
    return manifest


def _health(access: BuilderAccess, key: str) -> Dict[str, Any]:
    manifest = access.store.get(key)
    report = inspect_system(lambda: access.load(key), requires=manifest.requires, confined=True)
    from core.system_quality import quality_summary

    return {
        "quality": quality_summary(access.store.directory(key)),
        "system": key,
        "chat_system": access.system_key(key),
        "status": manifest.status,
        "loads": report["loaded"],
        "error": report["error"],
        "config_problems": report["config"],
        "machine_lacks": report["environment"],
        "agents": [
            {"key": agent["key"], "routable": agent["routable"], "default": agent["default"], "tools": agent["tools"]}
            for agent in report["agents"]
        ],
        "tool_packages": [
            {
                "tool": package["tool"],
                "package": package["package"],
                "tools": [tool["name"] for tool in package["tools"]],
                "tests": package["tests"],
                "requirements": package["requirements"],
                "problems": package["issues"],
            }
            for package in report["packages"]
        ],
        "healthy": report["loaded"] and not report["config"],
    }


# -- reading ------------------------------------------------------------------------------
@function_tool
def builder_catalog(context: RunContextWrapper) -> str:
    """What a new system can be made of: the systems there are, the models, the ready tools.

    Call it first. It lists the catalog's systems (read one with builder_read as
    an example), the created systems and their status, the models the server
    has, the shared tools any system may declare, and the ready tool sets a
    system reaches through settings.project_tools - with the exact path to use
    from a created system's directory.
    """
    try:
        access = _access(context)
    except _Refused as exc:
        return str(exc)
    return _answer(_catalog(access))


def _catalog(access: BuilderAccess) -> Dict[str, Any]:
    from tools.function_tools import AVAILABLE_TOOLS, tool_isolation
    from utils.tool_isolation import CONTAINER, WORKSPACE

    base = access.base_config
    examples = access.examples()
    catalog = {
        key: {
            "description": " ".join(system.description.split()),
            "config": str(access.catalog_systems().get(key)),
        }
        for key, system in (access.catalog.config.routing.systems.items() if access.catalog is not None else [])
        if key in examples
    }
    created = [
        {"key": manifest.key, "name": manifest.name, "status": manifest.status, "description": manifest.description}
        for manifest in access.store.list()
    ]
    models = {
        key: {"name": model.name, "provider": model.provider, "description": model.description or ""}
        for key, model in base.config.models.items()
    }
    shared = {}
    for name in sorted(AVAILABLE_TOOLS.keys()):
        where = tool_isolation(name)
        if where in (CONTAINER, WORKSPACE):
            tool = AVAILABLE_TOOLS[name]
            shared[name] = " ".join((getattr(tool, "description", "") or "").split())[:200]
    tool_sets = []
    for key, path in access.examples().items() if access.shared else []:
        directory = path.parent / "tools"
        if directory.is_dir() and any(directory.glob("*.py")):
            tool_sets.append(
                {
                    "from_system": key,
                    "tools_directory": _relative(directory, access.store.root),
                    "modules": sorted(p.stem for p in directory.glob("*.py") if not p.name.startswith("_")),
                    "how": "settings.project_tools: {enabled: true, tools_directory: <tools_directory>}; "
                    "then declare the tools you use under tools: as type: function",
                }
            )
    return {
        "store": str(access.store.root),
        "new_system_directory": f"{access.store.root}/<key>/",
        "catalog": catalog,
        "created": created,
        "models": models,
        # Copy these as they are: a provider may need headers (opencode needs its session header).
        "providers": access.providers(),
        "shared": access.shared,
        "max_systems": access.max_systems,
        "private_rules": [] if access.shared else [
            "Only this user's systems are listed and writable. Leave a draft for the owner to activate on /systems.",
            "You may edit owned active systems. Never change their lifecycle status yourself.",
            "Copy providers unchanged and use the catalog's model keys and names.",
            "Use shared tools or tool_package MCP tools. No project_tools imports or server_command.",
            "Keep allow_path_override: true and config_directory: .; omit logs_directory and routing.",
        ],
        "shared_tools": shared,
        "tool_sets": tool_sets,
        "hint": SYSTEMS_README_HINT,
    }


def _relative(path: Path, store_root: Path) -> str:
    """*path* as a created system's config reaches it: from <store>/<key>/."""
    import os

    return Path(os.path.relpath(path.resolve(), (store_root / "_").resolve())).as_posix()


def _read_tool_sets(access: BuilderAccess, key: str) -> Optional[Dict[str, Any]]:
    """The tools of one ready tool set, with where each acts."""
    from core.config import Config
    from core.managers.project_tools_loader import get_project_loader, set_project_loader

    path = access.examples().get(key)
    if path is None:
        return None
    previous = get_project_loader()
    try:
        loader = Config(str(path)).project_tools_loader
    finally:
        set_project_loader(previous)
    if loader is None:
        return None
    return {
        name: {"acts": loader.isolation.get(name) or "on the host (withheld on a server with accounts)",
               "description": " ".join((getattr(tool, "description", "") or "").split())[:200]}
        for name, tool in loader.get_all_tools().items()
    }


@function_tool
def builder_tool_set(context: RunContextWrapper, system: str) -> str:
    """The tools of one ready tool set (see builder_catalog.tool_sets): name, what it does, where it acts.

    Only tools that act in the user's container or workspace work on a server
    with accounts; the others are withheld there.

    Args:
        system: the catalog system whose tools/ directory it is
    """
    try:
        access = _access(context)
        tools = _read_tool_sets(access, system)
    except _Refused as exc:
        return str(exc)
    return _answer(tools) if tools is not None else f"'{system}' has no ready tool set."


@function_tool
def builder_read(context: RunContextWrapper, system: str, path: str = "config.yaml") -> str:
    """Read a file of a system - a catalog one, as an example, or a created one; a directory lists its files.

    Args:
        system: the system's key
        path: the file, relative to the system's directory; "" or a directory lists it
    """
    try:
        access = _access(context)
        root = _system_root(access, system)
        target = _inside(root, path)
    except _Refused as exc:
        return str(exc)
    if target.is_dir():
        files = sorted(
            p.relative_to(root).as_posix()
            for p in target.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.name not in SECRET_NAMES
        )
        return _answer({"system": system, "files": files[:300]})
    if not target.is_file():
        return f"No file '{path}' in '{system}'."
    if target.suffix not in READABLE_SUFFIXES and target.name != "requirements.txt":
        return f"'{path}' is not a text file the builder reads."
    if target.stat().st_size > MAX_FILE_BYTES:
        return f"'{path}' is over {MAX_FILE_BYTES} bytes."
    text = target.read_text(encoding="utf-8", errors="replace")
    if target.suffix in {".yaml", ".yml"}:
        document = yaml.safe_load(text)
        if isinstance(document, dict) and isinstance(document.get("providers"), dict):
            sanitized = access.redact_providers(document["providers"])
            if sanitized != document["providers"]:
                document["providers"] = sanitized
                text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    return text


def _system_root(access: BuilderAccess, system: str) -> Path:
    catalog = access.catalog_systems()
    if system in catalog:
        if system not in access.examples():
            raise _Refused(f"'{system}' is not an example to read.")
        return catalog[system].parent
    try:
        return access.store.directory(check_key(system)) if access.store.exists(system) else _missing(system)
    except StoreError as exc:
        raise _Refused(str(exc)) from None


def _missing(system: str) -> Path:
    raise _Refused(f"No system '{system}'. builder_catalog lists them.")


@function_tool
def builder_check(context: RunContextWrapper, system: str) -> str:
    """Check a created system the way the router does: does the config load, what is wrong in it,
    what this machine lacks, and what its tool packages hold. It does not call models or run tools.

    Args:
        system: the created system's key
    """
    try:
        access = _access(context)
        _created(access, system, writable=False)
    except _Refused as exc:
        return str(exc)
    return _answer(_health(access, system))


# -- writing ------------------------------------------------------------------------------
@function_tool
def builder_create(context: RunContextWrapper, key: str, name: str, description: str, config_yaml: str) -> str:
    """Create a new draft in the requesting user's permitted system store.

    Write the config from scratch (builder_read a catalog system for the format).
    Paths in it are relative to the new system's directory. The system is a
    draft until its owner activates it, or an admin publishes a shared draft.

    Args:
        key: the system's id and directory: 2-40 of a-z, 0-9, - and _, starting with a letter
        name: the name people see
        description: what the router reads to send messages here: the tasks it is for, and what it is not for
        config_yaml: the whole config.yaml
    """
    try:
        access = _access(context)
        check_key(key)
        if key in access.catalog_systems() or access.store.exists(key) or key.startswith(("us_", "ub_")):
            raise _Refused(f"The key '{key}' is taken.")
        if access.max_systems is not None and len(access.store.list()) + access.count_other() >= access.max_systems:
            raise _Refused(f"At most {access.max_systems} systems of your own are allowed.")
        _validate_config(config_yaml)
        access.validate(config_yaml, key)
        access.store.create(
            SystemManifest(
                key=key,
                name=name,
                description=description,
                origin=SystemOrigin(kind="admin" if access.shared else "user", user_id=access.user_id, username=access.user_id, source="builder"),
            ),
            config_yaml,
        )
    except (_Refused, StoreError, ValueError) as exc:
        return f"Not created: {exc}"
    access.done(key, "created", "by the system builder")
    return _answer({"created": key, "chat_system": access.system_key(key), "health": _health(access, key)})


@function_tool
def builder_write(context: RunContextWrapper, system: str, path: str, content: str) -> str:
    """Write a file of a draft system: config.yaml, a skill (skills/<name>.md), a tool package file
    (tools/<package>/<module>.py, test_<module>.py, requirements.txt), a README.md.

    config.yaml is checked against the config schema before it is written. Answers with the
    system's health after the change.

    Args:
        system: the draft's key
        path: the file, relative to the system's directory
        content: the whole new content of the file
    """
    try:
        access = _access(context)
        _created(access, system, writable=True)
        root = access.store.directory(system)
        target = _inside(root, path)
        relative = target.relative_to(root.resolve()).as_posix()
        if relative in ("system.yaml", "."):
            raise _Refused("system.yaml is the manifest: change it with builder_describe.")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise _Refused(f"over {MAX_FILE_BYTES} bytes")
        if relative == "config.yaml":
            _validate_config(content)
            access.validate(content, system)
        if relative == "quality.yaml":
            from core.system_quality import QualityContract
            QualityContract.model_validate(yaml.safe_load(content))
        target.parent.mkdir(parents=True, exist_ok=True)
        from core.system_store import _write
        _write(target, content)
        if relative == "quality.yaml":
            from core.system_quality import save_evidence
            save_evidence(root, "contract-required", {"required": True})
        access.store.touch(system)
    except (_Refused, StoreError, ValueError) as exc:
        return f"Not written: {exc}"
    access.done(system, "edited", f"{relative} by the system builder")
    return _answer({"written": relative, "health": _health(access, system)})


@function_tool
def builder_delete(context: RunContextWrapper, system: str, path: str) -> str:
    """Delete a file of a draft system (not config.yaml or system.yaml).

    Args:
        system: the draft's key
        path: the file, relative to the system's directory
    """
    try:
        access = _access(context)
        _created(access, system, writable=True)
        root = access.store.directory(system)
        target = _inside(root, path)
        relative = target.relative_to(root.resolve()).as_posix()
        if relative in ("config.yaml", "system.yaml") or not target.is_file():
            raise _Refused(f"'{path}' cannot be deleted")
        target.unlink()
        access.store.touch(system)
    except (_Refused, StoreError) as exc:
        return f"Not deleted: {exc}"
    access.done(system, "edited", f"{relative} deleted by the system builder")
    return f"Deleted {relative}."


@function_tool
def builder_describe(
    context: RunContextWrapper,
    system: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    requires: Optional[List[str]] = None,
) -> str:
    """Change a draft's name, its description for the router, or the programs it needs on PATH.

    Args:
        system: the draft's key
        name: the new name
        description: the new description the router reads
        requires: programs the system needs on PATH
    """
    changes = {k: v for k, v in {"name": name, "description": description, "requires": requires}.items() if v is not None}
    try:
        access = _access(context)
        _created(access, system, writable=True)
        manifest = access.store.update(system, **changes)
    except (_Refused, StoreError, ValueError) as exc:
        return f"Not changed: {exc}"
    access.done(system, "described", ", ".join(sorted(changes)) + " by the system builder")
    return _answer(manifest.model_dump())


@function_tool
async def builder_test_tools(context: RunContextWrapper, system: str, tool: str) -> str:
    """Run a tool package's tests in a throwaway container: install its requirements, cut the network,
    describe its tools and run every test_* function. The package's code never runs on the server.

    Args:
        system: the created system's key
        tool: the key of the config's MCP tool that declares the package (tool_package)
    """
    from core.tool_packages import PackageError, package_dir, sandbox_test

    try:
        access = _access(context)
        _created(access, system, writable=False)
        config = access.load(system)
        declared = config.config.tools.get(tool)
        if declared is None or not getattr(declared, "tool_package", None):
            raise _Refused(f"'{tool}' is not a tool of '{system}' with a tool_package")
        package = package_dir(access.store.directory(system), declared.tool_package)
    except (_Refused, StoreError, PackageError, ValueError) as exc:
        return f"Not tested: {exc}"
    from core.system_quality import record_tool_test, revision
    root = access.store.directory(system)
    tested_revision = revision(root)
    result = await sandbox_test(access.image, package)
    record_tool_test(root, tool, result, tested_revision)
    tests = result.get("tests") or {}
    access.done(
        system,
        "tools tested",
        f"{tool}: " + ("passed" if result["ok"] else f"failed at {result.get('stage')}")
        + (f", {tests.get('passed', 0)} passed / {tests.get('failed', 0)} failed" if tests else ""),
    )
    summary = {
        "ok": result["ok"],
        "stage": result.get("stage"),
        "error": result.get("error", "")[-3000:],
        "offline_tests": result.get("offline"),
        "tools": [t["name"] for t in (result.get("describe") or {}).get("tools", [])],
        "describe_errors": (result.get("describe") or {}).get("errors", []),
        "failed_tests": [
            {"test": t["test"], "error": t["error"][-1500:]} for t in tests.get("tests", []) if not t["ok"]
        ],
        "passed": tests.get("passed"),
        "failed": tests.get("failed"),
    }
    return _answer(summary)



@function_tool
async def builder_evaluate(context: RunContextWrapper, system: str) -> str:
    """Run quality.yaml acceptance scenarios with real models in disposable sandboxes.

    Costs model tokens. Enforces suite/scenario deadlines and observed usage
    limits. Stores revision-bound development evidence outside candidate files.
    Requires Docker. Does not publish or certify independent acceptance.
    """
    from core.system_evaluation import evaluate
    try:
        access = _access(context)
        _created(access, system, writable=False)
        return _answer(await evaluate(access, system))
    except (_Refused, StoreError, OSError, ValueError) as exc:
        return _answer({"passed": False, "error": str(exc)})


@function_tool
def builder_fork(context: RunContextWrapper, system: str, key: str, name: str) -> str:
    """Copy an owned system into a new draft to test changes without editing an active version.

    Reports are not copied: the new draft needs its own package tests and
    acceptance run. The original remains available for comparison and rollback.
    """
    import shutil
    from core.system_quality import acceptance_suite, load_evidence, save_evidence, system_files
    try:
        access = _access(context)
        source = _created(access, system, writable=False)
        check_key(key)
        if key in access.catalog_systems() or access.store.exists(key) or key.startswith(("us_", "ub_")):
            raise _Refused(f"The key '{key}' is taken.")
        if access.max_systems is not None and len(access.store.list()) + access.count_other() >= access.max_systems:
            raise _Refused(f"At most {access.max_systems} systems of your own are allowed.")
        root = access.store.directory(system)
        files = system_files(root)
        content = access.store.config_text(system)
        access.validate(content, key)
        access.store.create(SystemManifest(key=key, name=name, description=source.description,
                                          requires=source.requires,
                                          origin=SystemOrigin(kind=source.origin.kind, user_id=access.user_id,
                                                              source=system)), content)
        try:
            for path in files:
                relative = path.relative_to(root)
                if str(relative) in {"config.yaml", "system.yaml"}:
                    continue
                target = access.store.directory(key) / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            if load_evidence(root, "contract-required"):
                save_evidence(access.store.directory(key), "contract-required", {"required": True})
            independent = acceptance_suite(root)
            if independent:
                save_evidence(access.store.directory(key), "acceptance", independent.model_dump())
        except BaseException:
            access.store.delete(key)
            raise
        access.done(key, "forked", f"from {system}")
        return _answer({"created": key, "chat_system": access.system_key(key), "health": _health(access, key)})
    except (_Refused, StoreError, OSError, ValueError) as exc:
        return f"Not forked: {exc}"


@function_tool
def builder_compare(context: RunContextWrapper, baseline: str, candidate: str) -> str:
    """Compare two owned systems on identical acceptance scenarios and budgets.

    Reports regressions separately from time/token savings. Run both systems
    with builder_evaluate first; stale or different suites cannot be compared.
    """
    from core.system_quality import comparison, quality_summary
    try:
        access = _access(context)
        _created(access, baseline, writable=False)
        _created(access, candidate, writable=False)
        return _answer(comparison(quality_summary(access.store.directory(baseline)),
                                  quality_summary(access.store.directory(candidate))))
    except (_Refused, StoreError, OSError, ValueError) as exc:
        return _answer({"error": str(exc)})


SYSTEM_BUILDER_TOOLS: Dict[str, Any] = {
    "builder_compare": builder_compare,
    "builder_evaluate": builder_evaluate,
    "builder_fork": builder_fork,
    "builder_catalog": builder_catalog,
    "builder_tool_set": builder_tool_set,
    "builder_read": builder_read,
    "builder_check": builder_check,
    "builder_create": builder_create,
    "builder_write": builder_write,
    "builder_delete": builder_delete,
    "builder_describe": builder_describe,
    "builder_test_tools": builder_test_tools,
}

#: They act on the created systems only, and refuse without an admin's access.
TOOL_ISOLATION = {name: SYSTEMS for name in SYSTEM_BUILDER_TOOLS}

# What these tools do (utils.tool_effects): the created systems are the user's own
# store; evaluating and testing run their agents and tools.
from utils import tool_effects as _effects  # noqa: E402

TOOL_EFFECTS = {
    "builder_catalog": _effects.read(),
    "builder_check": _effects.read(),
    "builder_compare": _effects.read(),
    "builder_create": _effects.write(),
    "builder_delete": _effects.write(),
    "builder_describe": _effects.read(),
    "builder_evaluate": _effects.EXEC_ANY,
    "builder_fork": _effects.write(),
    "builder_read": _effects.read(),
    "builder_test_tools": _effects.EXEC_ANY,
    "builder_tool_set": _effects.write(),
    "builder_write": _effects.write(),
}

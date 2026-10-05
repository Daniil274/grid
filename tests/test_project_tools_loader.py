import sys

import pytest

from core.managers.project_tools_loader import ProjectToolsLoader


def test_project_tools_loader_supports_external_tools_directory(tmp_path):
    config_dir = tmp_path / "project" / "config"
    tools_dir = tmp_path / "shared" / "tools"
    config_dir.mkdir(parents=True)
    tools_dir.mkdir(parents=True)

    (tools_dir / "custom_tools.py").write_text(
        "def external_tool():\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )

    loader = ProjectToolsLoader(str(config_dir), "../../shared/tools")

    loaded = loader.load_project_tools()

    assert "external_tool" in loaded
    assert loaded["external_tool"]() == "ok"


def test_project_tools_do_not_shadow_grid_tools_package(tmp_path):
    import sys

    config_dir = tmp_path / "system"
    tools_dir = config_dir / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "file_tools.py").write_text(
        "def system_file_tool():\n"
        "    return 'system'\n",
        encoding="utf-8",
    )
    grid_file_tools = sys.modules.pop("tools.file_tools", None)

    try:
        loaded = ProjectToolsLoader(str(config_dir), "./tools").load_project_tools()

        assert loaded["system_file_tool"]() == "system"
        assert "tools.file_tools" not in sys.modules

        from tools.function_tools import AVAILABLE_TOOLS  # Grid's own package still imports

        assert AVAILABLE_TOOLS
    finally:
        if grid_file_tools is not None:
            sys.modules["tools.file_tools"] = grid_file_tools


def _system_with_tool(root, module: str, answer: str) -> ProjectToolsLoader:
    tools_dir = root / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / f"{module}.py").write_text(f"def greet():\n    return {answer!r}\n", encoding="utf-8")
    loader = ProjectToolsLoader(str(root), "./tools")
    loader.load_project_tools()
    return loader


def test_each_factory_resolves_the_project_tools_of_its_own_config(tmp_path):
    """Two systems with a tool of the same name: the process-wide loader, left
    on the other system, must not decide which one a factory gets."""
    from types import SimpleNamespace

    from core.agent_factory import AgentFactory
    from core.managers.project_tools_loader import get_project_loader, set_project_loader

    first = _system_with_tool(tmp_path / "first", "first_greetings", "first")
    second = _system_with_tool(tmp_path / "second", "second_greetings", "second")
    factory = object.__new__(AgentFactory)
    factory.config = SimpleNamespace(project_tools_loader=first)
    factory._wrap_tool_with_output_limit = lambda tool, key: tool
    factory.confine_tools = False
    previous = get_project_loader()
    set_project_loader(second)
    try:
        [tool] = factory._resolve_function_tools(["greet", "no_such_tool"])
    finally:
        set_project_loader(previous)

    assert tool() == "first"


def test_added_tools_never_replace_the_loaders_own(tmp_path):
    loader = _system_with_tool(tmp_path / "system", "own_greetings", "own")

    loader.add_tools({"greet": lambda: "added", "wave": lambda: "wave"})

    assert loader.get_tool("greet")() == "own"
    assert loader.get_tool("wave")() == "wave"


@pytest.mark.parametrize("error_type", ["SystemExit", "ImportError"])
def test_failed_import_is_reported_and_retried_without_partial_tools(tmp_path, error_type):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    broken_path = tools_dir / "unavailable.py"
    broken_path.write_text(
        "TOOL_REQUIREMENTS = {'partial_tool': 'Windows desktop required'}\n"
        "TOOL_ISOLATION = {'partial_tool': 'host'}\n"
        "def partial_tool():\n"
        "    return 'must not be exposed'\n"
        f"raise {error_type}('desktop dependency unavailable')\n",
        encoding="utf-8",
    )
    (tools_dir / "healthy.py").write_text(
        "def healthy_tool():\n    return 'ok'\n", encoding="utf-8"
    )
    loader = ProjectToolsLoader(str(tmp_path), "./tools")

    for _ in range(2):
        loaded = loader.load_project_tools()
        assert loaded["healthy_tool"]() == "ok"
        assert "partial_tool" not in loaded
        assert loader.load_errors["unavailable.py"] == (
            f"{error_type}: desktop dependency unavailable"
        )
        assert loader.requirements["partial_tool"] == "Windows desktop required"
        assert loader.isolation["partial_tool"] == "host"
        assert "unavailable" not in loader._module_cache
        assert not any(
            getattr(module, "__file__", None) == str(broken_path)
            for module in tuple(sys.modules.values())
        )

    broken_path.write_text("def partial_tool():\n    return 'repaired'\n", encoding="utf-8")
    loaded = loader.load_project_tools()
    assert loaded["partial_tool"]() == "repaired"
    assert "unavailable.py" not in loader.load_errors


def test_project_tools_loader_does_not_swallow_keyboard_interrupt(tmp_path):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    interrupted_path = tools_dir / "interrupted.py"
    interrupted_path.write_text("raise KeyboardInterrupt()\n", encoding="utf-8")
    try:
        with pytest.raises(KeyboardInterrupt):
            ProjectToolsLoader(str(tmp_path), "./tools").load_project_tools()
    finally:
        for name, module in tuple(sys.modules.items()):
            if getattr(module, "__file__", None) == str(interrupted_path):
                del sys.modules[name]

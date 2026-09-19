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

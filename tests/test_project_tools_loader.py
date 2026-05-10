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

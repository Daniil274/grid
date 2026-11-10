"""A chat that works on a device: its tools, its paths, its prompt and its switch.

The base tools keep their names, arguments and answers, and run on the device
the conversation chose (``execution_target`` in its metadata). Paths are judged
by that device's rules - Windows or POSIX - relative to its working folder, and
what leaves the folder is an outside action the user is asked about.
"""

import asyncio
import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.action_policy import ActionGate
from core.action_routing import route_call
from core.devices import execution
from core.devices import hub as hub_module
from core.devices.hub import DeviceError, DeviceHub
from core.factory.execution import EXECUTION_TARGET_KEY, EXECUTION_WORKDIR_KEY, ExecutionTargets
from core.managers.instructions_builder import InstructionsBuilder
from core.tool_threads import install as install_tool_threads
from grid_node.node import DO, LOOK, RUN, Driver, Node, ToolSpec, text
from schemas.action_policy import ActionPolicyConfig
from tests.test_device_hub import connected
from utils.path_utils import reset_current_factory, set_current_factory
from utils.tool_effects import read, write
from web_chat.session import ChatSession
from web_chat.space import UserSpace

# The example system's tools are plain functions in their own package: loaded by
# path, as a deployment loads them, so `tools` here stays Grid's own package.
TOOLS_DIR = Path(__file__).resolve().parents[1] / "examples" / "coder" / "tools"


def _load(name: str):
    module_name = f"coder_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, TOOLS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Tools run in worker threads and reach the device's session on the server's
# loop; the decorator that arranges it must be installed before they are made.
install_tool_threads()
file_tools = _load("file_tools")
search_tools = _load("search_tools")
bash_tool_module = _load("bash_tool")


# -- the device ---------------------------------------------------------------------------
class DeviceFiles(Driver):
    """A node's file system and shell: ``fs_*`` and ``shell_exec`` as grid-node answers."""

    name = "files"
    capabilities = ["files", "shell"]

    def __init__(self) -> None:
        self.calls = []
        self.files = {}
        self.globs = []
        self.greps = []
        self.shell = {}
        self.packages = {}

    def methods(self):
        return [method for method, _arguments in self.calls]

    def arguments(self, method):
        return [arguments for name, arguments in self.calls if name == method]

    def tools(self):
        async def fs_read(arguments):
            self.calls.append(("fs_read", dict(arguments)))
            return [text(self.files.get(arguments.get("filepath"), ""))]

        async def fs_write(arguments):
            self.calls.append(("fs_write", dict(arguments)))
            self.files[arguments["filepath"]] = arguments.get("content", "")
            return [text(f"Wrote {len(arguments.get('content', ''))} bytes to {arguments['filepath']}.")]

        async def fs_append(arguments):
            self.calls.append(("fs_append", dict(arguments)))
            added = arguments.get("content", "")
            self.files[arguments["filepath"]] = self.files.get(arguments["filepath"], "") + added
            return [text(f"Appended {len(added.encode())} bytes to {arguments['filepath']}.")]

        async def fs_edit(arguments):
            self.calls.append(("fs_edit", dict(arguments)))
            return [text(f"Applied 1 hunk(s) to {arguments['filepath']}.")]

        async def fs_glob(arguments):
            self.calls.append(("fs_glob", dict(arguments)))
            if not self.globs:
                return [text(f'No files matched "{arguments["pattern"]}" in {arguments.get("directory", ".")}.')]
            header = f'{len(self.globs)} match(es) for "{arguments["pattern"]}" in {arguments.get("directory", ".")}:'
            return [text(header + "\n" + "\n".join(self.globs))]

        async def fs_grep(arguments):
            self.calls.append(("fs_grep", dict(arguments)))
            if not self.greps:
                return [text(f'No matches for "{arguments["pattern"]}" in {arguments.get("directory", ".")}.')]
            header = f'{len(self.greps)} match(es) for "{arguments["pattern"]}" in {arguments.get("directory", ".")}:'
            return [text(header + "\n" + "\n".join(self.greps))]

        async def shell_exec(arguments):
            self.calls.append(("shell_exec", dict(arguments)))
            code, out, err = self.shell.get(arguments["command"], (0, "done\n", ""))
            return [text(json.dumps({"exit_code": code, "stdout": out, "stderr": err, "timed_out": False}))]

        async def tool_exec(arguments):
            self.calls.append(("tool_exec", dict(arguments)))
            return [text(self.packages.get(arguments["fn"], "ok"))]

        schema = {"type": "object", "properties": {}}
        return [
            ToolSpec("fs_read", "Read a file", schema, fs_read, LOOK),
            ToolSpec("fs_write", "Write a file", schema, fs_write, RUN),
            ToolSpec("fs_append", "Append to a file", schema, fs_append, RUN),
            ToolSpec("fs_edit", "Edit a file", schema, fs_edit, RUN),
            ToolSpec("fs_glob", "Find files", schema, fs_glob, LOOK),
            ToolSpec("fs_grep", "Search files", schema, fs_grep, LOOK),
            ToolSpec("shell_exec", "Run a command", schema, shell_exec, RUN),
            ToolSpec("tool_exec", "Run a package function", schema, tool_exec, DO),
        ]


def device_node(workdir: str, platform: str):
    """A node that names the folder it works in and the system it is, as grid-node does."""

    class NamedNode(Node):
        def __init__(self, **kwargs):
            kwargs.setdefault("platform", platform)
            super().__init__(**kwargs)

        def hello(self):
            return {**super().hello(), "default_workdir": workdir}

    return NamedNode


@pytest.fixture
def fresh_hub(monkeypatch):
    hub = DeviceHub()
    monkeypatch.setattr(hub_module, "_hub", hub)
    return hub


async def on_device(hub, *, workdir="/home/user/proj", platform="linux-x86_64", driver=None, device_id="dev-1"):
    """(driver, tasks, tunnel) of a device connected to *hub*, working in *workdir*."""
    driver = driver or DeviceFiles()
    tasks, end = await connected(hub, driver, device_id=device_id, user="alice",
                                node_class=device_node(workdir, platform))
    return driver, tasks, end


async def stop(tasks, end):
    await end.close()
    await asyncio.gather(*tasks, return_exceptions=True)


# -- what the tools read ----------------------------------------------------------------
class MemoryStore:
    """The conversation metadata a factory reads, without a store on disk."""

    def __init__(self, conversation="ctx-1"):
        self.metadata = {}
        self.conversation = conversation

    def get_context_metadata(self, context_id):
        return dict(self.metadata.get(context_id, {}))

    def update_context_metadata(self, context_id, updates, **_kwargs):
        self.metadata.setdefault(context_id, {}).update(updates)
        return True

    def get_current_context_id(self):
        return self.conversation


class _Config:
    """The one setting the path policy reads: where this run's files live."""

    def __init__(self, workspace):
        self._workspace = workspace

    def get_working_directory(self):
        return str(self._workspace)


class Chat(ExecutionTargets):
    """A factory as the tools see it: the chat's target and whose devices it may use."""

    def __init__(self, target="cloud", workdir="", user="alice", conversation="ctx-1", workspace=None):
        self.execution_target, self.execution_workdir = target, workdir
        self.config = _Config(workspace or Path.cwd())
        self.execution_user = user
        self._conversation = conversation
        self.context_manager = MemoryStore(conversation)
        self.container_id = None
        if target and target.casefold() != "cloud":
            # A chat that chose a device: the choice lives in its metadata.
            self.context_manager.metadata[conversation] = {
                EXECUTION_TARGET_KEY: target, EXECUTION_WORKDIR_KEY: workdir,
            }

    def get_active_context_id(self):
        return self.context_manager.conversation


@contextmanager
def working(factory):
    """Bind *factory* as the one the tools resolve paths and devices through."""
    set_current_factory(factory)
    try:
        yield factory
    finally:
        reset_current_factory()


async def invoke(tool, **arguments):
    """One tool call as the SDK makes it: the arguments as JSON, the answer as text."""
    return await tool.on_invoke_tool(SimpleNamespace(context=None), json.dumps(arguments))


# -- choosing the target -----------------------------------------------------------------
async def test_a_chat_works_on_its_device_and_the_devices_folder(fresh_hub):
    driver, tasks, end = await on_device(fresh_hub, workdir="/home/user/proj")
    try:
        factory = Chat("device:dev-1")
        target = execution.chosen(factory)
        assert (target.device_id, target.name) == ("dev-1", "test machine")
        assert target.platform == execution.POSIX and target.workdir == "/home/user/proj"
        assert target.user == "alice" and target.holder == "ctx-1"
        # A folder of the chat's own wins over the device's default one.
        chosen_folder = execution.chosen(Chat("device:dev-1", "/home/user/proj/src"))
        assert chosen_folder.workdir == "/home/user/proj/src"
        # A chat that works here names no device at all.
        assert execution.chosen(Chat()) is None
    finally:
        await stop(tasks, end)


async def test_a_device_the_user_does_not_have_is_an_error_never_the_servers_disk(fresh_hub):
    driver, tasks, end = await on_device(fresh_hub)
    try:
        with pytest.raises(DeviceError, match="No device dev-9"):
            execution.chosen(Chat("device:dev-9"))
        # Another user's device is not this chat's either.
        with pytest.raises(DeviceError, match="No device dev-1"):
            execution.chosen(Chat("device:dev-1", user="bob"))
        with working(Chat("device:dev-9")):
            answer = await invoke(file_tools.file_write, filepath="notes.txt", content="x")
        assert answer.startswith("❌ Write error: No device dev-9")
        assert not Path("notes.txt").exists()
        assert driver.methods() == []
    finally:
        await stop(tasks, end)


def test_targets_name_devices_and_the_cloud():
    assert execution.device_id_of("device:dev-1") == "dev-1"
    assert execution.device_id_of("device:../../etc") is None
    assert execution.device_id_of("device:") is None
    assert execution.device_id_of("cloud") is None and execution.device_id_of(None) is None
    assert execution.target_for("dev-1") == "device:dev-1"


# -- the base tools, on the device --------------------------------------------------------
async def test_the_file_tools_run_on_the_device(fresh_hub):
    driver, tasks, end = await on_device(fresh_hub, workdir="D:\\work\\app", platform="windows-amd64")
    try:
        driver.files["src/app.py"] = "line one\nline two\n"
        with working(Chat("device:dev-1")):
            written = await invoke(file_tools.file_write, filepath="src/app.py", content="print(1)\n", overwrite=True)
            read_back = await invoke(file_tools.file_read, filepath="src/app.py")
            appended = await invoke(file_tools.file_append, filepath="src/app.py", content="more\n")
            edited = await invoke(file_tools.file_edit, filepath="src/app.py", patch_content="*** Begin Patch\n")
        # The same answers the tools give here: the device only replaces the disk.
        assert written == "📝 File overwritten: src/app.py (2 lines, 9 bytes)"
        # The write above replaced the file, so the read shows the new content.
        assert read_back.startswith("📄 src/app.py (2 lines):") and "print(1)" in read_back
        assert appended == "✅ Appended to src/app.py: 1 lines, 5 bytes"
        assert edited == "✅ File updated: src/app.py (1 patch hunk(s))"

        assert driver.methods() == ["fs_write", "fs_read", "fs_append", "fs_edit"]
        for arguments in driver.arguments("fs_write"):
            # The device's folder and its freedom of path: the user was already
            # asked about anything outside it by the action policy.
            assert arguments["workdir"] == "D:\\work\\app" and arguments["allow_external"] is True
        assert driver.arguments("fs_write")[0]["filepath"] == "src/app.py"
        assert driver.arguments("fs_write")[0]["content"] == "print(1)\n"
        assert driver.arguments("fs_write")[0]["overwrite"] is True
        assert driver.arguments("fs_append")[0]["content"] == "more\n"
        assert driver.arguments("fs_edit")[0]["patch_content"].startswith("*** Begin Patch")
        assert driver.files["src/app.py"] == "print(1)\nmore\n"
    finally:
        await stop(tasks, end)


async def test_reading_a_part_of_a_file_names_the_lines_it_gives(fresh_hub):
    driver, tasks, end = await on_device(fresh_hub)
    try:
        driver.files["a.txt"] = "one\ntwo\nthree\n"
        with working(Chat("device:dev-1")):
            answer = await invoke(file_tools.file_read, filepath="a.txt", offset=1, limit_lines=2)
        assert answer.startswith("📄 a.txt (lines 2–")
        arguments = driver.arguments("fs_read")[0]
        assert (arguments["offset"], arguments["limit_lines"]) == (1, 2)
    finally:
        await stop(tasks, end)


async def test_glob_and_grep_search_on_the_device(fresh_hub):
    driver, tasks, end = await on_device(fresh_hub)
    try:
        driver.globs = ["src/a.py", "src/b.py"]
        driver.greps = ["src/a.py:3: import os"]
        with working(Chat("device:dev-1")):
            found = await invoke(search_tools.glob_tool, pattern="*.py", directory="src")
            matched = await invoke(search_tools.grep_tool, pattern="import", directory="src")
            # The node has nothing under *.rs, and says so in the tools' words.
            driver.globs = []
            empty = await invoke(search_tools.glob_tool, pattern="*.rs")
        assert found.startswith("🔍 Found 2 results for pattern '*.py':") and "src/a.py" in found
        assert matched.startswith("🔍 Found 1 matches for 'import'") and "import os" in matched
        assert empty == "🔍 Nothing found for pattern '*.rs' in '.'"
        assert driver.arguments("fs_glob")[0] == {
            "pattern": "*.py", "directory": "src", "max_results": 100,
            "workdir": "/home/user/proj", "allow_external": True,
        }
        assert driver.arguments("fs_grep")[0]["pattern"] == "import"
        assert driver.arguments("fs_grep")[0]["case_sensitive"] is False
    finally:
        await stop(tasks, end)


async def test_bash_runs_its_command_on_the_device(fresh_hub):
    driver, tasks, end = await on_device(fresh_hub, workdir="D:\\work\\app", platform="windows-amd64")
    try:
        driver.shell["pytest -q"] = (1, "2 passed\n", "one warning\n")
        driver.shell["timeout 5 sleep 9"] = (0, "", "")
        with working(Chat("device:dev-1")):
            answer = await invoke(bash_tool_module.bash_tool, command="pytest -q", description="run the tests")
        assert "$ pytest -q" in answer and "2 passed" in answer
        assert "stderr:\none warning" in answer and "⚠️ Exit code: 1" in answer
        assert "📋 run the tests" in answer
        arguments = driver.arguments("shell_exec")[0]
        assert arguments["command"] == "pytest -q" and arguments["timeout"] == 60
        assert arguments["working_dir"] == "." and arguments["workdir"] == "D:\\work\\app"
        assert arguments["allow_external"] is True
    finally:
        await stop(tasks, end)


def test_a_timed_out_device_command_says_how_long_was_given():
    answer = bash_tool_module._device_output(
        json.dumps({"timed_out": True}), "sleep 9", "", "", 15
    )
    assert answer == "⏱️ Command timed out on the device after 15 seconds"


# -- tool packages ------------------------------------------------------------------------
FUNCTION_TOOLS = '''
from grid_tool import function_tool


@function_tool(read_only=True)
def count(path: str) -> dict:
    """Count the words of a workspace file.

    Args:
        path: the file, relative to the workspace
    """
    return {"count": len(open(path, encoding="utf-8").read().split())}
'''


@pytest.fixture
def words_package(tmp_path):
    root = tmp_path / "system" / "tools" / "words"
    root.mkdir(parents=True)
    (root / "words.py").write_text(FUNCTION_TOOLS, encoding="utf-8")
    return root


def test_the_archive_a_device_downloads_is_the_one_the_server_serves(words_package):
    import core.tool_packages as packages
    from web_chat import devices_api

    data, digest = packages.package_archive(words_package)
    served, served_digest = devices_api.tool_package_archive(words_package)
    assert digest == served_digest and data == served
    # Once per content: the same tree, the same hash, without packing it again.
    assert packages.archive_digest(words_package) == digest
    assert packages.archive_digest(words_package) == digest
    (words_package / "words.py").write_text(FUNCTION_TOOLS + "\n# changed\n", encoding="utf-8")
    assert packages.archive_digest(words_package) != digest


async def test_a_package_tool_runs_it_on_the_device(fresh_hub, words_package):
    from core.tool_packages import archive_digest, build_package_function_tool

    driver, tasks, end = await on_device(fresh_hub)
    try:
        driver.packages["count"] = '{"count": 3}'
        tool, effect = build_package_function_tool(
            config_dir=words_package.parent.parent, package_rel="tools/words", tool_key="count"
        )
        assert tool is not None and effect.kind == "read"
        factory = Chat("device:dev-1")
        answer = await tool.on_invoke_tool(
            SimpleNamespace(context=SimpleNamespace(factory=factory)), json.dumps({"path": "a.txt"})
        )
        assert answer == '{"count": 3}'
        assert driver.methods() == ["tool_exec"]
        arguments = driver.arguments("tool_exec")[0]
        assert arguments["package"] == "words" and arguments["fn"] == "count"
        assert arguments["hash"] == archive_digest(words_package)
        assert arguments["arguments"] == {"path": "a.txt"}
        assert arguments["workdir"] == "/home/user/proj" and arguments["allow_external"] is True
    finally:
        await stop(tasks, end)


async def test_a_package_tool_of_another_device_is_not_run_on_the_server(fresh_hub, words_package):
    from core.tool_packages import build_package_function_tool

    driver, tasks, end = await on_device(fresh_hub)
    try:
        tool, _effect = build_package_function_tool(
            config_dir=words_package.parent.parent, package_rel="tools/words", tool_key="count"
        )
        answer = await tool.on_invoke_tool(
            SimpleNamespace(context=SimpleNamespace(factory=Chat("device:dev-9"))), json.dumps({"path": "a.txt"})
        )
        assert answer.startswith("❌ Tool error: No device dev-9")
        assert driver.methods() == []
    finally:
        await stop(tasks, end)


# -- paths on the device ------------------------------------------------------------------
def test_windows_paths_are_inside_the_folder_or_outside_it():
    folder = "D:\\work\\app"
    assert execution.locate_within("src\\app.py", folder, execution.WINDOWS) == "src/app.py"
    assert execution.locate_within("src/app.py", folder, execution.WINDOWS) == "src/app.py"
    # Case and separators differ; the folder is the same one.
    assert execution.locate_within("d:\\WORK\\app\\src\\App.py", folder, execution.WINDOWS) == "src/App.py"
    assert execution.locate_within(folder, folder, execution.WINDOWS) == "."
    assert execution.locate_within(".", folder, execution.WINDOWS) == "."
    # Out of the folder, onto another drive, or a drive-relative path: outside.
    assert execution.locate_within("..\\other\\app.py", folder, execution.WINDOWS) is None
    assert execution.locate_within("..\\..\\Windows\\System32\\a.dll", folder, execution.WINDOWS) is None
    assert execution.locate_within("E:\\work\\app\\a.py", folder, execution.WINDOWS) is None
    assert execution.locate_within("C:a.py", folder, execution.WINDOWS) is None
    assert execution.locate_within("D:\\work\\application\\a.py", folder, execution.WINDOWS) is None


def test_posix_paths_are_inside_the_folder_or_outside_it():
    folder = "/home/user/proj"
    assert execution.locate_within("src/app.py", folder, execution.POSIX) == "src/app.py"
    assert execution.locate_within("./src/../src/app.py", folder, execution.POSIX) == "src/app.py"
    assert execution.locate_within("/home/user/proj/src/app.py", folder, execution.POSIX) == "src/app.py"
    assert execution.locate_within(folder, folder, execution.POSIX) == "."
    assert execution.locate_within("../etc/passwd", folder, execution.POSIX) is None
    assert execution.locate_within("/etc/passwd", folder, execution.POSIX) is None
    assert execution.locate_within("/home/user/project2/a.py", folder, execution.POSIX) is None


async def test_the_policy_judges_paths_by_the_devices_folder(fresh_hub, tmp_path):
    driver, tasks, end = await on_device(fresh_hub, workdir="D:\\work\\app", platform="windows-amd64")
    try:
        raw_ctx = SimpleNamespace(factory=Chat("device:dev-1"))
        locate = ActionGate._locator(raw_ctx)
        assert locate("src\\app.py") == "src/app.py"
        assert locate("D:\\work\\app\\src\\app.py") == "src/app.py"
        assert locate("..\\Windows\\a.dll") is None

        policy = ActionPolicyConfig(mode="enforce")
        _name, selected = policy.filter(None)
        inside = route_call(policy, selected, write("filepath"), {"filepath": "src\\app.py"}, (), locate)
        outside = route_call(policy, selected, write("filepath"), {"filepath": "..\\..\\Windows\\a.dll"}, (), locate)
        assert inside.effect == "write"
        assert outside.effect == "external" and "outside the working folder" in outside.reasons
        read_outside = route_call(policy, selected, read("filepath"), {"filepath": "E:\\keys\\id.txt"}, (), locate)
        assert read_outside.effect == "external"

        # Here - on the server - the same argument is judged by the workspace.
        here = ActionGate._locator(SimpleNamespace(factory=Chat(workspace=tmp_path)))
        assert here("src/app.py") == "src/app.py"
        assert here("/etc/passwd") is None
    finally:
        await stop(tasks, end)


# -- the prompt ---------------------------------------------------------------------------
def test_the_prompt_says_where_the_work_happens():
    builder = InstructionsBuilder(config=None, context_manager=None)
    builder.execution_target_info = {
        "device": "dev-1", "name": "Alice's PC", "platform": "windows",
        "platform_label": "Windows", "workdir": "D:\\work\\app",
    }
    prompt = builder.build_path_context()
    assert prompt.startswith("Path information:")
    assert "directly on the user's device Alice's PC (Windows)" in prompt
    assert "Working folder on that device: D:\\work\\app" in prompt
    assert "file_read, file_write, file_edit, file_append, glob_tool, grep_tool and bash_tool run on the device" in prompt
    assert "Windows: paths may use backslashes" in prompt and "PowerShell" in prompt
    assert "path outside the working folder" in prompt
    assert "/workspace" not in prompt


def test_the_prompt_of_a_linux_device_says_so():
    builder = InstructionsBuilder(config=None, context_manager=None)
    builder.execution_target_info = lambda: {
        "device": "dev-1", "name": "Build box", "platform": "posix",
        "platform_label": "Linux/macOS", "workdir": "/home/user/proj",
    }
    prompt = builder.build_path_context()
    assert "Build box (Linux/macOS)" in prompt and "/home/user/proj" in prompt
    assert "Linux or macOS" in prompt and "PowerShell" not in prompt


def test_a_chat_that_works_here_gets_the_servers_paths():
    builder = InstructionsBuilder(config=None, context_manager=None)
    builder.execution_target_info = lambda: None
    prompt = builder.build_path_context()
    assert "Path information:" in prompt and "the user's device" not in prompt


def test_a_prompt_never_fails_a_run_when_the_device_is_gone():
    builder = InstructionsBuilder(config=None, context_manager=None)

    def broken():
        raise RuntimeError("hub is down")

    builder.execution_target_info = broken
    assert "Path information:" in builder.build_path_context()


# -- the factory keeps the choice ---------------------------------------------------------
async def test_the_choice_lives_in_the_conversation_and_no_one_elses(fresh_hub):
    driver, tasks, end = await on_device(fresh_hub)
    try:
        factory = Chat(conversation="ctx-1")
        assert factory.execution_target_of() == ("cloud", "")
        assert factory.target_device_id() is None and factory.execution_device() is None

        assert factory.set_execution_target("device:dev-1")
        assert factory.execution_target_of() == ("device:dev-1", "/home/user/proj")
        assert factory.context_manager.metadata["ctx-1"] == {
            EXECUTION_TARGET_KEY: "device:dev-1", EXECUTION_WORKDIR_KEY: "/home/user/proj",
        }
        assert factory.target_device_id() == "dev-1"
        assert factory.execution_device()["name"] == "test machine"
        assert factory.execution_device()["workdir"] == "/home/user/proj"
        assert factory.execution_value() == {"target": "device:dev-1", "workdir": "/home/user/proj"}

        # Another chat of the same space works where it did: here.
        assert factory.execution_target_of("ctx-2") == ("cloud", "")
        assert factory.set_execution_target("device:dev-9") is False
        assert factory.execution_target_of() == ("device:dev-1", "/home/user/proj")

        assert factory.set_execution_target("cloud")
        assert factory.execution_target_of() == ("cloud", "")
        assert not factory.execution_device()
    finally:
        await stop(tasks, end)


async def test_a_subagent_works_where_its_chat_does(fresh_hub):
    """Orchestrate and parallel run their agents in the chat's own context."""
    driver, tasks, end = await on_device(fresh_hub)
    try:
        factory = Chat("device:dev-1", user="alice", conversation="ctx-1")
        with working(factory):
            # The same factory, the same conversation: what a nested agent's
            # tools read, in its own thread.
            answer = await invoke(file_tools.file_read, filepath="a.txt")
        assert driver.methods() == ["fs_read"]
        assert "a.txt" in answer
    finally:
        await stop(tasks, end)


# -- the chat's switch --------------------------------------------------------------------
def space_with_devices(factory=None):
    space = object.__new__(UserSpace)
    factory = factory or SimpleNamespace(
        set_execution_target=Mock(return_value=True), execution_target_of=lambda *a: ("cloud", ""),
    )
    space.registry = SimpleNamespace(built_factories=lambda: {"coder": factory})
    space._chosen = {}
    space.user_id = "alice"
    stored = {}

    def store_metadata(context_id, updates, **_kwargs):
        stored.setdefault(context_id, {}).update(updates)
        return True

    space.conversations = SimpleNamespace(
        update_context_metadata=store_metadata,
        get_context_metadata=lambda context_id: dict(stored.get(context_id, {})),
    )
    return space, factory, stored


async def test_the_space_offers_the_devices_of_the_user_and_keeps_the_choice(fresh_hub):
    driver, tasks, end = await on_device(fresh_hub)
    try:
        space, factory, stored = space_with_devices()
        choices = space.device_choices()
        assert [(item["device"], item["name"], item["platform"], item["default_workdir"]) for item in choices] == [
            ("dev-1", "test machine", "linux-x86_64", "/home/user/proj")
        ]

        assert space.set_execution_target("ctx-1", "device:dev-1")
        assert stored["ctx-1"] == {
            EXECUTION_TARGET_KEY: "device:dev-1", EXECUTION_WORKDIR_KEY: "/home/user/proj",
        }
        factory.set_execution_target.assert_called_once_with(
            "device:dev-1", "/home/user/proj", context_id="ctx-1", user_id="alice"
        )
        state = space.execution_target("ctx-1")
        assert state["target"] == "device:dev-1" and state["workdir"] == "/home/user/proj"
        assert [item["device"] for item in state["devices"]] == ["dev-1"]

        # A device this user does not have connected now changes nothing.
        assert not space.set_execution_target("ctx-1", "device:dev-9")
        assert not space.set_execution_target("ctx-1", "somewhere else")
        assert space.execution_target("ctx-1")["target"] == "device:dev-1"

        assert space.set_execution_target("ctx-1", "cloud")
        assert space.execution_target("ctx-1") == {
            "target": "cloud", "workdir": "", "devices": state["devices"],
        }
    finally:
        await stop(tasks, end)


def test_a_space_without_devices_offers_none(monkeypatch):
    monkeypatch.setattr(hub_module, "_hub", DeviceHub())
    space, _factory, _stored = space_with_devices()
    assert space.device_choices() == []
    assert space.execution_target("ctx-1") == {"target": "cloud", "workdir": "", "devices": []}
    assert not space.set_execution_target("ctx-1", "device:dev-1")


class Socket:
    def __init__(self):
        self.sent = []

    async def send_json(self, event):
        self.sent.append(event)


def session_with(space):
    space.context_manager = lambda: SimpleNamespace(
        ensure_context=lambda context_id: None, revision_trusted_ephemeral=True
    )
    socket = Socket()
    return ChatSession(space, socket, "ctx-1"), socket


async def test_the_socket_moves_the_chat_onto_a_device():
    state = {"target": "device:dev-1", "workdir": "/home/user/proj", "devices": []}
    space = SimpleNamespace(
        set_execution_target=Mock(return_value=True),
        execution_target=Mock(return_value=state),
    )
    session, socket = session_with(space)
    await session._dispatch(
        json.dumps({"action": "execution_target", "target": "device:dev-1", "workdir": "/home/user/proj"})
    )
    space.set_execution_target.assert_called_once_with("ctx-1", "device:dev-1", "/home/user/proj")
    assert socket.sent == [{"type": "execution_target", "ok": True, **state}]


async def test_a_refused_switch_answers_with_the_state_the_chat_is_in():
    state = {"target": "cloud", "workdir": "", "devices": []}
    space = SimpleNamespace(
        set_execution_target=Mock(return_value=False),
        execution_target=Mock(return_value=state),
    )
    session, socket = session_with(space)
    await session._dispatch(json.dumps({"action": "execution_target", "target": "device:dev-9"}))
    space.set_execution_target.assert_called_once_with("ctx-1", "device:dev-9", "")
    assert socket.sent == [{"type": "execution_target", "ok": False, **state}]


async def test_the_socket_reports_where_the_chat_works():
    state = {"target": "device:dev-1", "workdir": "/home/user/proj", "devices": []}
    space = SimpleNamespace(set_execution_target=Mock(), execution_target=Mock(return_value=state))
    session, socket = session_with(space)
    await session._dispatch(json.dumps({"action": "execution_target"}))
    space.set_execution_target.assert_not_called()
    assert socket.sent == [{"type": "execution_target", "ok": True, **state}]

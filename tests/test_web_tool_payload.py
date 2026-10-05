"""All registered tool names share typed previews, including unknown MCP tools."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agents import RunItemStreamEvent

from web_chat.observer import WebStreamObserver
from web_chat.payload import MAX_IMAGES, MAX_PARTS, MAX_TEXT, normalize_payload
from web_chat.tool_summaries import TOOL_TITLES
from web_chat.trace import TraceRecorder, normalize_steps


def registered_names():
    # Inspect the actual shared registry dictionaries without executing tools,
    # loading integrations, reading credentials or depending on live servers.
    names = set(TOOL_TITLES)
    for path in (Path(__file__).parents[1] / "tools").glob("*_tools.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict) and any(
                isinstance(target, ast.Name) and target.id.endswith("_TOOLS") for target in node.targets
            ):
                names.update(key.value for key in node.value.keys if isinstance(key, ast.Constant) and isinstance(key.value, str))
    return sorted(names | {"unknown_future_tool", "remote.custom", "system_agent"})


@pytest.mark.parametrize("tool", registered_names())
@pytest.mark.parametrize("value", [None, "", False, 0, [], {}, {"ok": True, "items": [1, None]}, "plain <script> text"])
def test_every_tool_has_serializable_fallback(tool, value):
    payload = normalize_payload(value, tool=tool)
    json.dumps(payload, allow_nan=False)
    assert payload["version"] == 1
    assert payload["parts"]
    assert payload["parts"][0]["data"] == value
    assert not payload["truncated"]


@pytest.mark.parametrize("value", ["{invalid", "[1,", "NaN", "Infinity", "{\"x\": NaN}"])
def test_invalid_json_stays_text(value):
    assert normalize_payload(value)["parts"][0] == {"kind": "text", "media_type": "text/plain", "data": value}


def test_json_preserves_native_types_and_exact_source():
    source = '{"false":false,"zero":0,"empty":"","null":null,"items":[]}'
    p = normalize_payload(source)
    assert p["parts"][0]["data"] == json.loads(source)
    assert p["raw_text"] == source
    assert normalize_payload('"string"')["parts"][0]["data"] == "string"


def test_request_separates_command_diff_code_and_keeps_other_parameters():
    args = {"filepath": "test.py", "content": "print(1)", "patch_content": "@@\n-old\n+new", "timeout": 30}
    p = normalize_payload(args, role="input", tool="file_edit")
    assert p["parts"][0]["data"] == {"filepath": "test.py", "content": "print(1)", "timeout": 30}
    assert {part["kind"] for part in p["parts"]} == {"json", "diff"}
    assert json.loads(p["raw_text"]) == args
    assert p["parts"][0]["data"]["content"] == "print(1)"
    command = normalize_payload({"command": "echo hi"}, role="input", tool="bash_tool")
    assert command["parts"][-1]["language"] == "bash"


def test_verified_file_wrapper_not_errors_gets_code_view():
    for tool in ("read_file", "file_read", "read"):
        p = normalize_payload("📄 File content src/a.py:\n\nprint(1)", tool=tool)
        assert p["parts"][0] == {"kind": "code", "media_type": "text/plain", "data": "print(1)", "name": "src/a.py", "language": "python"}
    assert normalize_payload("❌ File missing", tool="read_file")["parts"][0]["kind"] == "text"
    assert normalize_payload("print(1)", tool="read_file")["parts"][0]["kind"] == "text"


def test_long_json_falls_back_to_bounded_text_not_broken_json():
    p = normalize_payload({"content": "x" * (MAX_TEXT * 2)})
    assert p["truncated"]
    assert p["parts"][0]["kind"] == "text"
    assert len(p["parts"][0]["data"]) == MAX_TEXT
    assert len(p["raw_text"]) == MAX_TEXT
    assert p["original_size"] > MAX_TEXT


def test_nested_and_cyclic_values_do_not_break_a_run():
    value = {}
    value["cycle"] = value
    p = normalize_payload(value)
    assert p["truncated"]
    assert p["original_size"] is None
    json.dumps(p)
    assert normalize_payload(float("nan"))["parts"][0]["data"] == "nan"
    assert normalize_payload('{"n":1e999}')["parts"][0]["data"] == {"n": "inf"}


def test_mcp_mixed_blocks_resources_and_structured_data():
    p = normalize_payload({"isError": False, "structuredContent": {"count": 1}, "content": [
        {"type": "text", "text": '{"ok":true}'},
        {"type": "text", "mimeType": "text/markdown", "text": "**hello**"},
        {"type": "image", "mimeType": "image/png", "data": "YWJj"},
        {"type": "resource", "resource": {"uri": "file:///x.md", "mimeType": "text/markdown", "text": "# File"}},
    ]})
    assert [x["kind"] for x in p["parts"]] == ["json", "json", "markdown", "image", "resource", "markdown", "json"]
    assert p["parts"][3]["data"] == "data:image/png;base64,YWJj"
    assert "YWJj" not in p["raw_text"]
    assert "# File" in p["raw_text"]
    assert normalize_payload({"content": [1, 2]})["parts"][0]["data"] == {"content": [1, 2]}


def test_malformed_mcp_resource_unknown_blocks_and_empty_content_survive():
    for value in ([{"type": "resource", "resource": None}], [{"type": []}],
                  {"content": [], "isError": True}, [{"type": "future", "x": 1}]):
        assert normalize_payload(value)["parts"]
    assert normalize_payload({"content": [], "isError": True})["is_error"]


def test_total_image_and_part_budgets_are_bounded():
    value = [{"type": "image", "mimeType": "image/png", "data": "a" * 250000}] * 8
    p = normalize_payload(value)
    assert p["truncated"]
    assert sum(len(x["data"]) for x in p["parts"] if x["kind"] == "image") <= MAX_IMAGES
    p = normalize_payload([{"type": "text", "text": "x"}] * 100)
    assert p["truncated"] and len(p["parts"]) <= MAX_PARTS


def test_sdk_models_and_stdout_are_structured():
    class Model:
        def model_dump(self, **kwargs):
            return {"stdout": "hello", "stderr": "", "exit_code": 0}
    p = normalize_payload(Model())
    assert [x.get("name") for x in p["parts"]] == ["Execution", "stdout", "stderr"]


def event(name, call_id, *, tool="", args=None, output=None):
    item = SimpleNamespace(raw_item={"name": tool, "call_id": call_id, "arguments": args}, output=output)
    return RunItemStreamEvent(name=name, item=item)


def test_observer_preserves_types_through_live_snapshot_and_replay():
    emitted = []
    recorder = TraceRecorder(emitted.append)
    obs = WebStreamObserver(recorder, emit_token=lambda _: None)
    obs.handle_event(event("tool_called", "one", tool="custom", args='{"path":"x"}'))
    obs.handle_event(event("tool_output", "one", output={"ok": False, "n": 0}))
    row = recorder.snapshot()[0]
    assert row["input_payload"]["parts"][0]["data"] == {"path": "x"}
    assert row["result_payload"]["parts"][0]["data"] == {"ok": False, "n": 0}
    assert emitted[-1]["step"]["result_payload"] == row["result_payload"]
    assert normalize_steps(json.loads(json.dumps(recorder.snapshot())))[0] == row
    assert row["detail"] and row["body"]  # Old clients still work.


def test_orphan_mcp_error_and_agent_report_keep_typed_results():
    recorder = TraceRecorder(lambda _: None)
    obs = WebStreamObserver(recorder, emit_token=lambda _: None)
    obs.handle_event(event("tool_output", "orphan", output='{"isError":true,"content":[]}'))
    assert recorder.snapshot()[0]["status"] == "error"
    child = obs.nested("coder", "agent")
    obs.handle_event(event("tool_called", "agent", tool="coder", args='{"input":"Fix **test**"}'))
    child.finish()
    obs.handle_event(event("tool_output", "agent", output="**Fixed**"))
    row = recorder.snapshot()[-1]
    assert row["input_payload"]["parts"][-1]["kind"] == "markdown"
    assert row["result_payload"]["parts"][0]["kind"] == "markdown"



def test_unknown_tool_arguments_are_not_reclassified():
    # A plain string field named content is only extracted for verified
    # file-write schemas; other tools keep their arguments as parameters.
    generic = normalize_payload({"content": "plain text"}, role="input", tool="notebook_create")
    assert generic["parts"][0]["data"]["content"] == "plain text"
    written = normalize_payload({"filepath": "a.py", "content": "print(1)"}, role="input", tool="write_file")
    assert {p["kind"] for p in written["parts"]} == {"json", "code"}
    assert next(p for p in written["parts"] if p.get("name") == "content")["language"] == "python"

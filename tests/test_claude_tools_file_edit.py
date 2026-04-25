import importlib.util
from pathlib import Path


MODULE_PATH = Path("/home/user/grid/examples/claude-tools/tools/file_tools.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("claude_tools_file_tools", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_apply_unified_patch_accepts_slightly_shifted_hunk():
    module = _load_module()
    original = (
        "header\n"
        "\n"
        "    case POWER_CONNECTED:\n"
        "        // old comment\n"
        "        bqChip_->enableCharging(true);\n"
        "        applyVINDPM(info_.vbus_type, info_.vbusv_mV);\n"
        "        break;\n"
    )
    patch = (
        "@@ -2,4 +2,5 @@\n"
        "     case POWER_CONNECTED:\n"
        "-        // old comment\n"
        "+        // new comment\n"
        "         bqChip_->enableCharging(true);\n"
        "+        bqChip_->forceDPDM();\n"
        "         applyVINDPM(info_.vbus_type, info_.vbusv_mV);\n"
    )

    updated = module._apply_unified_patch(original, patch)

    assert "// new comment" in updated
    assert "bqChip_->forceDPDM();" in updated
    assert "// old comment" not in updated


def test_apply_unified_patch_validates_context_lines():
    module = _load_module()
    original = (
        "line 1\n"
        "actual context\n"
        "line 3\n"
    )
    patch = (
        "@@ -2,2 +2,2 @@\n"
        " wrong context\n"
        "-line 3\n"
        "+line three\n"
    )

    try:
        module._apply_unified_patch(original, patch)
    except module._PatchApplyError as exc:
        message = str(exc)
    else:
        raise AssertionError("Patch unexpectedly applied")

    assert "Patch context not found" in message
    assert "wrong context" in message


def test_apply_unified_patch_supports_sequential_hunks_with_offset():
    module = _load_module()
    original = (
        "a\n"
        "b\n"
        "c\n"
        "d\n"
        "e\n"
    )
    patch = (
        "@@ -1,2 +1,3 @@\n"
        " a\n"
        " b\n"
        "+x\n"
        "@@ -4,2 +5,2 @@\n"
        " d\n"
        "-e\n"
        "+y\n"
    )

    updated = module._apply_unified_patch(original, patch)

    assert updated == "a\nb\nx\nc\nd\ny\n"

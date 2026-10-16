"""Media reach the models that accept them; an agent none of whose models does is told so.

Images, audio, video and PDF documents read by tools (core.media) or attached
by the user go to the first model of the agent that accepts the modality
(core.fallback_model, core.model_catalog); where none does, the agent reads an
error in their place - never a guess, never a video cut into frames.
"""

import asyncio
import base64
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from agents.tool import ToolOutputFileContent, ToolOutputImage, ToolOutputText

from core import fallback_model
from core.context_budget import MEDIA_TOKENS, request_tokens
from core.fallback_model import FallbackModel, ModelCandidate
from core.image_window import MAX_OTHER_MEDIA, limit_images
from core.media import MAX_IMAGE_SIDE, media_output, sniff
from core.modalities import AUDIO, IMAGE, PDF, TEXT, VIDEO, replace_modalities, request_modalities
from core.model_catalog import ModalityCatalog
from core.vision_model import VisionChatCompletionsModel

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40


def data_url(mime, data=b"x"):
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


IMAGE_PART = {"type": "input_image", "image_url": data_url("image/png"), "detail": "auto"}
AUDIO_PART = {"type": "input_file", "file_data": data_url("audio/wav", WAV), "filename": "note.wav"}
VIDEO_PART = {"type": "input_file", "file_data": data_url("video/mp4"), "filename": "clip.mp4"}
PDF_PART = {"type": "input_file", "file_data": data_url("application/pdf"), "filename": "doc.pdf"}


def tool_result(*parts):
    return [
        {"role": "user", "content": "look at the file"},
        {"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "📎 file"}, *parts]},
    ]


# -- what a request carries ---------------------------------------------------


def test_the_modalities_of_a_request_are_found_in_messages_and_tool_results():
    items = [{"role": "user", "content": [{"type": "input_text", "text": "hi"}, IMAGE_PART]}, *tool_result(AUDIO_PART, VIDEO_PART, PDF_PART)]
    assert request_modalities(items) == {IMAGE, AUDIO, VIDEO, PDF}
    assert request_modalities([{"role": "user", "content": "text"}]) == frozenset()
    chat_shapes = [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "", "format": "wav"}},
                                                 {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,"}}]}]
    assert request_modalities(chat_shapes) == {AUDIO, VIDEO}


def test_parts_of_a_modality_are_replaced_on_a_copy():
    items = tool_result(IMAGE_PART, AUDIO_PART)
    replaced = replace_modalities(items, frozenset({AUDIO}), lambda m: f"no {m}")
    assert request_modalities(replaced) == {IMAGE}
    assert replaced[1]["output"][2] == {"type": "input_text", "text": "no audio"}
    assert request_modalities(items) == {IMAGE, AUDIO}  # the original is as it was


# -- which model gets it ------------------------------------------------------


class Recorder:
    def __init__(self, refuse=None):
        self.refuse = refuse
        self.inputs = []

    async def get_response(self, *args, **kwargs):
        self.inputs.append(args[1])
        if self.refuse == "fail":
            raise RuntimeError("Upstream 503")
        if self.refuse and self.refuse in request_modalities(args[1]):
            raise RuntimeError(f"[400] This model does not support {self.refuse} input")
        return "ok"

    async def stream_response(self, *args, **kwargs):
        yield await self.get_response(*args, **kwargs)


@pytest.fixture(autouse=True)
def forget_refusals():
    for keys in fallback_model._REFUSED.values():
        keys.clear()
    yield
    for keys in fallback_model._REFUSED.values():
        keys.clear()


def candidate(key, model, modalities=None, vision=None):
    return ModelCandidate(key=key, model=model, settings=SimpleNamespace(), vision=vision,
                          modalities=frozenset(modalities) if modalities is not None else None)


def texts(items):
    out = []
    for item in items:
        for key in ("content", "output"):
            parts = item.get(key) if isinstance(item, dict) else None
            if isinstance(parts, list):
                out += [p.get("text", "") for p in parts if isinstance(p, dict)]
    return " ".join(out)


async def test_a_request_goes_to_the_first_model_that_accepts_what_it_carries():
    glm, gemini = Recorder(), Recorder()
    model = FallbackModel([candidate("glm", glm, {TEXT}), candidate("gemini", gemini, {TEXT, IMAGE, AUDIO, VIDEO})])

    assert await model.get_response(None, tool_result(AUDIO_PART), None) == "ok"

    assert glm.inputs == [] and request_modalities(gemini.inputs[0]) == {AUDIO}


async def test_an_agent_none_of_whose_models_takes_a_modality_is_told_so():
    glm, deepseek = Recorder(), Recorder()
    model = FallbackModel([candidate("glm", glm, {TEXT}), candidate("deepseek", deepseek, {TEXT, IMAGE})])

    await model.get_response(None, tool_result(VIDEO_PART), None)

    sent = (glm.inputs or deepseek.inputs)[0]
    assert request_modalities(sent) == frozenset()
    note = texts(sent)
    assert "this agent cannot take this video" in note and "(glm, deepseek)" in note
    assert "Do not guess its content" in note


async def test_video_is_not_turned_into_frames_for_a_model_that_only_sees():
    deepseek = Recorder()
    model = FallbackModel([candidate("deepseek", deepseek, {TEXT, IMAGE})])

    await model.get_response(None, tool_result(VIDEO_PART, IMAGE_PART), None)

    # The image goes, the video does not: no frames, no guess.
    assert request_modalities(deepseek.inputs[0]) == {IMAGE}
    assert "cannot take this video" in texts(deepseek.inputs[0])


async def test_audio_video_and_pdf_go_only_to_a_model_known_to_take_them():
    unknown = Recorder()
    model = FallbackModel([candidate("local", unknown)])  # nothing known about it

    await model.get_response(None, tool_result(IMAGE_PART, AUDIO_PART, PDF_PART), None)

    # An image is tried until refused; audio and PDF are not sent on a guess.
    assert request_modalities(unknown.inputs[0]) == {IMAGE}


async def test_when_the_models_that_accept_it_fail_the_others_answer_with_a_note():
    gemini, glm = Recorder(refuse="fail"), Recorder()
    model = FallbackModel([candidate("gemini", gemini, {TEXT, AUDIO}), candidate("glm", glm, {TEXT})])

    assert await model.get_response(None, tool_result(AUDIO_PART), None) == "ok"

    assert request_modalities(glm.inputs[0]) == frozenset()
    assert "the models that accept audio input (gemini) failed this request" in texts(glm.inputs[0])


async def test_a_provider_refusing_a_modality_is_believed_for_the_process():
    flaky, spare = Recorder(refuse=AUDIO), Recorder()
    model = FallbackModel([candidate("flaky", flaky, {TEXT, AUDIO}), candidate("spare", spare, {TEXT, AUDIO})])

    await model.get_response(None, tool_result(AUDIO_PART), None)
    assert len(flaky.inputs) == 1 and request_modalities(spare.inputs[0]) == {AUDIO}

    await model.get_response(None, tool_result(AUDIO_PART), None)
    assert len(flaky.inputs) == 1  # not asked again with audio


# -- what a model accepts -----------------------------------------------------

SNAPSHOT = {
    "providers": {
        "openrouter": {"api": "https://openrouter.ai/api/v1", "models": {
            "google/gemini-x": ["audio", "image", "pdf", "text", "video"], "z-ai/glm-x": ["text"]}},
        "opencode": {"api": "https://opencode.ai/inference/v1", "models": {"glm-x-flash": ["image", "text", "video"]}},
        "opencode-go": {"api": "https://opencode.ai/inference/go/v1", "models": {"glm-x-flash": ["image", "text"]}},
        "openai": {"api": None, "models": {"gpt-x": ["image", "pdf", "text"]}},
        "google": {"api": None, "models": {"gemini-y": ["image", "text", "audio"]}},
    }
}


def test_the_catalog_finds_a_model_by_endpoint_host_hint_or_id():
    catalog = ModalityCatalog.from_snapshot(SNAPSHOT)
    assert catalog.lookup("https://openrouter.ai/api/v1/", "google/gemini-x") == {TEXT, IMAGE, AUDIO, VIDEO, PDF}
    # Another plan at the same host: only what all its listings agree on.
    assert catalog.lookup("https://opencode.ai/zen/go/v1", "glm-x-flash") == {TEXT, IMAGE}
    # A provider without an endpoint in the catalog, by the hint its config gives (price_source).
    assert catalog.lookup("https://api.openai.com/v1", "gpt-x", "openai") == {TEXT, IMAGE, PDF}
    assert catalog.lookup("https://api.openai.com/v1", "gpt-x") == {TEXT, IMAGE, PDF}  # by id alone
    # A gateway id the gateway does not list: the vendor's own listing.
    assert catalog.lookup("https://openrouter.ai/api/v1", "google/gemini-y") == {TEXT, IMAGE, AUDIO}
    assert catalog.lookup("http://localhost:1234/v1", "my-model") is None


def test_what_a_model_accepts_comes_from_its_config_then_the_catalog(monkeypatch):
    from core.factory import models as models_module
    from core.factory.models import ModelProvider

    monkeypatch.setattr(models_module, "model_catalog", lambda: ModalityCatalog.from_snapshot(SNAPSHOT))
    providers = {
        "or": SimpleNamespace(base_url="https://openrouter.ai/api/v1", auth="api_key", price_source=None),
        "cli": SimpleNamespace(base_url="", auth="claude", price_source=None),
    }
    models = {
        "gemini": SimpleNamespace(name="google/gemini-x", provider="or", vision=None, input_modalities=None),
        "glm-seeing": SimpleNamespace(name="z-ai/glm-x", provider="or", vision=True, input_modalities=None),
        "gemini-blind": SimpleNamespace(name="google/gemini-x", provider="or", vision=False, input_modalities=None),
        "declared": SimpleNamespace(name="z-ai/glm-x", provider="or", vision=None, input_modalities=["audio"]),
        "claude": SimpleNamespace(name="sonnet", provider="cli", vision=True, input_modalities=["image"]),
        "unknown": SimpleNamespace(name="mystery", provider="or", vision=None, input_modalities=None),
    }
    provider = ModelProvider.__new__(ModelProvider)
    provider.config = SimpleNamespace(get_model=models.__getitem__, get_provider=providers.__getitem__)

    assert provider.input_modalities("gemini") == {TEXT, IMAGE, AUDIO, VIDEO, PDF}
    assert provider.input_modalities("glm-seeing") == {TEXT, IMAGE}
    assert provider.input_modalities("gemini-blind") == {TEXT, AUDIO, VIDEO, PDF}
    assert provider.input_modalities("declared") == {TEXT, AUDIO}
    assert provider.input_modalities("claude") == {TEXT}  # the CLI takes text only
    assert provider.input_modalities("unknown") is None


def test_the_shipped_catalog_knows_the_models_grid_runs_on():
    from core.model_catalog import catalog

    known = catalog()
    assert known.lookup("https://openrouter.ai/api/v1", "google/gemini-4-argon") >= {IMAGE, AUDIO, VIDEO}
    assert known.lookup("https://opencode.ai/zen/go/v1", "glm-5.3") == {TEXT}
    assert known.lookup("https://opencode.ai/zen/go/v1", "deepseek-v4.1-flash") == {TEXT, IMAGE}


# -- files as media -----------------------------------------------------------


def _png(width, height):
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(out, format="PNG")
    return out.getvalue()


def test_a_picture_is_read_as_an_image_scaled_to_the_limit(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(_png(4000, 1000))

    header, image = media_output(path, "shot.png")

    assert isinstance(header, ToolOutputText) and "image, image/png" in header.text
    assert f"scaled to {MAX_IMAGE_SIDE}x512" in header.text
    assert isinstance(image, ToolOutputImage) and image.image_url.startswith("data:image/png;base64,")


def test_audio_video_and_pdf_are_read_as_files_of_their_type(tmp_path):
    for name, data, mime in (
        ("note.wav", WAV, "audio/wav"),
        ("talk.mp3", b"ID3\x04" + b"\x00" * 60, "audio/mpeg"),
        ("clip.mp4", b"\x00\x00\x00\x20ftypisom" + b"\x00" * 60, "video/mp4"),
        ("doc.pdf", b"%PDF-1.7\n" + b"x" * 60, "application/pdf"),
    ):
        path = tmp_path / name
        path.write_bytes(data)
        header, media = media_output(path, name)
        assert isinstance(media, ToolOutputFileContent), name
        assert media.file_data.startswith(f"data:{mime};base64,") and media.filename == name


def test_media_that_cannot_be_sent_are_named_not_sent(tmp_path, monkeypatch):
    from core import media

    mkv = tmp_path / "film.mkv"
    mkv.write_bytes(b"\x1a\x45\xdf\xa3" + b"matroska" + b"\x00" * 60)
    assert media_output(mkv, "film.mkv").startswith("❌ film.mkv is video in a format models are not sent")

    big = tmp_path / "long.wav"
    big.write_bytes(WAV + b"\x00" * 2048)
    monkeypatch.setitem(media._LIMITS, AUDIO, 1024)
    assert "larger than the" in media_output(big, "long.wav")

    text = tmp_path / "notes.txt"
    text.write_text("BMW and MP3 are not file signatures here")
    assert media_output(text, "notes.txt") is None


def _coder_file_read():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "coder" / "tools"))
    try:
        import file_tools as coder_file_tools  # noqa: E402 - the coder's project tools
    finally:
        sys.path.pop(0)
    return coder_file_tools.file_read


@pytest.mark.parametrize("name, tool", [("read_file", None), ("file_read", "coder")])
def test_the_file_tools_hand_media_to_the_model(tmp_path, monkeypatch, name, tool):
    from tools import file_tools

    monkeypatch.chdir(tmp_path)
    (tmp_path / "shot.png").write_bytes(_png(10, 10))
    (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02binary")
    (tmp_path / "notes.txt").write_text("plain text")
    function = _coder_file_read() if tool == "coder" else file_tools.read_file

    def read(path):
        return asyncio.run(function.on_invoke_tool(  # the tool as the SDK runs it
            SimpleNamespace(context=None, tool_name=name, tool_call_id="t"), json.dumps({"filepath": path})
        ))

    image = read("shot.png")
    assert isinstance(image, list) and isinstance(image[1], ToolOutputImage)
    assert "binary file" in read("data.bin")
    assert "plain text" in read("notes.txt")


def test_the_trace_names_an_attachment_without_keeping_its_data():
    from web_chat.payload import normalize_payload

    big = ToolOutputFileContent(file_data=data_url("audio/wav", WAV * 5000), filename="note.wav")
    payload = normalize_payload([ToolOutputText(text="📎 note.wav"), big], tool="file_read")

    assert "note.wav (audio/wav) - sent to the model" in payload["raw_text"]
    assert len(json.dumps(payload)) < 2000


# -- in the shape of the provider ---------------------------------------------


def test_tool_result_media_move_to_a_user_message_in_the_providers_shape():
    wav = data_url("audio/wav", WAV)
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": [
            {"type": "text", "text": "📎 note.wav"},
            {"type": "file", "file": {"file_data": wav, "filename": "note.wav"}},
            {"type": "file", "file": {"file_data": data_url("video/mp4"), "filename": "clip.mp4"}},
            {"type": "file", "file": {"file_data": data_url("application/pdf"), "filename": "doc.pdf"}},
        ]},
    ]

    out = VisionChatCompletionsModel._extract_images_from_tool_messages(messages)

    assert out[1] == {"role": "tool", "tool_call_id": "c1", "content": "📎 note.wav"}
    media = out[2]["content"]
    assert out[2]["role"] == "user" and media[0] == {"type": "text", "text": "[Tool result media]"}
    assert media[1] == {"type": "input_audio", "input_audio": {"data": wav.split(",", 1)[1], "format": "wav"}}
    assert media[2] == {"type": "video_url", "video_url": {"url": data_url("video/mp4")}}
    assert media[3]["type"] == "file"


# -- what media cost and how many go out --------------------------------------


def test_media_count_as_media_not_as_their_base64():
    big_audio = {"type": "input_file", "file_data": data_url("audio/wav", WAV * 50_000), "filename": "a.wav"}
    assert request_tokens(tool_result(big_audio)) < MEDIA_TOKENS[AUDIO] + 100


def test_only_the_newest_heavy_media_go_out_with_a_request():
    items = [*tool_result(AUDIO_PART), *tool_result(VIDEO_PART), *tool_result(PDF_PART), *tool_result(IMAGE_PART)]

    limited = limit_images(items, 6)

    assert request_modalities(limited) == {VIDEO, PDF, IMAGE}
    assert MAX_OTHER_MEDIA == 2
    assert "Earlier audio recording omitted" in texts(limited)


def test_sniffing_is_by_content_not_by_name():
    assert sniff(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == "image/png"
    assert sniff(b"ID3\x04\x00") == "audio/mpeg"
    assert sniff(b"BMW is not a bitmap header") is None
    assert sniff(b"\xff\xfeh\x00i\x00") is None  # UTF-16 text, not an MP3 frame

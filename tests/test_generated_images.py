"""Images an image-generation model returns reach the chat and the stored answer.

Shapes follow OpenRouter's answer for google/gemini-3.1-flash-lite-image:
``message.images`` whole, ``delta.images`` streamed.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from openai.types.chat import ChatCompletion, ChatCompletionChunk

from core.generated_images import ImageCollector, ReportingStream, collecting, image_urls, report
from core.vision_model import VisionChatCompletionsModel
from schemas.schemas import ImageContent
from tests.test_run_agent import ScriptedRunner, factory, use  # noqa: F401 - fixture

PNG = "data:image/png;base64,iVBORw0KGgo="
JPEG = "data:image/jpeg;base64,/9j/4AAQ"


def images_field(*urls):
    return [{"type": "image_url", "image_url": {"url": url}} for url in urls]


def completion(content="", *urls):
    return ChatCompletion.model_validate({
        "id": "gen-1", "object": "chat.completion", "created": 0, "model": "google/gemini-3.1-flash-lite-image",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": content, "images": images_field(*urls)}}],
    })


def chunk(**delta):
    return ChatCompletionChunk.model_validate({
        "id": "gen-1", "object": "chat.completion.chunk", "created": 0, "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    })


# -- reading the field ---------------------------------------------------------


def test_the_images_field_is_read_from_a_sdk_message_and_a_raw_delta():
    assert image_urls(completion("", JPEG).choices[0].message) == [JPEG]
    assert image_urls({"images": images_field(PNG)}) == [PNG]
    assert image_urls(SimpleNamespace(content="text only")) == []


def test_only_inline_images_are_accepted():
    remote = "https://example.com/tracker.png"
    svg = "data:image/svg+xml;base64,PHN2Zz4="
    assert image_urls({"images": images_field(remote, svg, PNG)}) == [PNG]


def test_each_image_is_collected_once_and_passed_on_as_it_comes():
    shown = []
    collector = ImageCollector(on_image=shown.append)
    with collecting(collector):
        report({"images": images_field(PNG)})
        report({"images": images_field(PNG, JPEG)})
    assert collector.images == [PNG, JPEG]
    assert shown == [PNG, JPEG]
    report({"images": images_field(PNG)})  # outside a turn: dropped, not raised


async def test_a_stream_reports_the_images_of_its_deltas_and_passes_chunks_through():
    chunks = [chunk(content="Here "), chunk(images=images_field(JPEG)), chunk(content="it is.")]

    async def source():
        for item in chunks:
            yield item

    collector = ImageCollector()
    with collecting(collector):
        passed = [item async for item in ReportingStream(source())]
    assert passed == chunks
    assert collector.images == [JPEG]


async def test_the_model_reports_a_whole_response():
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=completion("A red circle.", JPEG)))))
    client.base_url = "https://openrouter.ai/api/v1"
    model = VisionChatCompletionsModel(model="google/gemini-3.1-flash-lite-image", openai_client=client)
    collector = ImageCollector()
    with collecting(collector):
        from agents import ModelSettings
        from agents.models.interface import ModelTracing
        from agents.tracing import generation_span

        with generation_span(disabled=True) as span:
            await model._fetch_response(
                None, "draw", ModelSettings(extra_body={"modalities": ["image", "text"]}),
                [], None, [], span, ModelTracing.DISABLED,
            )
    assert collector.images == [JPEG]
    sent = client.chat.completions.create.await_args.kwargs
    assert sent["extra_body"] == {"modalities": ["image", "text"]}


# -- the turn --------------------------------------------------------------------


async def test_generated_images_are_shown_live_and_stored_with_the_answer(factory):
    shown = []
    observer = SimpleNamespace(
        handle_event=lambda event, agent_key=None: None, handle_generated_image=shown.append
    )

    async def draws():
        report({"images": images_field(JPEG)})
        return SimpleNamespace(final_output="A red circle.", new_items=[])

    with use(ScriptedRunner(draws)):
        await factory.run_agent("worker", "Draw a red circle", stream_observer=observer)
    context_id = factory.context_manager.get_current_context_id()

    assert shown == [JPEG]
    answer = factory.context_manager.conversation_view(context_id)["messages"][-1]
    assert answer.get_text_content().startswith("A red circle.")
    assert [part.image_url.url for part in answer.get_images() if isinstance(part, ImageContent)] == [JPEG]
    # The model gets the answer back as text: no base64 in its history.
    history = factory.context_manager.get_conversation_history_as_sdk_messages()
    assert history[-1]["role"] == "assistant"
    assert isinstance(history[-1]["content"], str) and JPEG not in history[-1]["content"]


def test_modalities_are_requested_next_to_reasoning_settings(factory):
    model = factory.config.get_model("m").model_copy(
        update={"modalities": ["image", "text"], "reasoning": {"enabled": False}}
    )
    settings = factory._build_model_settings(model)
    assert settings.extra_body == {"reasoning": {"enabled": False}, "modalities": ["image", "text"]}
    assert factory._build_model_settings(factory.config.get_model("m")).extra_body is None


def test_the_chat_receives_an_image_event():
    from web_chat.observer import WebStreamObserver
    from web_chat.trace import TraceRecorder

    events = []
    observer = WebStreamObserver(
        TraceRecorder(events.append), emit_token=lambda text: None, emit_image=lambda url: events.append(url)
    )
    observer.handle_generated_image(JPEG)
    assert events[-1] == JPEG
    json.dumps(events[-1])


async def test_an_answer_that_is_only_an_image_claims_no_missing_report(factory):
    async def draws():
        report({"images": images_field(JPEG)})
        return SimpleNamespace(final_output="", new_items=[])

    with use(ScriptedRunner(draws)):
        answer = await factory.run_agent("worker", "Draw a red circle")
    assert "without a written report" not in answer

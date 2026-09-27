"""The per-request image budget: newest images go out, older ones become notes."""

import copy

from agents.run import CallModelData, ModelInputData

from core.image_window import image_budget_filter, limit_images
from tests.test_run_agent import ScriptedRunner, factory, use  # noqa: F401 - fixture


def image(n):
    return {"type": "input_image", "image_url": f"data:image/jpeg;base64,{n}"}


def screenshot_step(n):
    """What a perceive() call leaves in the session: a call and its result."""
    return [
        {"type": "function_call", "call_id": f"c{n}", "name": "perceive", "arguments": "{}"},
        {
            "type": "function_call_output",
            "call_id": f"c{n}",
            "output": [{"type": "input_text", "text": f"Screenshot {n}"}, image(n)],
        },
    ]


def sent_images(items):
    return [
        part["image_url"]
        for item in items
        if isinstance(item, dict)
        for key in ("content", "output")
        if isinstance(item.get(key), list)
        for part in item[key]
        if isinstance(part, dict) and part.get("type") == "input_image"
    ]


def history(steps):
    items = [{"role": "user", "content": [{"type": "input_text", "text": "Look"}, image("user")]}]
    for n in range(steps):
        items.extend(screenshot_step(n))
    return items


def test_only_the_newest_images_are_sent():
    items = history(40)
    limited = limit_images(items, 6)
    assert sent_images(limited) == [f"data:image/jpeg;base64,{n}" for n in range(34, 40)]
    assert len(limited) == len(items)


def test_an_omitted_image_leaves_a_note_in_its_place():
    limited = limit_images(history(3), 2)
    user_parts = limited[0]["content"]
    assert user_parts[0]["text"] == "Look"
    assert user_parts[1]["type"] == "input_text"
    assert "Earlier image omitted" in user_parts[1]["text"]
    # The step keeps its own text, so the turn still reads in order.
    assert limited[2]["output"][0]["text"] == "Screenshot 0"


def test_the_session_items_are_never_modified():
    items = history(10)
    before = copy.deepcopy(items)
    limited = limit_images(items, 2)
    assert items == before
    # Items without an omitted image are the originals; changed ones are copies.
    assert limited[-1] is items[-1]
    assert limited[0] is not items[0]


def test_within_the_budget_nothing_changes():
    items = history(3)
    assert limit_images(items, 10) == items


def test_the_filter_keeps_the_instructions():
    data = CallModelData(
        model_data=ModelInputData(input=history(5), instructions="Be careful."),
        agent=None,
        context=None,
    )
    filtered = image_budget_filter(1)(data)
    assert filtered.instructions == "Be careful."
    assert len(sent_images(filtered.input)) == 1


async def test_every_run_of_the_factory_carries_the_configured_budget(factory):
    factory.config.config.settings.image_processing.max_images_per_request = 4
    runner = ScriptedRunner("Done.")
    with use(runner):
        await factory.run_agent("worker", "Go")

    apply = runner.calls[0].run_config.call_model_input_filter
    data = CallModelData(model_data=ModelInputData(input=history(31), instructions=None), agent=None, context=None)
    assert len(sent_images(apply(data).input)) == 4

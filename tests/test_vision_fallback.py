"""Images reach models that see; a model that cannot gets a note, never a failed chat."""

from types import SimpleNamespace

import pytest

from core import fallback_model
from core.fallback_model import IMAGE_NOTE, FallbackModel, ModelCandidate, has_images, strip_images

IMAGE = {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"}
WITH_IMAGE = [{"role": "user", "content": [{"type": "input_text", "text": "what is this?"}, IMAGE]}]


class Recorder:
    """A model that records the input it got; ``refuse`` raises the provider's 400 on images."""

    def __init__(self, *, refuse=False):
        self.refuse = refuse
        self.inputs = []

    async def get_response(self, *args, **kwargs):
        items = args[1]
        self.inputs.append(items)
        if self.refuse and has_images(items):
            raise RuntimeError("Upstream request failed: [400] This model does not support image inputs")
        return "ok"

    async def stream_response(self, *args, **kwargs):
        items = args[1]
        self.inputs.append(items)
        if self.refuse and has_images(items):
            raise RuntimeError("Upstream request failed: [400] This model does not support image inputs")
        yield "answer"


@pytest.fixture(autouse=True)
def forget_learned_blind_models():
    fallback_model._BLIND.clear()
    yield
    fallback_model._BLIND.clear()


def candidate(key, model, vision=None):
    return ModelCandidate(key=key, model=model, settings=SimpleNamespace(), vision=vision)


def test_images_are_found_and_replaced_by_a_note():
    assert has_images(WITH_IMAGE) and not has_images("text") and not has_images([{"role": "user", "content": "hi"}])
    stripped = strip_images(WITH_IMAGE)
    assert not has_images(stripped)
    assert stripped[0]["content"][1] == {"type": "input_text", "text": IMAGE_NOTE}
    assert has_images(WITH_IMAGE)  # the original is left as it was


async def test_a_model_that_sees_goes_first_for_a_request_with_images():
    blind, seeing = Recorder(), Recorder()
    model = FallbackModel([candidate("glm", blind, vision=False), candidate("gpt", seeing, vision=True)])

    assert await model.get_response(None, WITH_IMAGE, None) == "ok"

    assert seeing.inputs == [WITH_IMAGE] and blind.inputs == []


async def test_a_blind_model_alone_gets_the_request_with_a_note():
    blind = Recorder(refuse=True)
    model = FallbackModel([candidate("glm", blind, vision=False)])

    assert [event async for event in model.stream_response(None, WITH_IMAGE, None)] == ["answer"]
    assert not has_images(blind.inputs[0])


async def test_a_provider_refusing_images_is_retried_without_them_and_remembered():
    glm = Recorder(refuse=True)
    model = FallbackModel([candidate("glm", glm)])

    assert [event async for event in model.stream_response(None, WITH_IMAGE, None)] == ["answer"]
    assert has_images(glm.inputs[0]) and not has_images(glm.inputs[1])

    # Learned for the process: the next request does not try the images again.
    assert await model.get_response(None, WITH_IMAGE, None) == "ok"
    assert len(glm.inputs) == 3 and not has_images(glm.inputs[2])


async def test_a_refusal_goes_to_a_model_that_may_see_before_retrying_without_images():
    glm, gpt = Recorder(refuse=True), Recorder()
    model = FallbackModel([candidate("glm", glm), candidate("gpt", gpt)])

    assert await model.get_response(None, WITH_IMAGE, None) == "ok"

    assert gpt.inputs == [WITH_IMAGE] and len(glm.inputs) == 1


async def test_one_model_failing_raises_its_own_error():
    class Broken:
        async def get_response(self, *args, **kwargs):
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await FallbackModel([candidate("only", Broken())]).get_response(None, "hi", None)

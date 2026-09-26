"""Images attached in the web chat: checked, normalized, delivered, shown."""

import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from schemas import ContextMessage
from schemas.schemas import ImageContent, ImageProcessingConfig, ImageUrl, TextContent
from web_chat.attachments import (
    MAX_IMAGES,
    AttachmentError,
    agent_message,
    normalize_image,
    normalize_images,
)
from web_chat.server import WebChatServer

CONFIG = ImageProcessingConfig(max_width=400, max_height=300, jpeg_quality=80)


def data_url(image, fmt="PNG", mime="image/png"):
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return f"data:{mime};base64," + base64.b64encode(buffer.getvalue()).decode()


def decode(url):
    assert url.startswith("data:image/jpeg;base64,")
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))


def test_an_image_is_reencoded_as_jpeg_within_the_configured_size():
    url = normalize_image(data_url(Image.new("RGB", (1600, 900), "navy")), CONFIG)
    image = decode(url)
    assert image.format == "JPEG"
    assert image.size == (400, 225)  # aspect ratio kept


def test_transparency_is_flattened_on_white():
    url = normalize_image(data_url(Image.new("RGBA", (10, 10), (0, 0, 0, 0))), CONFIG)
    assert decode(url).convert("RGB").getpixel((5, 5)) == (255, 255, 255)


def test_an_animated_gif_contributes_its_first_frame():
    frames = [Image.new("RGB", (20, 20), color) for color in ("red", "blue")]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])
    url = "data:image/gif;base64," + base64.b64encode(buffer.getvalue()).decode()
    red, _, blue = decode(normalize_image(url, CONFIG)).convert("RGB").getpixel((10, 10))
    assert red > 200 and blue < 60


@pytest.mark.parametrize(
    "value, reason",
    [
        ("data:image/svg+xml;base64,PHN2Zz4=", "not supported"),
        ("data:image/png;base64,@@@", "not an image data URL"),
        ("data:image/png;base64," + base64.b64encode(b"not an image").decode(), "could not be read"),
        ("https://example.com/cat.png", "not an image data URL"),
        (42, "not an image"),
    ],
)
def test_what_is_not_a_supported_image_is_refused(value, reason):
    with pytest.raises(AttachmentError, match=reason):
        normalize_image(value, CONFIG)


def test_the_number_of_images_is_bounded():
    url = data_url(Image.new("RGB", (4, 4)))
    with pytest.raises(AttachmentError, match="at most"):
        normalize_images([url] * (MAX_IMAGES + 1), CONFIG)
    with pytest.raises(AttachmentError):
        normalize_images("not a list", CONFIG)
    assert normalize_images(None, CONFIG) == []


def test_the_agent_gets_text_alone_or_an_sdk_message_with_images():
    assert agent_message("hello", []) == "hello"
    message = json.loads(agent_message("what is this?", ["data:image/jpeg;base64,AAAA"]))
    assert message["role"] == "user"
    assert message["content"][0] == {"type": "input_text", "text": "what is this?"}
    assert message["content"][1]["type"] == "input_image"
    only_image = json.loads(agent_message("", ["data:image/jpeg;base64,AAAA"]))
    assert [part["type"] for part in only_image["content"]] == ["input_image"]


def test_stored_images_are_served_with_the_message():
    message = ContextMessage(
        role="user",
        content=[
            TextContent(type="text", text="look"),
            ImageContent(type="image_url", image_url=ImageUrl(url="data:image/jpeg;base64,AAAA")),
        ],
        timestamp="2026-09-26T12:00:00",
    )
    image_only = ContextMessage(
        role="user",
        content=[ImageContent(type="image_url", image_url=ImageUrl(url="data:image/jpeg;base64,BBBB"))],
        timestamp="2026-09-26T12:00:01",
    )
    serialize = WebChatServer._serialize_message
    served = serialize(SimpleNamespace(), message)
    assert served["content"] == "look"
    assert served["images"] == ["data:image/jpeg;base64,AAAA"]
    assert serialize(SimpleNamespace(), image_only)["content"] == ""

"""Images of a chat message: kept in the chat's space, linked, seen only by a seeing model.

A message's images used to exist only inside the model request and the stored
data URLs. They are now also files of the chat's workspace - the user's own
bytes, named in the message's ``Attached files:`` block by their download URL
(scoped to the chat) and their workspace path - and the picture itself goes to
the model only when a model of the turn's runtime may read it.
"""

import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

try:
    from test_web_chat_session import Socket, png_data_url, space_for
except ModuleNotFoundError:
    from tests.test_web_chat_session import Socket, png_data_url, space_for

from schemas.schemas import ImageProcessingConfig, UploadsPolicy
from web_chat.attachments import (
    AttachmentError,
    ImageAttachment,
    normalize_images,
    runtime_accepts_images,
    save_images,
    with_attachments,
)
from web_chat.session import chat_session

#: What the session hands save_images: every image normalized first.
CONFIG = ImageProcessingConfig()
#: One line of the block the page reads back (web_chat/js/lib/files.js).
LINE = re.compile(r'^- \[(?P<name>[^\]]*)\]\((?P<url>/api/workspace/files/[^)\s]+)\)'
                  r' — workspace path: "(?P<path>(?:[^"\\]|\\.)*)"$')


def candidate(sees):
    """One model candidate: reads images or plainly refuses them."""

    def accepts(modality):
        return modality != "image" or sees

    return SimpleNamespace(accepts=accepts)


def runtime(sees):
    return SimpleNamespace(model=SimpleNamespace(candidates=[candidate(sees)]))


def space_in(root):
    """A space whose every chat has a directory of its own, as the real one does."""
    space = space_for(AsyncMock(return_value="ok"))

    def file_spaces(context_id):
        directory = root / context_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory, {}

    space.file_spaces = file_spaces
    return space


async def turn(space, context_id="ctx", payload=None):
    socket = Socket()
    task = asyncio.create_task(chat_session(space, socket, context_id))
    await socket.incoming.put(json.dumps(payload or {"message": "hello"}))
    await socket.event("done")
    await socket.incoming.put(None)
    await task
    return socket


def stored_images(directory):
    uploads = directory / "uploads"
    return sorted(uploads.iterdir()) if uploads.is_dir() else []


def block_of(text):
    """The lines of the message's closing Attached files block."""
    head, _, block = text.rpartition("Attached files:\n")
    assert head.endswith("\n\n") or head == "", text
    return [LINE.match(line) for line in block.split("\n")]


def png_bytes():
    import base64

    return base64.b64decode(png_data_url().partition(",")[2])


@pytest.mark.asyncio
async def test_a_seeing_model_gets_the_file_link_and_the_image(tmp_path):
    run = AsyncMock(return_value="A green square.")
    space = space_in(tmp_path)
    space.factory.create_agent = AsyncMock(return_value=runtime(sees=True))
    space.factory.run_agent = run

    await turn(space, payload={"message": "what is this?", "images": [png_data_url()]})

    sent = json.loads(run.await_args.kwargs["message"])
    assert [part["type"] for part in sent["content"]] == ["input_text", "input_image"]
    assert sent["content"][1]["image_url"].startswith("data:image/jpeg;base64,")

    files = stored_images(tmp_path / "ctx")
    assert len(files) == 1
    # The user's own bytes are kept; the model's JPEG is a separate copy.
    assert files[0].suffix == ".png"
    assert files[0].read_bytes() == png_bytes()

    text = sent["content"][0]["text"]
    assert text.startswith("what is this?")
    line = block_of(text)[0]
    assert line is not None, text
    assert json.loads(f'"{line.group("path")}"') == f"uploads/{files[0].name}"
    assert line.group("url") == f"/api/workspace/files/uploads/{files[0].name}?context=ctx"


@pytest.mark.asyncio
async def test_a_model_that_cannot_see_gets_the_file_and_its_link_only(tmp_path):
    run = AsyncMock(return_value="I cannot see images.")
    space = space_in(tmp_path)
    space.factory.create_agent = AsyncMock(return_value=runtime(sees=False))
    space.factory.run_agent = run

    await turn(space, payload={"message": "what is this?", "images": [png_data_url()]})

    message = run.await_args.kwargs["message"]
    assert isinstance(message, str)  # no SDK JSON: no image part at all
    assert "input_image" not in message
    files = stored_images(tmp_path / "ctx")
    assert len(files) == 1
    assert files[0].read_bytes() == png_bytes()
    assert f"(/api/workspace/files/uploads/{files[0].name}?context=ctx)" in message
    assert "what is this?" in message


@pytest.mark.asyncio
async def test_an_image_without_words_becomes_its_link(tmp_path):
    run = AsyncMock(return_value="ok")
    space = space_in(tmp_path)
    space.factory.create_agent = AsyncMock(return_value=runtime(sees=False))
    space.factory.run_agent = run

    await turn(space, payload={"images": [png_data_url()]})

    message = run.await_args.kwargs["message"]
    files = stored_images(tmp_path / "ctx")
    assert len(files) == 1
    assert message == (
        "Attached files:\n"
        f'- [{files[0].name}](/api/workspace/files/uploads/{files[0].name}?context=ctx)'
        f' — workspace path: "uploads/{files[0].name}"'
    )


@pytest.mark.asyncio
async def test_each_chat_keeps_its_own_images_and_names_itself_in_the_url(tmp_path):
    run = AsyncMock(return_value="ok")
    space = space_in(tmp_path)
    space.factory.create_agent = AsyncMock(return_value=runtime(sees=True))
    space.factory.run_agent = run

    for chat in ("chat-a", "chat-b"):
        socket = Socket()
        task = asyncio.create_task(chat_session(space, socket, chat))
        await socket.incoming.put(json.dumps({"message": chat, "images": [png_data_url()]}))
        await socket.event("done")
        await socket.incoming.put(None)
        await task

    in_a = stored_images(tmp_path / "chat-a")
    in_b = stored_images(tmp_path / "chat-b")
    assert len(in_a) == 1 and len(in_b) == 1
    assert in_a[0].parent != in_b[0].parent
    assert in_a[0].name != in_b[0].name
    text = run.await_args.kwargs["message"]
    assert f"context=chat-b" in text


@pytest.mark.asyncio
async def test_a_runtime_unknown_to_the_factory_keeps_the_images(tmp_path):
    """A transport that cannot be asked keeps what it did before."""
    run = AsyncMock(return_value="ok")
    space = space_in(tmp_path)
    space.factory.run_agent = run  # no create_agent at all

    await turn(space, payload={"message": "look", "images": [png_data_url()]})

    sent = json.loads(run.await_args.kwargs["message"])
    assert [part["type"] for part in sent["content"]] == ["input_text", "input_image"]
    assert len(stored_images(tmp_path / "ctx")) == 1


# -- a failed copy is reported, never a picture quietly lost -------------------------
@pytest.mark.asyncio
async def test_a_workspace_that_cannot_take_the_file_stops_the_turn_with_an_error(tmp_path):
    run = AsyncMock(return_value="unused")
    space = space_in(tmp_path)
    (tmp_path / "ctx").mkdir(parents=True)
    (tmp_path / "ctx" / "uploads").write_bytes(b"not a directory")

    socket = Socket()
    task = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put(json.dumps({"message": "look", "images": [png_data_url()]}))
    error = await socket.event("error")
    assert "could not be saved" in error["content"]
    await socket.event("done")
    await socket.incoming.put(None)
    await task
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_chat_without_a_workspace_reports_the_lost_image(tmp_path):
    run = AsyncMock(return_value="unused")
    space = space_for(run)  # neither file_spaces nor conversation_workspace

    socket = Socket()
    task = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put(json.dumps({"message": "look", "images": [png_data_url()]}))
    error = await socket.event("error")
    assert "no workspace" in error["content"]
    await socket.event("done")
    await socket.incoming.put(None)
    await task
    run.assert_not_awaited()


# -- the block the page reads back ---------------------------------------------------
def test_images_join_the_documents_block_and_an_edit_replaces_only_its_own_lines(tmp_path):
    url = "data:image/png;base64," + png_data_url().partition(",")[2]
    saved = save_images(normalize_images([url], CONFIG), tmp_path, "ctx")
    documents = 'Read it\n\nAttached files:\n- [notes.md](/api/workspace/files/uploads/notes.md) — workspace path: "uploads/notes.md"'
    text = with_attachments(documents, saved)
    lines = block_of(text)
    assert len(lines) == 2
    assert lines[0].group("path") == "uploads/notes.md"
    assert lines[1].group("path") == saved[0]["path"]

    edited = with_attachments(text, saved)
    assert edited == text  # the same block is not added twice

    newer = save_images(normalize_images([url], CONFIG), tmp_path, "ctx")
    after = with_attachments(with_attachments(documents, newer), saved)
    assert block_of(after)[0].group("path") == "uploads/notes.md"
    assert [line.group("url") for line in block_of(after)[1:]] == [saved[0]["url"]]


def test_save_images_keeps_the_original_bytes_and_downloads_by_its_chat(tmp_path):
    attachment = normalize_images([png_data_url()], CONFIG)[0]
    assert isinstance(attachment, ImageAttachment)
    saved = save_images([attachment, attachment], tmp_path, "ctx-1")
    assert len({item["name"] for item in saved}) == 2
    for item in saved:
        assert item["path"].startswith("uploads/image-") and item["path"].endswith(".png")
        assert item["url"] == f"/api/workspace/files/{item['path']}?context=ctx-1"
        assert item["type"] == "image/png"
        assert (tmp_path / item["path"]).read_bytes() == attachment.data
    assert save_images([], tmp_path) == []
    assert save_images([attachment], tmp_path)[0]["url"].startswith("/api/workspace/files/uploads/")


@pytest.mark.parametrize(
    "runtime, sees",
    [
        (object(), True),  # nothing known: treated as able
        (SimpleNamespace(model=SimpleNamespace(candidates=[])), True),
        (runtime(True), True),
        (runtime(False), False),
        (SimpleNamespace(model=SimpleNamespace(candidates=[candidate(False), candidate(True)])), True),
    ],
)
def test_runtime_accepts_images_reads_its_candidates(runtime, sees):
    assert runtime_accepts_images(runtime) is sees


# -- a real space serves the URL of the file it kept -----------------------------------
def _real_space(tmp_path, monkeypatch):
    try:
        from test_web_space import MINIMAL_CONFIG
    except ModuleNotFoundError:
        from tests.test_web_space import MINIMAL_CONFIG

    from web_chat.deployment import Deployment
    from web_chat.space import SpaceLayout, UserSpace

    monkeypatch.setenv("TEST_OPENROUTER_KEY", "test-key")
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    space = UserSpace(
        Deployment(config_path=str(config), routing_path=None),
        user_id="u1",
        layout=SpaceLayout.under(tmp_path / "users" / "u1"),
        chat_spaces=True,
    )
    return space


def test_the_url_of_a_kept_image_opens_it_in_the_chats_own_space(tmp_path, monkeypatch):
    from fastapi import APIRouter, FastAPI, Request
    from fastapi.testclient import TestClient

    from web_chat.uploads import register_upload_routes

    space = _real_space(tmp_path, monkeypatch)
    chat = space.context_manager().start_new_context()
    space.assign_chat_space(chat)
    workspace = space.file_spaces(chat)[0]
    saved = save_images(normalize_images([png_data_url()], CONFIG), workspace, chat)

    async def current(request: Request):
        return space

    api = APIRouter()
    register_upload_routes(api, current, lambda: UploadsPolicy(max_file_mb=1, max_files=3, quota_mb=2))
    app = FastAPI()
    app.include_router(api)
    client = TestClient(app)

    opened = client.get(saved[0]["url"])
    assert opened.status_code == 200, opened.text
    assert opened.content == png_bytes()
    # Without the chat, the route looks in the user's own workspace: the path is
    # the chat's, which is why the URL carries ?context=<chat>.
    assert client.get(saved[0]["url"].split("?")[0]).status_code == 404


# -- steering keeps its images too -----------------------------------------------------
def running_agent(sees):
    """A turn that waits for release; the messages it is given are recorded."""
    started, release, seen = asyncio.Event(), asyncio.Event(), []

    async def run(**kwargs):
        seen.append(kwargs["message"])
        if len(seen) == 1:
            started.set()
            await release.wait()
        return "done"

    return run, started, release, seen


@pytest.mark.asyncio
@pytest.mark.parametrize("sees", [True, False])
async def test_a_message_steered_into_a_running_turn_keeps_its_image(tmp_path, sees):
    run, started, release, _seen = running_agent(sees)
    space = space_in(tmp_path)
    space.factory.create_agent = AsyncMock(return_value=runtime(sees=sees))
    space.factory.run_agent = run
    given = []

    def steer(context_id, message):
        given.append(message)
        message.on_delivered(message)  # the agent's next call reads it at once here
        return True

    space.factory.steer = steer
    socket = Socket()
    session = asyncio.create_task(chat_session(space, socket, "ctx"))
    await socket.incoming.put(json.dumps({"message": "refactor"}))
    await asyncio.wait_for(started.wait(), 2)
    await socket.incoming.put(json.dumps({"message": "look at this", "images": [png_data_url()], "delivery": "next_step"}))
    await socket.event("steered")

    files = stored_images(tmp_path / "ctx")
    assert len(files) == 1
    assert files[0].read_bytes() == png_bytes()
    steered = given[-1]
    assert "look at this" in steered.text
    assert f"(/api/workspace/files/uploads/{files[0].name}?context=ctx)" in steered.text
    # Only a seeing model is handed the picture; the file and its link are kept either way.
    assert bool(steered.images) is sees
    if sees:
        assert steered.images[0].startswith("data:image/jpeg;base64,")

    release.set()
    await socket.event("done")
    await socket.incoming.put(None)
    await session

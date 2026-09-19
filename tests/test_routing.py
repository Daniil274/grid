"""Tests for automatic system and agent routing."""

from types import SimpleNamespace

import yaml

from core.config import Config
from core.routing import AutoRouter, Router, _parse_choice


class FakeClient:
    """Async OpenAI-like client that answers with scripted replies."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])


def _system_config(agents, default_agent):
    return {
        "settings": {"default_agent": default_agent},
        "providers": {"p": {"name": "p", "base_url": "http://localhost", "api_key": "k"}},
        "models": {"m": {"name": "m", "provider": "p"}},
        "agents": {
            key: {"name": key, "model": "m", "description": desc, **extra}
            for key, (desc, extra) in agents.items()
        },
    }


def _write(path, data):
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _make_systems(tmp_path):
    """Root config listing itself ('main') and a separate 'video' system."""
    video = _system_config(
        {
            "director": ("Coordinates video editing", {}),
            "annotator": ("Scene detection and annotation", {}),
        },
        "director",
    )
    _write(tmp_path / "video.yaml", video)
    root = _system_config(
        {
            "chat": ("Talks with the user", {}),
            "coder": ("Writes code", {}),
            "janitor": ("Background cleanup", {"routable": False}),
        },
        "chat",
    )
    root["routing"] = {
        "model": "m",
        "default_system": "main",
        "systems": {
            "main": {"config": "config.yaml", "description": "General assistant"},
            "video": {"config": "video.yaml", "description": "Video editing"},
        },
    }
    return Config(str(_write(tmp_path / "config.yaml", root)))


async def test_choose_returns_model_choice():
    client = FakeClient('{"choice": "b"}')
    router = Router(client, "m")

    assert await router.choose("task", {"a": "first", "b": "second"}) == "b"
    assert "- a: first" in client.prompts[0]
    assert "- b: second" in client.prompts[0]


async def test_choose_single_candidate_skips_model():
    client = FakeClient()

    assert await Router(client, "m").choose("task", {"only": "x"}) == "only"
    assert client.prompts == []


async def test_choose_falls_back_on_unknown_choice_and_errors():
    router = Router(FakeClient('{"choice": "ghost"}', RuntimeError("down")), "m")
    candidates = {"a": "first", "b": "second"}

    assert await router.choose("task", candidates, default="b") == "b"
    assert await router.choose("task", candidates) == "a"


def test_parse_choice_tolerates_fences_and_bare_ids():
    assert _parse_choice('```json\n{"choice": "video"}\n```') == "video"
    assert _parse_choice("video") == "video"
    assert _parse_choice("") is None


async def test_route_picks_system_then_agent(tmp_path):
    root = _make_systems(tmp_path)
    client = FakeClient('{"choice": "video"}', '{"choice": "annotator"}')
    auto = AutoRouter(root, Router(client, "m"))

    route = await auto.route("find the scene cuts in clip.mp4")

    assert (route.system, route.agent) == ("video", "annotator")
    assert route.config.get_default_agent() == "director"
    assert "- video: Video editing" in client.prompts[0]
    assert "- annotator: Scene detection and annotation" in client.prompts[1]


async def test_route_reuses_root_config_and_skips_unroutable_agents(tmp_path):
    root = _make_systems(tmp_path)
    client = FakeClient('{"choice": "main"}', '{"choice": "coder"}')
    auto = AutoRouter(root, Router(client, "m"))

    route = await auto.route("fix the bug")

    assert route.config is root
    assert route.agent == "coder"
    assert "janitor" not in client.prompts[1]


async def test_route_falls_back_when_system_config_is_broken(tmp_path):
    root = _make_systems(tmp_path)
    (tmp_path / "video.yaml").write_text("agents: [broken", encoding="utf-8")
    client = FakeClient('{"choice": "video"}', '{"choice": "chat"}')
    auto = AutoRouter(root, Router(client, "m"))

    route = await auto.route("cut the clip")

    assert (route.system, route.agent) == ("main", "chat")


async def test_route_hints_previous_choice(tmp_path):
    root = _make_systems(tmp_path)
    client = FakeClient(
        '{"choice": "video"}', '{"choice": "annotator"}',
        '{"choice": "video"}', '{"choice": "annotator"}',
    )
    auto = AutoRouter(root, Router(client, "m"))

    await auto.route("annotate clip.mp4")
    await auto.route("continue")

    assert "previous message went to 'video'" in client.prompts[2]
    assert "previous message went to 'annotator'" in client.prompts[3]


async def test_route_without_systems_routes_root_agents(tmp_path):
    root_data = _system_config({"chat": ("Talks", {}), "coder": ("Codes", {})}, "chat")
    root_data["routing"] = {"model": "m"}
    root = Config(str(_write(tmp_path / "config.yaml", root_data)))
    client = FakeClient('{"choice": "coder"}')
    auto = AutoRouter(root, Router(client, "m"))

    route = await auto.route("write a parser")

    assert route.config is root
    assert route.agent == "coder"
    assert len(client.prompts) == 1


def test_from_config_is_off_without_routing_model(tmp_path):
    root = Config(str(_write(tmp_path / "config.yaml", _system_config({"chat": ("Talks", {})}, "chat"))))

    assert AutoRouter.from_config(root) is None


def test_default_system_and_config_path(tmp_path):
    root = _make_systems(tmp_path)
    auto = AutoRouter(root, Router(FakeClient(), "m"))

    assert auto.default_system() == "main"
    assert auto.system_config_path("video") == (tmp_path / "video.yaml").resolve()
    assert auto.system_config_path("main") == root.config_path.resolve()


async def test_decisions_router_sends_candidates_as_criteria():
    import json

    import httpx

    from core.routing import DecisionsRouter

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"answers": {"route": {"type": "choice", "choice": "video", "confidence": 1}}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    router = DecisionsRouter(http, "https://x/api/alpha/decisions", "key", "typesafe/jev-1.13")

    choice = await router.choose("cut clip.mp4", {"engineering": "Coding", "video": "Video editing"},
                                 previous="engineering")

    assert choice == "video"
    body = requests[0]
    assert body["model"] == "typesafe/jev-1.13"
    assert body["questions"]["route"]["type"] == "choice"
    assert body["questions"]["route"]["criteria"] == {"engineering": "Coding", "video": "Video editing"}
    assert "cut clip.mp4" in body["state"] and "'engineering'" in body["state"]


async def test_route_reports_fallback_reason(tmp_path):
    root = _make_systems(tmp_path)
    (tmp_path / "video.yaml").write_text("agents: [broken", encoding="utf-8")
    auto = AutoRouter(root, Router(FakeClient('{"choice": "video"}', '{"choice": "chat"}'), "m"))

    route = await auto.route("cut the clip")

    assert "video" in route.warning and "main" in route.warning


async def test_check_systems_reports_broken_systems(tmp_path):
    root = _make_systems(tmp_path)
    # A tool that is declared but has no implementation anywhere.
    video = yaml.safe_load((tmp_path / "video.yaml").read_text(encoding="utf-8"))
    video["tools"] = {"ghost_tool": {"type": "function", "description": "missing implementation"}}
    video["agents"]["director"]["tools"] = ["ghost_tool"]
    video["agents"]["annotator"]["description"] = ""
    _write(tmp_path / "video.yaml", video)
    (tmp_path / "broken.yaml").write_text("agents: [broken", encoding="utf-8")
    routing = root.config.routing
    routing.systems["video"].requires = ["definitely-not-a-real-program"]
    routing.systems["broken"] = type(routing.systems["video"])(config="broken.yaml", description="Broken")

    problems = AutoRouter(root, Router(FakeClient(), "m")).check_systems()

    assert "main" not in problems
    assert problems["broken"] == ["config failed to load: " + problems["broken"][0].split(": ", 1)[1]]
    assert any("ghost_tool" in issue and "not implemented" in issue for issue in problems["video"])
    assert any("annotator" in issue and "no description" in issue for issue in problems["video"])
    assert any("definitely-not-a-real-program" in issue for issue in problems["video"])

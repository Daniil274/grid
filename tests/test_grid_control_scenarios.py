"""Scenario evaluation: the verifier's chat client, the egress proxy and trials."""

import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from core.workshop import ControlClient, Workshop
from grid_control import egress, verifier
from grid_control.controller import Controller
from grid_control.docker import DockerRuntime
from grid_control.models import Check, Policy, Scenario, Trial, decide
from grid_control.repository import Repository
from grid_control.service import create_app
from grid_control.store import Store

TOKEN = "t" * 32


# -- a fake Grid web chat ---------------------------------------------------


def _chat_app(script):
    """Web chat stand-in: answers each message with the events ``script`` returns."""
    app = FastAPI()

    @app.get("/")
    def index():
        return {"ok": True}

    @app.websocket("/api/chat/ws/{context_id}")
    async def chat(websocket: WebSocket, context_id: str):
        await websocket.accept()
        message = json.loads(await websocket.receive_text())["message"]
        for event in script(message):
            if event == "hang":
                await asyncio.sleep(3600)
            await websocket.send_text(json.dumps(event))
        await websocket.close()

    return app


def _answer(message):
    if "video" in message:
        return [
            {"type": "routed", "system": "video", "agent": "video_director"},
            {"type": "step", "step": {"id": "s1", "kind": "tool", "title": "call_video_annotator"}},
            # The call becomes the sub-agent's block, titled by the agent.
            {"type": "step", "step": {"id": "s1", "kind": "agent", "title": "video_annotator", "tool": "call_video_annotator"}},
            {"type": "step", "step": {"id": "s2", "kind": "reasoning", "title": "Thinking"}},
            {"type": "step", "step": {"id": "s3", "kind": "tool", "title": "Video Editor › video_make_preview"}},
            {"type": "token", "content": "Preview "},
            {"type": "token", "content": "is ready"},
            {"type": "done"},
        ]
    if "slow" in message or "route only" in message:
        return [{"type": "routed", "system": "engineering", "agent": "engineer"}, "hang"]
    return [
        {"type": "routed", "system": "engineering", "agent": "engineer"},
        {"type": "step", "step": {"id": "s1", "kind": "tool", "title": "codegraph.bash_tool"}},
        {"type": "final_output", "content": "Done: 42"},
        {"type": "done"},
    ]


@pytest.fixture(scope="module")
def chat_server():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(_chat_app(_answer), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield port
    # A scenario leaves a turn hanging; do not wait for it, or the server thread
    # outlives the module and spins in whatever test patches asyncio.sleep next.
    server.should_exit = True
    server.force_exit = True
    thread.join(5)
    assert not thread.is_alive()


def _scenario(**fields):
    return {
        "system": None, "agent": None, "tools_called": (), "tools_not_called": (),
        "output_matches": None, "timeout_seconds": 10, **fields,
    }


def test_verifier_judges_routing_tools_and_answer(chat_server):
    plan = {
        "host": "127.0.0.1",
        "port": chat_server,
        "timeout_seconds": 20,
        "checks": [{"name": "web", "path": "/", "expected_status": 200, "json_pointer": "/ok", "expected": True}],
        "scenarios": [
            _scenario(
                name="video", message="cut the video", system="video", agent="video_director",
                tools_called=("call_video_annotator", "video_make_preview"),
                tools_not_called=("video_render_cutlist",),
                output_matches="(?i)preview",
            ),
            _scenario(name="mcp-tool", message="run it", tools_called=("bash_tool",), output_matches="42"),
            _scenario(name="misrouted", message="edit my video", system="engineering"),
            _scenario(name="forbidden", message="hello", tools_not_called=("bash_tool",)),
            _scenario(name="slow", message="slow task", system="engineering",
                      output_matches="never", timeout_seconds=2),
            # Routing-only: decided at the routed event, the hanging turn is not awaited.
            _scenario(name="routing", message="route only", system="engineering", timeout_seconds=2),
        ],
    }

    report = verifier.run(plan)

    checks, details = report["checks"], report["details"]
    assert checks == {
        "web": True,
        "scenario:video": True,
        "scenario:mcp-tool": True,
        "scenario:misrouted": False,
        "scenario:forbidden": False,
        "scenario:slow": False,
        "scenario:routing": True,
    }
    assert "tools=Video Editor › video_make_preview,call_video_annotator;" in details["scenario:video"]
    assert "'Preview is ready'" in details["scenario:video"]
    assert "routed to system video, expected engineering" in details["scenario:misrouted"]
    assert "tool bash_tool was called" in details["scenario:forbidden"]
    assert "no answer within 2s" in details["scenario:slow"]


def test_verifier_fails_every_scenario_when_the_candidate_never_starts():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    report = verifier.run(
        {"host": "127.0.0.1", "port": port, "timeout_seconds": 6, "checks": [],
         "scenarios": [_scenario(name="any", message="hi", system="video")]}
    )
    assert report["checks"] == {"scenario:any": False}
    assert report["details"]["scenario:any"] == "candidate never became ready"


# -- egress proxy -----------------------------------------------------------


def test_egress_allowlist_matches_exact_hosts_and_wildcard_suffixes():
    hosts = ("openrouter.ai", "*.openai.com")
    assert egress.allowed("openrouter.ai", hosts)
    assert egress.allowed("OpenRouter.AI.", hosts)
    assert egress.allowed("api.openai.com", hosts)
    assert not egress.allowed("openai.com", hosts)
    assert not egress.allowed("evil-openrouter.ai", hosts)
    assert not egress.allowed("openrouter.ai.evil.com", hosts)


async def test_egress_refuses_everything_but_allowed_https_tunnels():
    server = await asyncio.start_server(
        lambda r, w: egress.handle(r, w, ("openrouter.ai",)), "127.0.0.1", 0
    )
    port = server.sockets[0].getsockname()[1]

    async def ask(request):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request)
        await writer.drain()
        line = await reader.readline()
        writer.close()
        return line.decode().strip()

    async with server:
        assert await ask(b"GET http://example.com/ HTTP/1.1\r\n\r\n") == "HTTP/1.1 405 Method Not Allowed"
        assert await ask(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n") == "HTTP/1.1 403 Forbidden"
        assert await ask(b"CONNECT openrouter.ai:80 HTTP/1.1\r\n\r\n") == "HTTP/1.1 403 Forbidden"


# -- policy -----------------------------------------------------------------


def _policy(**changes):
    base = dict(
        runtime_image="runtime", verifier_image="verifier", checks=(Check("web", "/"),),
        repetitions=1,
        scenarios=(Scenario("route-video", "cut the clip", system="video"),),
        dev_scenarios=(Scenario("route-code", "fix the bug", system="engineering"),),
    )
    return Policy(**{**base, **changes})


def test_policy_parses_scenarios_and_validates_them():
    policy = Policy.from_dict(
        {
            "runtime_image": "r", "verifier_image": "v",
            "scenarios": [{"name": "a", "message": "m", "tools_called": ["x"]}],
            "egress_hosts": ["openrouter.ai"],
        }
    )
    assert policy.scenarios[0].tools_called == ("x",)
    assert policy.result_names() == {"scenario:a"}
    assert policy.verifier_timeout() == 180 + 180

    with pytest.raises(ValueError, match="expects nothing"):
        Scenario("empty", "hello")
    with pytest.raises(ValueError, match="unique"):
        _policy(scenarios=(Scenario("a", "m", system="x"), Scenario("a", "n", system="y")))
    with pytest.raises(ValueError, match="Egress hosts"):
        _policy(egress_hosts=("http://evil.com",))
    with pytest.raises(ValueError, match="check or scenario"):
        _policy(checks=(), scenarios=())


def test_development_policy_runs_the_open_suite_once():
    development = _policy(repetitions=3, auto_promote=True).development()
    assert [s.name for s in development.scenarios] == ["route-code"]
    assert development.repetitions == 1 and not development.auto_promote
    with pytest.raises(ValueError, match="development scenarios"):
        _policy(dev_scenarios=()).development()


def test_decision_counts_scenarios_like_checks():
    policy = _policy()
    both = {"web": True, "scenario:route-video": True}
    only_web = {"web": True, "scenario:route-video": False}
    decision = decide(policy, [Trial(only_web, 1.0)], [Trial(both, 1.0)])
    assert decision["accepted"] and decision["improvement"] == 0.5
    with pytest.raises(ValueError, match="invalid check set"):
        decide(policy, [Trial({"web": True}, 1.0)], [Trial(both, 1.0)])


# -- trials through the workshop and the API --------------------------------


class ScriptedRuntime:
    """Candidates pass acceptance; details explain every scenario."""

    def revision(self, repo, ref):
        return ref

    def image(self, ref):
        return ref

    def build(self, repo, commit, *args):
        return "image-" + commit[:7]

    def trial(self, image, verifier_image, experiment, index, policy):
        names = policy.result_names()
        return Trial(
            {name: True for name in names},
            0.1,
            {name: f"expected detail of {name}" for name in names if name.startswith("scenario:")},
        )

    def cleanup(self, experiment):
        pass


def _git(path, *args):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=T", "-c", "user.email=t@local", *args],
        capture_output=True, check=True, text=True,
    ).stdout.strip()


@pytest.fixture
def control(tmp_path):
    source = tmp_path / "grid"
    source.mkdir()
    _git(source, "init", "--quiet")
    (source / "routing.yaml").write_text("routing: {}\n", encoding="utf-8")
    _git(source, "add", "--all")
    _git(source, "commit", "--quiet", "-m", "initial")
    repository = Repository.create(tmp_path / "evolution.git", source)
    store = Store(tmp_path / "control.db")
    app = create_app(Controller(store, ScriptedRuntime()), repository.path, _policy(), TOKEN)
    return store, TestClient(app, headers={"Authorization": "Bearer " + TOKEN})


def test_trial_tries_unfinished_work_and_shows_its_details(tmp_path, control):
    store, http = control
    workshop = Workshop(tmp_path / "workshop")
    with http:
        client = ControlClient(http)
        baseline = workshop.init(client)
        assert [s["name"] for s in client.scenarios()] == ["route-code"]

        (workshop.path / "prompt.md").write_text("draft\n", encoding="utf-8")
        report = workshop.trial(client)

        # The experiment is untouched: same branch head, the draft still uncommitted.
        assert _git(workshop.path, "rev-parse", "HEAD") == baseline
        assert "prompt.md" in _git(workshop.path, "status", "--porcelain")
    trial = store.get(report["id"])
    assert (trial["kind"], trial["status"]) == ("trial", "completed")
    with http:
        view = ControlClient(http).status(report["id"])
    assert view["events"][-1]["trial"]["details"] == {
        "scenario:route-code": "expected detail of scenario:route-code"
    }


def test_acceptance_details_stay_private(tmp_path, control):
    store, http = control
    workshop = Workshop(tmp_path / "workshop")
    with http:
        client = ControlClient(http)
        workshop.init(client)
        (workshop.path / "prompt.md").write_text("final\n", encoding="utf-8")
        report = workshop.submit(client, "Improve the prompt for routing")
    with http:
        view = ControlClient(http).status(report["id"])
    trials = [event["trial"] for event in view["events"] if "trial" in event]
    assert view["status"] == "accepted"
    assert trials and all("details" not in trial for trial in trials)
    assert all(trial["checks"]["scenario:route-video"] for trial in trials)
    # The operator still sees everything in the journal.
    journal = [e["trial"] for e in store.get(report["id"])["events"] if "trial" in e]
    assert journal[0]["details"]["scenario:route-video"] == "expected detail of scenario:route-video"


# -- docker wiring ----------------------------------------------------------


def test_trial_with_egress_hosts_routes_the_candidate_through_the_proxy():
    calls = []

    class Recording(DockerRuntime):
        def command(self, args, *, timeout=30, data=None):
            calls.append((args, timeout))
            if args[:2] == ["docker", "run"] and args[-1] == "-":
                return json.dumps({"checks": {"web": True, "scenario:route-video": True},
                                   "details": {}, "elapsed_seconds": 1.0}).encode()
            return b""

    trial = Recording().trial("image", "verifier", "0" * 32, 0, _policy(egress_hosts=("openrouter.ai",)))

    assert trial.checks["scenario:route-video"] is True
    commands = [args for args, _ in calls]
    assert commands[0][:3] == ["docker", "network", "create"] and "--internal" in commands[0]
    proxy_run = next(args for args in commands if "grid-eval-" + "0" * 32 + "-0-egress" in args and args[1] == "run")
    assert "'openrouter.ai'" in proxy_run[-1]
    assert ["docker", "network", "connect", "--alias", "egress"] == commands[2][:5]
    candidate = next(args for args in commands if "--network-alias=candidate" in args)
    assert "HTTPS_PROXY=http://egress:3128" in candidate
    verifier_timeout = next(timeout for args, timeout in calls if args[-1] == "-")
    assert verifier_timeout == 180 + 180


def test_example_policy_is_valid():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "grid_control" / "policy.example.json"
    policy = Policy.from_dict(json.loads(path.read_text(encoding="utf-8")))
    assert policy.scenarios and policy.dev_scenarios and policy.egress_hosts
    assert not {s.name for s in policy.scenarios} & {s.name for s in policy.dev_scenarios}

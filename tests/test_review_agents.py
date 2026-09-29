"""Review agents: a workbench per review, one turn at a time, proposals written only there."""

import asyncio
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.managers.project_tools_loader import ProjectToolsLoader
from utils.path_utils import factory_path_context
from web_chat.review.agents import CONVERSATION, FIRST_REQUEST, AnalysisBusy, ReviewAgents
from web_chat.review.store import Review, ReviewStore
from web_chat.review.workbench import Workbench

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = {
    "grid": {"commit": None, "changed": False},
    "target": {"context_id": "ctx-1", "message_id": "m2", "system": "engineering", "agent": "coder"},
    "conversation": {"title": "files", "messages": [
        {"role": "user", "content": "list the files"},
        {"role": "assistant", "agent": "coder", "content": "done: a.txt"},
    ]},
    "turn": {"steps": [{"kind": "tool", "title": "bash_tool", "body": "ls"}], "executions": []},
    "model_context": {"assembly": None, "instructions": None, "note": "Recorded for the latest answer only."},
    "agent_session": {"session_id": "agent_coder_ctx-1", "items": [{"role": "user", "content": "list the files"}]},
    "config": {"system": "engineering", "agent": {"name": "Coder", "tools": ["bash_tool"]}, "tools": {}, "models": {}},
}


def _review(review_id="r1", status="new") -> Review:
    return Review(
        id=review_id, created_at=1.0, user_id="u" * 32, username="alice", context_id="ctx-1", message_id="m2",
        system="engineering", agent="coder", note="it listed the wrong files", status=status, origin="user",
    )


# -- workbench ------------------------------------------------------------------------
def test_the_workbench_lays_out_the_evidence_to_read(tmp_path):
    bench = Workbench(tmp_path / "work" / "r1")

    bench.prepare(_review().to_dict(), EVIDENCE)

    case = bench.root / "case"
    assert "it listed the wrong files" in (case / "README.md").read_text(encoding="utf-8")
    conversation = (case / "conversation.md").read_text(encoding="utf-8")
    assert "## conversation#1 · assistant · coder  ← the reported answer" in conversation
    assert json.loads((case / "steps.json").read_text(encoding="utf-8"))[0]["title"] == "bash_tool"
    assert "latest answer only" in (case / "model_context.md").read_text(encoding="utf-8")
    assert (bench.root / "source" / "web_chat" / "server.py").exists()
    assert "runs now" in (bench.root / "source" / "SOURCE.md").read_text(encoding="utf-8")
    assert not list((bench.root / "source").rglob("*.wav")) and not list((bench.root / "source").rglob("__pycache__"))


def test_the_source_is_the_answers_commit_when_the_checkout_has_it(tmp_path):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if not head:
        pytest.skip("not a git checkout")
    bench = Workbench(tmp_path / "w")

    bench.prepare(_review().to_dict(), {**EVIDENCE, "grid": {"commit": head}})

    assert f"commit {head}" in (bench.root / "source" / "SOURCE.md").read_text(encoding="utf-8")
    assert (bench.root / "source" / "routing.yaml").exists()


def test_a_malformed_proposal_file_is_skipped(tmp_path):
    bench = Workbench(tmp_path / "w")
    bench.proposals_dir.mkdir(parents=True)
    (bench.proposals_dir / "1.json").write_text('{"title": "fix it"}', encoding="utf-8")
    (bench.proposals_dir / "2.json").write_text("not json", encoding="utf-8")

    assert bench.proposals() == [{"id": "1", "title": "fix it", "task": None}]


# -- turns ------------------------------------------------------------------------------
class FakeFactory:
    """Answers like the reviewer would: a message, and a proposal in the workbench."""

    def __init__(self, bench, manager, release=None):
        self.bench, self.manager, self.release = bench, manager, release
        self.asked, self.closed = [], False

    async def run_agent(self, agent, message, context_id):
        self.asked.append((agent, message, context_id))
        if self.release is not None:
            await self.release.wait()
        self.manager.activate_context(context_id)
        self.manager.add_message("user", message)
        self.manager.add_message("assistant", "The engineer read the wrong directory.")
        self.bench.proposals_dir.mkdir(exist_ok=True)
        (self.bench.proposals_dir / "p1.json").write_text(json.dumps({"title": "Say which directory"}), encoding="utf-8")
        return "done"

    async def cleanup(self):
        self.closed = True


def _agents(tmp_path, factories, release=None):
    store = ReviewStore(tmp_path / "reviews")
    store.add(_review(), EVIDENCE)

    def build(bench, manager):
        factory = FakeFactory(bench, manager, release)
        factories.append(factory)
        return factory

    return store, ReviewAgents(store, tmp_path / "reviews", build_factory=build)


async def _finish(agents, review_id="r1"):
    await asyncio.gather(agents._turns[review_id])


async def test_the_first_turn_reviews_the_case_and_records_proposals(tmp_path):
    factories = []
    store, agents = _agents(tmp_path, factories)

    await agents.ask("r1")
    assert store.get("r1").status == "in_review"
    await _finish(agents)

    [factory] = factories
    assert factory.asked == [("reviewer", FIRST_REQUEST, CONVERSATION)] and factory.closed
    state = agents.state("r1")
    assert [m["content"] for m in state["messages"]] == [FIRST_REQUEST, "The engineer read the wrong directory."]
    assert state["proposals"][0]["title"] == "Say which directory" and not state["running"]
    assert store.get("r1").status == "proposed"


async def test_one_turn_runs_at_a_time_per_review(tmp_path):
    release = asyncio.Event()
    store, agents = _agents(tmp_path, [], release)

    await agents.ask("r1", "why?")
    with pytest.raises(AnalysisBusy):
        await agents.ask("r1", "and?")
    assert agents.state("r1")["running"]
    release.set()
    await _finish(agents)


async def test_a_failed_turn_is_reported_not_raised(tmp_path):
    store, agents = _agents(tmp_path, [])

    class Broken(FakeFactory):
        async def run_agent(self, agent, message, context_id):
            raise RuntimeError("the model is down")

    agents._build_factory = lambda bench, manager: Broken(bench, manager)
    await agents.ask("r1")
    await _finish(agents)

    assert agents.state("r1")["error"] == "RuntimeError: the model is down"
    assert store.get("r1").status == "in_review"


async def test_an_unknown_review_is_refused(tmp_path):
    _, agents = _agents(tmp_path, [])

    with pytest.raises(LookupError):
        await agents.ask("nope")


# -- the proposal tool ------------------------------------------------------------------
def _propose_change():
    loader = ProjectToolsLoader(str(ROOT / "examples" / "context-review"), "tools")
    return loader.load_project_tools()["propose_change"]


async def _propose(tool, workdir, **args):
    factory = SimpleNamespace(config=SimpleNamespace(get_working_directory=lambda: str(workdir)), container_id=None)
    with factory_path_context(factory):
        return await tool.on_invoke_tool(None, json.dumps(args))


async def test_a_proposal_is_written_into_the_workbench_only(tmp_path):
    tool = _propose_change()
    diff = "--- a/examples/coder/config.yaml\n+++ b/examples/coder/config.yaml\n@@ -1 +1 @@\n-a\n+b\n"

    answer = await _propose(
        tool, tmp_path, title="Say which directory", cause="prompt", summary="It guessed.",
        evidence=["steps#0"], change=diff, scenario="ask again", confidence="high",
    )

    assert answer.startswith("✅")
    [written] = (tmp_path / "proposals").glob("*.json")
    assert json.loads(written.read_text(encoding="utf-8"))["change"] == diff.strip()


@pytest.mark.parametrize(
    "changes,problem",
    [
        ({"cause": "vibes"}, "cause must be one of"),
        ({"evidence": []}, "evidence needs"),
        ({"change": "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n"}, "must stay inside source/"),
        ({"change": "just words"}, "must be a unified diff"),
    ],
)
async def test_a_malformed_proposal_is_refused(tmp_path, changes, problem):
    args = {"title": "t", "cause": "prompt", "summary": "s", "evidence": ["steps#0"], **changes}

    answer = await _propose(_propose_change(), tmp_path, **args)

    assert answer.startswith("❌") and problem in answer
    assert not (tmp_path / "proposals").exists()


# -- the system ---------------------------------------------------------------------
async def test_the_context_review_system_builds_its_agents(tmp_path, monkeypatch):
    from core.agent_factory import AgentFactory
    from core.config.config import Config
    from core.context import ContextManager
    from tools.function_tools import resolve_tool
    from web_chat.review.agents import CONFIG_PATH

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    config = Config(str(CONFIG_PATH), str(tmp_path))
    for agent in config.config.agents.values():
        for name in agent.tools:
            tool = config.get_tool(name)
            if tool.type == "function":
                assert resolve_tool(name, config.project_tools_loader) is not None, name
    # Read-only by construction: no tool that writes, runs or fetches.
    granted = {name for agent in config.config.agents.values() for name in agent.tools}
    assert granted == {"file_read", "file_list", "file_search", "file_content_search", "propose_change", "call_proposer"}

    factory = AgentFactory(
        config=config, working_directory=str(tmp_path),
        context_manager=ContextManager(persist_path=str(tmp_path / "c.json")),
        session_db_path=str(tmp_path / "s.db"), logs_directory=str(tmp_path / "logs"),
    )
    try:
        for key in ("reviewer", "proposer"):
            agent = await factory.create_agent(key)
            assert agent is not None
    finally:
        await factory.cleanup()


# -- routes -------------------------------------------------------------------------
def test_admins_start_an_analysis_and_follow_it(tmp_path):
    from fastapi.testclient import TestClient

    from schemas.schemas import ReviewPolicy
    from web_chat.identity import User
    from web_chat.review.desk import ReviewDesk
    from web_chat.server import WebChatServer
    from web_chat.spaces import SpacePool

    admin = User(id="b" * 32, username="boss", role="admin")
    store, agents = _agents(tmp_path, [])

    async def identify(connection):
        return admin

    def client(agents_or_none):
        desk = ReviewDesk(store, ReviewPolicy, agents=agents_or_none)
        server = WebChatServer(
            SimpleNamespace(voice_enabled=lambda: False), SpacePool(lambda user_id: None), identify=identify,
            warm_user=None, reviews=desk,
        )
        return TestClient(server.app, headers={"Origin": "http://testserver"})

    with client(agents) as http:
        started = http.post("/api/admin/reviews/r1/analysis", json={})
        assert started.status_code == 202
        assert http.post("/api/admin/reviews/nope/analysis", json={}).status_code == 404
        # The turn runs in the background on the app's loop; the state follows it.
        for _ in range(50):
            state = http.get("/api/admin/reviews/r1/analysis").json()
            if not state["running"]:
                break
            time.sleep(0.05)
        assert state["proposals"][0]["title"] == "Say which directory"
    assert client(None).get("/api/admin/reviews/r1/analysis").status_code == 404

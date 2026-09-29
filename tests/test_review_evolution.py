"""A proposal's way on: its patch checked against this code, then a task in the evolution loop."""

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from core.workshop import ControlClient
from grid_control.controller import Controller
from grid_control.models import Check, Policy, Trial
from grid_control.service import create_app
from grid_control.store import Store
from schemas.schemas import ReviewPolicy
from web_chat.identity import User
from web_chat.review.agents import ReviewAgents
from web_chat.review.desk import ReviewDesk
from web_chat.review.evolution import EvolutionOutbox, check_patch
from web_chat.review.store import Review, ReviewStore
from web_chat.server import WebChatServer
from web_chat.spaces import SpacePool

ROOT = Path(__file__).resolve().parent.parent
# A diff as a proposal carries it: README.md's first line changed, with context.
_HEAD = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[:4]
FITTING = "".join(
    ["--- a/README.md\n", "+++ b/README.md\n", "@@ -1,4 +1,4 @@\n", f"-{_HEAD[0]}\n", f"+{_HEAD[0]}!\n"]
    + [f" {line}\n" for line in _HEAD[1:]]
)
STALE = "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-no such line\n+anything\n"
WORKSHOP_TOKEN, TASK_TOKEN = "t" * 32, "o" * 32


# -- the patch check ------------------------------------------------------------------
def test_a_patch_is_checked_against_this_code_and_changes_nothing():
    before = (ROOT / "README.md").read_bytes()

    fits, stale, empty = check_patch(FITTING), check_patch(STALE), check_patch("")

    assert fits["applies"] is True
    assert stale["applies"] is False and stale["detail"]
    assert empty["applies"] is None
    assert (ROOT / "README.md").read_bytes() == before


# -- the evolution loop, end to end ---------------------------------------------------------
class Runtime:
    def revision(self, repo, ref):
        return ref

    def image(self, ref):
        return ref

    def build(self, *args):
        return "image"

    def trial(self, *args):
        return Trial({"web": True}, 0.1)

    def cleanup(self, *args):
        pass


def _controller(tmp_path):
    store = Store(tmp_path / "control.db")
    policy = Policy("runtime", "verifier", (Check("web", "/"),), repetitions=1)
    return create_app(Controller(store, Runtime()), tmp_path, policy, WORKSHOP_TOKEN, TASK_TOKEN)


def _review_with_proposal(root: Path) -> tuple[ReviewStore, ReviewAgents]:
    store = ReviewStore(root)
    store.add(
        Review(
            id="r1", created_at=1.0, user_id="u" * 32, username="alice", context_id="ctx-1", message_id="m1",
            system="engineering", agent="coder", note="", status="proposed", origin="user",
        ),
        {},
    )
    agents = ReviewAgents(store, root)
    proposals = agents.workbench("r1").proposals_dir
    proposals.mkdir(parents=True)
    (proposals / "p1.json").write_text(json.dumps({
        "title": "Say which directory", "cause": "prompt", "summary": "It guessed.",
        "evidence": ["steps#0"], "change": FITTING, "scenario": "ask again", "confidence": "high",
    }), encoding="utf-8")
    return store, agents


def test_a_proposal_travels_to_the_workshop_and_its_verdict_comes_back(tmp_path):
    admin = User(id="b" * 32, username="boss", role="admin")

    async def identify(connection):
        return admin

    # One controller: its lifespan owns the journal, so only the first client enters it.
    with TestClient(_controller(tmp_path / "control"), headers={"Authorization": f"Bearer {TASK_TOKEN}"}) as operator_side:
        workshop_side = TestClient(operator_side.app, headers={"Authorization": f"Bearer {WORKSHOP_TOKEN}"})
        store, agents = _review_with_proposal(tmp_path / "reviews")
        desk = ReviewDesk(store, ReviewPolicy, agents=agents, evolution=EvolutionOutbox(operator_side))
        chat = TestClient(
            WebChatServer(
                SimpleNamespace(voice_enabled=lambda: False), SpacePool(lambda user_id: None), identify=identify,
                warm_user=None, reviews=desk,
            ).app,
            headers={"Origin": "http://testserver"},
        )

        checked = chat.post("/api/admin/reviews/r1/proposals/p1/check").json()
        sent = chat.post("/api/admin/reviews/r1/proposals/p1/evolve")
        again = chat.post("/api/admin/reviews/r1/proposals/p1/evolve")

        # The workshop's administrator takes the task and submits an experiment for it.
        workshop = ControlClient(workshop_side)
        [task] = workshop.tasks()
        taken = workshop.take_task(task["id"])
        experiment = workshop_side.post("/experiments", json={"baseline": "a" * 40, "candidate": "b" * 40}).json()["id"]
        workshop.link_task(task["id"], experiment)

        status = chat.get("/api/admin/reviews/r1/proposals/p1/task").json()

    assert checked["applies"] is True
    assert sent.status_code == 201 and again.status_code == 409
    assert taken["change"] == FITTING and taken["source"] == {"review": "r1", "proposal": "p1"}
    assert status["experiment"] == experiment and status["status"] == "taken" and status["verdict"]
    assert agents.state("r1")["proposals"][0]["task"]["id"] == task["id"]


def test_without_the_evolution_loop_sending_is_refused(tmp_path):
    admin = User(id="b" * 32, username="boss", role="admin")

    async def identify(connection):
        return admin

    store, agents = _review_with_proposal(tmp_path)
    chat = TestClient(
        WebChatServer(
            SimpleNamespace(voice_enabled=lambda: False), SpacePool(lambda user_id: None), identify=identify,
            warm_user=None, reviews=ReviewDesk(store, ReviewPolicy, agents=agents),
        ).app,
        headers={"Origin": "http://testserver"},
    )

    assert chat.post("/api/admin/reviews/r1/proposals/p1/evolve").status_code == 503
    assert chat.post("/api/admin/reviews/r1/proposals/nope/check").status_code == 404
    assert chat.get("/api/admin/reviews").json()["evolution"] is False

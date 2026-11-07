"""Lazy tools: the user's own tasks, their project, idempotent creation and a stable series."""

import json
from types import SimpleNamespace

import pytest

from core.lazy.model import Settings
from core.lazy.routine import Routine
from core.lazy.store import LazyStore
from core.quota_telemetry import QuotaReading, QuotaTelemetry, WindowReading
from tools import lazy_tools
from web_chat.lazy.chat import LazyChat, LazyProjectArchived, LazyProjectNotFound, LazyUnavailable
from web_chat.lazy.service import LazyAgents, LazyError, LazyStale
from web_chat.lazy.runner import StepOutcome, StepRunner, ensure_conversation, project_problem
from web_chat.projects import ProjectStore

NOW = 1_800_000_000.0
HOUR = 3600.0


# -- a space, as far as the lazy tools look into it ---------------------------------------
class Access:
    credentials = SimpleNamespace(available=lambda provider: True, problem=lambda p: None)

    def check_model(self, config, key):
        return None


class Config:
    MODELS = {"opus": "claude", "luna": "chatgpt"}

    def __init__(self, agents):
        self.agents = agents

    def get_agent(self, key):
        return SimpleNamespace(model_keys=lambda: list(self.agents[key]))

    def get_model(self, key):
        return SimpleNamespace(provider=key)

    def get_provider(self, key):
        return SimpleNamespace(auth=self.MODELS[key], name=key)


class Registry:
    def __init__(self, agents):
        self._config = Config(agents)
        self._access = Access()

    def systems(self):
        return [SimpleNamespace(key="eng", name="Engineering")]

    def agents(self, system):
        return {key: SimpleNamespace(routable=True) for key in self._config.agents}

    def agent_label(self, system, agent):
        return agent.title()

    def config(self, system):
        return self._config

    def factory(self, system):
        return SimpleNamespace(models=SimpleNamespace(access=self._access))


AGENTS = {"engineer": ["opus"]}


def space_of(project_store=None, agents=None):
    return SimpleNamespace(registry=Registry(agents or AGENTS), project_store=project_store)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def reading(five=20.0, week=20.0):
    return QuotaReading(
        "claude-code",
        {
            "five_hour": WindowReading("five_hour", five, NOW + 3 * HOUR),
            "seven_day": WindowReading("seven_day", week, NOW + 100 * HOUR),
        },
        observed_at=NOW,
        status="allowed",
    )


class Runner:
    async def run(self, task, step, settings, *, should_stop, on_conversation):
        raise AssertionError("A tool must never run a step")


def world_for(tmp_path, name="u", projects=True):
    store = LazyStore(tmp_path / f"{name}.db")
    store.save_settings(name, Settings(routine=Routine(hours={}, timezone="UTC")))
    quota = QuotaTelemetry()
    quota.publish(reading())
    projects_store = ProjectStore(tmp_path / "projects.json") if projects else None
    space = space_of(projects_store)
    lazy = LazyAgents(store, SimpleNamespace(), quota=quota, runner=Runner(), clock=Clock())
    chat = LazyChat(lazy, name, space)
    return SimpleNamespace(
        store=store, lazy=lazy, chat=chat, space=space, projects=projects_store, quota=quota, name=name
    )


@pytest.fixture
def world(tmp_path):
    return world_for(tmp_path)


def ctx(chat):
    return SimpleNamespace(context=SimpleNamespace(factory=SimpleNamespace(lazy_agents=chat)))


async def call(tool, chat, **arguments):
    return json.loads(await tool.on_invoke_tool(ctx(chat), json.dumps(arguments)))


PAYLOAD = {"title": "Report", "brief": "Write it", "system": "eng", "agent": "engineer", "size": "S"}


# -- what the agent sees ------------------------------------------------------------------
async def test_catalog_offers_the_agents_and_the_limits(world):
    out = await call(lazy_tools.lazy_catalog, world.chat)
    assert [agent["agent"] for agent in out["agents"]] == ["engineer"]
    assert out["agents"][0]["problem"] is None
    assert out["sizes"] and out["priorities"] and out["states"]
    assert out["limits"]["repeat_hours_min"] == 1
    assert "project_id" in out["fields"]


async def test_create_then_list_and_get_are_compact(world):
    made = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD, request_id="r1")
    assert made["created"] is True and made["status"] == "created"
    task_id = made["task"]["id"]

    listed = await call(lazy_tools.lazy_list, world.chat)
    assert [task["id"] for task in listed["tasks"]] == [task_id]
    assert "brief" not in listed["tasks"][0]  # compact: no bodies, no steps
    assert listed["tasks"][0]["steps"]["total"] >= 2

    one = await call(lazy_tools.lazy_get, world.chat, task_id=task_id)
    assert one["task"]["brief"] == "Write it"
    assert [step["kind"] for step in one["task"]["steps_detail"]][0] == "scout"
    assert one["placement"] is not None  # the planner's own answer, never recomputed here


async def test_deadline_is_an_iso_moment_in_the_users_zone(world):
    made = await call(
        lazy_tools.lazy_create, world.chat, **PAYLOAD, deadline="2026-11-10T18:00"
    )
    stored = world.store.task(world.name, made["task"]["id"])
    from datetime import datetime, timezone

    assert stored.deadline == datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc).timestamp()


# -- creation is idempotent ----------------------------------------------------------------
async def test_the_same_request_id_makes_one_task(world):
    first = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD, request_id="same")
    again = await call(lazy_tools.lazy_create, world.chat, **{**PAYLOAD, "title": "Other", "request_id": "same"})
    assert first["created"] is True and again["created"] is False and again["status"] == "exists"
    assert again["task"]["id"] == first["task"]["id"]
    assert again["task"]["title"] == "Report"  # nothing was overwritten
    assert len(world.store.tasks(world.name)) == 1
    assert "hint" in again


async def test_without_a_request_id_each_call_makes_a_task(world):
    await call(lazy_tools.lazy_create, world.chat, **PAYLOAD)
    await call(lazy_tools.lazy_create, world.chat, **PAYLOAD)
    assert len(world.store.tasks(world.name)) == 2


# -- made on pause: one save, never eligible -----------------------------------------------
async def test_a_task_can_be_made_already_paused_and_never_starts(world):
    made = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD, status="paused")
    task_id = made["task"]["id"]
    assert made["created"] is True and made["task"]["status"] == "paused"
    assert world.store.task(world.name, task_id).status == "paused"

    plan = world.lazy.plan_for(world.name, world.store.settings(world.name))
    assert plan.tasks[task_id].state == "paused"
    assert all(placement.task_id != task_id for placement in plan.placements)

    # A tick runs the fixture's Runner, which fails the test if anything starts.
    await world.lazy._tick()
    assert not world.lazy._running
    assert world.store.task(world.name, task_id).steps[0].status == "pending"


async def test_an_unknown_status_at_creation_is_refused_and_makes_nothing(world):
    out = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD, status="done")
    assert out["isError"] and out["code"] == "invalid"
    assert world.store.tasks(world.name) == []
    with pytest.raises(LazyError):
        world.chat.create({**PAYLOAD, "status": "cancelled"})
    assert world.store.tasks(world.name) == []


async def test_preview_understands_the_paused_status_and_saves_nothing(world):
    picture = world.chat.preview({**PAYLOAD, "status": "paused"})
    assert picture["placements"] == []
    assert world.store.tasks(world.name) == []


def test_recording_a_chat_does_not_undo_a_concurrent_pause(world, monkeypatch):
    task = world.lazy.draft(world.name, world.space, {**PAYLOAD, "title": "Report"})
    world.store.save_task(task)
    real = world.store.save_task
    state = {"raced": False}

    def racing(saved, *, expected_version=None):
        if not state["raced"]:
            state["raced"] = True
            world.lazy.update_task(world.name, task.id, {"status": "paused"})
            return False  # this write was based on a task that has moved
        return real(saved, expected_version=expected_version)

    monkeypatch.setattr(world.store, "save_task", racing)
    world.lazy._remember_conversation(world.name, task.id, "ctx-1")

    stored = world.store.task(world.name, task.id)
    assert state["raced"] is True
    assert stored.conversation_id == "ctx-1" and stored.status == "paused"


def test_a_chat_that_keeps_losing_the_race_is_left_for_the_next_step(world, monkeypatch):
    task = world.lazy.draft(world.name, world.space, {**PAYLOAD, "title": "Report"})
    world.store.save_task(task)
    monkeypatch.setattr(world.store, "save_task", lambda saved, *, expected_version=None: False)

    world.lazy._remember_conversation(world.name, task.id, "ctx-1")

    stored = world.store.task(world.name, task.id)
    assert stored.conversation_id is None and stored.status == "active"


# -- the user's projects ------------------------------------------------------------------
async def test_a_task_belongs_to_the_users_own_project(world):
    project, _ = world.projects.ensure("Launch")
    made = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD, project_id=project.id)
    task_id = made["task"]["id"]
    assert made["task"]["project_id"] == project.id
    assert world.store.task(world.name, task_id).project_id == project.id
    one = await call(lazy_tools.lazy_get, world.chat, task_id=task_id)
    assert one["project"] == {"id": project.id, "title": "Launch", "archived": False}
    only = await call(lazy_tools.lazy_list, world.chat, project_id=project.id)
    assert [task["id"] for task in only["tasks"]] == [task_id]
    assert (await call(lazy_tools.lazy_list, world.chat, project_id="prj-other"))["code"] == "project_not_found"


async def test_another_users_project_is_refused_and_nothing_is_made(world, tmp_path):
    stranger = ProjectStore(tmp_path / "stranger" / "projects.json")
    theirs, _ = stranger.ensure("Secret")
    out = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD, project_id=theirs.id)
    assert out["isError"] and out["code"] == "project_not_found"
    assert world.store.tasks(world.name) == []


async def test_an_archived_project_is_refused_and_nothing_is_made(world):
    project, _ = world.projects.ensure("Old")
    world.projects.update(project.id, archived=True)
    out = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD, project_id=project.id)
    assert out["code"] == "project_archived"
    assert world.store.tasks(world.name) == []
    with pytest.raises(LazyProjectArchived):
        world.chat.create({**PAYLOAD, "project_id": project.id})


async def test_a_task_can_be_moved_to_a_project_and_detached(world):
    project, _ = world.projects.ensure("Launch")
    made = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD)
    task_id = made["task"]["id"]
    moved = await call(lazy_tools.lazy_update, world.chat, task_id=task_id, project_id=project.id)
    assert moved["task"]["project_id"] == project.id
    detached = await call(lazy_tools.lazy_update, world.chat, task_id=task_id, project_id="")
    assert detached["task"]["project_id"] is None


# -- a stale change is refused ---------------------------------------------------------------
async def test_a_change_names_the_version_it_read(world):
    made = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD)
    task_id, version = made["task"]["id"], made["version"]
    done = await call(
        lazy_tools.lazy_update, world.chat, task_id=task_id, priority="high", expected_version=version
    )
    assert done["task"]["priority"] == "high" and done["version"] > version
    stale = await call(
        lazy_tools.lazy_update, world.chat, task_id=task_id, priority="low", expected_version=version
    )
    assert stale["isError"] and stale["code"] == "stale"
    assert world.store.task(world.name, task_id).priority == "high"
    with pytest.raises(LazyStale):
        world.chat.update(task_id, {"priority": "low"}, expected_version=version)


async def test_unknown_fields_are_refused(world):
    made = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD)
    out = await call(lazy_tools.lazy_update, world.chat, task_id=made["task"]["id"], priority="high", size="L")
    assert out["task"]["size"] == "L"
    with pytest.raises(LazyError):
        world.chat.update(made["task"]["id"], {"user_id": "someone-else"})


# -- a series of rounds ----------------------------------------------------------------------
async def test_a_repeating_task_opens_a_series(world):
    made = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD, repeat_hours=24)
    task = world.store.task(world.name, made["task"]["id"])
    assert task.series_id == task.id and made["task"]["repeat_hours"] == 24


async def test_the_series_is_paused_edited_and_cancelled_as_one(world):
    made = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD, repeat_hours=24)
    series = made["task"]["series_id"]
    assert series == made["task"]["id"]

    paused = await call(lazy_tools.lazy_update, world.chat, task_id=series, status="paused", series=True)
    assert paused["task"]["status"] == "paused" and paused["series_id"] == series
    assert world.store.task(world.name, series).status == "paused"

    edited = await call(lazy_tools.lazy_update, world.chat, task_id=series, repeat_hours=12, series=True)
    assert edited["task"]["repeat_hours"] == 12

    stopped = await call(lazy_tools.lazy_cancel, world.chat, task_id=series, series=True)
    assert stopped["status"] == "cancelled" and stopped["series_id"] == series
    assert len(stopped["cancelled"]) == 1
    assert world.store.tasks(world.name, finished=False) == []

    again = await call(lazy_tools.lazy_cancel, world.chat, task_id=series, series=True)
    assert again["status"] == "finished" and again["cancelled"] == []


async def test_a_series_cancel_stops_the_running_round_and_its_next_one(world, monkeypatch):
    made = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD, repeat_hours=24)
    series = made["task"]["series_id"]
    task = world.store.task(world.name, series)
    # The round the service makes when this one ends keeps the series and the project.
    project, _ = world.projects.ensure("Launch")
    task.project_id = project.id
    world.store.save_task(task)
    following = world.lazy._next_round(world.store.task(world.name, series), NOW)
    world.store.save_task(following)
    assert following.series_id == series and following.project_id == project.id
    assert following.id != series

    stopped = await call(lazy_tools.lazy_cancel, world.chat, task_id=series, series=True)
    assert {item["id"] for item in stopped["cancelled"]} == {series, following.id}
    assert world.store.tasks(world.name, finished=False) == []


# -- tenancy and the session ---------------------------------------------------------------
async def test_two_users_never_see_each_other(world, tmp_path):
    other = world_for(tmp_path, name="v")
    made = await call(lazy_tools.lazy_create, world.chat, **PAYLOAD)
    assert (await call(lazy_tools.lazy_list, other.chat))["tasks"] == []
    assert (await call(lazy_tools.lazy_get, other.chat, task_id=made["task"]["id"]))["code"] == "not_found"
    assert (await call(lazy_tools.lazy_cancel, other.chat, task_id=made["task"]["id"]))["code"] == "not_found"
    assert world.store.task(world.name, made["task"]["id"]).status == "active"


async def test_a_session_without_a_user_is_refused_not_guessed():
    out = json.loads(
        await lazy_tools.lazy_list.on_invoke_tool(
            SimpleNamespace(context=SimpleNamespace(factory=SimpleNamespace(lazy_agents=None))), "{}"
        )
    )
    assert out["isError"] and out["code"] == "unavailable"
    with pytest.raises(LazyUnavailable):
        lazy_tools._client(SimpleNamespace(context=None))


def test_tools_take_no_identity_path_or_quota_and_declare_their_effect():
    for name, tool in lazy_tools.LAZY_TOOLS.items():
        schema = set(tool.params_json_schema.get("properties", {}))
        assert not schema & {"user_id", "user", "path", "directory", "tenant", "workspace"}, name
        assert not schema & {"share", "safety", "allowances", "paused"}, name
    assert not any("delete" in name for name in lazy_tools.LAZY_TOOLS)
    assert set(lazy_tools.TOOL_EFFECTS) == set(lazy_tools.LAZY_TOOLS)
    assert set(lazy_tools.TOOL_ISOLATION) == set(lazy_tools.LAZY_TOOLS)
    assert lazy_tools.TOOL_EFFECTS["lazy_create"].kind == "write"
    assert lazy_tools.TOOL_EFFECTS["lazy_list"].kind == "read"


# -- the task's project reaches the step's chat --------------------------------------------
class StepSpace:
    def __init__(self, projects):
        self.project_store = projects
        self.started = []
        self.metadata = {}
        self.messages = []

    def context_manager(self):
        return self

    def start_new_context(self):
        self.started.append("ctx-1")
        return "ctx-1"

    def has_context(self, context_id):
        return context_id in self.started

    def update_conversation_metadata(self, context_id, **updates):
        self.metadata.setdefault(context_id, {}).update({k: v for k, v in updates.items() if v is not None})

    def append_message_to(self, context_id, role, text, metadata=None):
        self.messages.append((context_id, role, text))
        return True

    def get_context_metadata(self, context_id):
        return self.metadata.get(context_id, {})

    def update_context_metadata(self, context_id, values, create=True):
        self.metadata.setdefault(context_id, {}).update(values)
        return True

    def assign_chat_space(self, context_id):
        return context_id


def test_a_steps_chat_belongs_to_the_tasks_project(tmp_path):
    projects = ProjectStore(tmp_path / "projects.json")
    project, _ = projects.ensure("Launch")
    projects.add_notes(project.id, "Ship on Monday")
    space = StepSpace(projects)
    task = SimpleNamespace(
        id="lazy-1", title="Report", system="eng", agent="engineer", conversation_id=None, project_id=project.id
    )
    context_id = ensure_conversation(space, task)
    assert space.metadata[context_id]["project_id"] == project.id
    assert space.metadata[context_id]["lazy_task"] == "lazy-1"
    assert space.messages and "Ship on Monday" in space.messages[0][2]


def test_a_task_without_a_project_makes_an_ordinary_chat(tmp_path):
    space = StepSpace(ProjectStore(tmp_path / "projects.json"))
    task = SimpleNamespace(
        id="lazy-2", title="Report", system="eng", agent="engineer", conversation_id=None, project_id=None
    )
    context_id = ensure_conversation(space, task)
    assert "project_id" not in space.metadata[context_id]
    assert space.messages == []


# -- activation: a shared module discovered by name, declared by the system -----------------
def test_the_shared_registry_discovers_every_lazy_tool():
    from tools import resolve_tool, tool_effect, tool_isolation
    from tools.function_tools import AVAILABLE_TOOLS

    for name in lazy_tools.LAZY_TOOLS:
        assert name in AVAILABLE_TOOLS, name
        tool = resolve_tool(name, None)
        assert tool is not None and getattr(tool, "name", None) == name
        assert tool_effect(name, None) is lazy_tools.TOOL_EFFECTS[name]
        assert tool_isolation(name, None) == lazy_tools.TOOL_ISOLATION[name]


def test_coder_config_gives_engineer_the_lazy_tools_and_general_purpose_read_only():
    import yaml

    document = yaml.safe_load(open("examples/coder/config.yaml", encoding="utf-8"))
    declared = document["tools"]
    for name in lazy_tools.LAZY_TOOLS:
        assert declared[name]["type"] == "function", name
    engineer = set(document["agents"]["engineer"]["tools"])
    assert set(lazy_tools.LAZY_TOOLS) <= engineer
    gp = set(document["agents"]["general_purpose"]["tools"])
    assert gp & set(lazy_tools.LAZY_TOOLS) == {
        "lazy_catalog", "lazy_list", "lazy_get", "lazy_preview"
    }


# -- the independent checks: the page's own API is no way around the checks -----------------
def test_the_pages_api_refuses_a_project_that_is_not_yours_and_makes_nothing(world):
    # The page's POST/PATCH do not pass through LazyChat: the check lives in the
    # service, so a crafted body cannot bind a task to a made-up project either.
    with pytest.raises(LazyProjectNotFound) as missing:
        world.lazy.create_task(world.name, world.space, {**PAYLOAD, "project_id": "prj-made-up"})
    assert missing.value.code == "project_not_found"
    assert world.store.tasks(world.name) == []

    project, _ = world.projects.ensure("Old")
    world.projects.update(project.id, archived=True)
    with pytest.raises(LazyProjectArchived) as archived:
        world.lazy.create_task(world.name, world.space, {**PAYLOAD, "project_id": project.id})
    assert archived.value.code == "project_archived"
    assert world.store.tasks(world.name) == []

    made = world.lazy.create_task(world.name, world.space, PAYLOAD)
    with pytest.raises(LazyProjectNotFound):
        world.lazy.update_task(world.name, made.id, {"project_id": "prj-made-up"}, space=world.space)
    with pytest.raises(LazyProjectArchived):
        world.lazy.update_task(world.name, made.id, {"project_id": project.id}, space=world.space)
    assert world.store.task(world.name, made.id).project_id is None


def test_an_edit_without_a_space_cannot_attach_a_project(world):
    # An internal caller that names a project must hand the check its space.
    made = world.lazy.create_task(world.name, world.space, PAYLOAD)
    with pytest.raises(LazyProjectNotFound):
        world.lazy.update_task(world.name, made.id, {"project_id": "prj-made-up"})
    assert world.store.task(world.name, made.id).project_id is None


# -- a version is a compare-and-swap, not just a number that grows -------------------------
def test_edits_of_one_version_cannot_both_win(world):
    import threading

    made = world.lazy.create_task(world.name, world.space, PAYLOAD)
    version = made.version
    outcomes = []

    def edit(title):
        try:
            task = world.lazy.update_task(world.name, made.id, {"title": title}, expected_version=version)
            outcomes.append(("written", title, task.version))
        except LazyStale:
            outcomes.append(("stale", title, None))

    threads = [threading.Thread(target=edit, args=(f"Edit {index}",)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    written = [outcome for outcome in outcomes if outcome[0] == "written"]
    assert len(written) == 1, outcomes  # the check and the write are one step
    stored = world.store.task(world.name, made.id)
    assert stored.title == written[0][1]
    assert stored.version == version + 1


def test_a_step_that_ran_makes_a_change_read_before_it_stale(world):
    made = world.lazy.create_task(world.name, world.space, PAYLOAD)
    read_version = made.version
    task = world.store.task(world.name, made.id)
    task.note("Scout done", kind="done")  # what the runner does when a step ends
    world.store.save_task(task)
    with pytest.raises(LazyStale):
        world.lazy.update_task(world.name, made.id, {"title": "Late"}, expected_version=read_version)
    assert world.store.task(world.name, made.id).title == "Report"


def test_a_named_value_that_is_unknown_is_refused_and_changes_nothing(world):
    made = world.lazy.create_task(world.name, world.space, PAYLOAD)
    for changes in ({"size": "XXL"}, {"priority": "urgent"}, {"status": "done"}):
        with pytest.raises(LazyError) as refused:
            world.chat.update(made.id, changes)
        assert refused.value.code == "invalid"
    stored = world.store.task(world.name, made.id)
    assert (stored.size, stored.priority, stored.status) == ("S", "normal", "active")
    with pytest.raises(LazyError):
        world.lazy.create_task(world.name, world.space, {**PAYLOAD, "size": "XXL"})


# -- a preview is a read: it writes nothing, not even the defaults --------------------------
def test_preview_saves_nothing_not_even_the_settings_of_a_first_visit(tmp_path):
    store = LazyStore(tmp_path / "fresh.db")
    projects = ProjectStore(tmp_path / "projects.json")
    space = space_of(projects)
    quota = QuotaTelemetry()
    quota.publish(reading())
    lazy = LazyAgents(store, SimpleNamespace(), quota=quota, runner=Runner(), clock=Clock())
    chat = LazyChat(lazy, "fresh", space)

    import asyncio

    out = asyncio.run(call(lazy_tools.lazy_preview, chat, **PAYLOAD))
    assert out["steps"] and "plan" in out
    assert store.tasks("fresh") == []
    assert not store.has_settings("fresh")  # a read call leaves no row behind
    assert lazy_tools.TOOL_EFFECTS["lazy_preview"].kind == "read"
    assert lazy_tools.TOOL_EFFECTS["lazy_preview"].untrusted is False
    assert lazy_tools.TOOL_EFFECTS["lazy_get"].untrusted is True


def test_a_catalog_list_and_get_change_nothing(world):
    made = world.lazy.create_task(world.name, world.space, PAYLOAD)
    before = world.store.task(world.name, made.id).to_dict()
    import asyncio

    asyncio.run(call(lazy_tools.lazy_catalog, world.chat))
    asyncio.run(call(lazy_tools.lazy_list, world.chat))
    asyncio.run(call(lazy_tools.lazy_get, world.chat, task_id=made.id))
    assert world.store.task(world.name, made.id).to_dict() == before


# -- a series with a round in flight --------------------------------------------------------
def test_cancelling_a_series_stops_its_running_round_and_opens_no_next_one(world):
    made = world.lazy.create_task(world.name, world.space, {**PAYLOAD, "repeat_hours": 24})
    series = made.series_id
    account = world.store.settings(world.name).account
    world.lazy._running[(world.name, account)] = SimpleNamespace(task_id=series)

    cancelled = world.lazy.cancel_series(world.name, series)
    assert [task.id for task in cancelled] == [series]
    assert world.lazy._stops[series] == "Cancelled by you"  # the step is asked to stop
    stored = world.store.task(world.name, series)
    assert stored.status == "cancelled" and all(step.status != "pending" for step in stored.steps)
    # A cancelled round is finished work: the runner opens no round after it.
    assert [task for task in world.store.tasks(world.name, finished=False)] == []


def test_a_series_edit_while_a_round_is_in_flight_keeps_the_stop(world):
    made = world.lazy.create_task(world.name, world.space, {**PAYLOAD, "repeat_hours": 24})
    series = made.series_id
    account = world.store.settings(world.name).account
    world.lazy._running[(world.name, account)] = SimpleNamespace(task_id=series)

    out = world.chat.update(series, {"status": "paused"}, series=True)
    assert out["task"]["status"] == "paused"
    assert world.lazy._stops[series] == "Paused by you"


# -- the next round's chat is the project's too ---------------------------------------------
def test_a_repeat_rounds_chat_belongs_to_the_project(tmp_path):
    projects = ProjectStore(tmp_path / "projects.json")
    project, _ = projects.ensure("Launch")
    projects.add_notes(project.id, "Ship on Monday")
    space = StepSpace(projects)
    first = SimpleNamespace(
        id="lazy-1", title="Weekly report", user_id="u", brief="Write it", acceptance="", optional="",
        deadline=NOW, priority="normal", size="S", system="eng", agent="engineer", account=None,
        cap=None, repeat=24 * HOUR, series_id="lazy-1", project_id=project.id,
        created_at=NOW, updated_at=NOW, steps=[], history=[], conversation_id=None,
    )
    # The round the service makes when a repeating task ends (web_chat.lazy.service._next_round).
    store = LazyStore(tmp_path / "rounds.db")
    lazy = LazyAgents(store, SimpleNamespace(), quota=QuotaTelemetry(), runner=Runner(), clock=Clock())
    following = lazy._next_round(first, NOW)
    assert following.series_id == "lazy-1" and following.project_id == project.id
    assert following.request_id is None  # a round is not a retry of the caller's creation

    context_id = ensure_conversation(space, following)
    assert space.metadata[context_id]["project_id"] == project.id
    assert "Ship on Monday" in space.messages[0][2]


def test_concurrent_creations_with_one_request_id_make_one_task(world):
    import threading

    made = []
    errors = []

    def create():
        try:
            task, created = world.lazy.create_or_get(
                world.name, world.space, {**PAYLOAD, "request_id": "one-and-only"}
            )
            made.append((task.id, created))
        except Exception as exc:  # noqa: BLE001 - a failed thread must not hide
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert len({task_id for task_id, _ in made}) == 1
    assert sorted(created for _, created in made) == [False] * 5 + [True]
    assert len(world.store.tasks(world.name)) == 1


# -- the promises are the store's, not one object's: a second connection is a second process ---
def test_one_version_is_one_write_between_two_connections(tmp_path):
    """A second LazyStore on the same file is another process: its own lock, its own connection.

    The version check must still hold: SQLite's write transaction, not the
    object's threading.Lock (which no other process shares), decides the winner.
    """
    import threading

    world = world_for(tmp_path)
    made = world.lazy.create_task(world.name, world.space, PAYLOAD)
    version = made.version
    other = LazyStore(tmp_path / "u.db")
    try:
        barrier = threading.Barrier(6)
        outcomes = []

        def edit(store, title):
            barrier.wait()
            task = store.task(world.name, made.id)  # both read the same version
            task.title = title
            outcomes.append((store.save_task(task, expected_version=version), title))

        threads = [
            threading.Thread(target=edit, args=(world.store if index % 2 else other, f"Edit {index}"))
            for index in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        other.close()

    written = [title for ok, title in outcomes if ok]
    assert len(written) == 1, outcomes  # one transaction, one winner - even across connections
    stored = world.store.task(world.name, made.id)
    assert stored.title == written[0]
    assert stored.version == version + 1


def test_one_request_id_makes_one_task_between_two_connections(tmp_path):
    import threading

    world = world_for(tmp_path)
    other = LazyStore(tmp_path / "u.db")
    try:
        other_service = LazyAgents(other, SimpleNamespace(), quota=world.quota, runner=Runner(), clock=Clock())
        barrier = threading.Barrier(6)
        made = []
        errors = []

        def create(service):
            try:
                barrier.wait()
                task, created = service.create_or_get(
                    world.name, world.space, {**PAYLOAD, "request_id": "across-connections"}
                )
                made.append((task.id, created))
            except Exception as exc:  # noqa: BLE001 - a failed thread must not hide
                errors.append(exc)

        threads = [
            threading.Thread(target=create, args=(world.lazy if index % 2 else other_service,))
            for index in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        other.close()

    assert not errors, errors
    assert len({task_id for task_id, _ in made}) == 1
    assert sorted(created for _, created in made) == [False] * 5 + [True]
    assert len(world.store.tasks(world.name)) == 1
    assert world.store.task_by_request(world.name, "across-connections") is not None


def test_an_older_store_learns_the_version_and_the_key(tmp_path):
    """A lazy.db written before the columns existed keeps its versions and keys."""
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(str(path))
    db.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, finished INTEGER NOT NULL DEFAULT 0,"
        " updated_at REAL NOT NULL, data TEXT NOT NULL);"
    )
    data = {
        "id": "lazy-old", "user_id": "u", "title": "Old", "brief": "b", "version": 7,
        "request_id": "key-old", "status": "active", "steps": [], "history": [],
    }
    db.execute(
        "INSERT INTO tasks (id, user_id, finished, updated_at, data) VALUES (?, ?, ?, ?, ?)",
        ("lazy-old", "u", 0, NOW, json.dumps(data)),
    )
    db.commit()
    db.close()

    store = LazyStore(path)
    try:
        task = store.task("u", "lazy-old")
        assert task.version == 7
        assert store.task_by_request("u", "key-old").id == "lazy-old"
        task.note("edited")
        assert store.save_task(task, expected_version=7) is True
        assert store.task("u", "lazy-old").version == 8
        assert store.save_task(task, expected_version=7) is False
    finally:
        store.close()


# -- a project is where the work is: a move is refused, a gone project starts nothing ----------
def test_a_task_that_worked_cannot_be_moved_to_another_project(world):
    project, _ = world.projects.ensure("Launch")
    made = world.lazy.create_task(world.name, world.space, PAYLOAD)
    task = world.store.task(world.name, made.id)
    task.conversation_id = "ctx-old"  # the step's chat, made in the task's project
    world.store.save_task(task)

    with pytest.raises(LazyError) as refused:
        world.chat.update(made.id, {"project_id": project.id})
    assert refused.value.code == "project_locked"
    assert world.store.task(world.name, made.id).project_id is None  # nothing moved

    # detaching a task that worked in a project would leave that work behind too
    bound = world.lazy.create_task(world.name, world.space, {**PAYLOAD, "project_id": project.id})
    stored = world.store.task(world.name, bound.id)
    stored.conversation_id = "ctx-in-project"
    world.store.save_task(stored)
    with pytest.raises(LazyError) as refused:
        world.chat.update(bound.id, {"project_id": ""})
    assert refused.value.code == "project_locked"
    assert world.store.task(world.name, bound.id).project_id == project.id


def test_a_running_round_cannot_be_moved(world):
    project, _ = world.projects.ensure("Launch")
    made = world.lazy.create_task(world.name, world.space, PAYLOAD)
    account = world.store.settings(world.name).account
    world.lazy._running[(world.name, account)] = SimpleNamespace(task_id=made.id)

    with pytest.raises(LazyError) as refused:
        world.chat.update(made.id, {"project_id": project.id})
    assert refused.value.code == "project_locked"
    assert world.store.task(world.name, made.id).project_id is None


def test_a_task_whose_chat_is_gone_may_change_project(world):
    # The old chat was deleted: the next step makes a new one, in the new project.
    project, _ = world.projects.ensure("Launch")
    made = world.lazy.create_task(world.name, world.space, PAYLOAD)
    task = world.store.task(world.name, made.id)
    task.conversation_id = "ctx-gone"
    world.store.save_task(task)
    space = SimpleNamespace(
        project_store=world.projects,
        context_manager=lambda: SimpleNamespace(has_context=lambda context_id: False),
    )
    moved = world.lazy.update_task(world.name, made.id, {"project_id": project.id}, space=space)
    assert moved.project_id == project.id


class OneSpace:
    def __init__(self, space):
        self._space = space

    def use(self, user_id):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def leased():
            yield self._space

        return leased()


def test_a_step_does_not_start_when_its_project_is_gone(tmp_path):
    import asyncio

    space = StepSpace(ProjectStore(tmp_path / "projects.json"))
    task = SimpleNamespace(
        id="lazy-gone", user_id="u", title="Report", system="eng", agent="engineer",
        conversation_id=None, project_id="prj-gone",
    )
    outcome = asyncio.run(StepRunner(OneSpace(space)).run(task, SimpleNamespace(), Settings()))
    assert outcome.outcome == "blocked"
    assert "personal space" in outcome.reason
    assert space.started == []  # no chat, no work, nowhere to write


def test_a_step_does_not_start_when_its_project_is_archived(tmp_path):
    import asyncio

    projects = ProjectStore(tmp_path / "projects.json")
    project, _ = projects.ensure("Old")
    projects.update(project.id, archived=True)
    space = StepSpace(projects)
    task = SimpleNamespace(
        id="lazy-archived", user_id="u", title="Report", system="eng", agent="engineer",
        conversation_id=None, project_id=project.id,
    )
    assert project_problem(space, task) and "archived" in project_problem(space, task)
    outcome = asyncio.run(StepRunner(OneSpace(space)).run(task, SimpleNamespace(), Settings()))
    assert outcome.outcome == "blocked"
    assert space.started == []


def test_a_blocked_step_pauses_the_task_and_books_nothing(world):
    made = world.lazy.create_task(world.name, world.space, PAYLOAD)
    task = world.store.task(world.name, made.id)
    step = task.steps[0]
    step.status = "running"  # what _launch does before the runner looks at the project
    world.store.save_task(task)
    settings = world.store.settings(world.name)

    world.lazy.finish(
        world.name, made.id, step.id,
        StepOutcome("blocked", NOW, NOW, reason="Its project is gone: the work was not moved to your personal space."),
        settings,
    )

    stored = world.store.task(world.name, made.id)
    assert stored.status == "paused"
    assert stored.step(step.id).status == "pending"
    assert stored.step(step.id).tokens_used == 0
    assert "did not start" in stored.history[-1]["text"]

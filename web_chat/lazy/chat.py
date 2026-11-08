"""The user's lazy tasks, as a chat's agent may see and change them.

This is the trusted half of the lazy tools (``tools/lazy_tools.py``): the user
and the space are the host's - the object is made by the user's own space
(``web_chat.space.UserSpace._build_factory``), so a tool never names a user, a
path or a tenant, and a session without a user has no such object at all.

Everything here is a thin, checked layer over :class:`web_chat.lazy.service.LazyAgents`:
the planner, the store and the promises about a step are the service's own, never
copied. What this layer adds:

- project access: a task may belong to one of the user's projects, and only the
  ones their own project store holds (a chat of another user's project is not
  in it), so a name the model makes up is refused, not stored;
- a project is where the work is: a task that already worked in a chat of its
  project keeps that project (a move is refused with ``project_locked``, or the
  old work would be left behind), and a step does not start at all when the
  task's project was deleted or archived (``web_chat.lazy.runner``);
- compact DTOs: the agent reads a task, its steps and where it lands - not the
  whole page's picture with its windows, forecasts and chart;
- stable series: a repeating task's rounds share one ``series_id``, and pause,
  edit and cancel address that id, so "stop the daily report" stops the series
  rather than whichever round happens to be open;
- stale protection: a change may name the version it read (``Task.version``),
  and is refused when anything moved the task since.

Nothing here touches the user's allowances (a task's settings never change the
pool's shares). Nothing here waits for a step either: creation, edits and
cancellations only save and wake the service's loop, exactly as the page's API
does - and that woken loop may start the first due step at once. Only
``preview`` saves nothing, and only pausing or cancelling is guaranteed not to
start work.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from core.lazy.estimate import SIZES
from core.lazy.model import PRIORITIES, TASK_STATES, Task
from core.lazy.routine import zone
from core.lazy.windows import HOUR
from web_chat.lazy.accounts import LABELS, agent_choices
from web_chat.lazy.service import (
    EDITABLE,
    LazyAgents,
    LazyError,
    LazyProjectArchived,
    LazyProjectNotFound,
)

#: At most this many tasks in one list.
MAX_LIST = 50
#: History entries a single task's DTO carries.
HISTORY_TAIL = 5
#: How many other tasks a preview may report.
OTHERS_LIMIT = 5


def _at(epoch: Optional[float], tzname: str) -> Optional[dict]:
    """One moment both ways: epoch seconds and ISO-8601 with the zone's offset.

    Both forms are given on purpose: a machine compares epochs, a person (and a
    wall-clock schedule) reads the local time. ``timezone`` names the zone the
    ISO form is written in, so "04:00" is never mistaken for the server's 04:00.
    """
    if epoch is None:
        return None
    tz = zone(tzname)
    return {
        "epoch": round(float(epoch), 3),
        "iso": datetime.fromtimestamp(float(epoch), tz).isoformat(),
        "timezone": getattr(tz, "key", None) or tzname or "UTC",
    }


class LazyUnavailable(LazyError):
    """This session has no lazy tasks of a user (a standalone factory, evals, MCP)."""

    code = "unavailable"


class LazyChat:
    """One user's lazy tasks, for the agents of that user's chats."""

    def __init__(self, agents: LazyAgents, user_id: str, space: Any) -> None:
        self._agents = agents
        self._user_id = user_id
        self._space = space

    # -- what may be asked for ---------------------------------------------------------
    def catalog(self) -> dict:
        """What a task is made of here: the agents, the sizes, the limits."""
        settings = self._agents.store.settings(self._user_id)
        return {
            "subscription": settings.account,
            "subscription_label": LABELS.get(settings.account, settings.account),
            "agents": [
                {
                    "system": choice.system,
                    "agent": choice.agent,
                    "label": choice.label,
                    "system_label": choice.system_label,
                    "account": choice.account,
                    "problem": choice.problem,
                }
                for choice in agent_choices(self._space)
            ],
            "sizes": list(SIZES),
            "priorities": list(PRIORITIES),
            "states": list(TASK_STATES),
            "limits": {
                "title_chars": 160,
                "brief_chars": 8000,
                "repeat_hours_min": 1,
                "repeat_kinds": ["interval", "daily", "weekly"],
                "repeat_time": "HH:MM in the user's zone (or the zone named in the rule)",
                "cap_points": [0, 100],
                "list_max": MAX_LIST,
            },
            "fields": list(EDITABLE) + ["repeat_hours", "project_id"],
            "note": (
                "Work list-first: lazy_list, then lazy_get, then lazy_update or lazy_cancel. "
                "lazy_preview shows where a task would land without saving anything."
            ),
        }

    def list(self, *, include_finished: bool = False, project_id: str = "", limit: int = 20) -> dict:
        """The user's tasks, newest first - compactly: no steps, no plan."""
        project = self._project_or_error(project_id).id if str(project_id or "").strip() else None
        tasks = self._agents.store.tasks(self._user_id) if include_finished else self._agents.store.tasks(
            self._user_id, finished=False
        )
        if project is not None:
            tasks = [task for task in tasks if task.project_id == project]
        tasks = sorted(tasks, key=lambda task: task.created_at, reverse=True)
        size = max(1, min(MAX_LIST, int(limit or 20)))
        tz = self._tz()  # one settings read for the whole list
        return {
            "tasks": [self._brief(task, tz) for task in tasks[:size]],
            "count": len(tasks[:size]),
            "total": len(tasks),
            "truncated": len(tasks) > size,
        }

    def get(self, task_id: str) -> dict:
        """One task in full: its steps, its last history, where it lands."""
        task = self._task(task_id)
        placement = self._placement(task)
        if placement is not None:
            tz = self._tz()
            placement["times"] = {
                "scheduled_release": _at(placement.get("scheduled_release"), tz),
                "next_start": _at(placement.get("next_start"), tz),
                "finish_at": _at(placement.get("finish_at"), tz),
            }
        return {
            "task": self._detail(task),
            "project": self._project_brief(task.project_id),
            "placement": placement,
        }

    # -- changing ------------------------------------------------------------------------
    def preview(self, payload: dict) -> dict:
        """Where a task like this would land among the others - nothing is saved."""
        body = self._create_body(payload)
        self._project_or_error(body.get("project_id"))
        picture = self._agents.preview_new(self._user_id, self._space, body)
        picture["others"] = list(picture.get("others") or [])[:OTHERS_LIMIT]
        return picture

    def create(self, payload: dict, request_id: str = "") -> dict:
        """Make the task, once: the same ``request_id`` returns the one it made."""
        body = self._create_body(payload)
        project = self._project_or_error(body.get("project_id"))
        if project is not None:
            body["project_id"] = project.id
        body["request_id"] = str(request_id or "").strip()
        task, created = self._agents.create_or_get(self._user_id, self._space, body)
        out = {
            "task": self._brief(task),
            "created": created,
            "status": "created" if created else "exists",
            "version": task.version,
        }
        if not created:
            out["hint"] = (
                "A task with this request_id already exists and nothing was changed. "
                "Read it with lazy_get, or use a new request_id for another task."
            )
        return out

    def update(
        self,
        task_id: str,
        changes: Optional[dict] = None,
        *,
        expected_version: Optional[int] = None,
        series: bool = False,
    ) -> dict:
        """Change one round - or, with ``series``, the series' open round."""
        clean = self._changes(changes or {})
        if not clean:
            raise LazyError("Nothing to change: name at least one field.")
        if "project_id" in clean:
            project = self._project_or_error(clean["project_id"])
            clean["project_id"] = project.id if project is not None else ""
        if series:
            rounds = self._agents.update_series(
                self._user_id, task_id, clean, expected_version=expected_version, space=self._space
            )
        else:
            rounds = [self._agents.update_task(
                self._user_id, task_id, clean, expected_version=expected_version, space=self._space
            )]
        task = rounds[0]
        return {
            "task": self._brief(task),
            "changed": sorted(clean),
            "version": task.version,
            "series_id": task.series_id,
            "rounds": len(rounds),
        }

    def cancel(
        self, task_id: str, *, expected_version: Optional[int] = None, series: bool = False
    ) -> dict:
        """Stop one round - or, with ``series``, the series and every round after it."""
        if series:
            rounds = self._agents.cancel_series(self._user_id, task_id, expected_version=expected_version)
            if not rounds:
                return {
                    "cancelled": [],
                    "status": "finished",
                    "series_id": task_id,
                    "hint": "Every round of this series is already finished; nothing was cancelled.",
                }
            return {
                "cancelled": [self._brief(task) for task in rounds],
                "status": "cancelled",
                "series_id": rounds[0].series_id or rounds[0].id,
            }
        task = self._agents.cancel(self._user_id, task_id, expected_version=expected_version)
        return {
            "cancelled": [self._brief(task)],
            "status": task.status,
            "series_id": task.series_id,
        }

    # -- the caller's payload, checked ----------------------------------------------------
    def _create_body(self, payload: dict) -> dict:
        """Only the fields a task is made of, with the deadline as a moment."""
        body: Dict[str, Any] = {}
        for key in ("title", "brief", "acceptance", "optional", "priority", "size", "system", "agent"):
            value = payload.get(key)
            if value not in (None, ""):
                body[key] = value
        # A task can be made paused in the same save that makes it; preview reads
        # it too, and honestly shows a paused task with no step placed.
        if payload.get("status") not in (None, ""):
            body["status"] = payload["status"]
        if payload.get("project_id") not in (None, ""):
            body["project_id"] = payload["project_id"]
        if payload.get("repeat_hours") not in (None, ""):
            body["repeat_hours"] = payload["repeat_hours"]
        if payload.get("cap") not in (None, ""):
            body["cap"] = payload["cap"]
        if payload.get("deadline") not in (None, ""):
            body["deadline"] = self._moment(payload["deadline"])
        if payload.get("not_before") not in (None, ""):
            body["not_before"] = self._moment(payload["not_before"])
        if payload.get("repeat") not in (None, ""):
            body["repeat"] = payload["repeat"]
        return body

    def _changes(self, changes: dict) -> dict:
        """The known fields of *changes*, the deadline and repeat converted."""
        clean: Dict[str, Any] = {}
        unknown = [key for key in changes if key not in EDITABLE and key != "repeat_hours"]
        if unknown:
            raise LazyError(f"Unknown field(s): {', '.join(sorted(unknown))}.")
        for key, value in changes.items():
            if key in ("deadline", "not_before"):
                clean[key] = self._moment(value)
            elif key == "project_id":
                clean[key] = str(value or "").strip()
            else:
                clean[key] = value
        return clean

    def _moment(self, value: Any) -> Optional[float]:
        """A deadline as an epoch moment: seconds, or an ISO-8601 moment in the user's zone."""
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        text = str(value).strip()
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise LazyError(
                "The deadline is not a moment in time: give ISO-8601 (2026-11-10T18:00) or epoch seconds."
            ) from None
        if moment.tzinfo is None:
            settings = self._agents.store.settings(self._user_id)
            moment = moment.replace(tzinfo=zone(settings.routine.timezone))
        return moment.timestamp()

    # -- the user's projects -------------------------------------------------------------
    def _project_or_error(self, project_id: Any):
        clean = str(project_id or "").strip()
        if not clean:
            return None
        store = getattr(self._space, "project_store", None)
        if store is None:
            raise LazyUnavailable("Projects are not available in this session.")
        project = store.get(clean)
        if project is None:
            raise LazyProjectNotFound("Project not found: it is not one of your projects.")
        if project.archived:
            raise LazyProjectArchived("That project is archived: its work is not extended. Unarchive it first.")
        return project

    def _project_brief(self, project_id: Optional[str]) -> Optional[dict]:
        if not project_id:
            return None
        try:
            store = getattr(self._space, "project_store", None)
            project = store.get(project_id) if store is not None else None
        except Exception:  # noqa: BLE001 - a title is a convenience, never a failed read
            project = None
        if project is None:
            return {"id": project_id, "title": None, "missing": True}
        return {"id": project.id, "title": project.title, "archived": project.archived}

    # -- DTOs ----------------------------------------------------------------------------
    def _task(self, task_id: str) -> Task:
        task = self._agents.store.task(self._user_id, str(task_id or "").strip())
        if task is None:
            raise KeyError(task_id)
        return task

    def _tz(self) -> str:
        """The user's own zone: every wall time in a DTO is written in it."""
        return self._agents.store.settings(self._user_id).routine.timezone

    def _brief(self, task: Task, tz: Optional[str] = None) -> dict:
        done = sum(1 for step in task.steps if step.status == "done")
        tz = tz or self._tz()
        schedule = task.schedule
        return {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "priority": task.priority,
            "size": task.size,
            "project_id": task.project_id,
            "series_id": task.series_id,
            "repeat_hours": round(task.repeat / HOUR, 2) if task.repeat else None,
            # The typed rule, when there is one: interval, daily or weekly, with
            # its zone and its missed policy. ``repeat_hours`` stays for the
            # interval form alone.
            "schedule": schedule.to_dict() if schedule is not None else None,
            # The scheduled release: no step starts before it. Distinct from the
            # deadline (when it must be done) and from the placement's forecast
            # of where it will actually start.
            "not_before": task.not_before,
            "deadline": task.deadline,
            "steps": {"done": done, "total": len(task.steps)},
            "next_step": task.next_step.title if task.next_step else None,
            "updated_at": task.updated_at,
            "times": {
                "not_before": _at(task.not_before, tz),
                "deadline": _at(task.deadline, tz),
            },
            "version": task.version,
        }

    def _detail(self, task: Task) -> dict:
        data = self._brief(task)
        data.update({
            "brief": task.brief,
            "acceptance": task.acceptance,
            "optional": task.optional,
            "system": task.system,
            "agent": task.agent,
            "account": task.account,
            "account_label": LABELS.get(task.account) if task.account else None,
            "cap": task.cap,
            "run_now": task.run_now,
            "retry_at": task.retry_at,
            "retry_at_iso": (_at(task.retry_at, self._tz()) or {}).get("iso"),
            "conversation_id": task.conversation_id,
            "created_at": task.created_at,
            "steps_detail": [
                {
                    "id": step.id,
                    "kind": step.kind,
                    "title": step.title,
                    "status": step.status,
                    "optional": step.optional,
                    "tokens": step.tokens,
                    "minutes": step.minutes,
                    "resume": step.resume,
                    "reason": step.reason,
                    "summary": step.summary,
                }
                for step in task.steps
            ],
            "history": list(task.history[-HISTORY_TAIL:]),
        })
        return data

    def _placement(self, task: Task) -> Optional[dict]:
        """Where the task lands now, on its own subscription's plan."""
        settings = self._agents.store.settings(self._user_id)
        pool = self._agents.pool_settings(settings, task.account or settings.account)
        entry = self._agents.plan_for(self._user_id, pool).tasks.get(task.id)
        return entry.to_dict() if entry is not None else None

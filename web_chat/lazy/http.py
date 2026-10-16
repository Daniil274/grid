"""The /lazy page's API: the user's lazy tasks, their plan and the settings.

For a signed-in user (the ``api`` router); every answer that changes something
returns the whole state again, so the page never shows half an update:

    GET    /api/lazy?tz=<zone>               settings, windows now, plan, tasks, agents
    POST   /api/lazy/tasks                   a new task
    POST   /api/lazy/preview                 where a task like this would land, nothing saved
    PATCH  /api/lazy/tasks/{id}              edit it: deadline, priority, size, pause, ...
    POST   /api/lazy/tasks/{id}/preview      what an edit would do, nothing saved
    POST   /api/lazy/tasks/{id}/apply        take one of the plan's ways out
    POST   /api/lazy/tasks/{id}/cancel       stop it for good
    DELETE /api/lazy/tasks/{id}              forget it
    PUT    /api/lazy/settings                the share, the safety, the hours, the subscription
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from web_chat.lazy.service import LazyAgents, LazyError


def register_lazy_routes(api: APIRouter, current_user: Callable, current_space: Callable, lazy: LazyAgents) -> None:
    def state(user: Any, space: Any, timezone: Optional[str] = None) -> JSONResponse:
        return JSONResponse(lazy.state(user.id, space, timezone=timezone))

    def guarded(action: Callable[[], Any]) -> Any:
        try:
            return action()
        except LazyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except KeyError:
            raise HTTPException(status_code=404, detail="No such lazy task") from None

    @api.get("/api/lazy")
    async def lazy_state(tz: Optional[str] = None, user: Any = Depends(current_user), space: Any = Depends(current_space)) -> JSONResponse:
        return state(user, space, tz)

    @api.post("/api/lazy/tasks")
    async def create_task(body: dict, user: Any = Depends(current_user), space: Any = Depends(current_space)) -> JSONResponse:
        task = guarded(lambda: lazy.create_task(user.id, space, body))
        return JSONResponse({"created": task.id, **lazy.state(user.id, space)})

    @api.post("/api/lazy/preview")
    async def preview_new(body: dict, user: Any = Depends(current_user), space: Any = Depends(current_space)) -> JSONResponse:
        return JSONResponse(guarded(lambda: lazy.preview_new(user.id, space, body)))

    @api.patch("/api/lazy/tasks/{task_id}")
    async def update_task(task_id: str, body: dict, user: Any = Depends(current_user), space: Any = Depends(current_space)) -> JSONResponse:
        guarded(lambda: lazy.update_task(user.id, task_id, body))
        return state(user, space)

    @api.post("/api/lazy/tasks/{task_id}/preview")
    async def preview(task_id: str, body: dict, user: Any = Depends(current_user)) -> JSONResponse:
        return JSONResponse(guarded(lambda: lazy.preview(user.id, task_id, body.get("changes") or {})))

    @api.post("/api/lazy/tasks/{task_id}/apply")
    async def apply(task_id: str, body: dict, user: Any = Depends(current_user), space: Any = Depends(current_space)) -> JSONResponse:
        guarded(lambda: lazy.apply(user.id, task_id, body.get("change") or {}))
        return state(user, space)

    @api.post("/api/lazy/tasks/{task_id}/cancel")
    async def cancel(task_id: str, user: Any = Depends(current_user), space: Any = Depends(current_space)) -> JSONResponse:
        guarded(lambda: lazy.cancel(user.id, task_id))
        return state(user, space)

    @api.delete("/api/lazy/tasks/{task_id}")
    async def delete(task_id: str, user: Any = Depends(current_user), space: Any = Depends(current_space)) -> JSONResponse:
        guarded(lambda: lazy.delete(user.id, task_id))
        return state(user, space)

    @api.put("/api/lazy/settings")
    async def settings(body: dict, user: Any = Depends(current_user), space: Any = Depends(current_space)) -> JSONResponse:
        guarded(lambda: lazy.update_settings(user.id, body))
        return state(user, space)

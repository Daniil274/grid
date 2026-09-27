"""The personal agents API: a user lists, creates, edits and deletes their own agents.

    GET    /api/agents          the user's agents, and what they may build from
    POST   /api/agents          {name, description, system, template, model, tools, instructions, routable}
    PUT    /api/agents/{key}    the same fields
    DELETE /api/agents/{key}

Every route works in the asking user's space; the rules are
web_chat.personal_agents'. A created or edited agent is in the user's route
picker at the next bootstrap and runs from the next turn.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from web_chat.personal_agents import PersonalAgentError, PersonalAgentNotFound, PersonalAgentSpec
from web_chat.space import UserSpace


def catalog(space: UserSpace) -> dict[str, Any]:
    """The user's agents and the templates and models the policy offers them."""
    personal = space.personal_agents
    policy = space.deployment.personal_agents_policy
    enabled = personal is not None and policy.enabled
    registry = space.registry
    templates: list[dict[str, Any]] = []
    for system in registry.systems() if enabled else []:
        wanted = policy.templates.get(system.key, [])
        if not wanted:
            continue
        config = registry.config(system.key)
        models = config.config.models
        for key in wanted:
            agent = config.config.agents.get(key)
            if agent is None:
                continue
            tools = config.config.tools
            templates.append(
                {
                    "system": system.key,
                    "system_name": system.name,
                    "agent": key,
                    "name": agent.name or key,
                    "description": agent.description or "",
                    "model": agent.primary_model,
                    "models": [
                        {"key": model, "name": getattr(models.get(model), "name", model)}
                        for model in [agent.primary_model, *policy.models]
                        if model in models
                    ],
                    "tools": [
                        {"key": tool, "description": getattr(tools.get(tool), "description", "") or ""}
                        for tool in agent.tools
                    ],
                }
            )
    return {
        "enabled": enabled,
        "agents": [agent.model_dump() for agent in personal.list()] if personal is not None else [],
        "templates": templates,
        "limits": {"max_agents": policy.max_agents, "max_instructions_chars": policy.max_instructions_chars},
    }


def register_personal_agent_routes(router: APIRouter, current_space: Callable[..., Any]) -> None:
    def refused(exc: PersonalAgentError) -> HTTPException:
        code = status.HTTP_404_NOT_FOUND if isinstance(exc, PersonalAgentNotFound) else status.HTTP_400_BAD_REQUEST
        return HTTPException(status_code=code, detail=str(exc))

    @router.get("/api/agents")
    async def list_agents(space: UserSpace = Depends(current_space)) -> JSONResponse:
        return JSONResponse(catalog(space))

    @router.post("/api/agents")
    async def create_agent(spec: PersonalAgentSpec, space: UserSpace = Depends(current_space)) -> JSONResponse:
        try:
            agent = space.change_personal_agents(lambda agents: agents.create(spec, space.registry.config))
        except PersonalAgentError as exc:
            raise refused(exc) from None
        return JSONResponse(agent.model_dump(), status_code=status.HTTP_201_CREATED)

    @router.put("/api/agents/{key}")
    async def update_agent(key: str, spec: PersonalAgentSpec, space: UserSpace = Depends(current_space)) -> JSONResponse:
        try:
            agent = space.change_personal_agents(lambda agents: agents.update(key, spec, space.registry.config))
        except PersonalAgentError as exc:
            raise refused(exc) from None
        return JSONResponse(agent.model_dump())

    @router.delete("/api/agents/{key}")
    async def delete_agent(key: str, space: UserSpace = Depends(current_space)) -> JSONResponse:
        try:
            space.change_personal_agents(lambda agents: agents.delete(key))
        except PersonalAgentError as exc:
            raise refused(exc) from None
        return JSONResponse({"ok": True, "key": key})

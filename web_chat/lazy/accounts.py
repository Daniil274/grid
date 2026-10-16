"""Which subscription an agent runs on, and the windows of one that reports nothing.

An agent's models are tried in order and the first one the user may use runs
(core.agent_factory). That model's provider says whose quota a lazy step
spends: the host's Claude Code login, or the user's own ChatGPT plan. An agent
that would run on a pay-per-use API is not offered: lazy agents exist to work
within a subscription, never to bill one.

Claude Code reports its windows with every answer (core.quota_telemetry).
The ChatGPT plan reports nothing, so its use is counted from the tokens the
user's calls through it reported (web_chat.usage), against the capacity the
user declared - an estimate, and labeled as one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from core.lazy.model import Settings
from core.lazy.windows import WINDOWS
from core.quota_telemetry import CLAUDE_CODE, QuotaReading, WindowReading

CHATGPT = "chatgpt"
LABELS = {CLAUDE_CODE: "Claude Code subscription", CHATGPT: "ChatGPT plan"}
#: What a provider's ``auth`` means for the pool.
BY_AUTH = {"claude": CLAUDE_CODE, "chatgpt": CHATGPT}


def pool_key(account: str, user_id: str) -> str:
    """The ledger's name for an account: the host login is one, a ChatGPT plan is each user's."""
    return account if account == CLAUDE_CODE else f"{account}:{user_id}"


def reports_windows(account: str) -> bool:
    return account == CLAUDE_CODE


@dataclass(frozen=True)
class AgentChoice:
    system: str
    agent: str
    label: str
    system_label: str
    account: Optional[str]
    model: Optional[str]
    #: Why it cannot run lazy work, in words for the user; None when it can.
    problem: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "system": self.system, "agent": self.agent, "label": self.label, "system_label": self.system_label,
            "account": self.account, "model": self.model, "problem": self.problem,
        }


def agent_choice(space: Any, system: str, agent: str) -> AgentChoice:
    """The subscription *agent* of *system* would spend for this user, and its model."""
    registry = space.registry
    label = registry.agent_label(system, agent)
    system_label = next((info.name for info in registry.systems() if info.key == system), system)
    try:
        config = registry.config(system)
        agent_config = config.get_agent(agent)
        access = registry.factory(system).models.access
    except Exception as exc:  # noqa: BLE001 - a broken system is reported, not raised
        return AgentChoice(system, agent, label, system_label, None, None, f"Cannot be used: {exc}")
    problems: List[str] = []
    for key in agent_config.model_keys():
        try:
            access.check_model(config, key)
            provider = config.get_provider(config.get_model(key).provider)
        except Exception as exc:  # noqa: BLE001 - not in the plan, or misconfigured
            problems.append(str(exc))
            continue
        if not access.credentials.available(provider):
            problem = getattr(access.credentials, "problem", None)
            problems.append(problem(provider) if callable(problem) else f"No credential for {key}")
            continue
        account = BY_AUTH.get(getattr(provider, "auth", "api_key"))
        if account is None:
            return AgentChoice(
                system, agent, label, system_label, None, key,
                "Runs on a pay-per-use API, not a subscription: lazy work is not offered on it.",
            )
        return AgentChoice(system, agent, label, system_label, account, key)
    return AgentChoice(
        system, agent, label, system_label, None, None,
        problems[0] if problems else "None of its models is available to you.",
    )


def agent_choices(space: Any) -> List[AgentChoice]:
    """Every agent of every system the user can open, with the subscription it would spend."""
    choices: List[AgentChoice] = []
    for info in space.registry.systems():
        try:
            agents = space.registry.agents(info.key)
        except Exception:  # noqa: BLE001 - a broken system lists nothing
            continue
        for agent_key, agent in agents.items():
            if getattr(agent, "routable", True) is False:
                continue
            choices.append(agent_choice(space, info.key, agent_key))
    return choices


def estimated_reading(usage: Any, user_id: str, settings: Settings, now: float) -> QuotaReading:
    """A ChatGPT plan's windows from the tokens of the user's calls through it.

    Each window counts the tokens of its length back from now against the
    declared capacity; it frees up as the oldest of them age out, so it is
    taken to reset when the first call in it is one length old.
    """
    windows = {}
    for name, spec in WINDOWS.items():
        tokens, first = usage.tokens_since(user_id, CHATGPT, now - spec.length) if usage is not None else (0, None)
        capacity = max(1, int(settings.capacity_tokens.get(name, 1)))
        windows[name] = WindowReading(
            name=name,
            used=min(100.0, tokens / capacity * 100.0),
            resets_at=(first + spec.length) if first is not None else None,
        )
    return QuotaReading(account=pool_key(CHATGPT, user_id), windows=windows, observed_at=now, status="estimate")

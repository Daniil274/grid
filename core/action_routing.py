"""Where one call goes before any model is asked: run, judge, ask or refuse.

The policy gate (core.action_policy) routes every mediated call from facts it
can establish without a model. Those facts are what the tool declares it does
(utils.tool_effects, or the operator's ``tool_effects``), which paths its
arguments name, whether a shell command is on the operator's read-only list,
and what the run has already taken in. The user's filter (schemas.action_policy
ActionPolicyFilter) turns them into a route. Only a call routed to ``judge``
reaches the policy model.

Everything here is pure: the same call, filter and run facts always give the
same route, so the routing is tested and explained without a model.
"""

from __future__ import annotations

import fnmatch
import posixpath
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from schemas.action_policy import ROUTE_ORDER, ActionPolicyConfig, ActionPolicyFilter
from utils.tool_effects import (
    DELEGATE,
    DELEGATE_KIND,
    EGRESS,
    EXEC,
    EXTERNAL,
    READ,
    WRITE,
    Effect,
)

#: A tool that declares no effect, and that the operator did not describe.
UNKNOWN = "unknown"

#: Facts a run collects as its calls run (ActionRunState.flows).
SECRET = "secret"  # it read a secret
UNTRUSTED = "untrusted"  # it took in outside content

#: Shell operators that end one command and start the next.
_SEPARATORS = {"&&", "||", ";", "|", "&", "|&", ";;"}
#: Output redirections; only to the null device do they leave a command read-only.
_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
_NULL_DEVICES = {"/dev/null", "nul", "NUL"}
#: Constructs that run a command of their own inside another.
_SUBSTITUTIONS = ("`", "$(", "<(", ">(")


@dataclass(frozen=True)
class Routing:
    """How a call is routed, and why.

    ``effect`` is what the call does (an effect kind, or ``unknown``);
    ``flows`` are the facts it adds to its run once it has run.
    """

    effect: str
    route: str
    reasons: tuple[str, ...]
    flows: tuple[str, ...] = ()


def stricter(first: str, second: str) -> str:
    return max(first, second, key=ROUTE_ORDER.index)


def effect_of(policy: ActionPolicyConfig, tool: str, kind: str, declared: Optional[Effect]) -> Optional[Effect]:
    """What the call does: the tool's own declaration, else the operator's for
    its name, else delegation for an agent called as a tool."""
    if declared is not None:
        return declared
    for pattern, effect_kind in policy.tool_effects.items():
        if fnmatch.fnmatchcase(tool, pattern):
            return Effect(effect_kind)
    if kind == "agent":
        return DELEGATE
    return None


def matches(path: str, patterns: Iterable[str]) -> bool:
    """Whether workspace path *path* matches a glob: ``*`` also crosses
    directories, and a pattern without ``/`` matches the file name anywhere."""
    path = path.casefold()
    name = posixpath.basename(path)
    for pattern in patterns:
        pattern = pattern.casefold()
        if fnmatch.fnmatchcase(path, pattern):
            return True
        if "/" not in pattern and fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def _tokens(command: str) -> Optional[list[str]]:
    """The command split as a POSIX shell would, operators apart; None when it
    cannot be split (an unclosed quote)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _segments(tokens: list[str]) -> Optional[list[list[str]]]:
    """The commands of a command line, redirections to the null device removed.
    None when a redirection writes somewhere."""
    segments: list[list[str]] = [[]]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _SEPARATORS:
            segments.append([])
        elif token in _REDIRECTS:
            target = tokens[index + 1] if index + 1 < len(tokens) else ""
            if target not in _NULL_DEVICES:
                return None
            current = segments[-1]
            if current and current[-1].isdigit():
                current.pop()  # the descriptor of "2>/dev/null"
            index += 1
        elif token in (">&", "<&"):
            current = segments[-1]
            if current and current[-1].isdigit():
                current.pop()
            index += 1  # "2>&1" joins two outputs, writes nothing
        elif token == "<":
            index += 1  # reads a file into the command
        else:
            segments[-1].append(token)
        index += 1
    return [segment for segment in segments if segment]


def _entries(commands: Iterable[Any]) -> list[tuple[list[str], tuple[str, ...]]]:
    entries = []
    for command in commands:
        if isinstance(command, str):
            entries.append((command.split(), ()))
        else:
            for prefix, excluded in command.items():
                entries.append((prefix.split(), tuple(excluded)))
    return entries


def _excluded(token: str, option: str) -> bool:
    if len(option) == 2 and option[0] == "-" and option[1] != "-":
        # A short option, also inside a group such as "-ni".
        return token.startswith("-") and not token.startswith("--") and option[1] in token[1:]
    return token == option or token.startswith(option + "=")


def is_readonly(command: str, commands: Iterable[Any]) -> bool:
    """Whether every command of *command* is on the read-only list.

    Conservative: a substitution, a redirection that writes, an option the
    list excludes or a command that cannot be split makes it not read-only.
    """
    if not command.strip() or any(marker in command for marker in _SUBSTITUTIONS):
        return False
    tokens = _tokens(command)
    segments = _segments(tokens) if tokens is not None else None
    if not segments:
        return False
    entries = _entries(commands)
    for segment in segments:
        for prefix, excluded in entries:
            if segment[: len(prefix)] != prefix:
                continue
            if not any(_excluded(token, option) for token in segment[len(prefix):] for option in excluded):
                break
        else:
            return False
    return True


def command_paths(command: str) -> list[str]:
    """The words of *command* that may name files: everything but operators and options."""
    tokens = _tokens(command) or command.split()
    return [token for token in tokens if token not in _SEPARATORS | _REDIRECTS and not token.startswith("-")]


def _normalized(raw: str) -> str:
    return raw.replace("\\", "/").strip().strip("'\"")


def route_call(
    policy: ActionPolicyConfig,
    selected: ActionPolicyFilter,
    effect: Optional[Effect],
    arguments: Any,
    flows: Iterable[str],
    locate: Callable[[str], Optional[str]],
) -> Routing:
    """Route one call under the *selected* filter.

    ``arguments`` are the call's own; ``flows`` what its run has taken in so
    far; ``locate`` turns an argument path into a workspace path, or None
    when it is not one.
    """
    arguments = arguments if isinstance(arguments, dict) else {}
    flows = set(flows)
    kind = effect.kind if effect is not None else UNKNOWN
    reasons: list[str] = []
    produced: list[str] = [UNTRUSTED] if effect is not None and effect.untrusted else []

    raw_paths = [
        value for name in (effect.paths if effect is not None else ())
        if isinstance(value := arguments.get(name), str) and value.strip()
    ]
    command = arguments.get(effect.command) if effect is not None and effect.command else None
    if kind == EXEC and isinstance(command, str):
        if is_readonly(command, policy.readonly_commands):
            kind = READ
            reasons.append("read-only command")
        raw_paths.extend(command_paths(command))
    located = [(raw, locate(raw)) for raw in raw_paths]
    paths = [path if path is not None else _normalized(raw) for raw, path in located]
    # A read-only shell command can read outside the agent workspace. Treat
    # absolute/traversal paths the locator cannot contain as external effects;
    # this is policy routing, not a filesystem sandbox.
    if kind == READ and any(
        path is None
        and (posixpath.isabs(_normalized(raw)) or ".." in _normalized(raw).split("/"))
        for raw, path in located
    ):
        kind = EXTERNAL

    route = "allow" if kind == DELEGATE_KIND else getattr(selected, kind)
    reasons.insert(0, f"{kind}: {route}")

    secrets = [path for path in paths if matches(path, policy.secret_paths)]
    if secrets:
        route = stricter(route, selected.protected)
        reasons.append(f"secret: {secrets[0]}")
        if kind != WRITE:
            produced.append(SECRET)
    if kind not in (READ, DELEGATE_KIND):
        protected = [path for path in paths if matches(path, policy.protected_paths)]
        if protected:
            route = stricter(route, selected.protected)
            reasons.append(f"protected: {protected[0]}")
    if kind == READ and any(matches(path, policy.untrusted_paths) for path in paths):
        produced.append(UNTRUSTED)

    if selected.follow_flows:
        if UNTRUSTED in flows and kind in (WRITE, EXEC, EXTERNAL, UNKNOWN) and route == "allow":
            route = "judge"
            reasons.append("after outside content")
        if SECRET in flows and kind in (EGRESS, EXEC, EXTERNAL, UNKNOWN) and route == "allow":
            route = "judge"
            reasons.append("after a secret was read")
    return Routing(kind, route, tuple(reasons), tuple(dict.fromkeys(produced)))

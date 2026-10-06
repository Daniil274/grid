"""What a tool does, declared next to the tool - so the policy gate knows its effect.

The action policy (core.action_policy) decides most calls without asking a
model: reading the workspace, editing it, delegating to another agent. To do
that it must know what a call does, and it never guesses that from a tool's
name. A tool module declares it in a module-level ``TOOL_EFFECTS``, beside its
``TOOL_ISOLATION`` (utils.tool_isolation):

    TOOL_EFFECTS = {
        "file_read": read("filepath"),          # reads the workspace path in `filepath`
        "file_write": write("filepath"),        # changes it
        "bash_tool": run("command"),            # runs the shell command in `command`
        "web_fetch": egress(untrusted=True),    # reaches out; its result is outside content
        "orchestrate": DELEGATE,                # starts another agent, nothing more
    }

- :data:`READ`: looks at the workspace, the tracker or other own state; changes nothing.
- :data:`WRITE`: changes the workspace or the user's own stores - undone by
  editing again or from version control.
- :data:`EXEC`: runs code or commands; what they do is not known in advance.
- :data:`EGRESS`: sends a request out of the machine - a fetch, a search.
- :data:`EXTERNAL`: acts outside - publishes, pushes, sends, deploys, spends.
- :data:`DELEGATE`: starts or steers another agent; its own calls are judged.

A tool that declares nothing still works: the gate treats it as unknown and,
in the usual filters, asks the policy model about each call. Declaring is a
statement about the tool's code, so it belongs to whoever wrote and checked it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

READ = "read"
WRITE = "write"
EXEC = "exec"
EGRESS = "egress"
EXTERNAL = "external"
DELEGATE_KIND = "delegate"

#: Every kind of effect a tool can declare.
KINDS = (READ, WRITE, EXEC, EGRESS, EXTERNAL, DELEGATE_KIND)


@dataclass(frozen=True)
class Effect:
    """One tool's effect: its kind, and which arguments say where it acts.

    ``paths`` name the arguments that hold workspace paths, so the gate can
    tell a protected file (a secret, the policy itself) from any other.
    ``command`` names the argument that holds a shell command. ``untrusted``
    marks a tool whose result is content from outside - a web page, a search -
    that may carry instructions nobody here wrote.
    """

    kind: str
    paths: tuple[str, ...] = ()
    command: Optional[str] = None
    untrusted: bool = False

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"Unknown tool effect {self.kind!r}; one of {', '.join(KINDS)}")


def read(*paths: str, untrusted: bool = False) -> Effect:
    return Effect(READ, tuple(paths), untrusted=untrusted)


def write(*paths: str) -> Effect:
    return Effect(WRITE, tuple(paths))


def run(command: str, *paths: str) -> Effect:
    return Effect(EXEC, tuple(paths), command=command)


def egress(*paths: str, untrusted: bool = False) -> Effect:
    return Effect(EGRESS, tuple(paths), untrusted=untrusted)


def external(*paths: str) -> Effect:
    return Effect(EXTERNAL, tuple(paths))


EXEC_ANY = Effect(EXEC)
EXTERNAL_ANY = Effect(EXTERNAL)
DELEGATE = Effect(DELEGATE_KIND)

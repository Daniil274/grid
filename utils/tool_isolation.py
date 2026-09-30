"""Where a tool acts, declared next to the tool - and what an isolating space may give its agents.

On a server with accounts every user's agents run their commands in that
user's container, but the tools themselves run in the server process, on the
host. A tool is safe there only if it keeps to the user's side. A tool module
declares where each of its tools acts, in a module-level ``TOOL_ISOLATION``:

    TOOL_ISOLATION = {
        "bash_tool": CONTAINER,   # its commands run in the run's container
        "file_read": WORKSPACE,   # in the server, confined to the workspace
    }

- :data:`CONTAINER`: runs its commands in the run's container when there is one.
- :data:`WORKSPACE`: runs in the server process, but only inside the run's
  workspace, on the public internet, or on nothing of the machine at all.
- :data:`SYSTEMS`: runs in the server process on the web chat's created
  systems only (core.system_store) - the system builder's tools. The tools
  themselves use the store granted to the user: a private store for users,
  the shared store for admins (web_chat.space).
- anything else, and a tool that declares nothing: acts on the host machine -
  its desktop, its programs, the server's own files.

A space that must isolate its agents gives them only CONTAINER and WORKSPACE
tools (AgentFactory ``confine_tools``). An undeclared tool is withheld: a
new tool is safe for many users only once someone has checked it and said so.
"""

from __future__ import annotations

from typing import Optional

CONTAINER = "container"
WORKSPACE = "workspace"
SYSTEMS = "systems"

#: What an isolating space may give its agents.
CONFINED = frozenset({CONTAINER, WORKSPACE, SYSTEMS})


def is_confined(isolation: Optional[str]) -> bool:
    """Whether a tool declared *isolation* keeps to the user's side."""
    return isolation in CONFINED

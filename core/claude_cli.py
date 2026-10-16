"""The official Claude Code CLI as a model backend.

Grid runs a Claude subscription through the Claude Code CLI, never through a
key it stores or extracts itself. This module holds the parts of that bridge
that know nothing of the Agents SDK: an environment that cannot quietly switch
the CLI to an API key or a cloud account, the command line, and the
``stream-json`` protocol the CLI speaks in non-interactive mode.

Two rules shape it:

* **Auth stays with the CLI.** Grid reads no OAuth token and writes none. The
  CLI is launched with every variable that would bill an API key or a cloud
  provider removed, so only the login Claude Code already holds - the
  subscription - can pay. :meth:`ClaudeCodeRunner.require_subscription` reads
  that login back and is called before **every** launch, so a request cannot
  start on a bill the user never chose.
* **The protocol is spoken as documented.** ``claude -p --output-format
  stream-json --verbose`` prints one JSON object per line; :meth:`stream`
  yields them, and helpers read the text, the tool calls and the final result
  out of them. When Grid tools are in play the CLI is asked for a structured
  envelope with ``--json-schema`` instead of pretending it can emit Grid's own
  tool calls natively.

The CLI's built-in tools, its MCP servers and the host's settings hooks are
all disabled from the command line: this bridge is a model, not a second agent
with its own file access. The remaining honest limitation is that Claude Code
managed settings (deployed by an organization) cannot be switched off from
here; an operator who needs a hard guarantee must disable them for the host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Mapping, Optional, Sequence

from utils.exceptions import AgentError

logger = logging.getLogger("grid.claude_cli")

#: The name the CLI installs under; ``claude_binary`` overrides it.
DEFAULT_BINARY = "claude"

#: The login methods that spend a Claude plan. ``claude auth status`` names the
#: first; ``oauth_token`` is the long-lived token of the same plan login.
SUBSCRIPTION_AUTH_METHODS = frozenset({"claude.ai", "oauth_token"})

#: Login methods that bill somewhere else: an API key, a helper that mints one,
#: or a third-party/cloud account. A bridge that used them would not be the
#: subscription bridge this module exists to be.
API_KEY_AUTH_METHODS = frozenset({"api_key", "api_key_helper", "third_party"})

#: Variables that, left in the child environment, would let the CLI authorize
#: (and bill) an API key, a gateway, or a cloud account instead of the plan.
#: Removed before every launch so no call can silently leave the subscription.
CREDENTIAL_ENVIRONMENT: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_VERTEX_REGION",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_MICROSOFT_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_BEARER_TOKEN_BEDROCK",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GCLOUD_PROJECT",
    "CLOUD_ML_REGION",
    "AZURE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
)

#: Prefixes removed wholesale: every credential of a cloud provider, so a name
#: this list does not spell out cannot slip through either.
CREDENTIAL_ENVIRONMENT_PREFIXES: tuple[str, ...] = ("AWS_", "GOOGLE_", "AZURE_")

#: Seconds a CLI process is given to exit on ``terminate`` before it is killed.
TERMINATE_GRACE_SECONDS = 5.0

#: The internal tool Claude Code uses to satisfy ``--json-schema``: it emits an
#: ``assistant`` ``tool_use`` named ``StructuredOutput``. It is the only native
#: tool the model adapter accepts, and only on a turn that asked for a schema.
CLI_STRUCTURED_OUTPUT_TOOL = "StructuredOutput"

#: Flags that keep host ``user``/``project``/``local`` settings - hooks,
#: plugins, ``env`` overrides and an ``apiKeyHelper`` - from running. Passed to
#: generation *and* the auth-status probe, so no host helper can pay or run for
#: either. The empty value means "load none"; see docs/claude-subscription.md.
SETTING_SOURCES_FLAGS: tuple[str, ...] = ("--setting-sources", "")


class ClaudeCliError(AgentError):
    """The Claude Code bridge could not run, or ran outside the subscription."""


def claude_environment(environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """*environ* (the process environment by default) without credential variables.

    The result is what the CLI is launched with: PATH, HOME (where the login
    lives) and everything else stay; every variable that would authorize an API
    key, a gateway, or a cloud account is gone.
    """
    base = dict(os.environ if environ is None else environ)
    prefixes = CREDENTIAL_ENVIRONMENT_PREFIXES
    for name in list(base):
        if name in CREDENTIAL_ENVIRONMENT or name.startswith(prefixes):
            base.pop(name, None)
    return base


def auth_method(payload: Mapping[str, Any] | str) -> Optional[str]:
    """The ``authMethod`` of a ``claude auth status`` payload, or None if unset."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("authMethod")
    return value if isinstance(value, str) else None


def is_subscription(method: Optional[str]) -> bool:
    """Whether *method* spends a Claude plan rather than an API key or cloud account."""
    return method in SUBSCRIPTION_AUTH_METHODS


def require_subscription(method: Optional[str]) -> str:
    """Return *method* if it is a plan login; raise :class:`ClaudeCliError` otherwise.

    A missing login (``none``) and an API-key or cloud login are refused: the
    bridge must not run on a bill the user did not ask for.
    """
    if is_subscription(method):
        return method  # type: ignore[return-value]
    if method is None:
        raise ClaudeCliError(
            "The Claude Code CLI reported no authentication method. Run 'claude auth login' "
            "to sign in with your Claude subscription.",
            details={"authMethod": method},
        )
    if method in API_KEY_AUTH_METHODS:
        raise ClaudeCliError(
            f"The Claude Code CLI is signed in with '{method}', not a Claude subscription. "
            "Log out and sign in again with 'claude auth login' (choose the subscription, not "
            "an Anthropic Console API key). This bridge never runs on an API key or a cloud account.",
            details={"authMethod": method},
        )
    raise ClaudeCliError(
        f"Unsupported Claude Code authentication method '{method}'. Sign in with "
        "'claude auth login' to use your Claude subscription.",
        details={"authMethod": method},
    )


def parse_stream_line(line: str) -> Optional[Dict[str, Any]]:
    """One JSON object of the CLI's ``stream-json`` output, or None for a blank line.

    Raises :class:`ClaudeCliError` on a line that is not JSON: the protocol is
    line-delimited JSON, so anything else means the stream cannot be trusted.
    """
    text = line.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ClaudeCliError(
            "The Claude Code CLI printed a line that is not JSON; the stream-json "
            "protocol could not be read.",
            details={"line": text[:200]},
        ) from exc
    if not isinstance(payload, dict):
        raise ClaudeCliError(
            "The Claude Code CLI printed a stream-json line that is not an object.",
            details={"line": text[:200]},
        )
    return payload


@dataclass(frozen=True)
class ClaudeEvent:
    """One parsed line of the CLI's stream, in a shape the model reads."""

    kind: str
    subtype: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


def _event(payload: Dict[str, Any]) -> ClaudeEvent:
    return ClaudeEvent(
        kind=str(payload.get("type", "")),
        subtype=str(payload.get("subtype", "")),
        data=payload,
    )


def text_deltas(event: ClaudeEvent) -> List[str]:
    """Text fragments carried by *event* (partial ``text_delta`` or a whole block)."""
    if event.kind == "stream_event":
        inner = event.data.get("event") or {}
        if inner.get("type") == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                return [delta["text"]]
        return []
    if event.kind == "assistant":
        return [block["text"] for block in _content_blocks(event) if block.get("type") == "text" and isinstance(block.get("text"), str)]
    return []


def tool_uses(event: ClaudeEvent) -> List[Dict[str, Any]]:
    """Tool calls a complete ``assistant`` message announces.

    This bridge never surfaces them: with the CLI's own tools disabled a tool
    call here means something bypassed the command line, and it is refused
    rather than run a second time by Grid.
    """
    if event.kind != "assistant":
        return []
    return [block for block in _content_blocks(event) if block.get("type") == "tool_use"]


def final_result(event: ClaudeEvent) -> Optional[Dict[str, Any]]:
    """The ``result`` payload of a finished run, or None for any other event."""
    return event.data if event.kind == "result" else None


def structured_output(event: ClaudeEvent) -> Any:
    """The validated ``structured_output`` of a ``result`` event, or None."""
    result = final_result(event)
    if result is None:
        return None
    return result.get("structured_output")


def _content_blocks(event: ClaudeEvent) -> List[Dict[str, Any]]:
    message = event.data.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return []
    blocks = []
    for block in content:
        if isinstance(block, dict):
            blocks.append(block)
        elif hasattr(block, "model_dump"):
            blocks.append(block.model_dump())
    return blocks


Spawn = Callable[..., Any]


async def default_spawn(argv: Sequence[str], *, cwd: Optional[str], env: Mapping[str, str]) -> Any:
    """Launch the CLI, reading its stdout, writing the prompt on its stdin."""
    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=dict(env),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


class ClaudeCodeRunner:
    """One configuration of the Claude Code CLI, ready to answer a prompt.

    A runner is cheap and stateless between calls: each :meth:`stream` starts a
    fresh ``claude -p`` process. ``spawn`` is injectable so tests can drive the
    protocol without the binary present.

    Every launch is locked down: the CLI's built-in tools are disabled
    (``--tools ""``), MCP servers are ignored (``--strict-mcp-config``), and
    the host's user, project and local settings - hooks, plugins, ``env``
    overrides and key helpers - are not read (``--setting-sources ""``).
    """

    def __init__(
        self,
        *,
        binary: Optional[str] = None,
        working_directory: Optional[str] = None,
        environ: Optional[Mapping[str, str]] = None,
        model: Optional[str] = None,
        spawn: Optional[Spawn] = None,
    ) -> None:
        self.binary = binary or os.environ.get("GRID_CLAUDE_BINARY") or DEFAULT_BINARY
        self.working_directory = working_directory
        self._environ = environ
        self.model = model
        self._spawn = spawn or default_spawn

    def command(
        self,
        *,
        json_schema: Optional[Mapping[str, Any]] = None,
        partials: bool = False,
    ) -> List[str]:
        """The argv that answers one prompt, reading it from stdin.

        The prompt is never an argument; only the flags that lock the CLI down
        and the ``--json-schema`` (when a turn needs one) are.
        """
        argv: List[str] = [
            self.binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            # No built-in tools: this bridge is Grid's model, not a second agent.
            "--tools",
            "",
            # No MCP servers, whatever the host has configured.
            "--strict-mcp-config",
            # No user, project or local settings: no hooks, plugins, env
            # overrides or apiKeyHelper from the host can run or pay.
            *SETTING_SOURCES_FLAGS,
        ]
        if partials:
            argv.append("--include-partial-messages")
        if json_schema is not None:
            argv += [
                "--json-schema",
                json.dumps(json_schema, ensure_ascii=False, separators=(",", ":")),
            ]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def _env(self) -> Dict[str, str]:
        return claude_environment(self._environ)

    async def auth_status(self) -> Optional[str]:
        """The CLI's own ``authMethod``, or None when the CLI cannot be run.

        ``claude auth status`` prints JSON and exits 1 when signed out; a missing
        binary is reported as :class:`ClaudeCliError` so it is never mistaken for
        a signed-out user.

        The same host-settings lockdown generation gets is passed here too, so a
        host ``apiKeyHelper`` or settings ``env`` cannot run (or pay) merely to
        answer the status check.
        """
        process = await self._spawn(
            [self.binary, "auth", "status", *SETTING_SOURCES_FLAGS],
            cwd=self.working_directory,
            env=self._env(),
        )
        stdout, stderr = await _drain(process)
        payload: Any = stdout
        try:
            payload = json.loads(stdout) if stdout.strip() else None
        except ValueError:
            payload = None
        method = auth_method(payload) if payload is not None else None
        if method is None and process.returncode not in (0, 1, None):
            raise ClaudeCliError(
                f"Could not run the Claude Code CLI ('{self.binary} auth status'): {stderr.strip()[:400]}",
                details={"binary": self.binary, "returncode": process.returncode},
            )
        return method

    async def require_subscription(self) -> str:
        """The plan login the CLI holds; raise unless it is a subscription.

        Called before every :meth:`stream`, so no request is launched on an
        API key or a cloud account even if the host's login changed since the
        last one.
        """
        return require_subscription(await self.auth_status())

    async def stream(
        self,
        prompt: str,
        *,
        json_schema: Optional[Mapping[str, Any]] = None,
        partials: bool = False,
    ) -> AsyncIterator[ClaudeEvent]:
        """Run one prompt, yielding each parsed ``stream-json`` event as it arrives.

        The subscription login is checked first, then the CLI is launched with
        the credential-free environment and the prompt on stdin, so a large
        conversation is not limited by the command line. If the consumer stops
        early, the process is terminated rather than waited on forever.
        """
        if not self._available():
            raise ClaudeCliError(
                f"Claude Code CLI '{self.binary}' was not found on PATH. Install it and sign in "
                "with 'claude auth login' to use a Claude subscription.",
                details={"binary": self.binary},
            )
        await self.require_subscription()
        argv = self.command(json_schema=json_schema, partials=partials)
        process = await self._spawn(argv, cwd=self.working_directory, env=self._env())
        # Start draining stderr and arm the cleanup *before* touching stdin: a
        # write/drain that fails or is cancelled must still terminate and reap
        # the child, never leave it running.
        error_chunks: List[str] = []
        drain_stderr = asyncio.ensure_future(_read_stderr(process, error_chunks))
        returncode: Optional[int] = None
        try:
            if process.stdin is not None:
                process.stdin.write(prompt.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()
            async for raw in process.stdout:
                event = parse_stream_line(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
                if event is not None:
                    yield _event(event)
            # A normal stdout EOF means the turn is over: wait for the CLI's
            # real exit code instead of racing it with a terminate. Terminating
            # here used to kill a process that was about to exit 0 on its own.
            returncode = await process.wait()
        finally:
            if returncode is None:
                # Exceptional, early-close or cancelled path: the consumer is
                # gone, so make sure the child is too.
                returncode = await _terminate(process)
            await drain_stderr
        if returncode not in (0, None):
            detail = "".join(error_chunks).strip()[:600]
            raise ClaudeCliError(
                f"The Claude Code CLI exited with status {returncode}. {detail}",
                details={"binary": self.binary, "returncode": returncode},
            )

    def _available(self) -> bool:
        binary = self.binary
        if os.sep in binary or (os.altsep and os.altsep in binary):
            return os.path.exists(binary)
        return shutil.which(binary) is not None


async def _read_stderr(process: Any, sink: List[str]) -> None:
    stderr = getattr(process, "stderr", None)
    if stderr is None:
        return
    async for raw in stderr:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        sink.append(text)


async def _terminate(process: Any, grace: float = TERMINATE_GRACE_SECONDS) -> Optional[int]:
    """Make sure *process* is gone, returning its exit code.

    A process that already exited is returned as it is. A running one is asked
    to terminate, given *grace* seconds, then killed: a consumer that stops
    reading (a cancelled turn) must never block on ``wait()`` forever.
    """
    if getattr(process, "returncode", None) is not None:
        return process.returncode
    terminate = getattr(process, "terminate", None)
    if terminate is not None:
        try:
            terminate()
        except ProcessLookupError:
            pass
    try:
        return await asyncio.wait_for(process.wait(), grace)
    except asyncio.TimeoutError:
        kill = getattr(process, "kill", None)
        if kill is not None:
            try:
                kill()
            except ProcessLookupError:
                pass
        return await process.wait()


def _signal(process: Any, method: str) -> None:
    """Call ``process.terminate()``/``kill()`` if it exists, ignoring a gone child."""
    action = getattr(process, method, None)
    if action is None:
        return
    try:
        action()
    except ProcessLookupError:
        pass


async def _drain(process: Any) -> tuple[str, str]:
    """Wait out *process*, returning (stdout, stderr) as text.

    Once the process exits, its exit code is read from ``process.returncode``.
    ``communicate`` reads stdout and stderr concurrently and waits for exit, so
    a child that fills the pipe it is not being read from cannot deadlock the
    way sequential reads did. A cancelled wait terminates the child so it is
    not left orphaned.
    """
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        _signal(process, "terminate")
        raise
    return _text(stdout), _text(stderr)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)

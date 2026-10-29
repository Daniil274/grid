"""The Claude Code CLI as an Agents SDK model.

:class:`ClaudeCodeModel` is the one adapter between the two: it takes the
system instructions, the conversation and the tools the SDK hands it, runs the
official Claude Code CLI (core.claude_cli), and turns the CLI's ``stream-json``
back into the Responses events the SDK's runner consumes.

How the two talk, precisely:

* **Plain text.** With no tools and no structured output, the CLI streams text
  (``--include-partial-messages``) and its deltas become
  ``response.output_text.delta`` events. Partial deltas are what is shown; the
  completed ``assistant`` block is ignored once anything streamed, so text is
  never duplicated.
* **Tools.** The CLI cannot be asked for arbitrary Grid tool calls: with its
  own tools disabled it has no ``tool_use`` to emit. So when Grid tools or
  handoffs are in play, the model asks the CLI for a validated JSON envelope
  (``--json-schema``) of ``{"message": str, "tool_calls": [{"name": <enum>,
  "arguments": object}]}`` and translates ``result.structured_output`` into
  Responses ``function_call`` items. The envelope's ``name`` is an enum of
  exactly the Grid tools and handoffs this turn offers; every tool's parameter
  schema is written into the prompt so the model knows how to fill it.
* **Structured output.** When the SDK asks for an output schema, it becomes
  the CLI's ``--json-schema`` (or the envelope's ``final`` field when tools
  are present too), and the validated JSON is emitted as the model's text for
  the SDK to parse.
* **History.** Prior ``function_call`` and ``function_call_output`` items are
  rendered into the prompt, so a tool's result reaches the next turn instead
  of being dropped.
* **System prompt.** Grid's system instructions and the tool instructions
  replace Claude Code's own system prompt (``--system-prompt-file``), behind
  a preamble that names what the CLI still attaches about its host.
* **Thinking.** The CLI's thinking summaries become ``reasoning_content``
  deltas, which the SDK turns into the reasoning-summary events a Responses
  model streams; the model's ``reasoning.effort`` becomes ``--effort``.

* **Images.** An image part of the conversation (a user's attachment, a
  screenshot a tool returned) becomes an ``[image N]`` mark in the prompt and
  a Messages API ``image`` block after it; the turn then goes to the CLI as one
  ``stream-json`` user message. Grid's image window (core.image_window) has
  already bounded how many go with one request.

The CLI's own login (``claude auth login``) is the only authorization. It is
checked before **every** launch (core.claude_cli.require_subscription), and
the CLI runs with no built-in tools, no MCP tools (``--disallowedTools
"mcp__*"``) and no host settings; if its ``init`` event still announces tools,
the turn is refused before generation. Input the
CLI cannot take - audio, files, hosted tools - is refused with an explicit
error rather than silently dropped. The one limit that cannot be
closed from here is organization-managed Claude Code settings, which override
anything Grid passes; see docs/claude-subscription.md.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Sequence

from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import ModelResponse, TResponseStreamEvent
from agents.models.chatcmpl_converter import Converter
from agents.models.chatcmpl_stream_handler import ChatCmplStreamHandler
from agents.models.fake_id import FAKE_RESPONSES_ID
from agents.models.interface import Model, ModelTracing
from agents.tool import FunctionTool, Tool
from agents.tracing import generation_span
from agents.usage import Usage
from openai.types.chat import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import (
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.completion_usage import CompletionUsage
from openai.types.responses import Response
from openai.types.responses.response_prompt_param import ResponsePromptParam

from core.claude_cli import (
    CLI_STRUCTURED_OUTPUT_TOOL,
    ClaudeCliError,
    ClaudeCodeRunner,
    cli_effort,
    final_result,
    init_tools,
    ran_nothing,
    structured_output,
    text_deltas,
    block_stop,
    thinking_deltas,
    tool_input_delta,
    tool_input_start,
    tool_results,
    tool_uses,
)

logger = logging.getLogger("grid.claude_cli")

#: Leads every system prompt. The CLI adds context of its own around Grid's -
#: the signed-in account's email, its working directory, the platform - which
#: describes the host, not necessarily the person in the chat.
BRIDGE_PREAMBLE = (
    "You are the model behind a Grid agent. The Claude Code CLI only carries this "
    "conversation: its built-in tools, MCP servers, skills and settings are off, and you "
    "have no tools of your own.\n"
    "The CLI also attaches context about the machine and the account that run it - a "
    "\"userEmail\" block, a working directory, the platform. None of it is about the person "
    "in this chat. The email belongs to the account that runs the CLI: never write it out, "
    "and if you are asked for the user's email, say you do not know it. That directory is "
    "not their workspace either: their files are reached only through the Grid tools you "
    "are given."
)


def _usage_from_result(result: Dict[str, Any]) -> Optional[CompletionUsage]:
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    return CompletionUsage(
        prompt_tokens=prompt + cache_read + cache_creation,
        completion_tokens=completion,
        total_tokens=prompt + cache_read + cache_creation + completion,
    )


def _chunk(
    *,
    model: str,
    content: Optional[str] = None,
    tool_call: Optional[ChoiceDeltaToolCall] = None,
    finish_reason: Optional[str] = None,
    usage: Optional[CompletionUsage] = None,
    reasoning: Optional[str] = None,
) -> ChatCompletionChunk:
    delta = ChoiceDelta(content=content, tool_calls=[tool_call] if tool_call else None)
    if reasoning is not None:
        # The field the SDK's stream handler reads as a reasoning summary.
        delta.reasoning_content = reasoning  # type: ignore[attr-defined]
    return ChatCompletionChunk(
        id=FAKE_RESPONSES_ID,
        object="chat.completion.chunk",
        created=int(time.time()),
        model=model,
        choices=[Choice(index=0, delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


@dataclass(frozen=True)
class _ToolDef:
    """One Grid tool or handoff the model may ask for this turn."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class _Turn:
    """What the CLI is asked for, decided before it is launched."""

    prompt: str
    #: What replaces the CLI's own system prompt.
    system: str = BRIDGE_PREAMBLE
    #: The ``--json-schema`` the CLI validates its answer against, or None.
    json_schema: Optional[Dict[str, Any]] = None
    #: Whether partial text messages are wanted (plain streaming only).
    partials: bool = False
    #: How to read the answered ``result`` event.
    mode: str = "text"  # "text" | "structured" | "tools"
    output_schema: Optional[AgentOutputSchemaBase] = None
    #: ``--effort``, from the model settings' reasoning effort.
    effort: Optional[str] = None
    #: Messages API ``image`` blocks the prompt's ``[image N]`` marks stand for.
    images: tuple = ()


#: The image types the Messages API reads from base64 data.
IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


def _image_block(part: Mapping[str, Any], *, where: str) -> Dict[str, Any]:
    """A Messages API ``image`` block of an ``input_image``/``image_url`` part."""
    url = part.get("image_url")
    if isinstance(url, Mapping):
        url = url.get("url")
    if isinstance(url, str) and url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type = header[len("data:"):].split(";")[0].strip().lower()
        if ";base64" in header and media_type in IMAGE_MEDIA_TYPES and data:
            return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
        raise ClaudeCliError(
            f"The Claude Code bridge cannot send a '{media_type or 'unknown'}' image ({where}); it "
            f"takes base64 {', '.join(IMAGE_MEDIA_TYPES)}.",
            details={"media_type": media_type},
        )
    if isinstance(url, str) and url.startswith(("https://", "http://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    raise ClaudeCliError(
        f"The Claude Code bridge cannot send this image ({where}): it has neither image data nor "
        "an http(s) URL (a file_id is not supported).",
        details={"content_type": part.get("type")},
    )


def _text_content(content: Any, *, where: str, images: Optional[List[Dict[str, Any]]] = None) -> str:
    """A message's content as plain text, refusing anything the CLI cannot take.

    With *images*, each image part is appended to it as an ``image`` block and
    marked ``[image N]`` in the text; without, an image is refused too.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: List[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            parts.append(str(part))
            continue
        kind = part.get("type")
        if kind in ("text", "input_text", "output_text", "refusal", "summary_text"):
            value = part.get("text")
            if value is not None:
                parts.append(str(value))
        elif kind in ("input_image", "image_url") and images is not None:
            images.append(_image_block(part, where=where))
            parts.append(f"[image {len(images)}]")
        elif kind in ("input_image", "image_url", "input_file", "input_audio"):
            raise ClaudeCliError(
                f"The Claude Code bridge cannot send {kind} content ({where}). The CLI takes text "
                "only; remove the attachment or use a vision-capable provider for this turn.",
                details={"content_type": kind},
            )
        elif kind is not None:
            raise ClaudeCliError(
                f"The Claude Code bridge does not support '{kind}' content ({where}); it takes "
                "text only.",
                details={"content_type": kind},
            )
        else:
            parts.append(json.dumps(part, ensure_ascii=False))
    return "\n".join(parts)


def _as_dict(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        try:
            dumped = item.model_dump()
            return dumped if isinstance(dumped, dict) else None
        except Exception:  # noqa: BLE001 - a shape this bridge cannot read
            return None
    return None


def _render_history(input: Any, images: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """The conversation as prompt lines, keeping prior tool calls and results.

    Image parts go to *images* (see :func:`_text_content`) when it is given.
    """
    items: Sequence[Any]
    if isinstance(input, str):
        items = [{"role": "user", "content": input}]
    elif isinstance(input, (list, tuple)):
        items = input
    else:
        items = [{"role": "user", "content": input}]

    lines: List[str] = []
    for item in items:
        if isinstance(item, str):
            lines.append(f"user:\n{item}")
            continue
        data = _as_dict(item)
        if data is None:
            continue
        kind = data.get("type")
        if kind == "function_call":
            name = data.get("name", "")
            call_id = data.get("call_id") or data.get("id") or ""
            arguments = data.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
            lines.append(f"assistant tool_call {name} (call_id={call_id}): {arguments}")
            continue
        if kind == "function_call_output":
            call_id = data.get("call_id") or data.get("id") or ""
            output = data.get("output")
            rendered = (
                _text_content(output, where=f"tool result {call_id}", images=images)
                if isinstance(output, list)
                else (output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))
            )
            lines.append(f"tool_result (call_id={call_id}):\n{rendered}")
            continue
        if kind in ("reasoning", "item_reference"):
            continue  # model-internal, nothing for the CLI to read
        role = data.get("role")
        if role is None:
            continue
        content = _text_content(data.get("content"), where=f"{role} message", images=images)
        lines.append(f"{role}:\n{content}")
    return lines


def _tool_defs(tools: Sequence[Tool], handoffs: Sequence[Handoff[Any, Any]]) -> List[_ToolDef]:
    """The Grid tools and handoffs this turn offers, or an explicit refusal."""
    defs: List[_ToolDef] = []
    for tool in tools or ():
        if isinstance(tool, FunctionTool):
            schema = Converter.tool_to_openai(tool)["function"]
            defs.append(_ToolDef(schema["name"], schema.get("description") or "", schema.get("parameters") or {}))
        else:
            raise ClaudeCliError(
                f"The Claude Code bridge supports function tools only; '{type(tool).__name__}' is "
                "a hosted tool the CLI cannot be told about. Remove it from the agent or use a "
                "provider that supports hosted tools.",
                details={"tool_type": type(tool).__name__},
            )
    for handoff in handoffs or ():
        defs.append(
            _ToolDef(
                handoff.tool_name,
                handoff.tool_description or "",
                handoff.input_json_schema or {},
            )
        )
    return defs


def _envelope_schema(defs: Sequence[_ToolDef], output_schema: Optional[AgentOutputSchemaBase]) -> Dict[str, Any]:
    """The JSON Schema the CLI validates the tool envelope against."""
    properties: Dict[str, Any] = {
        "message": {
            "type": "string",
            "description": "What to show the user. May be empty when calling tools.",
        },
        "tool_calls": {
            "type": "array",
            "description": (
                "Grid tools to call. Empty when answering without tools. Each arguments object "
                "must match that tool's schema described in the prompt."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string", "enum": [d.name for d in defs]},
                    "arguments": {"type": "object"},
                },
                "required": ["name", "arguments"],
            },
        },
    }
    required = ["message", "tool_calls"]
    if output_schema is not None:
        properties["final"] = dict(output_schema.json_schema())
        properties["final"]["description"] = (
            "The final answer once no more tools are needed, matching the required schema."
        )
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def _tool_instructions(defs: Sequence[_ToolDef], output_schema: Optional[AgentOutputSchemaBase]) -> str:
    lines = [
        "To use a Grid tool, reply with the required JSON object and put the call in "
        '"tool_calls" (leave "message" empty if you have nothing to say yet). To answer '
        'without tools, set "tool_calls" to []. Grid tools cannot be called by name: a direct '
        'call fails with "No such tool available".',
    ]
    if output_schema is not None:
        lines.append(
            "When you are done and need no more tools, put the final answer in the "
            '"final" field, matching the schema given to the CLI.'
        )
    if defs:
        lines.append("Available Grid tools:")
        for tool in defs:
            lines.append(f"- {tool.name}: {tool.description}".rstrip())
            lines.append(f"  arguments schema: {json.dumps(tool.parameters, ensure_ascii=False)}")
    return "\n".join(lines)


#: How far back a partial string may end inside an escape (a torn surrogate pair).
_MAX_PARTIAL_ESCAPE = 11

_JSON_STRING = json.JSONDecoder(strict=False)


def _decode_prefix(raw: str) -> Optional[str]:
    """The text of a JSON string body cut mid-stream, without a torn escape.

    A trailing backslash, an incomplete unicode escape or a high surrogate
    still waiting for its pair is held back until the next fragment completes it.
    """
    for cut in range(min(len(raw), _MAX_PARTIAL_ESCAPE) + 1):
        try:
            text = _JSON_STRING.decode('"' + raw[: len(raw) - cut] + '"')
        except ValueError:
            continue
        if text and "\ud800" <= text[-1] <= "\udbff":
            continue
        return text
    return None


class _MessageField:
    """Reads the top-level ``"message"`` string of a JSON object as it arrives.

    The CLI writes a tool turn's envelope as the input of its StructuredOutput
    tool, fragment by fragment (``input_json_delta``). Fed those fragments in
    order, :meth:`feed` returns the message text decoded so far whenever it
    grows; keys of nested objects (a tool's arguments) are never mistaken for it.
    """

    KEY = "message"

    def __init__(self) -> None:
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._expect_key = False
        self._key: Optional[List[str]] = None
        self._last_key: Optional[str] = None
        self._await_value = False
        self._raw: Optional[List[str]] = None
        self.done = False

    def feed(self, fragment: str) -> Optional[str]:
        if self.done:
            return None
        before = len(self._raw) if self._raw is not None else -1
        for ch in fragment:
            if self._raw is not None:
                if self._escape:
                    self._escape = False
                elif ch == "\\":
                    self._escape = True
                elif ch == '"':
                    self.done = True
                    break
                self._raw.append(ch)
                continue
            if self._in_string:
                if self._escape:
                    self._escape = False
                elif ch == "\\":
                    self._escape = True
                elif ch == '"':
                    self._in_string = False
                    if self._key is not None:
                        self._last_key, self._key = "".join(self._key), None
                    continue
                if self._key is not None:
                    self._key.append(ch)
                continue
            if ch.isspace():
                continue
            if self._await_value:
                self._await_value = False
                if ch == '"':
                    self._raw = []
                    continue
            if ch == '"':
                self._in_string = True
                if self._depth == 1 and self._expect_key:
                    self._key, self._expect_key = [], False
            elif ch in "{[":
                self._depth += 1
                self._expect_key = ch == "{" and self._depth == 1
            elif ch in "}]":
                self._depth -= 1
            elif ch == "," and self._depth == 1:
                self._expect_key = True
            elif ch == ":" and self._depth == 1 and self._last_key == self.KEY:
                self._await_value = True
        if self._raw is None or (len(self._raw) == before and not self.done):
            return None
        return _decode_prefix("".join(self._raw))


class ClaudeCodeModel(Model):
    """An Agents SDK model that answers through the official Claude Code CLI."""

    def __init__(self, runner: ClaudeCodeRunner, model_name: str) -> None:
        self._runner = runner
        self.model = model_name
        #: The prompt of the last call, for logs and tests.
        self.last_prompt: Optional[str] = None

    # -- prompt -----------------------------------------------------------------
    def build_prompt(self, input: Any, images: Optional[List[Dict[str, Any]]] = None) -> str:
        """Render the conversation as the single prompt the CLI reads on stdin.

        Its images go to *images* when it is given; without, an image is refused.
        """
        return "\n\n".join(_render_history(input, images))

    def build_system(
        self,
        system_instructions: Optional[str],
        tools: Sequence[Tool] = (),
        handoffs: Sequence[Handoff[Any, Any]] = (),
        output_schema: Optional[AgentOutputSchemaBase] = None,
    ) -> str:
        """The system prompt that replaces the CLI's: preamble, Grid's, tool instructions.

        Tool instructions sit here rather than after the conversation: they are
        the same every turn, so they stay in the cached prefix.
        """
        parts = [BRIDGE_PREAMBLE]
        if system_instructions:
            parts.append(system_instructions)
        defs = _tool_defs(tools, handoffs)
        if defs or output_schema is not None:
            parts.append(_tool_instructions(defs, output_schema))
        return "\n\n".join(parts)

    def _plan(
        self,
        system_instructions: Optional[str],
        input: Any,
        tools: Sequence[Tool],
        handoffs: Sequence[Handoff[Any, Any]],
        output_schema: Optional[AgentOutputSchemaBase],
        model_settings: Any = None,
    ) -> _Turn:
        defs = _tool_defs(tools, handoffs)
        structured = output_schema is not None and not output_schema.is_plain_text()
        images: List[Dict[str, Any]] = []
        prompt = self.build_prompt(input, images)
        system = self.build_system(system_instructions, tools, handoffs, output_schema if structured else None)
        reasoning = getattr(model_settings, "reasoning", None)
        effort = cli_effort(getattr(reasoning, "effort", None))
        # Partial messages in every mode: they carry the thinking summaries,
        # which stream whether the answer is text, a schema or tool calls.
        if defs:
            return _Turn(
                prompt=prompt,
                system=system,
                json_schema=_envelope_schema(defs, output_schema if structured else None),
                partials=True,
                mode="tools",
                output_schema=output_schema if structured else None,
                effort=effort,
                images=tuple(images),
            )
        if structured:
            return _Turn(
                prompt=prompt,
                system=system,
                json_schema=dict(output_schema.json_schema()),  # type: ignore[union-attr]
                partials=True,
                mode="structured",
                output_schema=output_schema,
                effort=effort,
                images=tuple(images),
            )
        return _Turn(
            prompt=prompt, system=system, partials=True, mode="text", effort=effort, images=tuple(images)
        )

    # -- Model interface --------------------------------------------------------
    async def get_response(
        self,
        system_instructions: Optional[str],
        input: Any,
        model_settings: Any,
        tools: List[Tool],
        output_schema: Optional[AgentOutputSchemaBase],
        handoffs: List[Handoff],
        tracing: ModelTracing,
        previous_response_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        prompt: Optional[ResponsePromptParam] = None,
    ) -> ModelResponse:
        """One CLI turn, collected into a single :class:`ModelResponse`."""
        final: Optional[Response] = None
        async for event in self.stream_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        ):
            if getattr(event, "type", "") == "response.completed":
                final = event.response  # type: ignore[attr-defined]
        if final is None:
            return ModelResponse(output=[], usage=Usage(), response_id=None)
        usage = final.usage
        return ModelResponse(
            output=list(final.output),
            usage=Usage(
                requests=1,
                input_tokens=getattr(usage, "input_tokens", 0) or 0,
                output_tokens=getattr(usage, "output_tokens", 0) or 0,
                total_tokens=getattr(usage, "total_tokens", 0) or 0,
            ),
            response_id=None,
        )

    async def stream_response(
        self,
        system_instructions: Optional[str],
        input: Any,
        model_settings: Any,
        tools: List[Tool],
        output_schema: Optional[AgentOutputSchemaBase],
        handoffs: List[Handoff],
        tracing: ModelTracing,
        previous_response_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        prompt: Optional[ResponsePromptParam] = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """Run one CLI turn, yielding the Responses stream events as they arrive."""
        turn = self._plan(system_instructions, input, tools or (), handoffs or (), output_schema, model_settings)
        self.last_prompt = turn.prompt
        with generation_span(
            model=str(self.model),
            model_config={"base_url": "claude-code-cli"},
            disabled=tracing.is_disabled(),
        ) as span_generation:
            response = Response(
                id=FAKE_RESPONSES_ID,
                created_at=time.time(),
                model=self.model,
                object="response",
                output=[],
                tool_choice="auto",
                top_p=None,
                temperature=None,
                tools=[],
                parallel_tool_calls=False,
                reasoning=None,
            )
            stream = self._chat_chunks(turn)
            async for event in ChatCmplStreamHandler.handle_stream(response, stream, model=self.model):
                yield event
                if getattr(event, "type", "") == "response.completed" and tracing.include_data():
                    span_generation.span_data.output = [event.response.model_dump()]

    # -- translation ------------------------------------------------------------
    async def _chat_chunks(self, turn: _Turn) -> AsyncIterator[ChatCompletionChunk]:
        """The CLI's events as synthetic Chat Completions chunks the SDK reads."""
        usage: Optional[CompletionUsage] = None
        finish_reason = "stop"
        tool_index = 0
        seen_text = False
        schema_fallback: Any = None
        # Native calls announced but not yet answered, by tool_use id. A call
        # the CLI never answered with a tool_result did not run.
        unanswered: Dict[str, str] = {}
        # Tool turns: the envelope's "message" streams while the CLI writes the
        # StructuredOutput input, instead of arriving with the result. Not with
        # a structured "final" answer: its JSON must stay the only text.
        stream_message = turn.mode == "tools" and turn.partials and turn.output_schema is None
        envelope_index: Optional[int] = None
        envelope: Optional[_MessageField] = None
        streamed = ""
        diverged = False
        async for event in self._runner.stream(
            turn.prompt,
            json_schema=turn.json_schema,
            partials=turn.partials,
            system_prompt=turn.system,
            effort=turn.effort,
            images=turn.images,
        ):
            announced = init_tools(event)
            if announced is not None:
                # Preflight: at init the CLI says which tools it holds, before
                # generation. Only the internal StructuredOutput tool that
                # ``--json-schema`` needs is expected there.
                expected = {CLI_STRUCTURED_OUTPUT_TOOL} if turn.json_schema is not None else set()
                extra = [name for name in announced if name not in expected]
                if extra:
                    raise ClaudeCliError(
                        "The Claude Code CLI holds tools this bridge did not give it "
                        f"({', '.join(extra)}), but its built-in tools and MCP tools are "
                        "disabled for the command line. Refusing the turn before generation: "
                        "organization-managed Claude Code settings may be adding them.",
                        details={"tools": extra},
                    )
            for call in tool_uses(event):
                name = str(call.get("name"))
                if turn.json_schema is not None and name == CLI_STRUCTURED_OUTPUT_TOOL:
                    # Claude Code implements ``--json-schema`` as this internal
                    # tool; its input is kept in case the result omits the value.
                    schema_fallback = call.get("input")
                    continue
                # The model sometimes calls a tool by name instead of putting it
                # in the envelope. That is only a proposal until the CLI answers
                # it with a tool_result carrying the same tool_use id.
                call_id = str(call.get("id") or "")
                if call_id:
                    unanswered.setdefault(call_id, name)
            for result in tool_results(event):
                answered = unanswered.pop(str(result.get("tool_use_id") or ""), None)
                if answered is None or ran_nothing(result):
                    # No such call, or one the CLI held no tool for: it answered
                    # "No such tool available" and ran nothing.
                    continue
                raise ClaudeCliError(
                    f"The Claude Code CLI ran a tool it was not given ({answered}). Built-in "
                    "tools and MCP are disabled for this bridge, so the result is refused rather "
                    "than run twice.",
                    details={"tools": [answered]},
                )
            for fragment in thinking_deltas(event):
                yield _chunk(model=self.model, reasoning=fragment)
            if stream_message:
                opened = tool_input_start(event)
                if opened is not None:
                    if opened[1] == CLI_STRUCTURED_OUTPUT_TOOL:
                        envelope_index, envelope = opened[0], _MessageField()
                    elif opened[0] == envelope_index:
                        envelope_index, envelope = None, None
                piece = tool_input_delta(event)
                if piece is not None and envelope is not None and piece[0] == envelope_index and not diverged:
                    text = envelope.feed(piece[1])
                    if text is not None:
                        if text.startswith(streamed):
                            if len(text) > len(streamed):
                                yield _chunk(model=self.model, content=text[len(streamed):])
                                streamed = text
                        elif not streamed.startswith(text):
                            # A retried envelope says something else: stop
                            # streaming; the validated message settles it below.
                            diverged = True
                if envelope_index is not None and block_stop(event) == envelope_index:
                    envelope_index, envelope = None, None
            if turn.mode == "text":
                for fragment in text_deltas(event):
                    # Partial deltas are the stream; a completed assistant block is
                    # only the answer when nothing streamed at all (no duplication).
                    if fragment and (event.kind == "stream_event" or not seen_text):
                        seen_text = True
                        yield _chunk(model=self.model, content=fragment)
            result = final_result(event)
            if result is None:
                continue
            usage = _usage_from_result(result)
            if result.get("is_error"):
                # A plan limit, an API error, a refusal: the CLI's own words are
                # the explanation, not a missing envelope or an empty answer.
                reason = str(result.get("result") or "").strip() or str(result.get("subtype") or "error")
                raise ClaudeCliError(
                    f"The Claude Code CLI reported an error: {reason[:600]}",
                    details={"subtype": result.get("subtype"), "reason": result.get("result")},
                )
            answer = structured_output(event)
            if answer is None and schema_fallback is not None:
                # Some CLI builds surface the validated answer only through the
                # StructuredOutput tool_use; use it when the result omits it.
                answer = schema_fallback
            if turn.mode == "structured":
                if answer is None:
                    raise ClaudeCliError(
                        "The Claude Code CLI finished without structured output, but this turn "
                        "requires it. The schema may be too hard to satisfy; simplify it or retry.",
                        details={"subtype": result.get("subtype"), "reason": result.get("result")},
                    )
                yield _chunk(model=self.model, content=json.dumps(answer, ensure_ascii=False))
            elif turn.mode == "tools":
                if not isinstance(answer, Mapping):
                    raise ClaudeCliError(
                        "The Claude Code CLI finished without the required tool envelope, but this "
                        "turn offers Grid tools. The schema may be too hard to satisfy; retry the turn.",
                        details={"subtype": result.get("subtype"), "reason": result.get("result")},
                    )
                final = answer.get("final")
                if final is not None:
                    # A final structured answer ends the turn without tool calls.
                    yield _chunk(model=self.model, content=json.dumps(final, ensure_ascii=False))
                else:
                    for call in answer.get("tool_calls") or []:
                        if not isinstance(call, Mapping):
                            continue
                        finish_reason = "tool_calls"
                        yield _chunk(
                            model=self.model,
                            tool_call=ChoiceDeltaToolCall(
                                index=tool_index,
                                id=f"call_{tool_index}",
                                type="function",
                                function=ChoiceDeltaToolCallFunction(
                                    name=str(call.get("name")),
                                    arguments=json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                                ),
                            ),
                        )
                        tool_index += 1
                    message = answer.get("message")
                    if isinstance(message, str) and message:
                        # Only what has not streamed yet; a validated message that
                        # differs from the streamed draft follows it in full.
                        if message.startswith(streamed):
                            rest = message[len(streamed):]
                        else:
                            rest = "\n\n" + message
                        if rest:
                            yield _chunk(model=self.model, content=rest)
        yield _chunk(model=self.model, finish_reason=finish_reason, usage=usage)


class ClaudeCodeChatClient:
    """The slice of an OpenAI client that one-off requests use, answered by the CLI.

    Compaction and memory extraction ask the summary model through
    ``chat.completions.create``; for a Claude Code model this runs that request
    through the same locked-down runner as a plain text turn, so a long chat on
    a subscription is compacted like one on any other provider.
    """

    def __init__(self, runner: ClaudeCodeRunner) -> None:
        self._runner = runner
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, *, messages: Sequence[Mapping[str, Any]], **_: Any) -> Any:
        """One answer to *messages*; ``model``, ``max_tokens`` and the rest are the runner's."""
        instructions = [str(m.get("content") or "") for m in messages if m.get("role") == "system"]
        system = "\n\n".join([BRIDGE_PREAMBLE, *(text for text in instructions if text)])
        prompt = "\n\n".join(_render_history([dict(m) for m in messages if m.get("role") != "system"]))
        answer = ""
        async for event in self._runner.stream(prompt, system_prompt=system):
            result = final_result(event)
            if result is None:
                continue
            if result.get("is_error"):
                reason = str(result.get("result") or "").strip() or str(result.get("subtype") or "error")
                raise ClaudeCliError(
                    f"The Claude Code CLI reported an error: {reason[:600]}",
                    details={"subtype": result.get("subtype"), "reason": result.get("result")},
                )
            answer = str(result.get("result") or "")
        message = SimpleNamespace(role="assistant", content=answer)
        return SimpleNamespace(choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")])

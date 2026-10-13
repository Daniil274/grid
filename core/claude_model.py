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

The CLI's own login (``claude auth login``) is the only authorization. It is
checked before **every** launch (core.claude_cli.require_subscription), and
the CLI runs with no built-in tools, no MCP and no host settings. Input the
CLI cannot take - images, audio, files, hosted tools - is refused with an
explicit error rather than silently dropped. The one limit that cannot be
closed from here is organization-managed Claude Code settings, which override
anything Grid passes; see docs/claude-subscription.md.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
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
    final_result,
    structured_output,
    text_deltas,
    tool_uses,
)

logger = logging.getLogger("grid.claude_cli")


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
) -> ChatCompletionChunk:
    delta = ChoiceDelta(content=content, tool_calls=[tool_call] if tool_call else None)
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
    #: The ``--json-schema`` the CLI validates its answer against, or None.
    json_schema: Optional[Dict[str, Any]] = None
    #: Whether partial text messages are wanted (plain streaming only).
    partials: bool = False
    #: How to read the answered ``result`` event.
    mode: str = "text"  # "text" | "structured" | "tools"
    output_schema: Optional[AgentOutputSchemaBase] = None


def _text_content(content: Any, *, where: str) -> str:
    """A message's content as plain text, refusing anything the CLI cannot take."""
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
        elif kind in ("input_image", "input_file", "input_audio"):
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


def _render_history(input: Any) -> List[str]:
    """The conversation as prompt lines, keeping prior tool calls and results."""
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
                _text_content(output, where=f"tool result {call_id}")
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
        content = _text_content(data.get("content"), where=f"{role} message")
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
        "You are answering as a Grid model. You have no tools of your own: the Claude Code "
        "built-in tools, MCP servers and host settings are all disabled for this run.",
        "To use a Grid tool, reply with the required JSON object and put the call in "
        '"tool_calls" (leave "message" empty if you have nothing to say yet). To answer '
        'without tools, set "tool_calls" to [].',
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


class ClaudeCodeModel(Model):
    """An Agents SDK model that answers through the official Claude Code CLI."""

    def __init__(self, runner: ClaudeCodeRunner, model_name: str) -> None:
        self._runner = runner
        self.model = model_name
        #: The prompt of the last call, for logs and tests.
        self.last_prompt: Optional[str] = None

    # -- prompt -----------------------------------------------------------------
    def build_prompt(
        self,
        system_instructions: Optional[str],
        input: Any,
        tools: Sequence[Tool] = (),
        handoffs: Sequence[Handoff[Any, Any]] = (),
        output_schema: Optional[AgentOutputSchemaBase] = None,
    ) -> str:
        """Render the SDK's request as the single prompt the CLI reads on stdin."""
        lines: List[str] = []
        if system_instructions:
            lines.append(f"system:\n{system_instructions}")
        lines.extend(_render_history(input))
        defs = _tool_defs(tools, handoffs)
        if defs or output_schema is not None:
            lines.append("instructions:\n" + _tool_instructions(defs, output_schema))
        return "\n\n".join(lines)

    def _plan(
        self,
        system_instructions: Optional[str],
        input: Any,
        tools: Sequence[Tool],
        handoffs: Sequence[Handoff[Any, Any]],
        output_schema: Optional[AgentOutputSchemaBase],
    ) -> _Turn:
        defs = _tool_defs(tools, handoffs)
        structured = output_schema is not None and not output_schema.is_plain_text()
        prompt = self.build_prompt(system_instructions, input, tools, handoffs, output_schema if structured else None)
        if defs:
            return _Turn(
                prompt=prompt,
                json_schema=_envelope_schema(defs, output_schema if structured else None),
                partials=False,
                mode="tools",
                output_schema=output_schema if structured else None,
            )
        if structured:
            return _Turn(
                prompt=prompt,
                json_schema=dict(output_schema.json_schema()),  # type: ignore[union-attr]
                partials=False,
                mode="structured",
                output_schema=output_schema,
            )
        return _Turn(prompt=prompt, partials=True, mode="text")

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
        turn = self._plan(system_instructions, input, tools or (), handoffs or (), output_schema)
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
        async for event in self._runner.stream(
            turn.prompt, json_schema=turn.json_schema, partials=turn.partials
        ):
            native = tool_uses(event)
            if turn.json_schema is not None:
                # Claude Code implements ``--json-schema`` as an internal tool
                # named ``StructuredOutput``; its tool_use is expected on a
                # schema turn, so it is the one native call that is not an
                # escape. Its input is kept in case the result omits the value.
                for call in native:
                    if call.get("name") == CLI_STRUCTURED_OUTPUT_TOOL:
                        schema_fallback = call.get("input")
                native = [call for call in native if call.get("name") != CLI_STRUCTURED_OUTPUT_TOOL]
            if native:
                raise ClaudeCliError(
                    "The Claude Code CLI ran a tool it was not given. Built-in tools and MCP are "
                    "disabled for this bridge, so the result is refused rather than run twice.",
                    details={"tools": [str(call.get("name")) for call in native]},
                )
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
                logger.warning("Claude Code CLI reported an error result: %s", result.get("result"))
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
                        yield _chunk(model=self.model, content=message)
        yield _chunk(model=self.model, finish_reason=finish_reason, usage=usage)

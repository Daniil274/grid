"""
OpenAI Chat Completions model with vision support in tool results.

Chat Completions API (used by OpenRouter) does NOT support image_url in
tool messages (role: "tool").  The SDK strips non-text content by default.

This subclass:
1. Converts with preserve_tool_output_all_content=True so images survive
2. Post-processes messages: extracts image_url parts from tool messages
   and re-inserts them as separate user messages that vision models
   understand universally.

NOTE: _fetch_response body is a copy of openai_chatcompletions.py:246-376
(SDK 0.7.0). If the SDK is upgraded, compare and update accordingly.
"""
from __future__ import annotations

import json
import time
from typing import Any, Literal, cast

from openai import AsyncStream, Omit, omit
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.responses import Response
from openai.types.responses.response_prompt_param import ResponsePromptParam

from agents import _debug
from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import TResponseInputItem
from agents.logger import logger
from agents.model_settings import ModelSettings
from agents.models.chatcmpl_converter import Converter
from agents.models.chatcmpl_helpers import ChatCmplHelpers
from agents.models.fake_id import FAKE_RESPONSES_ID
from agents.models.interface import ModelTracing
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_responses import Converter as OpenAIResponsesConverter
from agents.tool import Tool
from agents.tracing.span_data import GenerationSpanData
from agents.tracing.spans import Span
from agents.util._json import _to_dump_compatible


class VisionChatCompletionsModel(OpenAIChatCompletionsModel):
    """
    OpenAIChatCompletionsModel with image support in tool results.

    Extracts image_url content from tool messages (which providers don't
    support) and re-inserts them as user messages (universally supported
    by vision models).
    """

    def __init__(
        self,
        model: str,
        openai_client: Any,
        preserve_reasoning_content: bool = False,
    ) -> None:
        super().__init__(model=model, openai_client=openai_client)
        self._preserve_reasoning_content = preserve_reasoning_content

    @staticmethod
    def _normalize_tool_content_to_parts(content: Any) -> list[dict] | None:
        """
        Normalize tool message content to a list of parts for image extraction.
        MCP tools often return content as a JSON string (SDK stringifies result.content).
        Also supports MCP image format: {"type": "image", "data": base64, "mimeType": "..."}.
        """
        if isinstance(content, list):
            parts = content
        elif isinstance(content, str) and content.strip():
            try:
                parsed = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                return None
            if not isinstance(parsed, list):
                return None
            parts = parsed
        else:
            return None

        normalized: list[dict] = []
        for part in parts:
            if not isinstance(part, dict):
                normalized.append({"type": "text", "text": str(part)})
                continue
            ptype = part.get("type")
            if ptype == "image_url":
                normalized.append(part)
            elif ptype == "input_image":
                url = part.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url", "")
                if url:
                    normalized.append({
                        "type": "image_url",
                        "image_url": {"url": url, "detail": part.get("detail", "high")},
                    })
            elif ptype == "image":
                # MCP format: {"type": "image", "data": base64, "mimeType": "image/png"}
                data = part.get("data", "")
                mime = part.get("mimeType", "image/png")
                if data:
                    data_uri = f"data:{mime};base64,{data}"
                    normalized.append({
                        "type": "image_url",
                        "image_url": {"url": data_uri, "detail": "high"},
                    })
            else:
                normalized.append(part)
        return normalized

    @staticmethod
    def _extract_images_from_tool_messages(
        messages: list[dict],
    ) -> list[dict]:
        """Move image_url parts from tool messages into follow-up user messages.
        Handles both list content (function tools) and JSON-string content (MCP tools).

        IMPORTANT: When an assistant message has multiple tool_calls (parallel),
        all tool messages must remain consecutive before any user messages are
        inserted. Providers like Moonshot AI reject messages where tool messages
        for the same assistant turn are separated by user messages.

        Strategy: process messages in groups. When we encounter a run of tool
        messages (consecutive), collect all their images and flush a single user
        message AFTER the entire run — not after each individual tool message.
        """
        result: list[dict] = []
        pending_images: list[dict] = []  # images accumulated across current tool-message run

        for msg in messages:
            if msg.get("role") != "tool":
                # Leaving a run of tool messages — flush collected images first
                if pending_images:
                    user_content: list[dict] = [
                        {"type": "text", "text": "[Tool result image]"},
                        *pending_images,
                    ]
                    result.append({"role": "user", "content": user_content})
                    pending_images = []
                result.append(msg)
                continue

            raw_content = msg.get("content")
            parts = VisionChatCompletionsModel._normalize_tool_content_to_parts(raw_content)
            if parts is None:
                result.append(msg)
                continue

            text_parts = []
            image_parts = []
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    image_parts.append(part)
                else:
                    text_parts.append(part)

            if not image_parts:
                result.append(msg)
                continue

            # Tool message — text only (providers require string or text-only list)
            tool_msg = dict(msg)
            if text_parts:
                # Flatten to string for maximum compatibility
                tool_msg["content"] = "\n".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in text_parts
                )
            else:
                tool_msg["content"] = "[Image provided below]"
            result.append(tool_msg)

            # Accumulate images — will be flushed after the entire tool-message run
            pending_images.extend(image_parts)

        # Flush any remaining images at the end
        if pending_images:
            user_content = [
                {"type": "text", "text": "[Tool result image]"},
                *pending_images,
            ]
            result.append({"role": "user", "content": user_content})

        return result

    @staticmethod
    def _extract_reasoning_by_call_id(items: list) -> dict[str, str]:
        """Scan response items and map each tool call_id to its preceding reasoning text.

        A reasoning item applies to all function_call items that immediately follow it
        (before any other non-function-call item resets the context).
        """
        result: dict[str, str] = {}
        pending: str | None = None
        for item in items:
            itype = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
            if itype == "reasoning":
                summaries = (
                    item.get("summary", []) if isinstance(item, dict)
                    else getattr(item, "summary", [])
                )
                texts = [
                    (s.get("text", "") if isinstance(s, dict) else getattr(s, "text", ""))
                    for s in summaries
                ]
                summary_text = "\n".join(t for t in texts if t).strip()
                direct_reasoning = (
                    item.get("reasoning_content", "") if isinstance(item, dict)
                    else getattr(item, "reasoning_content", "")
                ) or (
                    item.get("content", "") if isinstance(item, dict)
                    else getattr(item, "content", "")
                )
                if isinstance(direct_reasoning, list):
                    direct_reasoning = "\n".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in direct_reasoning
                    )
                pending = (summary_text or str(direct_reasoning or "").strip()) or None
            elif itype == "function_call":
                if pending:
                    call_id = (
                        item.get("call_id") if isinstance(item, dict)
                        else getattr(item, "call_id", None)
                    )
                    if not call_id:
                        call_id = (
                            item.get("id") if isinstance(item, dict)
                            else getattr(item, "id", None)
                        )
                    if call_id:
                        result[call_id] = pending
                    # keep pending — applies to all consecutive parallel tool calls
            elif itype in ("function_call_output", "computer_call_output"):
                # Tool results mark end of reasoning scope — reset
                pending = None
            elif itype is None:
                # User input messages have no "type" field — reset
                pending = None
            # "message" (assistant ResponseOutputMessage) does NOT reset pending:
            # the SDK emits reasoning → message → function_call in one turn
        return result

    @staticmethod
    def _inject_reasoning_content(items: list, messages: list[dict]) -> None:
        """Inject reasoning_content into assistant messages that contain tool_calls.

        Some providers (e.g. Moonshot AI / kimi) require reasoning_content in every
        assistant message that has tool_calls when thinking is enabled.  The SDK's
        Converter.items_to_messages only does this for DeepSeek; this method handles
        it independently for any model flagged with preserve_reasoning_content.
        """
        call_id_to_reasoning = VisionChatCompletionsModel._extract_reasoning_by_call_id(items)
        for msg in messages:
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                continue
            if msg.get("reasoning_content"):
                continue  # already present
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id and tc_id in call_id_to_reasoning:
                    msg["reasoning_content"] = call_id_to_reasoning[tc_id]
                    break
            if not msg.get("reasoning_content"):
                msg["reasoning_content"] = "[reasoning unavailable]"

    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: bool = False,
        prompt: ResponsePromptParam | None = None,
    ) -> ChatCompletion | tuple[Response, AsyncStream[ChatCompletionChunk]]:
        converted_messages = Converter.items_to_messages(
            input, model=self.model, preserve_tool_output_all_content=True
        )
        if self._preserve_reasoning_content and not isinstance(input, str):
            self._inject_reasoning_content(input, converted_messages)

        if system_instructions:
            converted_messages.insert(
                0,
                {
                    "content": system_instructions,
                    "role": "system",
                },
            )
        converted_messages = _to_dump_compatible(converted_messages)

        # Move images from tool messages → user messages for provider compatibility
        converted_messages = self._extract_images_from_tool_messages(converted_messages)

        if tracing.include_data():
            span.span_data.input = converted_messages

        if model_settings.parallel_tool_calls and tools:
            parallel_tool_calls: bool | Omit = True
        elif model_settings.parallel_tool_calls is False:
            parallel_tool_calls = False
        else:
            parallel_tool_calls = omit
        tool_choice = Converter.convert_tool_choice(model_settings.tool_choice)
        response_format = Converter.convert_response_format(output_schema)

        converted_tools = [Converter.tool_to_openai(tool) for tool in tools] if tools else []

        for handoff in handoffs:
            converted_tools.append(Converter.convert_handoff_tool(handoff))

        converted_tools = _to_dump_compatible(converted_tools)
        tools_param = converted_tools if converted_tools else omit

        if _debug.DONT_LOG_MODEL_DATA:
            logger.debug("Calling LLM")
        else:
            messages_json = json.dumps(
                converted_messages,
                indent=2,
                ensure_ascii=False,
            )
            tools_json = json.dumps(
                converted_tools,
                indent=2,
                ensure_ascii=False,
            )
            logger.debug(
                f"{messages_json}\n"
                f"Tools:\n{tools_json}\n"
                f"Stream: {stream}\n"
                f"Tool choice: {tool_choice}\n"
                f"Response format: {response_format}\n"
            )

        reasoning_effort = model_settings.reasoning.effort if model_settings.reasoning else None
        store = ChatCmplHelpers.get_store_param(self._get_client(), model_settings)

        stream_options = ChatCmplHelpers.get_stream_options_param(
            self._get_client(), model_settings, stream=stream
        )

        stream_param: Literal[True] | Omit = True if stream else omit

        ret = await self._get_client().chat.completions.create(
            model=self.model,
            messages=converted_messages,
            tools=tools_param,
            temperature=self._non_null_or_omit(model_settings.temperature),
            top_p=self._non_null_or_omit(model_settings.top_p),
            frequency_penalty=self._non_null_or_omit(model_settings.frequency_penalty),
            presence_penalty=self._non_null_or_omit(model_settings.presence_penalty),
            max_tokens=self._non_null_or_omit(model_settings.max_tokens),
            tool_choice=tool_choice,
            response_format=response_format,
            parallel_tool_calls=parallel_tool_calls,
            stream=cast(Any, stream_param),
            stream_options=self._non_null_or_omit(stream_options),
            store=self._non_null_or_omit(store),
            reasoning_effort=self._non_null_or_omit(reasoning_effort),
            verbosity=self._non_null_or_omit(model_settings.verbosity),
            top_logprobs=self._non_null_or_omit(model_settings.top_logprobs),
            prompt_cache_retention=self._non_null_or_omit(model_settings.prompt_cache_retention),
            extra_headers=self._merge_headers(model_settings),
            extra_query=model_settings.extra_query,
            extra_body=model_settings.extra_body,
            metadata=self._non_null_or_omit(model_settings.metadata),
            **(model_settings.extra_args or {}),
        )

        if isinstance(ret, ChatCompletion):
            return ret

        responses_tool_choice = OpenAIResponsesConverter.convert_tool_choice(
            model_settings.tool_choice
        )
        if responses_tool_choice is None or responses_tool_choice is omit:
            responses_tool_choice = "auto"

        response = Response(
            id=FAKE_RESPONSES_ID,
            created_at=time.time(),
            model=self.model,
            object="response",
            output=[],
            tool_choice=responses_tool_choice,  # type: ignore[arg-type]
            top_p=model_settings.top_p,
            temperature=model_settings.temperature,
            tools=[],
            parallel_tool_calls=parallel_tool_calls or False,
            reasoning=model_settings.reasoning,
        )
        return response, ret

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

    @staticmethod
    def _extract_images_from_tool_messages(
        messages: list[dict],
    ) -> list[dict]:
        """Move image_url parts from tool messages into follow-up user messages."""
        result: list[dict] = []
        for msg in messages:
            if msg.get("role") != "tool" or not isinstance(msg.get("content"), list):
                result.append(msg)
                continue

            text_parts = []
            image_parts = []
            for part in msg["content"]:
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

            # Separate user message with the image(s)
            user_content: list[dict] = [
                {"type": "text", "text": "[Tool result image]"},
                *image_parts,
            ]
            result.append({"role": "user", "content": user_content})

        return result

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
        # Preserve images so we can extract them in post-processing
        converted_messages = Converter.items_to_messages(
            input, model=self.model, preserve_tool_output_all_content=True
        )

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

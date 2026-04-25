"""
Utilities for converting between different multimodal message formats.
Handles conversion between OpenAI Vision API format and Agents SDK format.
"""

from typing import List, Dict, Any, Union, Optional
import logging
from pathlib import Path

from schemas.schemas import (
    ContextMessage, TextContent, ImageContent, FileImageContent,
    ImageUrl, ContentPart
)
from utils.image_utils import ImageUtils

logger = logging.getLogger(__name__)


class MultimodalConverter:
    """Converter for multimodal messages between different formats."""

    @staticmethod
    def openai_to_context_message(
        role: str,
        content: Union[str, List[Dict[str, Any]]],
        timestamp: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> ContextMessage:
        """
        Convert OpenAI message format to ContextMessage.

        Args:
            role: Message role (user, assistant, system)
            content: Message content (string or list of content parts)
            timestamp: Message timestamp
            metadata: Optional metadata

        Returns:
            ContextMessage instance
        """
        if isinstance(content, str):
            # Simple text message
            return ContextMessage(
                role=role,
                content=content,
                timestamp=timestamp,
                metadata=metadata
            )

        # Multimodal content - convert parts
        content_parts: List[ContentPart] = []

        for part in content:
            if not isinstance(part, dict):
                continue

            part_type = part.get("type")

            if part_type in ("text", "input_text"):
                # Support both text and input_text formats
                text_part = TextContent(
                    type="text",
                    text=part.get("text", "")
                )
                content_parts.append(text_part)

            elif part_type in ("image_url", "input_image"):
                # Support both image_url and input_image formats
                # For input_image, image_url is a direct string
                # For image_url, image_url can be a string or dict
                if part_type == "input_image":
                    # Realtime API format: {"type": "input_image", "image_url": "...", "detail": "..."}
                    url = part.get("image_url", "")
                    detail = part.get("detail", "auto")
                else:
                    # OpenAI Vision API format: {"type": "image_url", "image_url": {...}}
                    image_url_data = part.get("image_url")
                    if isinstance(image_url_data, str):
                        # Simple string URL
                        url = image_url_data
                        detail = "auto"
                    elif isinstance(image_url_data, dict):
                        # URL with detail
                        url = image_url_data.get("url", "")
                        detail = image_url_data.get("detail", "auto")
                    else:
                        logger.warning(f"Invalid image_url format: {image_url_data}")
                        continue

                if url:
                    image_part = ImageContent(
                        type="image_url",
                        image_url=ImageUrl(url=url, detail=detail)
                    )
                    content_parts.append(image_part)

            elif part_type == "image_file":
                # Internal format for file paths
                file_path = part.get("file_path", "")
                detail = part.get("detail", "auto")

                if file_path:
                    image_part = FileImageContent(
                        type="image_file",
                        file_path=file_path,
                        detail=detail
                    )
                    content_parts.append(image_part)

        return ContextMessage(
            role=role,
            content=content_parts if content_parts else "[empty multimodal content]",
            timestamp=timestamp,
            metadata=metadata
        )

    @staticmethod
    def context_message_to_openai(message: ContextMessage) -> Dict[str, Any]:
        """
        Convert ContextMessage to OpenAI message format.

        Args:
            message: ContextMessage instance

        Returns:
            OpenAI message dict
        """
        if isinstance(message.content, str):
            # Simple text message
            return {
                "role": message.role,
                "content": message.content
            }

        # Multimodal content
        content_parts = []

        for part in message.content:
            if isinstance(part, TextContent):
                content_parts.append({
                    "type": "text",
                    "text": part.text
                })

            elif isinstance(part, ImageContent):
                image_url = part.image_url
                if isinstance(image_url, ImageUrl):
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": image_url.url,
                            "detail": image_url.detail or "auto"
                        }
                    })
                elif isinstance(image_url, dict):
                    content_parts.append({
                        "type": "image_url",
                        "image_url": image_url
                    })

            elif isinstance(part, FileImageContent):
                # Convert file to base64 for OpenAI API
                base64_url = ImageUtils.file_to_base64(part.file_path)
                if base64_url:
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": base64_url,
                            "detail": part.detail or "auto"
                        }
                    })
                else:
                    logger.warning(f"Failed to convert file to base64: {part.file_path}")

            elif isinstance(part, dict):
                # Pass through dict parts
                content_parts.append(part)

        return {
            "role": message.role,
            "content": content_parts if content_parts else "..."
        }

    @staticmethod
    def context_message_to_agents_sdk(message: ContextMessage) -> Union[str, List[Dict[str, Any]]]:
        """
        Convert ContextMessage to format compatible with Agents SDK.

        The Agents SDK expects messages as either:
        - str for simple text messages
        - List[Dict] for multimodal messages

        For Realtime API compatibility, uses input_text and input_image formats
        as specified in RealtimeModelInputTextContent and RealtimeModelInputImageContent.

        Args:
            message: ContextMessage instance

        Returns:
            Message content in Agents SDK format
        """
        if isinstance(message.content, str):
            return message.content

        # Multimodal - convert to SDK format
        content_parts = []

        for part in message.content:
            if isinstance(part, TextContent):
                # SDK-compatible input_text format
                content_parts.append({
                    "type": "input_text",
                    "text": part.text
                })

            elif isinstance(part, ImageContent):
                # Normalize path/URL and return input_image (SDK format)
                image_url = part.image_url
                if isinstance(image_url, ImageUrl):
                    url = image_url.url
                    detail = image_url.detail or "auto"
                elif isinstance(image_url, dict):
                    url = image_url.get("url", "")
                    detail = image_url.get("detail", "auto")
                elif isinstance(image_url, str):
                    url = image_url
                    detail = "auto"
                else:
                    continue

                # Convert local files to base64, leave http as-is
                if ImageUtils.is_base64_image(url):
                    pass
                elif ImageUtils.is_local_path(url):
                    base64_url = ImageUtils.file_to_base64(url)
                    if base64_url:
                        url = base64_url
                    else:
                        logger.warning(f"Failed to convert image file: {url}")
                        continue

                image_part: Dict[str, Any] = {
                    "type": "input_image",
                    "image_url": url
                }
                if detail and detail != "auto":
                    image_part["detail"] = detail
                content_parts.append(image_part)

            elif isinstance(part, FileImageContent):
                # Convert file to base64 and return input_image
                base64_url = ImageUtils.file_to_base64(part.file_path)
                if base64_url:
                    image_part: Dict[str, Any] = {
                        "type": "input_image",
                        "image_url": base64_url
                    }
                    if part.detail and part.detail != "auto":
                        image_part["detail"] = part.detail
                    content_parts.append(image_part)
                else:
                    logger.warning(f"Failed to convert image file: {part.file_path}")

            elif isinstance(part, dict):
                # Dict part: convert to input_text/input_image format (SDK)
                part_type = part.get("type")
                if part_type == "image_url":
                    image_url_data = part.get("image_url")
                    if isinstance(image_url_data, dict):
                        url = image_url_data.get("url", "")
                        detail = image_url_data.get("detail", "auto")
                    elif isinstance(image_url_data, str):
                        url = image_url_data
                        detail = "auto"
                    else:
                        url = ""
                        detail = "auto"
                    
                    if url:
                        if ImageUtils.is_local_path(url) and not ImageUtils.is_base64_image(url):
                            base64_url = ImageUtils.file_to_base64(url)
                            if base64_url:
                                url = base64_url
                            else:
                                logger.warning(f"Failed to convert image file: {url}")
                                continue
                        
                        image_part: Dict[str, Any] = {
                            "type": "input_image",
                            "image_url": url
                        }
                        if detail and detail != "auto":
                            image_part["detail"] = detail
                        content_parts.append(image_part)
                elif part_type == "input_image":
                    # Keep as input_image, but normalize local paths
                    url = part.get("image_url", "")
                    detail = part.get("detail", "auto")
                    if url:
                        if ImageUtils.is_local_path(url) and not ImageUtils.is_base64_image(url):
                            base64_url = ImageUtils.file_to_base64(url)
                            if base64_url:
                                url = base64_url
                            else:
                                logger.warning(f"Failed to convert image file: {url}")
                                continue
                        image_part: Dict[str, Any] = {
                            "type": "input_image",
                            "image_url": url
                        }
                        if detail and detail != "auto":
                            image_part["detail"] = detail
                        content_parts.append(image_part)
                elif part_type == "text":
                    # Convert text -> input_text
                    content_parts.append({
                        "type": "input_text",
                        "text": part.get("text", "")
                    })
                elif part_type == "input_text":
                    # Keep as input_text
                    content_parts.append({
                        "type": "input_text",
                        "text": part.get("text", "")
                    })
                else:
                    # Pass through other dict parts
                    content_parts.append(part)

        return content_parts if content_parts else "[empty]"

    @staticmethod
    def extract_text_from_multimodal(content: Union[str, List[Any]]) -> str:
        """
        Extract text content from multimodal message.

        Args:
            content: Message content (string or list of parts)

        Returns:
            Extracted text
        """
        if isinstance(content, str):
            return content

        text_parts = []
        for part in content:
            if isinstance(part, TextContent):
                text_parts.append(part.text)
            elif isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                text_parts.append(part.get("text", ""))
            elif isinstance(part, str):
                text_parts.append(part)

        return " ".join(text_parts) if text_parts else "[multimodal content]"

    @staticmethod
    def has_images(content: Union[str, List[Any]]) -> bool:
        """
        Check if content contains images.

        Args:
            content: Message content

        Returns:
            True if content contains images
        """
        if isinstance(content, str):
            return False

        for part in content:
            if isinstance(part, (ImageContent, FileImageContent)):
                return True
            elif isinstance(part, dict):
                if part.get("type") in ["image_url", "image_file", "input_image"]:
                    return True

        return False

    @staticmethod
    def create_multimodal_message(
        role: str,
        text: str,
        image_sources: Optional[List[str]] = None,
        timestamp: Optional[str] = None
    ) -> ContextMessage:
        """
        Create a multimodal message with text and images.

        Args:
            role: Message role
            text: Text content
            image_sources: List of image sources (URLs, base64, or file paths)
            timestamp: Optional timestamp

        Returns:
            ContextMessage with multimodal content
        """
        from datetime import datetime

        if not timestamp:
            timestamp = datetime.now().isoformat()

        if not image_sources:
            # Text-only message
            return ContextMessage(
                role=role,
                content=text,
                timestamp=timestamp
            )

        # Build multimodal content
        content_parts: List[ContentPart] = []

        # Add text part
        if text:
            content_parts.append(TextContent(type="text", text=text))

        # Add image parts
        for source in image_sources:
            source_type, error = ImageUtils.validate_image_source(source)

            if source_type == "invalid":
                logger.warning(f"Invalid image source: {error}")
                continue

            if source_type == "file":
                # Local file - store as file reference
                content_parts.append(FileImageContent(
                    type="image_file",
                    file_path=source,
                    detail="auto"
                ))
            else:
                # URL or base64
                content_parts.append(ImageContent(
                    type="image_url",
                    image_url=ImageUrl(url=source, detail="auto")
                ))

        return ContextMessage(
            role=role,
            content=content_parts,
            timestamp=timestamp
        )

    # ------------------------------------------------------------------
    # Tool output helpers
    # ------------------------------------------------------------------
    @staticmethod
    def tool_output_to_content_parts(output: Union[List[Any], Dict[str, Any], Any]) -> List[ContentPart]:
        """
        Normalize multimodal tool output to a list of ContentPart.

        Supports dictionaries in input_text/input_image/image_url format and
        ready-made Pydantic models TextContent/ImageContent/FileImageContent.
        """
        if output is None:
            return []

        items: List[Any]
        if isinstance(output, list):
            items = output
        else:
            items = [output]

        parts: List[ContentPart] = []

        for item in items:
            if isinstance(item, (TextContent, ImageContent, FileImageContent)):
                parts.append(item)
                continue

            if isinstance(item, str):
                parts.append(TextContent(type="text", text=item))
                continue

            if isinstance(item, dict):
                part_type = item.get("type")

                if part_type in ("text", "input_text"):
                    parts.append(TextContent(type="text", text=item.get("text", "")))
                    continue

                if part_type in ("input_image", "image_url"):
                    image_url_data = item.get("image_url")
                    url = ""
                    detail = item.get("detail", "auto")

                    if isinstance(image_url_data, dict):
                        url = image_url_data.get("url", "")
                        detail = image_url_data.get("detail", detail)
                    elif isinstance(image_url_data, str):
                        url = image_url_data

                    if url:
                        parts.append(
                            ImageContent(
                                type="image_url",
                                image_url=ImageUrl(url=url, detail=detail or "auto"),
                            )
                        )
                    continue

                if part_type == "image_file":
                    file_path = item.get("file_path", "")
                    detail = item.get("detail", "auto")
                    if file_path:
                        parts.append(
                            FileImageContent(
                                type="image_file",
                                file_path=file_path,
                                detail=detail,
                            )
                        )
                    continue

                # Unknown type — keep as-is
                parts.append(item)  # type: ignore[arg-type]
                continue

            # Fallback — convert to text
            try:
                parts.append(TextContent(type="text", text=str(item)))
            except Exception:
                continue

        return parts

    @staticmethod
    def store_tool_multimodal_output(
        ctx: Any,
        tool_name: str,
        output: Union[List[Any], Dict[str, Any], Any],
        *,
        role: str = "assistant",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Saves multimodal tool output to context instead of returning to the user.

        Args:
            ctx: RunContextWrapper obtained in the tool (ctx.context.factory/context_id)
            tool_name: tool name
            output: tool output (string/list/dict)
            role: message role in the context
            metadata: additional metadata

        Returns:
            Used context_id or None if saving failed.
        """
        try:
            parts = MultimodalConverter.tool_output_to_content_parts(output)
            if not parts:
                return None

            context_obj = getattr(ctx, "context", None)
            factory = getattr(context_obj, "factory", None)
            context_id = getattr(context_obj, "context_id", None)

            if not factory or not hasattr(factory, "context_manager"):
                logger.debug("store_tool_multimodal_output: no access to factory.context_manager")
                return None

            meta = {"source": "tool", "tool_name": tool_name}
            if metadata:
                meta.update(metadata)

            factory.context_manager.add_message(role, parts, metadata=meta)

            try:
                return context_id or factory.context_manager.get_current_context_id()
            except Exception:
                return context_id
        except Exception as exc:
            logger.warning(
                "Failed to save multimodal output of tool %s to context: %s",
                tool_name,
                exc,
                exc_info=exc,
            )
            return None

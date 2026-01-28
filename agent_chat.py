#!/usr/bin/env python3
"""
Legacy chat interface for Grid Agent System.
Simplified version of the main.py CLI for backward compatibility.
"""

import asyncio
import argparse
import sys
import time
import logging
import os
import re
from pathlib import Path
from typing import Optional

# Add grid package to path
sys.path.insert(0, str(Path(__file__).parent))

# Use Proactor event loop on Windows to support asyncio subprocess APIs (required for MCP)
if sys.platform == "win32":
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass

from core.config import Config
from core.agent_factory import AgentFactory
from core.tracing_config import configure_tracing_from_env
from utils.exceptions import GridError
from utils.logger import Logger
from utils.multimodal_converter import MultimodalConverter
from utils.image_utils import ImageUtils

# Configure tracing instead of logging
configure_tracing_from_env()

# Configure logging: console + files
Logger.configure(
    level="INFO",
    log_dir=str(Path(__file__).parent / "logs"),
    enable_console=True,
    enable_json=True,
    enable_legacy_logs=True,
    force_reconfigure=True,
)

# Configure minimal logging for external libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("openai.agents").setLevel(logging.CRITICAL)
logging.getLogger("grid").setLevel(logging.INFO)


def parse_message_with_images(user_input: str) -> tuple[str, list[str]]:
    """
    Parse user input to extract text and image paths.

    Syntax: "text & 'path/to/image.png'" or "text & path/to/image.png"
    Multiple images: "text & image1.png & image2.jpg"

    Returns:
        Tuple of (text_message, list_of_image_paths)
    """
    if '&' not in user_input:
        return user_input, []

    # Split by & to get text and image parts
    parts = user_input.split('&')
    text = parts[0].strip()
    image_paths = []

    for part in parts[1:]:
        part = part.strip()

        # Remove quotes if present
        if part.startswith("'") and part.endswith("'"):
            part = part[1:-1]
        elif part.startswith('"') and part.endswith('"'):
            part = part[1:-1]

        # Validate image source
        if part:
            source_type, error = ImageUtils.validate_image_source(part)
            if source_type != 'invalid':
                image_paths.append(part)
            else:
                print(f"Warning: Invalid image source: {part} - {error}")

    return text, image_paths


def prepare_agent_message(text: str, image_paths: list[str]) -> str:
    """
    Prepare message for agent with optional images.

    According to Agents SDK examples, images should be sent as a list of messages
    where each message has "role" and "content" fields. Content can be a list
    of content parts with "type": "input_image" or "type": "input_text".

    Args:
        text: Text message
        image_paths: List of image paths

    Returns:
        Formatted message for agent (text string or JSON string with proper message format)
    """
    if not image_paths:
        return text

    # Create multimodal message
    from datetime import datetime
    message = MultimodalConverter.create_multimodal_message(
        role="user",
        text=text,
        image_sources=image_paths,
        timestamp=datetime.now().isoformat()
    )

    # Convert to Agents SDK format - get content parts
    content = MultimodalConverter.context_message_to_agents_sdk(message)

    # According to Agents SDK examples, we need to format as a message with role and content
    # Format: {"role": "user", "content": [...]}
    import json
    if isinstance(content, str):
        # Simple text - return as is
        return content
    else:
        # Multimodal - wrap in proper message format
        # Agents SDK expects: [{"role": "user", "content": [...]}]
        message_dict = {
            "role": "user",
            "content": content
        }
        return json.dumps(message_dict)


async def main():
    """Главная функция."""
    parser = argparse.ArgumentParser(description="Legacy Grid agent chat interface")
    parser.add_argument(
        "--agent", "-a",
        type=str,
        default=None,
        help="Agent name (default from config)"
    )
    parser.add_argument(
        "--path", "-p",
        type=str,
        default=None,
        help="Working directory"
    )
    parser.add_argument(
        "--context-path",
        type=str,
        default=None,
        help="Context path for agent"
    )
    parser.add_argument(
        "--message", "-m",
        type=str,
        default=None,
        help="Single message to send (interactive mode if not provided)"
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default="config.yaml",
        help="Configuration file path"
    )
    
    args = parser.parse_args()
    
    try:
        # Beautiful initialization
        print("Запуск Grid Agent System...")
        
        # Load configuration
        print("Load Config")
        config = Config(args.config, args.path)
        print("Load Config - Конфигурация загружена")
        
        # Create factory
        print("Initialize SecurityAwareAgentFactory")
        factory = AgentFactory(config, args.path)
        print("Initialize SecurityAwareAgentFactory - Фабрика агентов инициализирована")
        selected_context_id: Optional[str] = None
        last_context_id: Optional[str] = None

        def extract_context_id_from_text(text: Optional[str]) -> Optional[str]:
            if not text:
                return None
            match = re.search(r"ctx-[0-9a-f]{8}", text)
            return match.group(0) if match else None
        
        # Tracing is configured automatically by Agents SDK
        
        # Determine agent
        agent_key = args.agent or config.get_default_agent()

        # Context is automatically managed by ContextManager
        # - New clean context is created on each startup
        # - Old contexts are preserved and accessible via Context ID
        print("Context - Новая сессия создана, старые контексты доступны по ID")
        
        print("Grid Agent System готов к работе")
        
        print("\n" + "="*60)
        print("Grid Agent System")
        print("="*60)
        print(f"Агент: {agent_key}")
        print(f"Рабочая директория: {config.get_working_directory()}")
        if args.context_path:
            print(f"Контекстный путь: {args.context_path}")
        print("="*60)
        
        if args.message:
            # Single message mode
            print(f"Обработка сообщения")

            try:
                # Parse message for images
                text, image_paths = parse_message_with_images(args.message)

                # Show image info if any
                if image_paths:
                    print(f"Images found: {len(image_paths)}")
                    for img_path in image_paths:
                        info = ImageUtils.get_image_info(img_path)
                        if info['valid']:
                            size_mb = info['size_bytes'] / (1024 * 1024) if info['size_bytes'] else 0
                            print(f"   - {img_path} ({size_mb:.2f} MB, {info['mime_type']})")
                        else:
                            print(f"   - {img_path} (ошибка: {info['error']})")

                # Prepare message for agent
                agent_message = prepare_agent_message(text, image_paths)

                # Track agent execution
                print(f"Agent {agent_key} (agent: {agent_key})")

                start_time = time.time()
                use_streaming = True  # Включаем стриминг для режима одного сообщения
                inline_context_id = extract_context_id_from_text(text)
                request_context_id = inline_context_id or selected_context_id

                # Если пользователь явно указал context_id, не используем use_active_context
                use_active_context = request_context_id is None

                # Отладочная информация
                if request_context_id:
                    print(f"Используем контекст: {request_context_id}")
                else:
                    print("Используем активный контекст")

                response = await factory.run_agent(
                    agent_key,
                    agent_message,
                    args.context_path,
                    context_id=request_context_id,
                    stream=use_streaming,
                    use_active_context=use_active_context,
                )
                last_context_id = factory.get_active_context_id()
                duration = time.time() - start_time
                
                # Try to get token usage information
                token_usage = None
                try:
                    # Estimate token usage (approximation since we don't have direct access)
                    # This is a rough estimate - in production you'd want to capture real usage
                    estimated_prompt_tokens = len(args.message.split()) * 1.3  # rough estimate
                    estimated_completion_tokens = len(response.split()) * 1.3
                    
                    # Try to get model from agent config
                    agent_config = config.get_agent(agent_key)
                    model_name = getattr(agent_config, 'model', 'unknown')
                    
                    # Token calculation removed
                except Exception as e:
                    pass  # Ignore token calculation errors
                
                print(f"\nОтвет сгенерирован ({duration:.2f}с, {len(response)} символов)")

                print("Success")
                
            except Exception as e:
                print("Operation completed")
                print(f"Ошибка: {e}")
        else:
            # Interactive mode
            print("\nCommands:")
            print("  'exit' or 'quit' - Exit")
            print("  'clear' - Start new context (old contexts saved)")
            print("  'context' - Show current context info")
            print("  'contexts' - List all saved context IDs")
            print("  'use <context_id>' - Switch to a saved context")
            print("  'help' - Show this help")
            print("\nContext IDs:")
            print("  Use context ID in message: 'ctx-abc12345 your message'")
            print("  Old contexts are automatically saved and accessible")
            print("\nImages:")
            print("  Use '&' to attach images: 'Your message & path/to/image.png'")
            print("  Multiple images: 'Message & image1.jpg & image2.png'")
            print("-" * 60)
            
            while True:
                try:
                    user_input = input("\nYou: ").strip()
                    
                    if user_input.lower() in ['exit', 'quit']:
                        print("Goodbye!")
                        break
                    elif user_input.lower() == 'clear':
                        print("Clear Context")
                        cleared_id = factory.clear_context()
                        selected_context_id = None
                        last_context_id = cleared_id
                        print("Clear Context - Создан новый контекст")
                        print(f"New context ID: {cleared_id}")
                        print("Старые контексты сохранены и доступны по ID")
                        continue
                    elif user_input.lower() == 'context':
                        print("Get Context")
                        context_info = factory.get_context_info()
                        print("Get Context - Информация о контексте получена")
                        
                        print(f"\n📋 Информация о контексте:")
                        print(f"   Сообщений: {context_info.get('conversation_messages', 0)}")
                        print(f"   История выполнения: {context_info.get('execution_history', 0)}")
                        print(f"   Использование памяти: {context_info.get('memory_usage_mb', 0):.2f} МБ")
                        active_id = context_info.get('current_context_id')
                        if active_id:
                            print(f"   Active context ID: {active_id}")
                        if selected_context_id:
                            print(f"   Selected for next runs: {selected_context_id}")
                        if last_context_id and last_context_id != selected_context_id:
                            print(f"   Last response context ID: {last_context_id}")
                        available_ids = [cid for cid in context_info.get('available_contexts', []) if cid != active_id]
                        if available_ids:
                            print(f"   Known contexts: {', '.join(available_ids)}")
                        if context_info.get('last_user_message'):
                            last_msg = context_info['last_user_message']
                            print(f"   Последнее сообщение: {last_msg}")
                        continue
                    elif user_input.lower() == 'contexts':
                        ids = factory.list_context_ids()
                        if not ids:
                            print('No saved contexts yet.')
                        else:
                            print('Known contexts:')
                            for ctx_id in ids:
                                marker = ' (selected)' if ctx_id == selected_context_id else ''
                                print(f'  - {ctx_id}{marker}')
                        continue
                    elif user_input.lower().startswith('use '):
                        target_id = user_input[4:].strip()
                        if not target_id:
                            print('Provide context id after "use".')
                            continue
                        try:
                            selected_context_id = factory.activate_context(target_id)
                            last_context_id = selected_context_id
                            print(f'Switched to context {selected_context_id}')
                        except Exception as exc:
                            print(f'Failed to switch context: {exc}')
                        continue
                    elif user_input.lower() == 'help':
                        print("\nAvailable commands:")
                        print("  exit, quit - Exit the chat")
                        print("  clear - Start new context (old contexts saved)")
                        print("  context - Show current context information")
                        print("  contexts - List all saved context IDs")
                        print("  use <context_id> - Switch to a saved context")
                        print("  help - Show this help message")
                        print("\nContext IDs:")
                        print("  Use in message: 'ctx-abc12345 your message'")
                        print("  Each session starts fresh, old contexts auto-saved")
                        print("\nImages:")
                        print("  Attach images using '&': 'Your message & path/to/image.png'")
                        print("  Multiple images: 'Message & img1.jpg & img2.png'")
                        print("  Images are saved in context and accessible after restart")
                        continue
                    elif not user_input:
                        continue

                    # Parse message for images
                    text, image_paths = parse_message_with_images(user_input)

                    # Show image info if any
                    if image_paths:
                        print(f"Images found: {len(image_paths)}")
                        for img_path in image_paths:
                            info = ImageUtils.get_image_info(img_path)
                            if info['valid']:
                                size_mb = info['size_bytes'] / (1024 * 1024) if info['size_bytes'] else 0
                                print(f"   - {img_path} ({size_mb:.2f} MB, {info['mime_type']})")
                            else:
                                print(f"   - {img_path} (ошибка: {info['error']})")

                    # Prepare message for agent
                    agent_message = prepare_agent_message(text, image_paths)

                    # Process user message with beautiful logging
                    try:
                        # Track execution with token counting
                        print(f"Agent {agent_key} (agent: {agent_key})")

                        start_time = time.time()
                        use_streaming = True  # Включаем стриминг для интерактивного режима
                        inline_context_id = extract_context_id_from_text(text)
                        request_context_id = inline_context_id or selected_context_id

                        # Если пользователь явно указал context_id, не используем use_active_context
                        use_active_context = request_context_id is None

                        # Отладочная информация
                        if request_context_id:
                            print(f"Используем контекст: {request_context_id}")
                        else:
                            print("Используем активный контекст")

                        response = await factory.run_agent(
                            agent_key,
                            agent_message,
                            args.context_path,
                            context_id=request_context_id,
                            stream=use_streaming,
                            use_active_context=use_active_context,
                        )
                        last_context_id = factory.get_active_context_id()
                        duration = time.time() - start_time
                        
                        # Try to get token usage information
                        token_usage = None
                        try:
                            estimated_prompt_tokens = len(user_input.split()) * 1.3
                            estimated_completion_tokens = len(response.split()) * 1.3
                            
                            agent_config = config.get_agent(agent_key)
                            model_name = getattr(agent_config, 'model', 'unknown')
                            # Token calculation removed
                        except Exception:
                            pass
                        
                        print(f"\nОтвет получен ({duration:.2f}с, {len(response)} символов)")
                        
                        # При стриминге ответ уже выведен в реальном времени, добавляем только новую строку
                        if use_streaming:
                            print(f"\n")  # Добавляем новую строку после стримингового вывода
                        else:
                            print(f"\n{agent_key}: {response}")
                        if last_context_id:
                            print(f"Context ID: {last_context_id}")
                            # Обновляем selected_context_id для следующего вызова
                            selected_context_id = last_context_id
                        
                    except Exception as e:
                        print("Operation completed")
                        print(f"Ошибка: {e}")
                    
                except KeyboardInterrupt:
                    print("\n\nInterrupted. Goodbye!")
                    break
                except EOFError:
                    print("\n\nEOF. Goodbye!")
                    break
        
        # Beautiful cleanup and session summary
        print("Cleanup")
        await factory.cleanup()
        print("Cleanup - Ресурсы освобождены")
        
        # Session summary
        print("Grid Agent System завершил работу")
        
    except GridError as e:
        print(f"Ошибка Grid: {e}")
        print(f"Grid Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Неожиданная ошибка: {e}")
        print(f"Unexpected Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())

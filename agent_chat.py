#!/usr/bin/env python3
"""
Legacy chat interface for Grid Agent System.
Simplified version of the main.py CLI for backward compatibility.
"""

import signal
import asyncio
import argparse
import sys
__import__('pysqlite3')
sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
import time
import logging
import os
import re
from datetime import datetime
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
from core.agent_factory import AgentFactory, ConsoleStreamObserver
from core.compact import compact_conversation, CompactMessage, estimate_messages_tokens
from schemas import ContextMessage
try:
    # Optional: only available when Docker SDK is installed and Docker is running
    from core.managers.container_manager import ContainerManager
except Exception:
    ContainerManager = None
from core.tracing.config import configure_tracing_from_env
from utils.exceptions import GridError
from utils.cli_chat import CliChatRenderer
from utils.grid_paths import get_default_logs_dir
from utils.logger import Logger
from utils.multimodal_converter import MultimodalConverter
from utils.image_utils import ImageUtils

# Configure tracing instead of logging
configure_tracing_from_env()

# Initial logging (console + files); will be reconfigured after config load if agent_logging.enabled is set
Logger.configure(
    level="INFO",
    log_dir=str(get_default_logs_dir()),
    enable_console=False,
    enable_json=False,
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


def _context_to_compact_messages(messages) -> list[CompactMessage]:
    compact_messages: list[CompactMessage] = []
    for msg in messages:
        metadata = msg.metadata or {}
        timestamp = None
        if getattr(msg, "timestamp", None):
            try:
                timestamp = datetime.fromisoformat(msg.timestamp)
            except Exception:
                timestamp = None
        compact_messages.append(
            CompactMessage(
                role=msg.role,
                content=msg.content,
                message_id=metadata.get("message_id"),
                uuid=metadata.get("uuid"),
                timestamp=timestamp,
                metadata=metadata.copy(),
                is_compact_summary=bool(metadata.get("is_compact_summary")),
                is_compact_boundary=bool(metadata.get("is_compact_boundary")),
            )
        )
    return compact_messages


def _compact_to_context_messages(messages: list[CompactMessage]) -> list[ContextMessage]:
    context_messages: list[ContextMessage] = []
    for msg in messages:
        metadata = (msg.metadata or {}).copy()
        if msg.message_id:
            metadata.setdefault("message_id", msg.message_id)
        if msg.uuid:
            metadata.setdefault("uuid", msg.uuid)
        if msg.is_compact_summary:
            metadata["is_compact_summary"] = True
        if msg.is_compact_boundary:
            metadata["is_compact_boundary"] = True
        context_messages.append(
            ContextMessage(
                role=msg.role,
                content=msg.content,
                timestamp=msg.timestamp.isoformat() if msg.timestamp else datetime.now().isoformat(),
                metadata=metadata or None,
            )
        )
    return context_messages


def get_agent_skill_status(config: Config, agent_key: str) -> tuple[list[tuple[str, Path]], list[str]]:
    """Return declared system skills split into found and missing."""
    found: list[tuple[str, Path]] = []
    missing: list[str] = []

    try:
        agent_cfg = config.get_agent(agent_key)
    except Exception:
        return found, missing

    skills_dir = Path(config.config_path).parent / "skills"
    for skill_name in agent_cfg.system_skills:
        skill_path = None
        for ext in (".md", ".txt"):
            candidate = skills_dir / f"{skill_name}{ext}"
            if candidate.exists():
                skill_path = candidate
                break
        if skill_path is not None:
            found.append((skill_name, skill_path))
        else:
            missing.append(skill_name)

    return found, missing


def print_agent_skill_status(config: Config, agent_key: str) -> None:
    """Print startup summary of system skills for the selected agent."""
    found, missing = get_agent_skill_status(config, agent_key)
    declared_total = len(found) + len(missing)

    print("Skills")
    if declared_total == 0:
        print(f"Skills - Agent '{agent_key}' has no system_skills defined")
        return

    print(f"Skills - Agent '{agent_key}' declared skills: {declared_total}")
    if found:
        print("  Found:")
        for skill_name, skill_path in found:
            print(f"    - {skill_name}: {skill_path}")
    if missing:
        print("  Not found:")
        for skill_name in missing:
            print(f"    - {skill_name}")


async def main():
    """Main entry point."""
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
    parser.add_argument(
        "--user-id", "-u",
        type=str,
        default="default_user",
        help="User identifier for isolation"
    )
    parser.add_argument(
        "--timeline",
        action="store_true",
        help="Start embedded timeline dashboard (default: run separately via python -m timeline)",
    )
    
    args = parser.parse_args()
    
    try:
        # Beautiful initialization
        print("Starting Grid Agent System...")
        
        # Optional embedded timeline (normally: python -m timeline in another terminal)
        timeline_handle = None
        if args.timeline:
            try:
                from timeline.integration import run_timeline_server, TimelineHandle
                timeline_handle = TimelineHandle()
                asyncio.create_task(run_timeline_server(handle=timeline_handle, port=8789))
            except Exception as _tl_err:
                print(f"Timeline server not started: {_tl_err}")

        # Load configuration
        print("Load Config")
        config = Config(args.config, args.path)
        print("Load Config - Configuration loaded")

        # Container isolation (optional)
        # If enabled, we run tools (git/beads/mcp) inside a per-user container.
        container_id = None
        user_workspace = None
        try:
            isolation_cfg = getattr(config.config, "isolation", None)
            isolation_enabled = False
            if isolation_cfg:
                isolation_enabled = getattr(isolation_cfg, "enabled", False) if not isinstance(isolation_cfg, dict) else bool(isolation_cfg.get("enabled", False))

            if isolation_enabled:
                # Container workspace is separate from the agent's working_directory concept.
                # If user explicitly passed --path, use that path as-is;
                # otherwise use a fixed ./workspace/user_{id} base so containers
                # are never recreated just because working_directory config changed.
                if args.path is not None:
                    user_workspace = Path(args.path).resolve()
                else:
                    user_workspace = Path(config.config_path).resolve().parent / "workspace" / f"user_{args.user_id}"
                user_workspace.mkdir(parents=True, exist_ok=True)

                if ContainerManager:
                    cm = ContainerManager(config)
                    if cm.enabled:
                        container = cm.get_or_create_container(str(args.user_id), workspace=user_workspace)
                        if container:
                            container_id = container.id
                            print(f"🐳 Container isolation enabled: {container.name} ({container_id[:12]})")
                        else:
                            print("⚠️ Container isolation enabled in config, but container could not be created. Falling back to local tools.")
                    else:
                        print("⚠️ Container isolation enabled in config, but Docker client is unavailable. Falling back to local tools.")
                else:
                    print("⚠️ Container isolation enabled in config, but Docker SDK is unavailable. Falling back to local tools.")

                # Reload config with per-user working directory (keeps CLI behavior deterministic)
                config = Config(args.config, str(user_workspace))

        except Exception as e:
            print(f"⚠️ Failed to initialize container isolation: {e}. Falling back to local tools.")

        # Reconfigure logging from config (e.g. disable console when agent_logging.enabled is False)
        agent_logging = config.config.settings.agent_logging
        logs_dir = config.get_logs_directory()
        Logger.configure(
            level="INFO",
            log_dir=logs_dir,
            enable_console=False,
            enable_json=False,
            enable_legacy_logs=agent_logging.enabled,
            force_reconfigure=True,
        )
        Logger.configure_agent_logging(
            enabled=agent_logging.enabled,
            level=agent_logging.level,
            log_dir=logs_dir,
        )

        chat_ui = CliChatRenderer(enabled=True)
        stream_observer = ConsoleStreamObserver(
            render_text_deltas=False,
            renderer=chat_ui,
        )

        # Create factory
        print("Initialize SecurityAwareAgentFactory")
        factory = AgentFactory(
            config=config,
            working_directory=config.get_working_directory(),
            container_id=container_id,
            stream_observer=stream_observer,
        )
        print("Initialize SecurityAwareAgentFactory - Agent factory initialized")
        selected_context_id: Optional[str] = None
        last_context_id: Optional[str] = None
        # Update timeline dashboard with factory for rerun support
        if timeline_handle is not None:
            timeline_handle.update_factory(factory)

        def extract_context_id_from_text(text: Optional[str]) -> Optional[str]:
            if not text:
                return None
            match = re.search(r"ctx-[0-9a-f]{8}", text)
            return match.group(0) if match else None

        def is_context_id(value: Optional[str]) -> bool:
            return bool(value and re.fullmatch(r"ctx-[0-9a-f]{8}", value))


        # Tracing is configured automatically by Agents SDK

        # Determine agent
        agent_key = args.agent or config.get_default_agent()
        print_agent_skill_status(config, agent_key)

        activated_existing_context = False
        if is_context_id(args.context_path):
            try:
                selected_context_id = factory.activate_context(args.context_path)
                last_context_id = selected_context_id
                activated_existing_context = True
                print(f"Context - Activated saved context: {selected_context_id}")
            except Exception as exc:
                print(f"⚠️ Failed to activate context {args.context_path}: {exc}")

        if not activated_existing_context:
            print("Context - New session created, old contexts accessible by ID")
        
        # Let the timeline server task complete its startup (print URL, etc.)
        # before we enter interactive mode or single-message mode.
        await asyncio.sleep(0)
        
        print("Grid Agent System ready for work")
        
        
        chat_ui.print_banner(
            agent_key=agent_key,
            working_directory=config.get_working_directory(),
            context_path=args.context_path,
        )
        
        # ── Ctrl+C handling ───────────────────────────────────────────
        # Use asyncio's native signal handler so SIGINT cancels the current
        # task cleanly instead of raising KeyboardInterrupt at random places.
        _shutdown_flag = False
        _main_task = asyncio.current_task()

        def _on_sigint():
            nonlocal _shutdown_flag, _main_task
            if _shutdown_flag:
                print("\nForce exit...")
                os._exit(1)
            _shutdown_flag = True
            # Cancel the main task to trigger CancelledError cleanly
            if _main_task:
                _main_task.cancel()

        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, _on_sigint)
        except NotImplementedError:
            # Windows ProactorEventLoop supports subprocesses but not
            # add_signal_handler; use the regular signal module there.
            signal.signal(
                signal.SIGINT,
                lambda _signum, _frame: loop.call_soon_threadsafe(_on_sigint),
            )

        if args.message:
            # Single message mode
            print(f"Processing message")

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
                            print(f"   - {img_path} (error: {info['error']})")

                # Prepare message for agent
                agent_message = prepare_agent_message(text, image_paths)

                chat_ui.print_rule("Running")
                chat_ui.print_user_message(text)
                chat_ui.print_status(f"Agent: {agent_key}", style="cyan")

                start_time = time.time()
                use_streaming = True
                inline_context_id = extract_context_id_from_text(text)
                request_context_id = inline_context_id or selected_context_id
                use_active_context = request_context_id is None

                if request_context_id:
                    chat_ui.print_status(f"Context: {request_context_id}", style="bright_black")
                else:
                    chat_ui.print_status("Context: active session", style="bright_black")

                response = await factory.run_agent(
                    agent_key,
                    agent_message,
                    args.context_path,
                    context_id=request_context_id,
                    stream=use_streaming,
                    use_active_context=use_active_context,
                    user_id=args.user_id if hasattr(args, "user_id") else None
                )
                last_context_id = factory.get_active_context_id()
                duration = time.time() - start_time

                chat_ui.print_rule(f"Response in {duration:.2f}s")
                chat_ui.print_assistant_message(response, agent_name=agent_key)
                if last_context_id:
                    chat_ui.print_status(f"Context ID: {last_context_id}", style="bright_black")
                    selected_context_id = last_context_id

                # Show token count in context
                try:
                    _msgs = factory.context_manager._conversation_history
                    _tokens = estimate_messages_tokens(_context_to_compact_messages(_msgs)) if _msgs else 0
                    try:
                        _agent_cfg = config.get_agent(agent_key)
                        _model_cfg = config.get_model(_agent_cfg.model)
                        _ctx_window = getattr(_model_cfg, "context_window", None)
                    except Exception:
                        _ctx_window = None
                    if _ctx_window:
                        _pct = round(_tokens / max(1, _ctx_window) * 100, 1)
                        chat_ui.print_status(f"Tokens: ~{_tokens:,} / {_ctx_window:,} ({_pct}%)", style="bright_black")
                    else:
                        chat_ui.print_status(f"Tokens: ~{_tokens:,}", style="bright_black")
                except Exception:
                    pass

            except Exception as e:
                print("Operation completed")
                print(f"Error: {e}")
            except asyncio.CancelledError:
                print("\nInterrupted.")
        else:
            # Interactive mode
            print("\nCommands:")
            print("  'exit' or 'quit' - Exit")
            print("  'clear' - Start new context (old contexts saved)")
            print("  'context' or '/context' - Show current context info")
            print("  'contexts' or '/contexts' - List all saved context IDs")
            print("  'use <context_id>' - Switch to a saved context")
            print("  'compact' or '/compact' - Force context compaction (LLM summary)")
            print("  'help' - Show this help")
            print("\nContext IDs:")
            print("  Use context ID in message: 'ctx-abc12345 your message'")
            print("  Old contexts are automatically saved and accessible")
            print("\nImages:")
            print("  Use '&' to attach images: 'Your message & path/to/image.png'")
            print("  Multiple images: 'Message & image1.jpg & image2.png'")
            print("-" * 60)
            
            async def ainput(prompt: str = "") -> str:
                print(prompt, end="", flush=True)
                loop = asyncio.get_running_loop()
                import sys
                try:
                    line = await loop.run_in_executor(None, sys.stdin.readline)
                except asyncio.CancelledError:
                    # Ctrl+C in run_in_executor manifests as CancelledError,
                    # translate to KeyboardInterrupt for uniform handling.
                    raise KeyboardInterrupt()
                if not line:
                    raise EOFError
                return line.rstrip('\n')
            
            while True:
                try:
                    user_input = await ainput("\nYou: ")
                    user_input = user_input.strip()
                    
                    if user_input.lower() in ['exit', 'quit']:
                        print("Goodbye!")
                        break
                    elif user_input.lower() == 'clear':
                        print("Clear Context")
                        cleared_id = factory.clear_context()
                        selected_context_id = None
                        last_context_id = cleared_id
                        print("Clear Context - New context created")
                        print(f"New context ID: {cleared_id}")
                        print("Old contexts are saved and accessible by ID")
                        continue
                    elif user_input.lower() in {'context', '/context'}:
                        print("Get Context")
                        context_info = factory.get_context_info()
                        print("Get Context - Context information retrieved")

                        current_messages = factory.context_manager._conversation_history
                        compact_messages = _context_to_compact_messages(current_messages)
                        estimated_tokens = estimate_messages_tokens(compact_messages) if compact_messages else 0

                        context_window = None
                        context_pct = None
                        try:
                            agent_config = config.get_agent(agent_key)
                            model_cfg = config.get_model(agent_config.model)
                            context_window = getattr(model_cfg, "context_window", None)
                            if context_window:
                                context_pct = round((estimated_tokens / max(1, context_window)) * 100, 1)
                        except Exception:
                            pass
                        
                        print(f"\n📋 Context info:")
                        print(f"   Messages: {context_info.get('conversation_messages', 0)}")
                        print(f"   Execution history: {context_info.get('execution_history', 0)}")
                        print(f"   Memory usage: {context_info.get('memory_usage_mb', 0):.2f} MB")
                        print(f"   Estimated tokens: {estimated_tokens}")
                        if context_window:
                            print(f"   Model window: {context_window} tokens")
                            print(f"   Filled: {context_pct}%")
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
                            print(f"   Last message: {last_msg}")
                        continue
                    elif user_input.lower() in {'contexts', '/contexts'}:
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
                    elif user_input.lower() in {'/compact', 'compact'}:
                        print("Compacting context...")
                        try:
                            messages = factory.context_manager._conversation_history
                            if not messages:
                                print("Context is empty — compaction not needed.")
                                continue

                            compact_cfg = factory.config.config.compact
                            compact_messages = _context_to_compact_messages(messages)

                            tokens_before = estimate_messages_tokens(compact_messages)

                            # Get client and model for the current agent
                            compact_client, compact_model = factory._get_compact_client_and_model(agent_key)

                            result = await compact_conversation(
                                messages=compact_messages,
                                llm_client=compact_client,
                                model=compact_model,
                                suppress_followup_questions=False,
                                is_auto_compact=False,
                                compact_cfg=compact_cfg,
                            )

                            if not result.success():
                                print(
                                    result.user_display_message
                                    or "Compaction skipped: result does not reduce context size."
                                )
                                continue

                            from core.compact import run_post_compact_cleanup
                            factory.context_manager.replace_conversation_history(
                                _compact_to_context_messages(result.compacted_messages)
                            )
                            run_post_compact_cleanup()
                            factory._compact_tracking.consecutive_failures = 0

                            tokens_after = getattr(result, 'tokens_after', 0)
                            tokens_saved = getattr(result, 'tokens_saved', tokens_before - tokens_after)
                            print(
                                f"Compact complete: ~{tokens_before} -> ~{tokens_after} tokens, "
                                f"saved ~{tokens_saved}."
                            )
                        except Exception as ce:
                            print(f"Compact error: {ce}")
                        continue
                    elif user_input.lower() == 'help':
                        print("\nAvailable commands:")
                        print("  exit, quit - Exit the chat")
                        print("  clear - Start new context (old contexts saved)")
                        print("  context, /context - Show current context information")
                        print("  contexts, /contexts - List all saved context IDs")
                        print("  use <context_id> - Switch to a saved context")
                        print("  compact, /compact - Force context compaction (LLM summary)")
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
                                print(f"   - {img_path} (error: {info['error']})")

                    # Prepare message for agent
                    agent_message = prepare_agent_message(text, image_paths)

                    # Process user message with beautiful logging
                    try:
                        chat_ui.print_rule("Running")
                        chat_ui.print_user_message(text)
                        start_time = time.time()
                        use_streaming = True
                        inline_context_id = extract_context_id_from_text(text)
                        request_context_id = inline_context_id or selected_context_id
                        use_active_context = request_context_id is None

                        if request_context_id:
                            chat_ui.print_status(f"Context: {request_context_id}", style="bright_black")
                        else:
                            chat_ui.print_status("Context: active session", style="bright_black")

                        response = await factory.run_agent(
                            agent_key,
                            agent_message,
                            args.context_path,
                            context_id=request_context_id,
                            stream=use_streaming,
                            use_active_context=use_active_context,
                            user_id=args.user_id if hasattr(args, "user_id") else None
                        )
                        last_context_id = factory.get_active_context_id()
                        duration = time.time() - start_time

                        chat_ui.print_rule(f"Response in {duration:.2f}s")
                        chat_ui.print_assistant_message(response, agent_name=agent_key)
                        if last_context_id:
                            chat_ui.print_status(f"Context ID: {last_context_id}", style="bright_black")
                            selected_context_id = last_context_id

                        # Show token count in context
                        try:
                            _msgs = factory.context_manager._conversation_history
                            _tokens = estimate_messages_tokens(_context_to_compact_messages(_msgs)) if _msgs else 0
                            try:
                                _agent_cfg = config.get_agent(agent_key)
                                _model_cfg = config.get_model(_agent_cfg.model)
                                _ctx_window = getattr(_model_cfg, "context_window", None)
                            except Exception:
                                _ctx_window = None
                            if _ctx_window:
                                _pct = round(_tokens / max(1, _ctx_window) * 100, 1)
                                chat_ui.print_status(f"Tokens: ~{_tokens:,} / {_ctx_window:,} ({_pct}%)", style="bright_black")
                            else:
                                chat_ui.print_status(f"Tokens: ~{_tokens:,}", style="bright_black")
                        except Exception:
                            pass

                    except Exception as e:
                        print("Operation completed")
                        print(f"Error: {e}")
                    
                except KeyboardInterrupt:
                    print("\n\nInterrupted. Goodbye!")
                    break
                except asyncio.CancelledError:
                    print("\n\nInterrupted. Goodbye!")
                    break
                except EOFError:
                    print("\n\nEOF. Goodbye!")
                    break
        
        # Beautiful cleanup and session summary
        try:
            print("Cleanup")
            await factory.cleanup()
            print("Cleanup - Resources freed")
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nCleanup interrupted.")
        
        # Session summary
        print("Grid Agent System finished")
        
    except GridError as e:
        print(f"Grid Error: {e}")
        print(f"Grid Error: {e}")
        sys.exit(1)
    except asyncio.CancelledError:
        print("\nInterrupted.")
        sys.exit(0)
    except Exception as e:
        print(f"Unexpected error: {e}")
        print(f"Unexpected Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Clean exit on Ctrl+C — suppress the ugly asyncio.run() traceback.
        # The inner loop already printed "Interrupted. Goodbye!" so just exit.
        print()
        sys.exit(0)

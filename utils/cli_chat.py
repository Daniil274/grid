"""
Rich-powered CLI chat rendering helpers.
"""

from __future__ import annotations

from typing import Any, Optional

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.syntax import Syntax
    from rich.text import Text
except Exception:  # pragma: no cover - fallback when rich is unavailable
    Console = None  # type: ignore[assignment]
    Markdown = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Rule = None  # type: ignore[assignment]
    Syntax = None  # type: ignore[assignment]
    Text = None  # type: ignore[assignment]


class CliChatRenderer:
    """Small facade for pretty terminal chat output with graceful fallback."""

    def __init__(self, *, enabled: bool = True, force_terminal: Optional[bool] = None) -> None:
        self.enabled = bool(enabled and Console and Panel and Rule)
        self.console = (
            Console(force_terminal=force_terminal)
            if self.enabled
            else None
        )

    def print_banner(self, agent_key: str, working_directory: str, context_path: Optional[str] = None) -> None:
        if not self.enabled:
            print("=" * 60)
            print(f"Grid Chat | agent={agent_key}")
            print(f"Working directory: {working_directory}")
            if context_path:
                print(f"Context path: {context_path}")
            print("=" * 60)
            return

        body = Text()
        body.append("Agent: ", style="bold cyan")
        body.append(agent_key, style="white")
        body.append("\nWorking directory: ", style="bold cyan")
        body.append(working_directory, style="white")
        if context_path:
            body.append("\nContext path: ", style="bold cyan")
            body.append(context_path, style="white")
        self.console.print(
            Panel(
                body,
                title="Grid Chat",
                border_style="cyan",
                padding=(1, 2),
            )
        )

    def print_rule(self, title: str) -> None:
        if self.enabled:
            self.console.print(Rule(title, style="bright_black"))
        else:
            print(f"\n--- {title} ---")

    def print_user_message(self, message: str) -> None:
        if not self.enabled:
            print(f"\nYou: {message}")
            return

        self.console.print(
            Panel(
                message or " ",
                title="You",
                border_style="blue",
                padding=(0, 1),
            )
        )

    def print_assistant_message(self, message: str, agent_name: str = "Assistant") -> None:
        if not self.enabled:
            print(f"\n{agent_name}: {message}")
            return

        content = self._rich_content(message)
        self.console.print(
            Panel(
                content,
                title=agent_name,
                border_style="green",
                padding=(0, 1),
            )
        )

    def print_status(self, message: str, *, style: str = "yellow") -> None:
        if self.enabled:
            self.console.print(Text(message, style=style))
        else:
            print(message)

    def _tool_title(
        self,
        label: str,
        tool_name: str,
        *,
        agent_name: Optional[str] = None,
        duration: Optional[str] = None,
    ) -> str:
        parts = [label]
        if agent_name:
            parts.append(agent_name)
        if tool_name:
            parts.append(tool_name)
        if duration:
            parts.append(duration)
        return " | ".join(parts)

    def print_tool_call(
        self,
        tool_name: str,
        args_preview: str = "",
        *,
        agent_name: Optional[str] = None,
        duration: Optional[str] = None,
    ) -> None:
        text = f"{tool_name}"
        if args_preview:
            text += f"\n{args_preview}"
        title = self._tool_title("Tool", tool_name, agent_name=agent_name, duration=duration)
        if self.enabled:
            self.console.print(
                Panel(text, title=title, border_style="magenta", padding=(0, 1))
            )
        else:
            print(f"[tool] {text}")

    def print_tool_output(
        self,
        tool_name: str,
        output_preview: str,
        *,
        agent_name: Optional[str] = None,
        duration: Optional[str] = None,
    ) -> None:
        text = output_preview or "(empty)"
        title = self._tool_title("Tool Result", tool_name, agent_name=agent_name, duration=duration)
        if self.enabled:
            self.console.print(
                Panel(text, title=title, border_style="bright_black", padding=(0, 1))
            )
        else:
            print(f"[tool-result] {tool_name}: {text}")

    def print_handoff(self, source: str, target: str, *, completed: bool = False) -> None:
        arrow = "=>" if completed else "->"
        label = "Handoff Complete" if completed else "Handoff"
        text = f"{source} {arrow} {target}"
        if self.enabled:
            self.console.print(
                Panel(text, title=label, border_style="yellow", padding=(0, 1))
            )
        else:
            print(f"[handoff] {text}")

    def _rich_content(self, message: str) -> Any:
        if not self.enabled or not message.strip():
            return message

        stripped = message.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            lines = stripped.splitlines()
            first = lines[0][3:].strip()
            code = "\n".join(lines[1:-1])
            return Syntax(code, first or "text", word_wrap=True)

        return Markdown(message, code_theme="monokai", hyperlinks=True)

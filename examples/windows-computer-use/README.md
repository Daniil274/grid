# Windows Computer Use

Autonomous Windows desktop agent that can perceive and control any application on the user's machine.

## Architecture

### Hybrid perception (fast semantic + vision fallback)

| Method | When used | Latency | Token cost |
|--------|-----------|---------|------------|
| `uia_inspect` — Windows UI Automation tree | Primary: buttons, fields, menus | <1s | Zero |
| `screenshot(som=True)` — annotated view | Secondary: visually complex UI | 1–3s | Low |
| `screenshot()` — clean capture | Fallback: canvas, games, RDP | 1–3s | Medium |

The agent uses the accessibility tree as its first choice. Vision is only engaged
when UIA returns no elements (canvas apps, legacy software, remote desktop).

### Tools

| Tool | Purpose |
|------|---------|
| `uia_inspect` | Read accessibility tree → exact click coordinates |
| `screenshot` | Capture screen; `som=True` overlays numbered markers |
| `active_window` | Get title, position, size of foreground window |
| `list_windows` | Enumerate all visible top-level windows |
| `focus_window` | Bring a window to foreground by title |
| `mouse` | click / double_click / drag / scroll |
| `keyboard` | Type text (Unicode/Cyrillic safe) or press hotkeys |
| `run_command` | Execute PowerShell/CMD; destructive ops auto-blocked |
| `scratchpad_write/read/append` | Persistent session notepad for long tasks |

### ReAct cycle

Every non-trivial action follows **Perceive → Think → Act → Observe**:

1. `uia_inspect()` or `screenshot()` — understand current state
2. Reason about target element and action
3. Execute one atomic action (click / type / hotkey / command)
4. Verify result before next step

## Setup

```bash
pip install -r requirements.txt
```

Requirements: `PyAutoGUI`, `Pillow`, `pywin32` — all available on standard Python Windows installs.
UIA elements are read via PowerShell's built-in .NET `UIAutomationClient` — no extra packages needed.

## Run

```bash
cd examples/windows-computer-use
python ../../grid.py
```

## Key design decisions

- **UIA via PowerShell** — zero Python dependencies for accessibility; works on any Windows version.
- **Set-of-Marks** — numbered element overlays eliminate coordinate hallucination for complex UIs.
- **Scratchpad** — file-based working memory survives context compaction in long sessions.
- **Destructive command guard** — `run_command` auto-blocks known destructive patterns and requires user confirmation.
- **Positive system prompt** — all instructions state what TO DO, not what to avoid, reducing LLM confusion.

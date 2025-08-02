# Beautiful Grid Logging System 🎨

A rich, colorful logging system for the Grid Agent System that provides beautiful console output with tool tracking, todo management, and progress visualization.

## Features

### ✨ Rich Visual Output
- **Colorful symbols**: Different symbols and colors for each log level
- **Tool tracking**: Automatic tracking of tool operations with results
- **Todo management**: Dynamic todo list with status updates
- **Diff visualization**: Beautiful code diff display for file changes
- **Progress indicators**: Visual progress tracking for operations

### 🎯 Log Levels & Symbols

| Level | Symbol | Color | Usage |
|-------|--------|-------|-------|
| INFO | ● | Cyan | General information |
| SUCCESS | ✓ | Green | Successful operations |
| WARNING | ⚠ | Yellow | Warnings |
| ERROR | ✗ | Red | Errors |
| DEBUG | 🔍 | Gray | Debug information |
| TOOL | ◦ | Magenta | Tool operations |
| TODO | ☐ | Blue | Todo items |

### 📋 Todo Status Indicators

| Status | Symbol | Color | Description |
|--------|--------|-------|-------------|
| Pending | ☐ | Gray | Not started |
| In Progress | ◐ | Yellow | Currently working |
| Completed | ☒ | Green | Finished |

## Usage

### Basic Usage

```python
from utils.pretty_logger import PrettyLogger, update_todos

logger = PrettyLogger("my_module")

# Basic logging
logger.info("Starting operation")
logger.success("Operation completed")
logger.warning("Something to note")
logger.error("An error occurred")

# Tool operations
operation = logger.tool_start("Read", file_path="config.yaml")
logger.tool_result(operation, lines_read=360)

# Todo management
todos = [
    {"id": "1", "content": "Load configuration", "status": "pending", "priority": "high"},
    {"id": "2", "content": "Initialize system", "status": "in_progress", "priority": "high"},
    {"id": "3", "content": "Start processing", "status": "completed", "priority": "medium"}
]
update_todos(todos)
```

### Tool Operation Tracking

The logger automatically tracks tool operations and formats their results beautifully:

```python
# File operations
logger.tool_read("config.yaml", 360)
logger.tool_edit("core/config.py", old_string, new_string)
logger.tool_ls("/workspaces/grid", 26)

# Command execution
logger.tool_bash("python script.py", "Run test script", result="Success")

# Search operations
logger.tool_grep("import.*schemas", files_found=5, matches=12)
```

### Beautiful Agent Chat Interface

Use the enhanced chat interface for a beautiful experience:

```bash
# Single message
python agent_chat_pretty.py --message "Hello, how are you?"

# Interactive mode
python agent_chat_pretty.py

# With specific agent
python agent_chat_pretty.py --agent researcher
```

### Demo

Run the demo to see all features:

```bash
python demo_pretty_logs.py
```

## Example Output

```
● Loading Grid Agent System configuration...
◦ Config(path=config.yaml)
  ⎿  Configuration loaded successfully ctrl+r to expand
◦ AgentFactory
  ⎿  Agent factory initialized ctrl+r to expand
✓ Grid Agent System ready

☐ Update Todos
  ⎿  ◐ Process user message with coordinator
  ⎿  ☐ Generate response
  ⎿  ☐ Update conversation context

◦ Update(core/config.py)
  ⎿  Updated core/config.py with 3 additions and 3 removals
       11 -  from ..schemas import GridConfig
       12 -  from ..utils.exceptions import ConfigError
       13 -  from ..utils.logger import Logger
       11 +  from schemas import GridConfig
       12 +  from utils.exceptions import ConfigError
       13 +  from utils.logger import Logger

✓ Beautiful logging system ready!
```

## Integration

### With Existing Code

Replace standard logging calls:

```python
# Before
logger.info("Processing file")

# After  
from utils.pretty_logger import log_info
log_info("Processing file")
```

### With Agent Factory

The beautiful logger integrates seamlessly with the existing agent system and automatically tracks all operations.

## Colors

The system uses ANSI color codes that work in most modern terminals:
- **Cyan** (96m): Info messages
- **Green** (92m): Success messages  
- **Yellow** (93m): Warnings
- **Red** (91m): Errors
- **Magenta** (95m): Tool operations
- **Blue** (94m): Todo items
- **Gray** (90m): Debug/pending items

Colors can be disabled by setting `colors_enabled = False` on the logger instance.

## Files

- `utils/pretty_logger.py` - Main beautiful logger implementation
- `agent_chat_pretty.py` - Enhanced chat interface with beautiful logs
- `demo_pretty_logs.py` - Demonstration script
- `BEAUTIFUL_LOGS.md` - This documentation

## Tips

1. **Expandable content**: Look for "ctrl+r to expand" hints for detailed output
2. **Tool tracking**: Each tool operation shows a summary and duration
3. **Todo updates**: Keep todos updated to show progress visually
4. **Error handling**: Errors are highlighted in red with clear messages
5. **Performance**: The logger is designed to be fast and non-blocking
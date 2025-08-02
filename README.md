# Grid Agent System 🎯

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code Style: Black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**Enterprise-grade AI agent orchestration system with beautiful logging and comprehensive tooling.**

## ✨ Features

### 🎨 Beautiful Logging System
- **Rich visual output** with colorful symbols and formatting
- **Dynamic todo lists** with real-time progress tracking  
- **Tool operation monitoring** with detailed execution logs
- **Diff visualization** for code changes and file edits

### 🛠️ Comprehensive Tool Suite
- **File Operations**: Read, write, search, and analyze files with beautiful progress tracking
- **Git Integration**: Full Git workflow support with status monitoring and branch management
- **Agent Orchestration**: Coordinate multiple specialized agents for complex tasks
- **MCP Support**: Model Context Protocol integration for extended capabilities

### 🏗️ Enterprise Architecture
- **Modular Design**: Clean separation of concerns with pluggable components
- **Configuration Management**: YAML-based configuration with validation
- **Error Handling**: Comprehensive error handling with detailed logging
- **Testing**: Full test suite with pytest integration

## 🚀 Quick Start

### Prerequisites
- Python 3.12+
- Git (for version control operations)
- Virtual environment (recommended)

### Installation

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd grid
   ```

2. **Create virtual environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\\Scripts\\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure the system**
   ```bash
   cp config.yaml.example config.yaml
   # Edit config.yaml with your settings
   ```

5. **Set up environment**
   ```bash
   cp .env.example .env
   # Add your API keys to .env
   ```

### Basic Usage

**Interactive Chat Mode:**
```bash
python agent_chat.py
```

**Single Message:**
```bash
python agent_chat.py --message "List files in current directory"
```

**Specify Agent:**
```bash
python agent_chat.py --agent file_agent --message "Read config.yaml"
```

## 📁 Project Structure

```
grid/
├── core/                   # Core system components
│   ├── config.py          # Configuration management
│   ├── agent_factory.py   # Agent creation and orchestration
│   └── context.py         # Conversation context management
├── tools/                  # Tool implementations
│   ├── file_tools.py      # File operation tools
│   ├── git_tools.py       # Git operation tools
│   ├── function_tools.py  # Tool integration layer
│   └── mcp.py            # MCP protocol support
├── utils/                  # Utilities and helpers
│   ├── pretty_logger.py   # Beautiful logging system
│   ├── logger.py          # Standard logging
│   └── exceptions.py      # Custom exceptions
├── tests/                  # Test suite
├── schemas.py             # Pydantic data models
├── agent_chat.py          # Main chat interface
├── config.yaml           # System configuration
└── requirements.txt       # Python dependencies
```

## ⚙️ Configuration

### Agent Configuration
Configure agents in `config.yaml`:

```yaml
agents:
  file_agent:
    name: "File Agent"
    model: "gpt-4"
    tools: ["file_read", "file_write", "file_list"]
    description: "Specialized in file operations"
```

### Tool Configuration
Add custom tools:

```yaml
tools:
  custom_tool:
    type: "function"
    name: "my_custom_tool"
    description: "Custom tool description"
```

### Provider Setup
Configure AI providers:

```yaml
providers:
  openai:
    name: "OpenAI"
    base_url: "https://api.openai.com/v1"
    api_key_env: "OPENAI_API_KEY"
```

## 🔧 Available Tools

### File Operations
- `read_file(filepath)` - Read file contents with progress tracking
- `write_file(filepath, content)` - Write files with beautiful logging
- `list_files(directory)` - List directory contents with formatting
- `get_file_info(filepath)` - Get detailed file information
- `search_files(pattern, directory)` - Search files with regex support

### Git Operations  
- `git_status(directory)` - Show repository status with change tracking
- `git_log(directory)` - Display commit history
- `git_diff(directory, filename)` - Show file differences
- `git_add_file(directory, filename)` - Stage files for commit
- `git_commit(directory, message)` - Create commits

## 🎯 Example Usage

### File Operations with Beautiful Logging
```python
# The system automatically shows:
● Read(file_path=config.yaml)
  ⎿  Read 348 lines ctrl+r to expand

☐ Update Todos
  ⎿  ☒ Open file config.yaml  
  ⎿  ☒ Read content
  ⎿  ☒ Return result
```

### Git Operations with Progress Tracking
```python
# Shows detailed progress:
◦ GitStatus(directory=.)
  ⎿  Found 5 changes ctrl+r to expand

☐ Update Todos
  ⎿  ☒ Check repository .
  ⎿  ☒ Get Git status  
  ⎿  ☒ Format result
```

## 🧪 Testing

Run the test suite:
```bash
pytest tests/
```

Run with coverage:
```bash
pytest tests/ --cov=. --cov-report=html
```

## 📊 Monitoring

The system provides comprehensive monitoring:
- **Real-time operation tracking** with todo lists
- **Tool execution metrics** with timing information  
- **Error tracking** with detailed stack traces
- **Context management** with conversation history

## 🔒 Security

- **API key management** through environment variables
- **Input validation** with Pydantic schemas
- **Safe command execution** with subprocess protection
- **Path traversal protection** for file operations

## 🚦 Development

### Code Style
We use Black for code formatting:
```bash
black .
```

### Type Checking
Run mypy for type checking:
```bash
mypy .
```

### Contributing
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests
5. Run the test suite
6. Submit a pull request

## 📈 Performance

- **Async operations** for improved performance
- **Caching** for frequently accessed data
- **Connection pooling** for API calls
- **Lazy loading** for resource optimization

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🤝 Support

- **Documentation**: Check the `docs/` directory
- **Issues**: Report bugs via GitHub issues
- **Discussions**: Join GitHub discussions for questions

## 🎉 Acknowledgments

- Built with the [Agents](https://github.com/example/agents) framework
- Inspired by modern CLI tools and beautiful terminal interfaces
- Thanks to all contributors and the open-source community

---

**Grid Agent System** - Making AI agent orchestration beautiful and enterprise-ready. 🚀
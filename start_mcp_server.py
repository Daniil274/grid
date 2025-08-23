#!/usr/bin/env python3
"""
Удобный скрипт запуска Grid MCP Server.
Предоставляет доступ к Grid агентской системе через Model Context Protocol.
"""

import sys
import subprocess
import argparse
from pathlib import Path

def check_mcp_dependency():
    """Проверка наличия MCP зависимости."""
    try:
        import mcp
        return True
    except ImportError:
        return False

def install_mcp_dependency():
    """Установка MCP зависимости."""
    print("Installing MCP dependency...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "mcp>=1.0.0"])
        return True
    except subprocess.CalledProcessError:
        return False

def main():
    """Главная функция."""
    parser = argparse.ArgumentParser(description="Start Grid MCP Server")
    parser.add_argument(
        "--config", "-c",
        default="config.yaml", 
        help="Path to Grid configuration file (default: config.yaml)"
    )
    parser.add_argument(
        "--working-directory", "-w",
        help="Working directory for Grid system"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level (default: INFO)"
    )
    parser.add_argument(
        "--install-deps", 
        action="store_true",
        help="Install MCP dependencies if missing"
    )
    
    args = parser.parse_args()
    
    # Проверка зависимостей
    if not check_mcp_dependency():
        print("❌ MCP dependency not found!")
        if args.install_deps:
            print("Installing MCP dependency...")
            if install_mcp_dependency():
                print("✅ MCP dependency installed successfully")
            else:
                print("❌ Failed to install MCP dependency")
                return 1
        else:
            print("Please install MCP dependency:")
            print("  pip install mcp>=1.0.0")
            print("Or run with --install-deps flag to install automatically")
            return 1
    
    # Проверка конфигурационного файла
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"❌ Configuration file not found: {config_path}")
        print("Make sure you have config.yaml in the current directory")
        return 1
    
    # Подготовка команды запуска
    cmd = [
        sys.executable, 
        str(Path(__file__).parent / "mcp_server.py"),
        "--config", args.config,
        "--log-level", args.log_level
    ]
    
    if args.working_directory:
        cmd.extend(["--working-directory", args.working_directory])
    
    # Информация о запуске
    print("🚀 Starting Grid MCP Server...")
    print(f"📁 Config file: {config_path.resolve()}")
    if args.working_directory:
        print(f"📁 Working directory: {args.working_directory}")
    print(f"📊 Log level: {args.log_level}")
    print("-" * 50)
    print("To use this MCP server with Claude Code:")
    print("1. Add to your MCP client configuration:")
    print(f'   "grid-agents": {{')
    print(f'     "command": "python",')
    print(f'     "args": ["{Path(__file__).parent.resolve() / "mcp_server.py"}"]')
    print(f'   }}')
    print("2. Or use the start script directly:")
    print(f'   "grid-agents": {{')
    print(f'     "command": "python",')
    print(f'     "args": ["{Path(__file__).resolve()}", "--config", "{config_path.resolve()}"]')
    print(f'   }}')
    print("-" * 50)
    print("Available MCP tools after connection:")
    print("  - run_agent_<agent_name>: Run specific Grid agents")
    print("  - list_grid_agents: List all available agents")
    print("  - get_grid_status: Get system status")
    print("  - clear_grid_context: Clear conversation context")
    print("=" * 50)
    
    # Запуск MCP сервера
    try:
        subprocess.run(cmd, check=True)
    except KeyboardInterrupt:
        print("\n👋 Grid MCP Server stopped by user")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"❌ Grid MCP Server failed with exit code: {e.returncode}")
        return e.returncode
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
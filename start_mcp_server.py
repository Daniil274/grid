#!/usr/bin/env python3
"""
Скрипт запуска Grid MCP Server
"""

import sys
import os
import subprocess
from pathlib import Path

def check_dependencies():
    """Проверка наличия необходимых зависимостей."""
    try:
        import mcp
        import yaml
        import openai
        print("✅ Все зависимости найдены")
        return True
    except ImportError as e:
        print(f"❌ Отсутствует зависимость: {e}")
        print("Установите зависимости: pip install -r requirements.txt")
        return False

def check_config():
    """Проверка наличия файла конфигурации."""
    config_path = Path("config.yaml")
    if config_path.exists():
        print("✅ Файл конфигурации найден")
        return True
    else:
        print("❌ Файл конфигурации не найден: config.yaml")
        example_path = Path("config.yaml.example")
        if example_path.exists():
            print("💡 Найден config.yaml.example - скопируйте его в config.yaml")
        return False

def main():
    """Главная функция."""
    print("🚀 Запуск Grid Agent System MCP Server")
    print("=" * 50)
    
    # Проверяем зависимости
    if not check_dependencies():
        sys.exit(1)
    
    # Проверяем конфигурацию
    if not check_config():
        sys.exit(1)
    
    print("🌟 Инициализация завершена")
    print("📡 Запуск MCP сервера...")
    print("=" * 50)
    
    try:
        # Запускаем HTTP MCP сервер (более стабильный)
        result = subprocess.run([
            sys.executable, "http_mcp_server.py"
        ], cwd=Path.cwd())
        
        if result.returncode != 0:
            print("❌ MCP сервер завершился с ошибкой")
            sys.exit(result.returncode)
            
    except KeyboardInterrupt:
        print("\n👋 MCP сервер остановлен пользователем")
    except Exception as e:
        print(f"❌ Ошибка запуска: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main() 
#!/usr/bin/env python3
"""
Скрипт настройки Grid MCP Server
"""

import sys
import subprocess
import os
from pathlib import Path

def install_dependencies():
    """Установка MCP зависимостей."""
    print("📦 Устанавливаю MCP зависимости...")
    
    try:
        # Обновляем pip
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], check=True)
        
        # Устанавливаем зависимости
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], check=True)
        
        print("✅ Зависимости установлены успешно")
        return True
        
    except subprocess.CalledProcessError as e:
        print(f"❌ Ошибка установки зависимостей: {e}")
        return False

def setup_config():
    """Настройка конфигурации."""
    config_path = Path("config.yaml")
    example_path = Path("config.yaml.example")
    
    if config_path.exists():
        print("✅ Файл конфигурации уже существует")
        return True
    
    if example_path.exists():
        print("📋 Копирую пример конфигурации...")
        config_path.write_text(example_path.read_text(encoding='utf-8'), encoding='utf-8')
        print("✅ Конфигурация создана из примера")
        print("💡 Отредактируйте config.yaml для ваших настроек")
        return True
    else:
        print("❌ Файл config.yaml.example не найден")
        return False

def test_setup():
    """Тестирование настройки."""
    print("🧪 Тестирую настройку...")
    
    try:
        # Проверяем импорты
        import mcp
        from core.config import Config
        from core.security_agent_factory import SecurityAwareAgentFactory
        
        # Пробуем загрузить конфигурацию
        config = Config("config.yaml")
        agents = config.list_agents()
        
        print(f"✅ Найдено {len(agents)} агентов:")
        for key, desc in agents.items():
            print(f"  • {key}: {desc}")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка тестирования: {e}")
        return False

def main():
    """Главная функция настройки."""
    print("🛠️  Настройка Grid Agent System MCP Server")
    print("=" * 60)
    
    # Устанавливаем зависимости
    if not install_dependencies():
        sys.exit(1)
    
    # Настраиваем конфигурацию
    if not setup_config():
        sys.exit(1)
    
    # Тестируем настройку
    if not test_setup():
        sys.exit(1)
    
    print("=" * 60)
    print("🎉 Настройка завершена успешно!")
    print("📝 Следующие шаги:")
    print("   1. Отредактируйте config.yaml при необходимости")
    print("   2. Запустите: python start_mcp_server.py")
    print("   3. Подключите к MCP клиенту (Claude Desktop, Cursor, etc.)")

if __name__ == "__main__":
    main() 
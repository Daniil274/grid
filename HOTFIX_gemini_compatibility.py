#!/usr/bin/env python3
"""
HOTFIX: Исправление совместимости имен инструментов с Gemini API

Проблема: Gemini API требует строгого формата имен инструментов
- Должны начинаться с буквы или подчеркивания
- Только a-z, A-Z, 0-9, _, ., -
- Максимум 64 символа

Этот hotfix добавляет санитизацию имен инструментов для Gemini.
"""

import re
from typing import List, Any
from core.agent_factory import AgentFactory
from core.config import Config


def sanitize_tool_name_for_gemini(name: str) -> str:
    """
    Санитизирует имя инструмента для совместимости с Gemini API.
    
    Args:
        name: Исходное имя инструмента
    
    Returns:
        Санитизированное имя, совместимое с Gemini API
    """
    if not name:
        return "unknown_tool"
    
    # Заменяем недопустимые символы на подчеркивания
    sanitized = re.sub(r'[^a-zA-Z0-9_.-]', '_', name)
    
    # Убеждаемся, что начинается с буквы или подчеркивания
    if sanitized and not (sanitized[0].isalpha() or sanitized[0] == '_'):
        sanitized = f"tool_{sanitized}"
    
    # Ограничиваем длину до 64 символов
    if len(sanitized) > 64:
        sanitized = sanitized[:60] + "_trunc"
    
    # Если получилась пустая строка, возвращаем default
    if not sanitized:
        sanitized = "tool_function"
    
    return sanitized


def add_missing_config_method():
    """Добавляет отсутствующий метод get_all_models в класс Config."""
    
    def get_all_models(self):
        """Возвращает все настроенные модели."""
        return self.config.get('models', {})
    
    def get_all_agents(self):
        """Возвращает всех настроенных агентов."""
        return self.config.get('agents', {})
    
    # Добавляем методы в класс Config
    Config.get_all_models = get_all_models
    Config.get_all_agents = get_all_agents
    
    print("✅ Добавлены отсутствующие методы в класс Config")


def patch_agent_factory_for_gemini():
    """Патчит AgentFactory для совместимости с Gemini API."""
    
    # Сохраняем оригинальный метод
    original_create_context_aware_agent_tool = AgentFactory._create_context_aware_agent_tool
    
    def patched_create_context_aware_agent_tool(
        self,
        sub_agent,
        tool_name: str,
        tool_description: str,
        context_strategy: str = "minimal",
        context_depth: int = 5,
        include_tool_history: bool = False
    ):
        """Патченая версия с санитизацией имен инструментов."""
        
        # Санитизируем имя инструмента для Gemini
        sanitized_name = sanitize_tool_name_for_gemini(tool_name)
        
        if sanitized_name != tool_name:
            print(f"🔧 Санитизация имени инструмента: '{tool_name}' → '{sanitized_name}'")
        
        # Вызываем оригинальный метод с санитизированным именем
        return original_create_context_aware_agent_tool(
            self,
            sub_agent,
            sanitized_name,
            tool_description,
            context_strategy,
            context_depth,
            include_tool_history
        )
    
    # Заменяем метод в классе
    AgentFactory._create_context_aware_agent_tool = patched_create_context_aware_agent_tool
    
    print("✅ AgentFactory пропатчен для совместимости с Gemini API")


def apply_hotfix():
    """Применяет все исправления."""
    print("🚀 Применение HOTFIX для совместимости с Gemini API...")
    
    try:
        # Исправление 1: Добавляем отсутствующие методы в Config
        add_missing_config_method()
        
        # Исправление 2: Патчим AgentFactory для санитизации имен
        patch_agent_factory_for_gemini()
        
        print("✅ HOTFIX успешно применен!")
        print("📝 Рекомендуется внести эти изменения в основной код")
        
        return True
        
    except Exception as e:
        print(f"❌ Ошибка применения HOTFIX: {e}")
        return False


if __name__ == "__main__":
    success = apply_hotfix()
    
    if success:
        print("\n🧪 Запуск повторного QA тестирования...")
        import subprocess
        import sys
        
        try:
            # Запускаем повторное тестирование
            result = subprocess.run([
                sys.executable, "tests/qa_test_runner.py"
            ], capture_output=True, text=True, timeout=300)
            
            if result.returncode == 0:
                print("✅ Повторное QA тестирование прошло успешно!")
            else:
                print("⚠️ Остались проблемы в QA тестировании")
                print("STDOUT:", result.stdout[-500:])  # Последние 500 символов
                print("STDERR:", result.stderr[-500:])
                
        except subprocess.TimeoutExpired:
            print("⏰ Тестирование превысило таймаут 5 минут")
        except Exception as e:
            print(f"❌ Ошибка запуска повторного тестирования: {e}")
    
    else:
        print("💥 HOTFIX не удалось применить. Проверьте ошибки выше.")
        sys.exit(1)
"""
Пакет тестирования системы Grid.

Содержит:
- test_framework.py: Базовый фреймворк тестирования
- test_agents.py: Тесты агентов
- test_tools.py: Тесты инструментов  
- test_context.py: Тесты системы контекста
- test_integration.py: Интеграционные тесты
- config_test.yaml: Тестовая конфигурация
- mock_tools.py: Мок инструменты
- run_tests.py: Запускающий скрипт

Использование:
    python tests/run_tests.py
    или
    pytest tests/ -v
"""

__version__ = "1.0.0"
__author__ = "Grid AI Team"

# Импорты для удобства использования
from .test_framework import TestEnvironment, AgentTestCase, AgentTestSuite
from .mock_tools import reset_mock_state, setup_mock_data

__all__ = [
    "TestEnvironment",
    "AgentTestCase", 
    "AgentTestSuite",
    "reset_mock_state",
    "setup_mock_data"
]
"""
Конфигурация pytest и общие фикстуры для тестов Grid.
"""

import pytest
import asyncio
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, AsyncMock

from core.config import Config
from core.agent_factory import AgentFactory
from tests.test_framework import AgentTestEnvironment


@pytest.fixture
def event_loop():
    """Создает новый event loop для каждого теста."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def test_environment():
    """Фикстура для создания изолированной тестовой среды."""
    async with AgentTestEnvironment() as env:
        yield env


@pytest.fixture
def temp_config_file():
    """Создает временный конфигурационный файл для тестов."""
    config_content = """
settings:
  default_agent: "test_simple_agent"
  max_history: 10
  max_turns: 5
  agent_timeout: 30
  debug: true
  mcp_enabled: false

agents:
  test_simple_agent:
    name: "Тестовый простой агент"
    model: "test_model"
    tools: []
    
  test_file_agent:
    name: "Тестовый файловый агент"
    model: "test_model" 
    tools: ["file_read", "file_write"]

models:
  test_model:
    provider: "mock"
    model: "mock-model"
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(config_content)
        temp_file = f.name
    
    yield temp_file
    
    # Очистка
    if os.path.exists(temp_file):
        os.unlink(temp_file)


@pytest.fixture
def mock_openai_client():
    """Мок OpenAI клиента для тестов."""
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value={
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "Тестовый ответ"
                }
            }]
        }
    )
    
    with patch('core.agent_factory.AsyncOpenAI', return_value=mock_client):
        yield mock_client


@pytest.fixture
def temp_working_directory():
    """Создает временную рабочую директорию для тестов."""
    temp_dir = tempfile.mkdtemp(prefix="grid_test_")
    yield temp_dir
    
    # Очистка
    import shutil
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)


# Маркеры для категоризации тестов
def pytest_configure(config):
    """Настройка pytest с определением маркеров."""
    config.addinivalue_line("markers", "unit: marks tests as unit tests")
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
    config.addinivalue_line("markers", "security: marks tests as security-related")
    config.addinivalue_line("markers", "api: marks tests as API tests")
    config.addinivalue_line("markers", "performance: marks tests as performance tests")
    config.addinivalue_line("markers", "slow: marks tests as slow running")


def pytest_collection_modifyitems(config, items):
    """Модифицирует коллекцию тестов, добавляя маркеры автоматически."""
    for item in items:
        # Автоматически добавляем маркеры на основе имени файла
        if "test_security" in str(item.fspath):
            item.add_marker(pytest.mark.security)
        elif "test_api" in str(item.fspath):
            item.add_marker(pytest.mark.api)
        elif "test_integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
        elif "performance" in str(item.fspath) or "performance" in item.name:
            item.add_marker(pytest.mark.performance)
        
        # Медленные тесты
        if "slow" in item.name or "stress" in item.name:
            item.add_marker(pytest.mark.slow)


# Пропуск тестов при отсутствии зависимостей
def pytest_runtest_setup(item):
    """Настройка перед запуском каждого теста."""
    # Пропускаем integration тесты если не установлены зависимости
    if item.get_closest_marker("integration"):
        try:
            import fastapi
            import uvicorn
        except ImportError:
            pytest.skip("Integration тесты требуют установки API зависимостей")
    
    # Пропускаем security тесты если модули недоступны
    if item.get_closest_marker("security"):
        try:
            # Проверяем наличие security модулей
            from security_agents import security_guardian
        except ImportError:
            pytest.skip("Security тесты требуют наличия security_agents модулей")


# Логирование тестов
@pytest.fixture(autouse=True)
def test_logging(caplog):
    """Автоматическая настройка логирования для всех тестов."""
    import logging
    caplog.set_level(logging.WARNING)  # Показываем только предупреждения и ошибки
    yield caplog


# Очистка после тестов
@pytest.fixture(autouse=True)
async def cleanup_after_test():
    """Автоматическая очистка после каждого теста."""
    yield
    
    # Очищаем временные файлы
    temp_files = Path(".").glob("test_*.tmp")
    for temp_file in temp_files:
        try:
            temp_file.unlink()
        except FileNotFoundError:
            pass 
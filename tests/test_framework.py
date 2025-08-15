"""
Тестовый фреймворк для системы агентов Grid.
Предоставляет инфраструктуру для тестирования агентов, инструментов и контекста.
"""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, Union
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from core.config import Config
from core.agent_factory import AgentFactory
from core.context import ContextManager
from schemas import AgentExecution


class MockProvider:
    """Мок провайдера для тестирования."""
    
    def __init__(self, responses: List[str] = None):
        self.responses = responses or ["Тестовый ответ"]
        self.call_count = 0
        self.last_messages = []
    
    async def chat_completion(self, messages: List[Dict], **kwargs) -> Dict:
        """Имитирует ответ от API."""
        self.last_messages = messages
        response = self.responses[self.call_count % len(self.responses)]
        self.call_count += 1
        
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": response
                }
            }]
        }


class TestEnvironment:
    """Изолированная тестовая среда."""
    
    def __init__(self, config_path: str = None):
        self.config_path = config_path or "tests/config_test.yaml"
        self.temp_dir = None
        self.config = None
        self.agent_factory = None
        self.mock_provider = MockProvider()
        
    async def __aenter__(self):
        """Инициализация тестовой среды."""
        # Создаем временную директорию
        self.temp_dir = tempfile.mkdtemp(prefix="grid_test_")
        
        # Загружаем тестовую конфигурацию
        self.config = Config(config_path=self.config_path)
        self.config.set_working_directory(self.temp_dir)
        
        # Создаем агентную фабрику
        self.agent_factory = AgentFactory(self.config, self.temp_dir)
        
        # Мокаем OpenAI клиент
        self._setup_mocks()
        
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Очистка тестовой среды."""
        if self.agent_factory:
            await self.agent_factory.cleanup()
        
        # Останавливаем патчеры
        if hasattr(self, '_openai_patcher'):
            self._openai_patcher.stop()
        if hasattr(self, '_runner_patcher'):
            self._runner_patcher.stop()
        
        # Очищаем временную директорию
        if self.temp_dir and os.path.exists(self.temp_dir):
            import shutil
            shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def _setup_mocks(self):
        """Настройка моков для тестирования."""
        # Мокаем OpenAI клиент
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=self.mock_provider.chat_completion
        )
        
        # Патчим создание клиента в AgentFactory
        patcher = patch('core.agent_factory.AsyncOpenAI', return_value=mock_client)
        patcher.start()
        
        # Мокаем agents.Runner.run для возврата правильного результата
        mock_result = MagicMock()
        mock_result.final_output = "Тестовый ответ агента"
        
        async def mock_runner_run(*args, **kwargs):
            return mock_result
        
        runner_patcher = patch('core.agent_factory.Runner.run', side_effect=mock_runner_run)
        runner_patcher.start()
        
        # Сохраняем патчеры для очистки
        self._openai_patcher = patcher
        self._runner_patcher = runner_patcher
    
    def set_mock_responses(self, responses: List[str]):
        """Устанавливает ответы мок провайдера."""
        self.mock_provider.responses = responses
        self.mock_provider.call_count = 0
    
    def get_mock_calls(self) -> List[Dict]:
        """Возвращает историю вызовов мок провайдера."""
        return self.mock_provider.last_messages
    
    def create_test_file(self, filename: str, content: str) -> str:
        """Создает тестовый файл."""
        filepath = os.path.join(self.temp_dir, filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        return filepath


class AgentTestCase:
    """Базовый класс для тестовых сценариев агентов."""
    
    def __init__(self, name: str, agent_key: str, description: str = ""):
        self.name = name
        self.agent_key = agent_key
        self.description = description
        self.test_steps: List[Callable] = []
        self.setup_steps: List[Callable] = []
        self.teardown_steps: List[Callable] = []
    
    def setup(self, step: Callable):
        """Добавляет шаг подготовки."""
        self.setup_steps.append(step)
        return self
    
    def test(self, step: Callable):
        """Добавляет тестовый шаг."""
        self.test_steps.append(step)
        return self
    
    def teardown(self, step: Callable):
        """Добавляет шаг очистки."""
        self.teardown_steps.append(step)
        return self
    
    async def run(self, env: TestEnvironment) -> 'TestResult':
        """Выполняет тестовый сценарий."""
        result = TestResult(self.name)
        
        try:
            # Выполняем шаги подготовки
            for setup_step in self.setup_steps:
                await setup_step(env)
            
            # Выполняем тестовые шаги
            for test_step in self.test_steps:
                step_result = await test_step(env)
                if isinstance(step_result, dict):
                    result.add_step_result(step_result)
            
            result.success = True
            
        except Exception as e:
            result.success = False
            result.error = str(e)
            result.exception = e
        
        finally:
            # Выполняем шаги очистки
            for teardown_step in self.teardown_steps:
                try:
                    await teardown_step(env)
                except Exception as e:
                    result.warnings.append(f"Teardown warning: {e}")
        
        return result


class TestResult:
    """Результат выполнения теста."""
    
    def __init__(self, test_name: str):
        self.test_name = test_name
        self.success = False
        self.error: Optional[str] = None
        self.exception: Optional[Exception] = None
        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.step_results: List[Dict] = []
        self.warnings: List[str] = []
    
    def add_step_result(self, result: Dict):
        """Добавляет результат шага."""
        self.step_results.append(result)
    
    def finish(self):
        """Завершает тест."""
        self.end_time = time.time()
    
    @property
    def duration(self) -> float:
        """Длительность выполнения теста."""
        end = self.end_time or time.time()
        return end - self.start_time
    
    def to_dict(self) -> Dict:
        """Конвертирует результат в словарь."""
        return {
            "test_name": self.test_name,
            "success": self.success,
            "error": self.error,
            "duration": self.duration,
            "step_results": self.step_results,
            "warnings": self.warnings
        }


class AgentTestSuite:
    """Набор тестов для агентов."""
    
    def __init__(self, name: str, config_path: str = None):
        self.name = name
        self.config_path = config_path
        self.test_cases: List[AgentTestCase] = []
        self.results: List[TestResult] = []
    
    def add_test(self, test_case: AgentTestCase):
        """Добавляет тестовый случай."""
        self.test_cases.append(test_case)
    
    async def run_all(self) -> List[TestResult]:
        """Выполняет все тесты."""
        self.results = []
        
        for test_case in self.test_cases:
            async with TestEnvironment(self.config_path) as env:
                result = await test_case.run(env)
                result.finish()
                self.results.append(result)
        
        return self.results
    
    def get_summary(self) -> Dict:
        """Возвращает сводку результатов."""
        total = len(self.results)
        passed = sum(1 for r in self.results if r.success)
        failed = total - passed
        
        return {
            "suite_name": self.name,
            "total_tests": total,
            "passed": passed,
            "failed": failed,
            "success_rate": passed / total if total > 0 else 0,
            "total_duration": sum(r.duration for r in self.results),
            "results": [r.to_dict() for r in self.results]
        }


# Утилиты для создания тестов

async def test_basic_agent_response(env: TestEnvironment, agent_key: str, message: str, expected_keywords: List[str] = None) -> Dict:
    """Базовый тест ответа агента."""
    start_time = time.time()
    
    response = await env.agent_factory.run_agent(agent_key, message)
    
    duration = time.time() - start_time
    
    # Проверяем, что ответ не пустой
    assert response and response.strip(), "Ответ агента пуст"
    
    # Проверяем ключевые слова если указаны
    if expected_keywords:
        response_lower = response.lower()
        for keyword in expected_keywords:
            assert keyword.lower() in response_lower, f"Ключевое слово '{keyword}' не найдено в ответе"
    
    return {
        "step_type": "basic_response",
        "agent_key": agent_key,
        "message": message,
        "response": response,
        "duration": duration,
        "response_length": len(response)
    }


async def test_tool_usage(env: TestEnvironment, agent_key: str, message: str, expected_tools: List[str] = None) -> Dict:
    """Тест использования инструментов агентом."""
    start_time = time.time()
    
    # Очищаем историю перед тестом
    env.agent_factory.clear_context()
    
    response = await env.agent_factory.run_agent(agent_key, message)
    
    # Получаем последнее выполнение
    executions = env.agent_factory.get_recent_executions(1)
    execution = executions[0] if executions else None
    
    duration = time.time() - start_time
    
    # Проверяем использование инструментов
    if expected_tools and execution:
        tools_used = execution.tools_used or []
        for tool in expected_tools:
            assert tool in tools_used, f"Ожидаемый инструмент '{tool}' не был использован"
    
    return {
        "step_type": "tool_usage",
        "agent_key": agent_key,
        "message": message,
        "response": response,
        "tools_used": execution.tools_used if execution else [],
        "duration": duration
    }


async def test_context_retention(env: TestEnvironment, agent_key: str, messages: List[str]) -> Dict:
    """Тест сохранения контекста между сообщениями."""
    responses = []
    
    for i, message in enumerate(messages):
        response = await env.agent_factory.run_agent(agent_key, message)
        responses.append(response)
        
        # Проверяем, что контекст сохраняется
        if i > 0:
            context_info = env.agent_factory.get_context_info()
            assert context_info.get("message_count", 0) > i, "Контекст не сохраняется между сообщениями"
    
    return {
        "step_type": "context_retention",
        "agent_key": agent_key,
        "messages": messages,
        "responses": responses,
        "final_context_info": env.agent_factory.get_context_info()
    }
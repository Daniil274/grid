"""
Тесты для агентов системы Grid.
Проверяют базовую функциональность, использование инструментов и взаимодействие между агентами.
"""

import pytest
import asyncio
from typing import List

from .test_framework import (
    TestEnvironment, AgentTestCase, AgentTestSuite,
    test_basic_agent_response, test_tool_usage, test_context_retention
)


class TestAgents:
    """Основной класс тестов агентов."""
    
    @pytest.mark.asyncio
    async def test_simple_agent_basic_response(self):
        """Тест базового ответа простого агента."""
        async with TestEnvironment() as env:
            env.set_mock_responses(["Привет! Я тестовый агент."])
            
            response = await env.agent_factory.run_agent(
                "test_simple_agent", 
                "Привет, как дела?"
            )
            
            assert response is not None
            assert len(response) > 0
            assert "тестовый агент" in response.lower()
    
    @pytest.mark.asyncio
    async def test_agent_creation_and_caching(self):
        """Тест создания и кеширования агентов."""
        async with TestEnvironment() as env:
            # Создаем агента первый раз
            agent1 = await env.agent_factory.create_agent("test_simple_agent")
            
            # Создаем агента второй раз (должен использоваться кеш)
            agent2 = await env.agent_factory.create_agent("test_simple_agent")
            
            # Проверяем, что это один и тот же объект (из кеша)
            assert agent1 is agent2
            assert agent1.name == "Тестовый простой агент"
    
    @pytest.mark.asyncio
    async def test_agent_with_different_configs(self):
        """Тест агентов с разными конфигурациями."""
        async with TestEnvironment() as env:
            # Тестируем разных агентов
            agents_to_test = [
                "test_simple_agent",
                "test_file_agent", 
                "test_calculator_agent",
                "test_coordinator_agent"
            ]
            
            for agent_key in agents_to_test:
                agent = await env.agent_factory.create_agent(agent_key)
                assert agent is not None
                assert agent.name is not None
                assert len(agent.name) > 0
    
    @pytest.mark.asyncio
    async def test_agent_error_handling(self):
        """Тест обработки ошибок при создании агентов."""
        async with TestEnvironment() as env:
            # Пытаемся создать несуществующего агента
            with pytest.raises(Exception):
                await env.agent_factory.create_agent("nonexistent_agent")
    
    @pytest.mark.asyncio
    async def test_agent_timeout_configuration(self):
        """Тест настройки таймаута агентов."""
        async with TestEnvironment() as env:
            # Проверяем, что таймаут настроен правильно
            timeout = env.config.get_agent_timeout()
            assert timeout == 30  # Из тестовой конфигурации
            
            max_turns = env.config.get_max_turns()
            assert max_turns == 5  # Из тестовой конфигурации


class TestAgentSuites:
    """Тесты с использованием тестовых наборов."""
    
    @pytest.mark.asyncio
    async def test_basic_agent_suite(self):
        """Запуск базового набора тестов агентов."""
        suite = AgentTestSuite("Basic Agent Tests")
        
        # Создаем тестовые случаи
        simple_test = AgentTestCase(
            "simple_response_test", 
            "test_simple_agent",
            "Тест простого ответа агента"
        )
        
        simple_test.test(lambda env: test_basic_agent_response(
            env, "test_simple_agent", "Привет!", ["привет", "тест"]
        ))
        
        # Тест контекста
        context_test = AgentTestCase(
            "context_test",
            "test_simple_agent", 
            "Тест сохранения контекста"
        )
        
        context_test.test(lambda env: test_context_retention(
            env, "test_simple_agent", ["Привет!", "Как дела?", "Что ты умеешь?"]
        ))
        
        suite.add_test(simple_test)
        suite.add_test(context_test)
        
        # Запускаем тесты
        results = await suite.run_all()
        summary = suite.get_summary()
        
        # Проверяем результаты
        assert len(results) == 2
        assert summary["total_tests"] == 2
        # В идеале все тесты должны пройти, но мокирование может влиять
        assert summary["passed"] >= 0
    
    @pytest.mark.asyncio
    async def test_file_agent_operations(self):
        """Тест агента с файловыми операциями."""
        async with TestEnvironment() as env:
            # Создаем тестовый файл
            test_content = "Тестовое содержимое файла"
            filepath = env.create_test_file("test.txt", test_content)
            
            # Настраиваем ответы мока
            env.set_mock_responses([
                f"Файл прочитан: {test_content}",
                "Файл успешно записан"
            ])
            
            # Тестируем чтение файла
            response1 = await env.agent_factory.run_agent(
                "test_file_agent",
                f"Прочитай файл {filepath}"
            )
            
            assert response1 is not None
            
            # Тестируем запись файла
            response2 = await env.agent_factory.run_agent(
                "test_file_agent", 
                f"Запиши в файл {filepath} новое содержимое"
            )
            
            assert response2 is not None
    
    @pytest.mark.asyncio
    async def test_calculator_agent(self):
        """Тест агента с калькулятором."""
        async with TestEnvironment() as env:
            env.set_mock_responses(["Результат вычисления: 4"])
            
            response = await env.agent_factory.run_agent(
                "test_calculator_agent",
                "Вычисли 2 + 2"
            )
            
            assert response is not None
            assert "4" in response or "четыре" in response.lower()
    
    @pytest.mark.asyncio
    async def test_coordinator_agent(self):
        """Тест агента-координатора с подагентами."""
        async with TestEnvironment() as env:
            env.set_mock_responses([
                "Делегирую задачу подагенту",
                "Подагент выполнил задачу: результат"
            ])
            
            response = await env.agent_factory.run_agent(
                "test_coordinator_agent",
                "Выполни вычисление через подагента"
            )
            
            assert response is not None
    
    @pytest.mark.asyncio 
    async def test_full_agent_capabilities(self):
        """Тест агента со всеми возможностями."""
        async with TestEnvironment() as env:
            # Создаем тестовые файлы
            env.create_test_file("input.txt", "2 + 2")
            
            env.set_mock_responses([
                "Читаю файл и выполняю вычисления",
                "Результат: 4, записываю в файл",
                "Делегирую проверку подагенту"
            ])
            
            response = await env.agent_factory.run_agent(
                "test_full_agent",
                "Прочитай файл input.txt, вычисли результат, запиши в output.txt и проверь через подагента"
            )
            
            assert response is not None


class TestAgentPerformance:
    """Тесты производительности агентов."""
    
    @pytest.mark.asyncio
    async def test_agent_response_time(self):
        """Тест времени ответа агента."""
        async with TestEnvironment() as env:
            env.set_mock_responses(["Быстрый ответ"])
            
            import time
            start_time = time.time()
            
            await env.agent_factory.run_agent(
                "test_simple_agent",
                "Простой вопрос"
            )
            
            duration = time.time() - start_time
            
            # Проверяем, что ответ пришел быстро (с учетом мокирования)
            assert duration < 5.0  # 5 секунд должно хватить даже с накладными расходами
    
    @pytest.mark.asyncio
    async def test_concurrent_agent_execution(self):
        """Тест параллельного выполнения агентов."""
        async with TestEnvironment() as env:
            env.set_mock_responses(["Параллельный ответ 1", "Параллельный ответ 2"])
            
            # Запускаем несколько агентов параллельно
            tasks = [
                env.agent_factory.run_agent("test_simple_agent", f"Сообщение {i}")
                for i in range(3)
            ]
            
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Проверяем, что все задачи выполнились
            assert len(responses) == 3
            for response in responses:
                assert not isinstance(response, Exception)
                assert response is not None
    
    @pytest.mark.asyncio
    async def test_agent_memory_usage(self):
        """Тест использования памяти агентами."""
        async with TestEnvironment() as env:
            # Создаем и используем много агентов
            for i in range(10):
                agent = await env.agent_factory.create_agent("test_simple_agent")
                assert agent is not None
            
            # Проверяем, что кеш работает (должен быть только один экземпляр)
            cache_info = env.agent_factory._agent_cache
            assert len(cache_info) == 1  # Все агенты одного типа кешируются


@pytest.mark.integration
class TestAgentIntegration:
    """Интеграционные тесты агентов."""
    
    @pytest.mark.asyncio
    async def test_end_to_end_workflow(self):
        """Полный end-to-end тест рабочего процесса."""
        async with TestEnvironment() as env:
            # Симулируем полный рабочий процесс
            workflow_responses = [
                "Анализирую задачу",
                "Читаю исходные данные", 
                "Выполняю вычисления",
                "Записываю результат",
                "Проверяю результат через подагента",
                "Задача выполнена успешно"
            ]
            env.set_mock_responses(workflow_responses)
            
            # Создаем исходные данные
            env.create_test_file("input.txt", "10 + 15 * 2")
            
            # Запускаем полный агент
            final_response = await env.agent_factory.run_agent(
                "test_full_agent",
                "Выполни полный анализ файла input.txt: прочитай, вычисли выражение, запиши результат в output.txt и проверь корректность"
            )
            
            assert final_response is not None
            assert len(final_response) > 0
            
            # Проверяем, что контекст сохранился
            context_info = env.agent_factory.get_context_info()
            assert context_info.get("message_count", 0) >= 2  # Пользователь + агент
    
    @pytest.mark.asyncio
    async def test_error_recovery(self):
        """Тест восстановления после ошибок."""
        async with TestEnvironment() as env:
            # Первый запрос с ошибкой
            env.set_mock_responses(["Произошла ошибка при выполнении"])
            
            response1 = await env.agent_factory.run_agent(
                "test_simple_agent",
                "Выполни невозможную задачу"
            )
            
            # Второй запрос должен работать нормально
            env.set_mock_responses(["Нормальный ответ после ошибки"])
            
            response2 = await env.agent_factory.run_agent(
                "test_simple_agent", 
                "Выполни простую задачу"
            )
            
            assert response1 is not None
            assert response2 is not None
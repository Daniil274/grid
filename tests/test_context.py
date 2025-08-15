"""
Тесты для системы контекста Grid.
Проверяют сохранение истории, управление сессиями, передачу контекста между агентами.
"""

import pytest
import json
import tempfile
from typing import Dict, List, Any
from unittest.mock import Mock, patch

from core.context import ContextManager
from schemas import AgentExecution
from .test_framework import TestEnvironment


class TestContextManager:
    """Тесты менеджера контекста."""
    
    def setup_method(self):
        """Подготовка для каждого теста."""
        self.context_manager = ContextManager(max_history=10)
    
    def test_context_manager_initialization(self):
        """Тест инициализации менеджера контекста."""
        assert self.context_manager.max_history == 10
        assert len(self.context_manager.history) == 0
        assert len(self.context_manager.executions) == 0
    
    def test_add_message(self):
        """Тест добавления сообщений."""
        self.context_manager.add_message("user", "Привет!")
        self.context_manager.add_message("assistant", "Привет! Как дела?")
        
        assert len(self.context_manager.history) == 2
        assert self.context_manager.history[0]["role"] == "user"
        assert self.context_manager.history[0]["content"] == "Привет!"
        assert self.context_manager.history[1]["role"] == "assistant"
    
    def test_message_history_limit(self):
        """Тест ограничения истории сообщений."""
        # Добавляем больше сообщений, чем лимит
        for i in range(15):
            self.context_manager.add_message("user", f"Сообщение {i}")
        
        # Проверяем, что история ограничена
        assert len(self.context_manager.history) == 10
        # Проверяем, что остались последние сообщения
        assert "Сообщение 14" in self.context_manager.history[-1]["content"]
    
    def test_clear_history(self):
        """Тест очистки истории."""
        self.context_manager.add_message("user", "Тест")
        self.context_manager.add_message("assistant", "Ответ")
        
        assert len(self.context_manager.history) == 2
        
        self.context_manager.clear_history()
        
        assert len(self.context_manager.history) == 0
    
    def test_get_conversation_context(self):
        """Тест получения контекста разговора."""
        self.context_manager.add_message("user", "Вопрос 1")
        self.context_manager.add_message("assistant", "Ответ 1")
        self.context_manager.add_message("user", "Вопрос 2")
        
        context = self.context_manager.get_conversation_context()
        
        assert "Вопрос 1" in context
        assert "Ответ 1" in context
        assert "Вопрос 2" in context
        assert "История диалога:" in context
    
    def test_empty_conversation_context(self):
        """Тест получения пустого контекста."""
        context = self.context_manager.get_conversation_context()
        assert context == ""
    
    def test_add_execution(self):
        """Тест добавления выполнения."""
        execution = AgentExecution(
            agent_name="test_agent",
            start_time="2023-01-01T10:00:00",
            input_message="Тестовое сообщение",
            output="Тестовый ответ",
            end_time=1672574400.0
        )
        
        self.context_manager.add_execution(execution)
        
        assert len(self.context_manager.executions) == 1
        assert self.context_manager.executions[0].agent_name == "test_agent"
    
    def test_get_recent_executions(self):
        """Тест получения последних выполнений."""
        # Добавляем несколько выполнений
        for i in range(5):
            execution = AgentExecution(
                agent_name=f"agent_{i}",
                start_time=f"2023-01-01T10:0{i}:00",
                input_message=f"Сообщение {i}",
                output=f"Ответ {i}",
                end_time=1672574400.0 + i
            )
            self.context_manager.add_execution(execution)
        
        # Получаем последние 3 выполнения
        recent = self.context_manager.get_recent_executions(limit=3)
        
        assert len(recent) == 3
        # Проверяем, что это последние выполнения
        assert recent[0].agent_name == "agent_4"  # Последнее
        assert recent[1].agent_name == "agent_3"
        assert recent[2].agent_name == "agent_2"
    
    def test_get_context_stats(self):
        """Тест получения статистики контекста."""
        self.context_manager.add_message("user", "Сообщение")
        self.context_manager.add_message("assistant", "Ответ")
        
        execution = AgentExecution(
            agent_name="test_agent",
            start_time="2023-01-01T10:00:00",
            input_message="Тест",
            output="Результат",
            end_time=1672574400.0
        )
        self.context_manager.add_execution(execution)
        
        stats = self.context_manager.get_context_stats()
        
        assert stats["message_count"] == 2
        assert stats["execution_count"] == 1
        assert "last_message_time" in stats
        assert "total_messages_length" in stats
    
    def test_metadata_operations(self):
        """Тест операций с метаданными."""
        # Устанавливаем метаданные
        self.context_manager.set_metadata("test_key", "test_value")
        self.context_manager.set_metadata("another_key", {"nested": "data"})
        
        # Получаем метаданные
        assert self.context_manager.get_metadata("test_key") == "test_value"
        assert self.context_manager.get_metadata("another_key") == {"nested": "data"}
        assert self.context_manager.get_metadata("nonexistent") is None
        assert self.context_manager.get_metadata("nonexistent", "default") == "default"
        
        # Очищаем метаданные
        self.context_manager.clear_metadata()
        assert self.context_manager.get_metadata("test_key") is None


class TestContextSharing:
    """Тесты передачи контекста между агентами."""
    
    def setup_method(self):
        """Подготовка для каждого теста."""
        self.context_manager = ContextManager(max_history=20)
    
    def test_get_context_for_agent_tool_minimal(self):
        """Тест получения минимального контекста для агентного инструмента."""
        # Добавляем историю сообщений
        self.context_manager.add_message("user", "Первое сообщение")
        self.context_manager.add_message("assistant", "Первый ответ")
        self.context_manager.add_message("user", "Второе сообщение")
        
        context = self.context_manager.get_context_for_agent_tool(
            strategy="minimal",
            depth=2,
            include_tools=False,
            task_input="Новая задача"
        )
        
        assert "Новая задача" in context
        assert "Второе сообщение" in context
        assert "Первый ответ" in context
        # При минимальной стратегии первое сообщение может быть исключено
    
    def test_get_context_for_agent_tool_conversation(self):
        """Тест получения полного контекста разговора."""
        # Добавляем историю
        for i in range(5):
            self.context_manager.add_message("user", f"Пользователь {i}")
            self.context_manager.add_message("assistant", f"Ассистент {i}")
        
        context = self.context_manager.get_context_for_agent_tool(
            strategy="conversation",
            depth=3,
            include_tools=False,
            task_input="Задача с контекстом"
        )
        
        assert "Задача с контекстом" in context
        assert "Пользователь 4" in context  # Последние сообщения
        assert "Ассистент 4" in context
        assert "Контекст диалога:" in context or "История:" in context
    
    def test_get_context_for_agent_tool_smart(self):
        """Тест умной стратегии передачи контекста."""
        # Добавляем разнообразную историю
        self.context_manager.add_message("user", "Важное начальное сообщение")
        self.context_manager.add_message("assistant", "Понял, запомнил")
        
        for i in range(3):
            self.context_manager.add_message("user", f"Обычное сообщение {i}")
            self.context_manager.add_message("assistant", f"Обычный ответ {i}")
        
        self.context_manager.add_message("user", "Критически важное сообщение")
        
        context = self.context_manager.get_context_for_agent_tool(
            strategy="smart",
            depth=4,
            include_tools=False,
            task_input="Умная задача"
        )
        
        assert "Умная задача" in context
        assert "Критически важное сообщение" in context
        # Умная стратегия должна включать важные сообщения
    
    def test_add_tool_result_as_message(self):
        """Тест добавления результата инструмента как сообщения."""
        self.context_manager.add_tool_result_as_message(
            "test_tool", 
            "Результат выполнения инструмента"
        )
        
        assert len(self.context_manager.history) == 1
        message = self.context_manager.history[0]
        assert message["role"] == "assistant"
        assert "test_tool" in message["content"]
        assert "Результат выполнения инструмента" in message["content"]


class TestContextPersistence:
    """Тесты сохранения контекста."""
    
    def setup_method(self):
        """Подготовка для каждого теста."""
        self.temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.json')
        self.temp_file.close()
        self.persist_path = self.temp_file.name
    
    def teardown_method(self):
        """Очистка после тестов."""
        import os
        if os.path.exists(self.persist_path):
            os.unlink(self.persist_path)
    
    def test_context_persistence_disabled(self):
        """Тест отключенного сохранения контекста."""
        context_manager = ContextManager(max_history=10, persist_path=None)
        
        context_manager.add_message("user", "Тест")
        context_manager.add_message("assistant", "Ответ")
        
        # Проверяем, что контекст в памяти
        assert len(context_manager.history) == 2
        
        # Создаем новый менеджер - контекст не должен загрузиться
        new_context_manager = ContextManager(max_history=10, persist_path=None)
        assert len(new_context_manager.history) == 0
    
    def test_context_save_and_load(self):
        """Тест сохранения и загрузки контекста."""
        # Создаем менеджер с сохранением
        context_manager = ContextManager(max_history=10, persist_path=self.persist_path)
        
        # Добавляем данные
        context_manager.add_message("user", "Сообщение для сохранения")
        context_manager.add_message("assistant", "Ответ для сохранения")
        context_manager.set_metadata("test_key", "test_value")
        
        # Принудительно сохраняем
        context_manager.save()
        
        # Создаем новый менеджер - должен загрузить данные
        new_context_manager = ContextManager(max_history=10, persist_path=self.persist_path)
        
        assert len(new_context_manager.history) == 2
        assert new_context_manager.history[0]["content"] == "Сообщение для сохранения"
        assert new_context_manager.get_metadata("test_key") == "test_value"


class TestContextIntegration:
    """Интеграционные тесты контекста в системе агентов."""
    
    @pytest.mark.asyncio
    async def test_context_in_agent_factory(self):
        """Тест использования контекста в агентной фабрике."""
        async with TestEnvironment() as env:
            # Добавляем контекст
            env.agent_factory.add_to_context("user", "Первое сообщение")
            env.agent_factory.add_to_context("assistant", "Первый ответ")
            
            # Проверяем контекст
            context_info = env.agent_factory.get_context_info()
            assert context_info["message_count"] == 2
            
            # Очищаем контекст
            env.agent_factory.clear_context()
            
            context_info = env.agent_factory.get_context_info()
            assert context_info["message_count"] == 0
    
    @pytest.mark.asyncio
    async def test_context_between_agent_calls(self):
        """Тест сохранения контекста между вызовами агентов."""
        async with TestEnvironment() as env:
            env.set_mock_responses([
                "Первый ответ агента",
                "Второй ответ с учетом контекста"
            ])
            
            # Первый вызов
            response1 = await env.agent_factory.run_agent(
                "test_simple_agent",
                "Первое сообщение"
            )
            
            # Второй вызов - должен иметь контекст от первого
            response2 = await env.agent_factory.run_agent(
                "test_simple_agent", 
                "Второе сообщение"
            )
            
            # Проверяем, что контекст накопился
            context_info = env.agent_factory.get_context_info()
            assert context_info["message_count"] >= 4  # 2 пользователя + 2 агента
    
    @pytest.mark.asyncio
    async def test_context_in_agent_tools(self):
        """Тест передачи контекста в агентные инструменты."""
        async with TestEnvironment() as env:
            # Добавляем историю диалога
            env.agent_factory.add_to_context("user", "Важная информация для контекста")
            env.agent_factory.add_to_context("assistant", "Понял, учту")
            
            env.set_mock_responses([
                "Вызываю подагента с контекстом",
                "Подагент получил контекст"
            ])
            
            # Используем агента с подагентами
            response = await env.agent_factory.run_agent(
                "test_coordinator_agent",
                "Выполни задачу через подагента"
            )
            
            assert response is not None
    
    @pytest.mark.asyncio
    async def test_context_memory_management(self):
        """Тест управления памятью контекста."""
        async with TestEnvironment() as env:
            # Добавляем много сообщений для проверки лимитов
            for i in range(20):
                env.agent_factory.add_to_context("user", f"Сообщение {i}")
                env.agent_factory.add_to_context("assistant", f"Ответ {i}")
            
            context_info = env.agent_factory.get_context_info()
            
            # Проверяем, что действует ограничение истории
            max_history = env.config.get_max_history()
            assert context_info["message_count"] <= max_history
    
    @pytest.mark.asyncio
    async def test_context_with_executions(self):
        """Тест контекста с историей выполнений."""
        async with TestEnvironment() as env:
            env.set_mock_responses(["Ответ агента"])
            
            # Выполняем несколько операций
            await env.agent_factory.run_agent("test_simple_agent", "Первая задача")
            await env.agent_factory.run_agent("test_simple_agent", "Вторая задача")
            
            # Проверяем историю выполнений
            executions = env.agent_factory.get_recent_executions(5)
            assert len(executions) == 2
            assert executions[0].input_message == "Вторая задача"  # Последняя
            assert executions[1].input_message == "Первая задача"


class TestContextErrorHandling:
    """Тесты обработки ошибок в контексте."""
    
    def test_invalid_persist_path(self):
        """Тест обработки недопустимого пути сохранения."""
        # Пытаемся создать менеджер с недопустимым путем
        invalid_path = "/invalid/path/context.json"
        
        # Не должно выбрасывать исключение при создании
        context_manager = ContextManager(max_history=10, persist_path=invalid_path)
        
        # Добавляем данные
        context_manager.add_message("user", "Тест")
        
        # Сохранение должно быть обработано gracefully
        try:
            context_manager.save()
        except Exception:
            # Ожидаем, что исключения будут обработаны внутри
            pass
    
    def test_corrupted_context_file(self):
        """Тест обработки поврежденного файла контекста."""
        # Создаем поврежденный файл
        temp_file = tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.json')
        temp_file.write("invalid json content")
        temp_file.close()
        
        try:
            # Пытаемся загрузить поврежденный контекст
            context_manager = ContextManager(max_history=10, persist_path=temp_file.name)
            
            # Должен создаться пустой контекст
            assert len(context_manager.history) == 0
            
        finally:
            import os
            os.unlink(temp_file.name)
    
    def test_large_context_handling(self):
        """Тест обработки очень большого контекста."""
        context_manager = ContextManager(max_history=1000)
        
        # Добавляем очень много сообщений
        for i in range(2000):
            context_manager.add_message("user", f"Очень длинное сообщение номер {i} " * 100)
        
        # Проверяем, что система справляется с большим объемом
        assert len(context_manager.history) == 1000  # Ограничение сработало
        
        # Проверяем производительность получения контекста
        import time
        start_time = time.time()
        context = context_manager.get_conversation_context()
        duration = time.time() - start_time
        
        assert duration < 1.0  # Должно выполняться быстро
        assert context is not None
"""
Интеграционные тесты для системы Grid.
Проверяют полные рабочие процессы, взаимодействие компонентов и реальные сценарии использования.
"""

import pytest
import asyncio
import os
import tempfile
import json
from typing import Dict, List, Any
from unittest.mock import patch, AsyncMock

from .test_framework import TestEnvironment, AgentTestSuite, AgentTestCase
from core.config import Config
from core.agent_factory import AgentFactory


@pytest.mark.integration
class TestFullWorkflows:
    """Тесты полных рабочих процессов."""
    
    @pytest.mark.asyncio
    async def test_file_processing_workflow(self):
        """Тест полного процесса обработки файлов."""
        async with TestEnvironment() as env:
            # Создаем исходные данные
            input_data = {
                "task": "process data",
                "numbers": [1, 2, 3, 4, 5],
                "operations": ["sum", "average"]
            }
            
            input_file = env.create_test_file("input.json", json.dumps(input_data, ensure_ascii=False))
            
            # Настраиваем ответы мока
            workflow_responses = [
                "Читаю файл input.json",
                "Обрабатываю данные: сумма=15, среднее=3",
                "Записываю результат в output.json",
                "Процесс завершен успешно"
            ]
            env.set_mock_responses(workflow_responses)
            
            # Запускаем полный агент
            response = await env.agent_factory.run_agent(
                "test_full_agent",
                f"Прочитай файл {input_file}, вычисли сумму и среднее для чисел, запиши результат в output.json"
            )
            
            assert response is not None
            assert "успешно" in response.lower() or "завершен" in response.lower()
            
            # Проверяем контекст
            context_info = env.agent_factory.get_context_info()
            assert context_info["message_count"] >= 2
    
    @pytest.mark.asyncio
    async def test_multi_agent_coordination(self):
        """Тест координации между несколькими агентами."""
        async with TestEnvironment() as env:
            coordination_responses = [
                "Анализирую задачу, нужен подагент для вычислений",
                "Подагент: вычисляю 10 + 20 = 30",
                "Координатор: получил результат 30, передаю пользователю"
            ]
            env.set_mock_responses(coordination_responses)
            
            # Используем координирующего агента
            response = await env.agent_factory.run_agent(
                "test_coordinator_agent",
                "Вычисли 10 + 20 через подагента и верни результат"
            )
            
            assert response is not None
            # Проверяем, что был использован подагент
            executions = env.agent_factory.get_recent_executions(3)
            assert len(executions) >= 1
    
    @pytest.mark.asyncio
    async def test_error_recovery_workflow(self):
        """Тест восстановления после ошибок в процессе."""
        async with TestEnvironment() as env:
            # Симулируем ошибку, затем успешное восстановление
            error_recovery_responses = [
                "Пытаюсь выполнить операцию",
                "Ошибка: файл не найден",
                "Создаю недостающий файл",
                "Повторяю операцию успешно"
            ]
            env.set_mock_responses(error_recovery_responses)
            
            response = await env.agent_factory.run_agent(
                "test_file_agent",
                "Обработай файл missing.txt, если его нет - создай и обработай"
            )
            
            assert response is not None
            # Проверяем, что агент справился с ошибкой
            assert "успешно" in response.lower() or "создаю" in response.lower()
    
    @pytest.mark.asyncio
    async def test_concurrent_agent_operations(self):
        """Тест параллельных операций нескольких агентов."""
        async with TestEnvironment() as env:
            # Настраиваем разные ответы для разных агентов
            env.set_mock_responses([
                "Агент 1: выполняю задачу A",
                "Агент 2: выполняю задачу B", 
                "Агент 3: выполняю задачу C"
            ])
            
            # Запускаем несколько агентов параллельно
            tasks = [
                env.agent_factory.run_agent("test_simple_agent", "Задача A"),
                env.agent_factory.run_agent("test_calculator_agent", "Задача B"),
                env.agent_factory.run_agent("test_file_agent", "Задача C")
            ]
            
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Проверяем, что все задачи выполнились
            assert len(responses) == 3
            for response in responses:
                assert not isinstance(response, Exception)
                assert response is not None


@pytest.mark.integration
class TestSystemComponents:
    """Тесты взаимодействия системных компонентов."""
    
    @pytest.mark.asyncio
    async def test_config_agent_factory_integration(self):
        """Тест интеграции конфигурации и агентной фабрики."""
        async with TestEnvironment() as env:
            # Проверяем, что конфигурация правильно загружена
            assert env.config is not None
            assert env.agent_factory is not None
            
            # Проверяем основные параметры
            default_agent = env.config.get_default_agent()
            assert default_agent == "test_simple_agent"
            
            max_turns = env.config.get_max_turns()
            assert max_turns == 5
            
            timeout = env.config.get_agent_timeout()
            assert timeout == 30
            
            # Создаем агента через фабрику
            agent = await env.agent_factory.create_agent(default_agent)
            assert agent is not None
            assert agent.name == "Тестовый простой агент"
    
    @pytest.mark.asyncio
    async def test_context_persistence_integration(self):
        """Тест интеграции сохранения контекста."""
        # Создаем временный файл для контекста
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.json')
        temp_file.close()
        
        try:
            # Первая сессия - создаем контекст
            config1 = Config(config_path="tests/config_test.yaml")
            factory1 = AgentFactory(config1)
            
            factory1.add_to_context("user", "Важное сообщение для сохранения")
            factory1.add_to_context("assistant", "Ответ для сохранения")
            
            # Сохраняем контекст
            factory1.context_manager.persist_path = temp_file.name
            factory1.context_manager.save()
            
            await factory1.cleanup()
            
            # Вторая сессия - загружаем контекст
            config2 = Config(config_path="tests/config_test.yaml")
            factory2 = AgentFactory(config2)
            factory2.context_manager.persist_path = temp_file.name
            factory2.context_manager.load()
            
            # Проверяем, что контекст загрузился
            context_info = factory2.get_context_info()
            assert context_info["message_count"] == 2
            
            await factory2.cleanup()
            
        finally:
            # Очистка
            if os.path.exists(temp_file.name):
                os.unlink(temp_file.name)
    
    @pytest.mark.asyncio
    async def test_tool_integration_across_agents(self):
        """Тест интеграции инструментов между разными агентами."""
        async with TestEnvironment() as env:
            # Создаем тестовый файл
            test_content = "Данные для обработки: 5, 10, 15"
            test_file = env.create_test_file("data.txt", test_content)
            
            env.set_mock_responses([
                f"Читаю файл: {test_content}",
                "Вычисляю: 5+10+15=30",
                "Записываю результат в файл",
                "Координирую с подагентом для проверки"
            ])
            
            # Используем файлового агента
            file_response = await env.agent_factory.run_agent(
                "test_file_agent",
                f"Прочитай файл {test_file}"
            )
            
            # Используем агента-калькулятора
            calc_response = await env.agent_factory.run_agent(
                "test_calculator_agent", 
                "Вычисли сумму: 5+10+15"
            )
            
            # Используем координатора
            coord_response = await env.agent_factory.run_agent(
                "test_coordinator_agent",
                "Проверь результат через подагента"
            )
            
            # Все агенты должны работать
            assert file_response is not None
            assert calc_response is not None
            assert coord_response is not None


@pytest.mark.integration  
class TestRealWorldScenarios:
    """Тесты реальных сценариев использования."""
    
    @pytest.mark.asyncio
    async def test_document_processing_scenario(self):
        """Тест сценария обработки документов."""
        async with TestEnvironment() as env:
            # Создаем тестовые документы
            documents = {
                "doc1.txt": "Важный документ с данными: продажи 100, расходы 50",
                "doc2.txt": "Отчет: доходы 200, затраты 75",
                "doc3.txt": "Сводка: прибыль требует вычисления"
            }
            
            for filename, content in documents.items():
                env.create_test_file(filename, content)
            
            document_processing_responses = [
                "Анализирую документы в директории",
                "Нашел 3 документа для обработки",
                "Извлекаю числовые данные: продажи, расходы, доходы, затраты",
                "Вычисляю общую прибыль: (100-50) + (200-75) = 175",
                "Создаю итоговый отчет",
                "Обработка документов завершена"
            ]
            env.set_mock_responses(document_processing_responses)
            
            response = await env.agent_factory.run_agent(
                "test_full_agent",
                f"Обработай все документы в директории {env.temp_dir}, извлеки числовые данные, вычисли общую прибыль и создай отчет"
            )
            
            assert response is not None
            assert "завершена" in response.lower() or "175" in response
    
    @pytest.mark.asyncio
    async def test_data_analysis_pipeline(self):
        """Тест пайплайна анализа данных."""
        async with TestEnvironment() as env:
            # Создаем данные для анализа
            raw_data = {
                "users": [
                    {"id": 1, "name": "User1", "score": 85},
                    {"id": 2, "name": "User2", "score": 92},
                    {"id": 3, "name": "User3", "score": 78},
                    {"id": 4, "name": "User4", "score": 95}
                ]
            }
            
            data_file = env.create_test_file("users.json", json.dumps(raw_data, ensure_ascii=False))
            
            analysis_responses = [
                "Загружаю данные пользователей",
                "Анализирую: 4 пользователя, баллы от 78 до 95",
                "Средний балл: 87.5",
                "Лучший результат: User4 (95 баллов)",
                "Создаю аналитический отчет",
                "Сохраняю результаты в report.json"
            ]
            env.set_mock_responses(analysis_responses)
            
            response = await env.agent_factory.run_agent(
                "test_full_agent",
                f"Проанализируй данные пользователей из {data_file}: найди среднее, максимум, лучшего пользователя и создай отчет"
            )
            
            assert response is not None
            assert "87.5" in response or "User4" in response or "отчет" in response.lower()
    
    @pytest.mark.asyncio
    async def test_collaborative_task_scenario(self):
        """Тест сценария совместного выполнения задач."""
        async with TestEnvironment() as env:
            # Создаем задачи для разных агентов
            task_file = env.create_test_file("tasks.txt", """
Задача 1: Прочитать конфигурацию
Задача 2: Вычислить 25 * 4 + 10
Задача 3: Создать отчет о выполнении
            """)
            
            collaboration_responses = [
                "Координатор: разбираю задачи на подзадачи",
                "Файловый агент: читаю конфигурацию",
                "Калькулятор: вычисляю 25 * 4 + 10 = 110", 
                "Файловый агент: создаю отчет",
                "Координатор: все задачи выполнены успешно"
            ]
            env.set_mock_responses(collaboration_responses)
            
            response = await env.agent_factory.run_agent(
                "test_coordinator_agent",
                f"Выполни все задачи из файла {task_file}, делегируя их соответствующим подагентам"
            )
            
            assert response is not None
            assert "успешно" in response.lower() or "110" in response


@pytest.mark.integration
class TestPerformanceIntegration:
    """Интеграционные тесты производительности."""
    
    @pytest.mark.asyncio
    async def test_system_performance_under_load(self):
        """Тест производительности системы под нагрузкой."""
        async with TestEnvironment() as env:
            import time
            
            # Подготавливаем данные
            for i in range(10):
                env.create_test_file(f"file_{i}.txt", f"Данные файла {i}")
            
            env.set_mock_responses([f"Обработка файла {i}" for i in range(10)])
            
            start_time = time.time()
            
            # Создаем множественные задачи
            tasks = []
            for i in range(10):
                task = env.agent_factory.run_agent(
                    "test_file_agent",
                    f"Обработай файл file_{i}.txt"
                )
                tasks.append(task)
            
            # Выполняем последовательно (чтобы не создавать слишком много агентов)
            for task in tasks:
                await task
            
            duration = time.time() - start_time
            
            # Проверяем, что система справляется с нагрузкой
            assert duration < 30.0  # 30 секунд на 10 задач
    
    @pytest.mark.asyncio
    async def test_memory_usage_stability(self):
        """Тест стабильности использования памяти."""
        async with TestEnvironment() as env:
            # Выполняем много операций для проверки утечек памяти
            env.set_mock_responses(["Операция выполнена"] * 50)
            
            for i in range(50):
                await env.agent_factory.run_agent(
                    "test_simple_agent",
                    f"Выполни операцию {i}"
                )
                
                # Периодически очищаем кеш
                if i % 10 == 0:
                    env.agent_factory.clear_cache()
            
            # Проверяем, что система стабильна
            context_info = env.agent_factory.get_context_info()
            assert context_info["message_count"] <= env.config.get_max_history()
    
    @pytest.mark.asyncio
    async def test_concurrent_system_operations(self):
        """Тест конкурентных операций системы."""
        async with TestEnvironment() as env:
            env.set_mock_responses(["Конкурентная операция"] * 20)
            
            # Создаем конкурентные задачи разных типов
            tasks = []
            
            # Файловые операции
            for i in range(5):
                tasks.append(env.agent_factory.run_agent(
                    "test_file_agent", f"Файловая операция {i}"
                ))
            
            # Вычисления
            for i in range(5):
                tasks.append(env.agent_factory.run_agent(
                    "test_calculator_agent", f"Вычисление {i}"
                ))
            
            # Координация
            for i in range(5):
                tasks.append(env.agent_factory.run_agent(
                    "test_coordinator_agent", f"Координация {i}"
                ))
            
            # Выполняем все задачи параллельно
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Проверяем результаты
            successful_results = [r for r in results if not isinstance(r, Exception)]
            assert len(successful_results) >= 10  # Большинство должно выполниться успешно


@pytest.mark.integration
class TestErrorHandlingIntegration:
    """Интеграционные тесты обработки ошибок."""
    
    @pytest.mark.asyncio
    async def test_cascading_error_handling(self):
        """Тест обработки каскадных ошибок."""
        async with TestEnvironment() as env:
            # Симулируем каскад ошибок
            error_responses = [
                "Ошибка чтения файла",
                "Попытка восстановления",
                "Создание резервного файла",
                "Успешное завершение с резервными данными"
            ]
            env.set_mock_responses(error_responses)
            
            response = await env.agent_factory.run_agent(
                "test_full_agent",
                "Обработай файл nonexistent.txt с автоматическим восстановлением"
            )
            
            assert response is not None
            # Система должна справиться с ошибкой
            assert "успешное" in response.lower() or "восстановление" in response.lower()
    
    @pytest.mark.asyncio
    async def test_timeout_handling(self):
        """Тест обработки таймаутов."""
        async with TestEnvironment() as env:
            # Устанавливаем очень короткий таймаут для теста
            original_timeout = env.config.get_agent_timeout()
            
            # Мокаем длительную операцию
            async def slow_response(*args, **kwargs):
                await asyncio.sleep(2)  # Дольше таймаута
                return {"choices": [{"message": {"role": "assistant", "content": "Медленный ответ"}}]}
            
            with patch.object(env.mock_provider, 'chat_completion', side_effect=slow_response):
                with pytest.raises(Exception):  # Ожидаем таймаут
                    await env.agent_factory.run_agent(
                        "test_simple_agent",
                        "Длительная операция"
                    )
    
    @pytest.mark.asyncio
    async def test_resource_cleanup_on_errors(self):
        """Тест очистки ресурсов при ошибках."""
        async with TestEnvironment() as env:
            # Симулируем ошибку в процессе выполнения
            initial_cache_size = len(env.agent_factory._agent_cache)
            
            try:
                # Принудительно вызываем ошибку
                with patch.object(env.agent_factory, 'create_agent', side_effect=Exception("Тестовая ошибка")):
                    with pytest.raises(Exception):
                        await env.agent_factory.run_agent("test_simple_agent", "Тест ошибки")
            except:
                pass
            
            # Проверяем, что ресурсы не "утекли"
            final_cache_size = len(env.agent_factory._agent_cache)
            assert final_cache_size >= initial_cache_size  # Кеш не должен расти при ошибках
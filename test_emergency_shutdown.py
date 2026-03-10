"""
Тест emergency shutdown механизма для динамических агентов.

Демонстрирует:
1. Создание pipeline через orchestrate()
2. Вызов emergency_shutdown из агента
3. Остановку всех running tasks
4. Возврат информации оркестратору
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from core.config import Config
from core.agent_factory import AgentFactory


async def test_emergency_shutdown():
    """Тестирует emergency shutdown механизм."""

    print("="*80)
    print("ТЕСТ: Emergency Shutdown для динамических агентов")
    print("="*80)

    # 1. Создаем конфигурацию и фабрику
    print("\n1. Инициализация AgentFactory...")
    config = Config(config_file=str(project_root / "config.yaml"))
    factory = AgentFactory(config=config)

    # 2. Создаем контекст для pipeline
    print("2. Создание нового контекста...")
    context_id = factory.context_manager.start_new_context()
    print(f"   [OK] Контекст создан: {context_id}")

    # 3. Проверяем регистрацию инструментов
    print("\n3. Проверка регистрации emergency tools...")
    from tools import AVAILABLE_TOOLS
    assert "emergency_shutdown" in AVAILABLE_TOOLS, "emergency_shutdown не зарегистрирован!"
    assert "get_pipeline_status" in AVAILABLE_TOOLS, "get_pipeline_status не зарегистрирован!"
    print(f"   [OK] emergency_shutdown: зарегистрирован")
    print(f"   [OK] get_pipeline_status: зарегистрирован")

    # 4. Создаем динамического агента, который вызовет emergency_shutdown
    print("\n4. Создание динамического агента с emergency_shutdown...")

    # Задача для агента - симулировать критическую ошибку
    test_task = """
Выполни следующую задачу:

1. Сначала выведи: "Начинаю работу..."
2. Затем симулируй критическую ошибку: представь, что база данных недоступна
3. Вызови emergency_shutdown с причиной: "Database connection failed: Connection refused. All tasks require database access."
4. Используй severity="critical"

ВАЖНО: Ты ДОЛЖЕН вызвать emergency_shutdown для демонстрации механизма!
"""

    agent_prompt = """
Ты тестовый агент для демонстрации emergency shutdown.

У тебя есть инструмент emergency_shutdown(reason, severity) для остановки всего pipeline.

Твоя задача:
1. Начни выполнение
2. Обнаружь "критическую проблему" (симуляция)
3. Вызови emergency_shutdown с детальным описанием причины
4. НЕ продолжай работу после вызова emergency_shutdown

Это ТЕСТ - ты ДОЛЖЕН вызвать emergency_shutdown для проверки работы механизма.
"""

    agent = await factory.create_dynamic_agent(
        name="test-emergency-agent",
        instructions=agent_prompt,
        model_key="glm-4.7-flash",
        tool_names=["emergency_shutdown", "get_pipeline_status"]
    )

    print(f"   [OK] Агент создан: {agent.name}")
    print(f"   [OK] Инструменты: emergency_shutdown, get_pipeline_status")

    # 5. Тестируем orchestrate() с emergency shutdown
    print("\n5. Запуск orchestrate() с задачей...")
    print("   (агент должен вызвать emergency_shutdown)")

    try:
        # Импортируем orchestrate
        from tools.orchestrator_tools import orchestrate
        from agents import RunContextWrapper
        from core.agent_factory import GridRunContext

        # Создаем context wrapper
        grid_context = GridRunContext(
            factory=factory,
            context_id=context_id,
            user_id="test_user"
        )
        context_wrapper = RunContextWrapper(context=grid_context)

        # Вызываем orchestrate
        result_json = await orchestrate(
            context=context_wrapper,
            task=test_task,
            agent_system_prompt=agent_prompt,
            model_key="glm-4.7-flash",
            executor_tools=["emergency_shutdown", "get_pipeline_status"],
            context_id=context_id
        )

        print("\n6. Результат выполнения:")
        print("-" * 80)

        import json
        result = json.loads(result_json)

        # Проверяем, был ли emergency shutdown
        if result.get("emergency_stopped"):
            print("   [SUCCESS] УСПЕХ: Emergency shutdown был выполнен!")
            print(f"   Pipeline ID: {result.get('pipeline_id')}")
            print(f"   Причина: {result.get('emergency_reason')}")
            print(f"   Severity: {result.get('emergency_severity')}")
            print(f"   Завершенных задач: {result.get('completed_tasks', 0)}")
            print(f"   Проваленных задач: {result.get('failed_tasks', 0)}")
            print(f"   Всего задач: {result.get('total_tasks', 0)}")
        else:
            print("   [WARNING]  ВНИМАНИЕ: Emergency shutdown НЕ был вызван")
            print(f"   Результат: {result.get('final', '')[:200]}...")

        print("-" * 80)

    except asyncio.CancelledError:
        print("   [SUCCESS] CancelledError перехвачен (ожидаемо при emergency shutdown)")
    except Exception as e:
        print(f"   [ERROR] ОШИБКА: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

    # 7. Cleanup
    print("\n7. Cleanup...")
    await factory.cleanup()
    print("   [OK] Cleanup завершен")

    print("\n" + "="*80)
    print("ТЕСТ ЗАВЕРШЕН")
    print("="*80)


async def test_pipeline_registry():
    """Тестирует PipelineRegistry напрямую."""

    print("\n" + "="*80)
    print("ДОПОЛНИТЕЛЬНЫЙ ТЕСТ: PipelineRegistry")
    print("="*80)

    from core.pipeline_registry import PipelineRegistry, PipelineStatus

    # 1. Создаем registry
    print("\n1. Создание PipelineRegistry...")
    registry = PipelineRegistry()
    print("   [OK] Registry создан")

    # 2. Регистрируем pipeline
    print("\n2. Регистрация pipeline...")
    pipeline_id = await registry.register_pipeline(
        orchestrator_name="test_orchestrator",
        context_id="test-ctx-123",
        user_id="test_user"
    )
    print(f"   [OK] Pipeline зарегистрирован: {pipeline_id}")

    # 3. Создаем mock task
    print("\n3. Регистрация mock task...")
    async def mock_task():
        await asyncio.sleep(10)  # Долгая задача

    task = asyncio.create_task(mock_task())
    task_id = await registry.register_task(
        pipeline_id=pipeline_id,
        agent_name="mock_agent",
        task=task,
        metadata={"test": True}
    )
    print(f"   [OK] Task зарегистрирован: {task_id}")

    # 4. Получаем статус
    print("\n4. Получение статуса pipeline...")
    status = await registry.get_pipeline_status(pipeline_id)
    print(f"   [OK] Статус: {status['status']}")
    print(f"   [OK] Running tasks: {len(status['running_tasks'])}")

    # 5. Выполняем emergency shutdown
    print("\n5. Вызов emergency_shutdown...")
    result = await registry.emergency_shutdown(
        pipeline_id=pipeline_id,
        reason="Test emergency shutdown",
        severity="warning"
    )
    print(f"   [OK] Shutdown выполнен: {result['success']}")
    print(f"   [OK] Отменено задач: {result['cancelled_tasks']}")

    # 6. Проверяем финальный статус
    print("\n6. Проверка финального статуса...")
    final_status = await registry.get_pipeline_status(pipeline_id)
    print(f"   [OK] Финальный статус: {final_status['status']}")
    print(f"   [OK] Emergency reason: {final_status.get('emergency_reason')}")

    print("\n" + "="*80)
    print("ДОПОЛНИТЕЛЬНЫЙ ТЕСТ ЗАВЕРШЕН")
    print("="*80)


async def main():
    """Главная функция для запуска всех тестов."""

    print("\n" + "="*80)
    print("ЗАПУСК ТЕСТОВ EMERGENCY SHUTDOWN")
    print("="*80)

    # Тест 1: PipelineRegistry напрямую
    await test_pipeline_registry()

    # Тест 2: Интеграционный тест с orchestrate
    # await test_emergency_shutdown()  # Закомментировано, т.к. требует LLM

    print("\n" + "="*80)
    print("ВСЕ ТЕСТЫ ЗАВЕРШЕНЫ")
    print("="*80)


if __name__ == "__main__":
    asyncio.run(main())

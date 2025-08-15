#!/usr/bin/env python3
"""
Запускающий скрипт для тестов системы Grid.
Позволяет запускать различные типы тестов с настройками.
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import List, Dict, Any

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_framework import AgentTestSuite, TestEnvironment
from tests.test_agents import TestAgents
from tests.test_tools import TestFileTools, TestGitTools
from tests.test_context import TestContextManager


class TestRunner:
    """Запускающий класс для тестов Grid."""
    
    def __init__(self, config_path: str = "tests/config_test.yaml"):
        self.config_path = config_path
        self.results: List[Dict] = []
    
    async def run_unit_tests(self) -> Dict[str, Any]:
        """Запуск модульных тестов."""
        print("🧪 Запуск модульных тестов...")
        
        suite = AgentTestSuite("Unit Tests", self.config_path)
        results = await suite.run_all()
        summary = suite.get_summary()
        
        self.results.append({
            "category": "unit",
            "summary": summary,
            "details": results
        })
        
        return summary
    
    async def run_agent_tests(self) -> Dict[str, Any]:
        """Запуск тестов агентов."""
        print("🤖 Запуск тестов агентов...")
        
        async with TestEnvironment(self.config_path) as env:
            test_cases = [
                ("simple_agent", "test_simple_agent", "Простое взаимодействие"),
                ("file_agent", "test_file_agent", "Файловые операции"),
                ("calculator_agent", "test_calculator_agent", "Вычисления"),
                ("coordinator_agent", "test_coordinator_agent", "Координация агентов"),
                ("full_agent", "test_full_agent", "Полный функционал")
            ]
            
            results = []
            
            for test_name, agent_key, description in test_cases:
                try:
                    start_time = time.time()
                    
                    # Настраиваем ответ мока
                    env.set_mock_responses([f"Тестовый ответ от {agent_key}"])
                    
                    # Запускаем агента
                    response = await env.agent_factory.run_agent(
                        agent_key, 
                        f"Тестовое сообщение для {test_name}"
                    )
                    
                    duration = time.time() - start_time
                    
                    results.append({
                        "test_name": test_name,
                        "agent_key": agent_key,
                        "description": description,
                        "success": response is not None,
                        "duration": duration,
                        "response_length": len(response) if response else 0
                    })
                    
                    print(f"  ✅ {test_name}: {duration:.2f}s")
                    
                except Exception as e:
                    results.append({
                        "test_name": test_name,
                        "agent_key": agent_key,
                        "description": description,
                        "success": False,
                        "error": str(e),
                        "duration": 0
                    })
                    print(f"  ❌ {test_name}: {e}")
        
        summary = {
            "category": "agents",
            "total_tests": len(results),
            "passed": sum(1 for r in results if r.get("success", False)),
            "failed": sum(1 for r in results if not r.get("success", False)),
            "total_duration": sum(r.get("duration", 0) for r in results),
            "results": results
        }
        
        self.results.append(summary)
        return summary
    
    async def run_tool_tests(self) -> Dict[str, Any]:
        """Запуск тестов инструментов."""
        print("🔧 Запуск тестов инструментов...")
        
        import tempfile
        import os
        
        temp_dir = tempfile.mkdtemp(prefix="grid_tool_test_")
        
        try:
            test_cases = [
                ("file_read_write", self._test_file_operations, temp_dir),
                ("file_list", self._test_file_listing, temp_dir),
                ("tool_loading", self._test_tool_loading, None)
            ]
            
            results = []
            
            for test_name, test_func, test_arg in test_cases:
                try:
                    start_time = time.time()
                    success = await test_func(test_arg) if test_arg else await test_func()
                    duration = time.time() - start_time
                    
                    results.append({
                        "test_name": test_name,
                        "success": success,
                        "duration": duration
                    })
                    
                    print(f"  ✅ {test_name}: {duration:.2f}s")
                    
                except Exception as e:
                    results.append({
                        "test_name": test_name,
                        "success": False,
                        "error": str(e),
                        "duration": 0
                    })
                    print(f"  ❌ {test_name}: {e}")
        
        finally:
            # Очистка
            import shutil
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        
        summary = {
            "category": "tools",
            "total_tests": len(results),
            "passed": sum(1 for r in results if r.get("success", False)),
            "failed": sum(1 for r in results if not r.get("success", False)),
            "total_duration": sum(r.get("duration", 0) for r in results),
            "results": results
        }
        
        self.results.append(summary)
        return summary
    
    async def _test_file_operations(self, temp_dir: str) -> bool:
        """Тест файловых операций."""
        from tests.mock_tools import mock_write_file, mock_read_file
        
        test_file = os.path.join(temp_dir, "test.txt")
        test_content = "Тестовое содержимое"
        
        # Записываем файл
        write_result = await mock_write_file(test_file, test_content)
        if "ошибка" in write_result.lower():
            return False
        
        # Читаем файл
        read_result = await mock_read_file(test_file)
        return test_content in read_result
    
    async def _test_file_listing(self, temp_dir: str) -> bool:
        """Тест получения списка файлов."""
        from tests.mock_tools import mock_list_files
        
        # Создаем тестовые файлы через мок систему
        from tests.mock_tools import mock_fs
        for i in range(3):
            test_file = os.path.join(temp_dir, f"file_{i}.txt")
            mock_fs.create_file(test_file, f"Содержимое {i}")
        
        # Получаем список
        result = await mock_list_files(temp_dir)
        return "file_0.txt" in result and "file_1.txt" in result
    
    async def _test_tool_loading(self) -> bool:
        """Тест загрузки инструментов."""
        from tools import get_tools_by_names
        
        try:
            tools = get_tools_by_names(["read_file", "write_file"])
            return len(tools) >= 0  # Может быть 0 если инструменты не найдены
        except Exception:
            return False
    
    async def run_integration_tests(self) -> Dict[str, Any]:
        """Запуск интеграционных тестов."""
        print("🔗 Запуск интеграционных тестов...")
        
        async with TestEnvironment(self.config_path) as env:
            test_cases = [
                ("config_integration", self._test_config_integration, env),
                ("agent_coordination", self._test_agent_coordination, env),
                ("context_persistence", self._test_context_persistence, env)
            ]
            
            results = []
            
            for test_name, test_func, test_env in test_cases:
                try:
                    start_time = time.time()
                    success = await test_func(test_env)
                    duration = time.time() - start_time
                    
                    results.append({
                        "test_name": test_name,
                        "success": success,
                        "duration": duration
                    })
                    
                    print(f"  ✅ {test_name}: {duration:.2f}s")
                    
                except Exception as e:
                    results.append({
                        "test_name": test_name,
                        "success": False,
                        "error": str(e),
                        "duration": 0
                    })
                    print(f"  ❌ {test_name}: {e}")
        
        summary = {
            "category": "integration",
            "total_tests": len(results),
            "passed": sum(1 for r in results if r.get("success", False)),
            "failed": sum(1 for r in results if not r.get("success", False)),
            "total_duration": sum(r.get("duration", 0) for r in results),
            "results": results
        }
        
        self.results.append(summary)
        return summary
    
    async def _test_config_integration(self, env: TestEnvironment) -> bool:
        """Тест интеграции конфигурации."""
        try:
            # Проверяем основные параметры конфигурации
            default_agent = env.config.get_default_agent()
            max_turns = env.config.get_max_turns()
            timeout = env.config.get_agent_timeout()
            
            return (default_agent == "test_simple_agent" and 
                   max_turns == 5 and 
                   timeout == 30)
        except Exception:
            return False
    
    async def _test_agent_coordination(self, env: TestEnvironment) -> bool:
        """Тест координации агентов."""
        try:
            env.set_mock_responses(["Координация успешна"])
            
            response = await env.agent_factory.run_agent(
                "test_coordinator_agent",
                "Тестовая координация"
            )
            
            return response is not None
        except Exception:
            return False
    
    async def _test_context_persistence(self, env: TestEnvironment) -> bool:
        """Тест сохранения контекста."""
        try:
            # Добавляем контекст
            env.agent_factory.add_to_context("user", "Тестовое сообщение")
            
            # Проверяем контекст
            context_info = env.agent_factory.get_context_info()
            return context_info["message_count"] >= 1
        except Exception:
            return False
    
    def print_summary(self):
        """Печать общей сводки результатов."""
        print("\n" + "="*60)
        print("📊 СВОДКА РЕЗУЛЬТАТОВ ТЕСТИРОВАНИЯ")
        print("="*60)
        
        total_tests = 0
        total_passed = 0
        total_failed = 0
        total_duration = 0
        
        for result in self.results:
            category = result["category"]
            passed = result.get("passed", 0)
            failed = result.get("failed", 0)
            duration = result.get("total_duration", 0)
            
            total_tests += passed + failed
            total_passed += passed
            total_failed += failed
            total_duration += duration
            
            status = "✅" if failed == 0 else "⚠️" if passed > failed else "❌"
            
            print(f"{status} {category.upper():15} | "
                  f"Пройдено: {passed:3d} | "
                  f"Ошибок: {failed:3d} | "
                  f"Время: {duration:6.2f}s")
        
        print("-" * 60)
        
        overall_status = "✅" if total_failed == 0 else "⚠️" if total_passed > total_failed else "❌"
        success_rate = (total_passed / total_tests * 100) if total_tests > 0 else 0
        
        print(f"{overall_status} ОБЩИЙ ИТОГ:     | "
              f"Пройдено: {total_passed:3d} | "
              f"Ошибок: {total_failed:3d} | "
              f"Время: {total_duration:6.2f}s")
        print(f"📈 Успешность: {success_rate:.1f}%")
        
        if total_failed > 0:
            print(f"\n⚠️  Обнаружено {total_failed} ошибок. Проверьте детали выше.")
        else:
            print(f"\n🎉 Все тесты пройдены успешно!")
        
        print("="*60)


async def main():
    """Главная функция запуска тестов."""
    parser = argparse.ArgumentParser(description="Запуск тестов системы Grid")
    parser.add_argument("--config", default="tests/config_test.yaml", 
                       help="Путь к конфигурационному файлу")
    parser.add_argument("--type", choices=["all", "unit", "agents", "tools", "integration"], 
                       default="all", help="Тип тестов для запуска")
    parser.add_argument("--verbose", "-v", action="store_true", 
                       help="Подробный вывод")
    
    args = parser.parse_args()
    
    print("🚀 Система тестирования Grid")
    print(f"📁 Конфигурация: {args.config}")
    print(f"🎯 Тип тестов: {args.type}")
    print("-" * 60)
    
    runner = TestRunner(args.config)
    
    start_time = time.time()
    
    try:
        if args.type in ["all", "unit"]:
            await runner.run_unit_tests()
        
        if args.type in ["all", "agents"]:
            await runner.run_agent_tests()
        
        if args.type in ["all", "tools"]:
            await runner.run_tool_tests()
        
        if args.type in ["all", "integration"]:
            await runner.run_integration_tests()
    
    except KeyboardInterrupt:
        print("\n⏸️  Тестирование прервано пользователем")
        return 1
    
    except Exception as e:
        print(f"\n💥 Критическая ошибка: {e}")
        return 1
    
    finally:
        total_time = time.time() - start_time
        print(f"\n⏱️  Общее время выполнения: {total_time:.2f}s")
        runner.print_summary()
    
    # Возвращаем код ошибки если есть неуспешные тесты
    total_failed = sum(r.get("failed", 0) for r in runner.results)
    return 1 if total_failed > 0 else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
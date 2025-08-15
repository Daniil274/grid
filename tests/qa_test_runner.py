#!/usr/bin/env python3
"""
QA Тест-раннер для реального тестирования системы Grid на дешевых моделях.
Проводит комплексное тестирование с реальными API вызовами.
"""

import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import List, Dict, Any, Optional
import tempfile

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import Config
from core.agent_factory import AgentFactory
from utils.logger import Logger

logger = Logger(__name__)


class QATestResult:
    """Результат QA теста."""
    
    def __init__(self, test_name: str, category: str):
        self.test_name = test_name
        self.category = category
        self.success = False
        self.error: Optional[str] = None
        self.warning: Optional[str] = None
        self.duration = 0.0
        self.details: Dict[str, Any] = {}
        self.start_time = time.time()
    
    def finish(self, success: bool = True, error: str = None, warning: str = None, **details):
        """Завершает тест с результатом."""
        self.success = success
        self.error = error
        self.warning = warning
        self.duration = time.time() - self.start_time
        self.details.update(details)
    
    def to_dict(self) -> Dict:
        """Конвертирует в словарь для отчетности."""
        return {
            "test_name": self.test_name,
            "category": self.category,
            "success": self.success,
            "error": self.error,
            "warning": self.warning,
            "duration": round(self.duration, 2),
            "details": self.details
        }


class QATestRunner:
    """Основной класс для проведения QA тестирования."""
    
    def __init__(self, config_path: str = "tests/config_qa.yaml"):
        self.config_path = config_path
        self.config: Optional[Config] = None
        self.factory: Optional[AgentFactory] = None
        self.results: List[QATestResult] = []
        self.temp_dir = None
        
    async def setup(self):
        """Инициализация QA окружения."""
        print("🔧 Настройка QA окружения...")
        
        # Проверяем переменные окружения
        if not os.getenv("OPENROUTER_API_KEY"):
            raise RuntimeError("❌ Не установлена переменная OPENROUTER_API_KEY")
        
        # Создаем временную директорию для тестов
        self.temp_dir = tempfile.mkdtemp(prefix="grid_qa_test_")
        print(f"📁 Временная директория: {self.temp_dir}")
        
        # Загружаем конфигурацию
        self.config = Config(config_path=self.config_path)
        
        # Создаем фабрику агентов
        self.factory = AgentFactory(self.config, self.temp_dir)
        
        print("✅ QA окружение готово")
    
    async def cleanup(self):
        """Очистка QA окружения."""
        if self.factory:
            await self.factory.cleanup()
        
        # Очищаем временную директорию
        if self.temp_dir and os.path.exists(self.temp_dir):
            import shutil
            try:
                shutil.rmtree(self.temp_dir)
                print(f"🗑️ Временная директория очищена: {self.temp_dir}")
            except Exception as e:
                print(f"⚠️ Не удалось очистить временную директорию: {e}")
    
    async def run_all_qa_tests(self) -> List[QATestResult]:
        """Запускает все QA тесты."""
        print("🚀 Запуск полного QA тестирования на дешевых моделях")
        print("=" * 60)
        
        # Категории тестов
        test_categories = [
            ("basic", self.test_basic_functionality),
            ("agent_creation", self.test_agent_creation),
            ("simple_interactions", self.test_simple_interactions),
            ("tool_usage", self.test_tool_usage),
            ("mcp_integration", self.test_mcp_integration),
            ("agent_coordination", self.test_agent_coordination),
            ("error_handling", self.test_error_handling),
            ("performance", self.test_performance)
        ]
        
        for category, test_func in test_categories:
            print(f"\n📂 Категория: {category.upper()}")
            print("-" * 40)
            
            try:
                await test_func()
            except Exception as e:
                result = QATestResult(f"{category}_critical_error", category)
                result.finish(False, f"Критическая ошибка: {str(e)}")
                self.results.append(result)
                print(f"💥 Критическая ошибка в категории {category}: {e}")
        
        return self.results
    
    async def test_basic_functionality(self):
        """Тест базовой функциональности."""
        
        # Тест 1: Проверка конфигурации
        result = QATestResult("config_validation", "basic")
        try:
            default_agent = self.config.get_default_agent()
            models = self.config.get_all_models()
            agents = self.config.get_all_agents()
            
            result.finish(True, details={
                "default_agent": default_agent,
                "models_count": len(models),
                "agents_count": len(agents)
            })
            print("✅ Конфигурация валидна")
            
        except Exception as e:
            result.finish(False, str(e))
            print(f"❌ Ошибка конфигурации: {e}")
        
        self.results.append(result)
        
        # Тест 2: Проверка API ключа
        result = QATestResult("api_key_check", "basic")
        try:
            api_key = os.getenv("OPENROUTER_API_KEY")
            if api_key and len(api_key) > 10:
                result.finish(True, details={"key_length": len(api_key)})
                print("✅ API ключ присутствует")
            else:
                result.finish(False, "API ключ отсутствует или некорректный")
                print("❌ Проблема с API ключом")
        
        except Exception as e:
            result.finish(False, str(e))
            print(f"❌ Ошибка проверки API: {e}")
        
        self.results.append(result)
    
    async def test_agent_creation(self):
        """Тест создания агентов."""
        
        agents_to_test = [
            "qa_simple_agent",
            "qa_test_agent", 
            "qa_mcp_agent",
            "qa_coordinator_agent"
        ]
        
        for agent_key in agents_to_test:
            result = QATestResult(f"create_{agent_key}", "agent_creation")
            
            try:
                start_time = time.time()
                agent = await self.factory.create_agent(agent_key)
                creation_time = time.time() - start_time
                
                if agent:
                    result.finish(True, details={
                        "agent_name": agent.name,
                        "creation_time": round(creation_time, 2)
                    })
                    print(f"✅ Агент {agent_key} создан за {creation_time:.2f}с")
                else:
                    result.finish(False, "Агент не создан")
                    print(f"❌ Агент {agent_key} не создан")
            
            except Exception as e:
                result.finish(False, str(e))
                print(f"❌ Ошибка создания агента {agent_key}: {e}")
            
            self.results.append(result)
    
    async def test_simple_interactions(self):
        """Тест простых взаимодействий с агентами."""
        
        test_cases = [
            ("qa_simple_agent", "Привет! Как дела?", ["привет", "дела", "хорошо"]),
            ("qa_simple_agent", "Сколько будет 2+2?", ["4", "четыре"]),
            ("qa_simple_agent", "Какая сегодня дата?", ["дата", "сегодня"]),
        ]
        
        for agent_key, message, expected_keywords in test_cases:
            result = QATestResult(f"interaction_{agent_key}_{len(message)}", "simple_interactions")
            
            try:
                start_time = time.time()
                response = await self.factory.run_agent(agent_key, message)
                response_time = time.time() - start_time
                
                if response:
                    # Проверяем наличие ключевых слов
                    keywords_found = []
                    response_lower = response.lower()
                    
                    for keyword in expected_keywords:
                        if keyword.lower() in response_lower:
                            keywords_found.append(keyword)
                    
                    success = len(keywords_found) > 0
                    
                    result.finish(success, details={
                        "message": message,
                        "response_length": len(response),
                        "response_time": round(response_time, 2),
                        "keywords_found": keywords_found,
                        "response_preview": response[:100] + "..." if len(response) > 100 else response
                    })
                    
                    if success:
                        print(f"✅ Взаимодействие с {agent_key}: {response_time:.2f}с, найдено ключевых слов: {len(keywords_found)}")
                    else:
                        result.warning = "Ключевые слова не найдены"
                        print(f"⚠️ Взаимодействие с {agent_key}: ответ получен, но ключевые слова не найдены")
                
                else:
                    result.finish(False, "Пустой ответ от агента")
                    print(f"❌ Пустой ответ от {agent_key}")
            
            except Exception as e:
                result.finish(False, str(e))
                print(f"❌ Ошибка взаимодействия с {agent_key}: {e}")
            
            self.results.append(result)
    
    async def test_tool_usage(self):
        """Тест использования инструментов."""
        
        # Создаем тестовый файл
        test_file = os.path.join(self.temp_dir, "qa_test.txt")
        test_content = "QA тестовые данные\nСтрока 2\nСтрока 3"
        
        with open(test_file, 'w', encoding='utf-8') as f:
            f.write(test_content)
        
        test_cases = [
            ("file_read", f"Прочитай файл {test_file}"),
            ("file_write", f"Запиши в файл {self.temp_dir}/output.txt текст: 'QA тест прошел успешно'"),
            ("file_list", f"Покажи список файлов в директории {self.temp_dir}"),
        ]
        
        for tool_type, message in test_cases:
            result = QATestResult(f"tool_{tool_type}", "tool_usage")
            
            try:
                start_time = time.time()
                response = await self.factory.run_agent("qa_test_agent", message)
                response_time = time.time() - start_time
                
                if response:
                    # Проверяем использование инструментов
                    executions = self.factory.get_recent_executions(1)
                    tools_used = executions[0].tools_used if executions else []
                    
                    result.finish(True, details={
                        "message": message,
                        "response_length": len(response),
                        "response_time": round(response_time, 2),
                        "tools_used": tools_used,
                        "response_preview": response[:150] + "..." if len(response) > 150 else response
                    })
                    
                    print(f"✅ Инструмент {tool_type}: {response_time:.2f}с, инструменты: {tools_used}")
                
                else:
                    result.finish(False, "Пустой ответ")
                    print(f"❌ Инструмент {tool_type}: пустой ответ")
            
            except Exception as e:
                result.finish(False, str(e))
                print(f"❌ Ошибка инструмента {tool_type}: {e}")
            
            self.results.append(result)
    
    async def test_mcp_integration(self):
        """Тест MCP интеграции."""
        
        result = QATestResult("mcp_basic", "mcp_integration")
        
        try:
            start_time = time.time()
            
            # Тестируем MCP агента с sequential thinking
            message = "Проанализируй задачу: нужно создать файл с данными и прочитать его. Используй последовательное мышление для планирования."
            
            response = await self.factory.run_agent("qa_mcp_agent", message)
            response_time = time.time() - start_time
            
            if response:
                result.finish(True, details={
                    "response_length": len(response),
                    "response_time": round(response_time, 2),
                    "response_preview": response[:200] + "..." if len(response) > 200 else response
                })
                print(f"✅ MCP интеграция: {response_time:.2f}с")
            else:
                result.finish(False, "Пустой ответ от MCP агента")
                print("❌ MCP интеграция: пустой ответ")
        
        except Exception as e:
            result.finish(False, str(e))
            print(f"❌ Ошибка MCP интеграции: {e}")
        
        self.results.append(result)
    
    async def test_agent_coordination(self):
        """Тест координации агентов."""
        
        result = QATestResult("agent_coordination", "agent_coordination")
        
        try:
            start_time = time.time()
            
            message = "Делегируй подагенту задачу: прочитать список файлов в текущей директории и найти файлы с расширением .txt"
            
            response = await self.factory.run_agent("qa_coordinator_agent", message)
            response_time = time.time() - start_time
            
            if response:
                # Проверяем, что было выполнение подагента
                executions = self.factory.get_recent_executions(5)
                sub_agent_used = any("подагент" in exec.agent_name.lower() or "sub" in exec.agent_name.lower() for exec in executions)
                
                result.finish(True, details={
                    "response_length": len(response),
                    "response_time": round(response_time, 2),
                    "sub_agent_used": sub_agent_used,
                    "executions_count": len(executions),
                    "response_preview": response[:200] + "..." if len(response) > 200 else response
                })
                
                if sub_agent_used:
                    print(f"✅ Координация агентов: {response_time:.2f}с, подагент использован")
                else:
                    result.warning = "Подагент возможно не использован"
                    print(f"⚠️ Координация агентов: {response_time:.2f}с, подагент возможно не использован")
            
            else:
                result.finish(False, "Пустой ответ от координатора")
                print("❌ Координация агентов: пустой ответ")
        
        except Exception as e:
            result.finish(False, str(e))
            print(f"❌ Ошибка координации агентов: {e}")
        
        self.results.append(result)
    
    async def test_error_handling(self):
        """Тест обработки ошибок."""
        
        error_test_cases = [
            ("invalid_file", "Прочитай файл /несуществующий/путь/файл.txt"),
            ("invalid_operation", "Выполни невозможную операцию xyz123"),
            ("empty_input", ""),
        ]
        
        for error_type, message in error_test_cases:
            result = QATestResult(f"error_{error_type}", "error_handling")
            
            try:
                start_time = time.time()
                response = await self.factory.run_agent("qa_test_agent", message)
                response_time = time.time() - start_time
                
                # Для тестов обработки ошибок успех = получение ответа без падения
                if response is not None:
                    result.finish(True, details={
                        "message": message,
                        "response_length": len(response),
                        "response_time": round(response_time, 2),
                        "response_preview": response[:100] + "..." if len(response) > 100 else response
                    })
                    print(f"✅ Обработка ошибки {error_type}: {response_time:.2f}с")
                else:
                    result.finish(False, "Null ответ")
                    print(f"❌ Обработка ошибки {error_type}: null ответ")
            
            except Exception as e:
                result.finish(False, str(e))
                print(f"❌ Критическая ошибка в тесте {error_type}: {e}")
            
            self.results.append(result)
    
    async def test_performance(self):
        """Тест производительности."""
        
        # Тест скорости ответа
        result = QATestResult("response_speed", "performance")
        
        try:
            messages = [
                "Привет!",
                "Как дела?",
                "Что такое искусственный интеллект?",
                "Объясни принцип работы нейронных сетей",
                "Какие преимущества у облачных технологий?"
            ]
            
            times = []
            responses = []
            
            for i, message in enumerate(messages):
                start_time = time.time()
                response = await self.factory.run_agent("qa_simple_agent", message)
                response_time = time.time() - start_time
                
                times.append(response_time)
                responses.append(len(response) if response else 0)
                
                print(f"  📊 Запрос {i+1}: {response_time:.2f}с, {len(response) if response else 0} символов")
            
            avg_time = sum(times) / len(times)
            max_time = max(times)
            min_time = min(times)
            avg_length = sum(responses) / len(responses)
            
            result.finish(True, details={
                "requests_count": len(messages),
                "avg_response_time": round(avg_time, 2),
                "max_response_time": round(max_time, 2),
                "min_response_time": round(min_time, 2),
                "avg_response_length": round(avg_length, 0),
                "individual_times": [round(t, 2) for t in times]
            })
            
            print(f"✅ Производительность: среднее время {avg_time:.2f}с, длина ответа {avg_length:.0f} символов")
        
        except Exception as e:
            result.finish(False, str(e))
            print(f"❌ Ошибка теста производительности: {e}")
        
        self.results.append(result)
    
    def generate_qa_report(self) -> Dict:
        """Генерирует QA отчет."""
        
        # Группируем результаты по категориям
        categories = {}
        for result in self.results:
            category = result.category
            if category not in categories:
                categories[category] = {
                    "total": 0,
                    "passed": 0,
                    "failed": 0,
                    "warnings": 0,
                    "total_duration": 0,
                    "tests": []
                }
            
            cat = categories[category]
            cat["total"] += 1
            cat["total_duration"] += result.duration
            cat["tests"].append(result.to_dict())
            
            if result.success:
                cat["passed"] += 1
            else:
                cat["failed"] += 1
            
            if result.warning:
                cat["warnings"] += 1
        
        # Общая статистика
        total_tests = len(self.results)
        total_passed = sum(cat["passed"] for cat in categories.values())
        total_failed = sum(cat["failed"] for cat in categories.values())
        total_warnings = sum(cat["warnings"] for cat in categories.values())
        total_duration = sum(cat["total_duration"] for cat in categories.values())
        
        success_rate = (total_passed / total_tests * 100) if total_tests > 0 else 0
        
        return {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total_tests": total_tests,
                "passed": total_passed,
                "failed": total_failed,
                "warnings": total_warnings,
                "success_rate": round(success_rate, 1),
                "total_duration": round(total_duration, 2)
            },
            "categories": categories,
            "config_used": self.config_path
        }
    
    def print_qa_report(self):
        """Печатает QA отчет."""
        
        report = self.generate_qa_report()
        summary = report["summary"]
        
        print("\n" + "=" * 60)
        print("📊 QA ОТЧЕТ - ТЕСТИРОВАНИЕ НА ДЕШЕВЫХ МОДЕЛЯХ")
        print("=" * 60)
        print(f"🕐 Время: {report['timestamp']}")
        print(f"⚙️ Конфигурация: {report['config_used']}")
        print("-" * 60)
        
        # Общая статистика
        status_emoji = "✅" if summary["failed"] == 0 else "⚠️" if summary["passed"] > summary["failed"] else "❌"
        
        print(f"{status_emoji} ОБЩИЙ РЕЗУЛЬТАТ:")
        print(f"  Всего тестов: {summary['total_tests']}")
        print(f"  Пройдено: {summary['passed']} ✅")
        print(f"  Не пройдено: {summary['failed']} ❌")
        print(f"  Предупреждения: {summary['warnings']} ⚠️")
        print(f"  Успешность: {summary['success_rate']}%")
        print(f"  Общее время: {summary['total_duration']}с")
        
        print("\n📂 РЕЗУЛЬТАТЫ ПО КАТЕГОРИЯМ:")
        print("-" * 60)
        
        # Результаты по категориям
        for category_name, category_data in report["categories"].items():
            passed = category_data["passed"]
            failed = category_data["failed"]
            warnings = category_data["warnings"]
            duration = category_data["total_duration"]
            
            cat_status = "✅" if failed == 0 else "⚠️" if passed > failed else "❌"
            
            print(f"{cat_status} {category_name.upper():20} | "
                  f"Пройдено: {passed:2d} | "
                  f"Ошибок: {failed:2d} | "
                  f"Предупр.: {warnings:2d} | "
                  f"Время: {duration:6.2f}с")
        
        # Детали неудачных тестов
        failed_tests = [r for r in self.results if not r.success]
        if failed_tests:
            print(f"\n❌ НЕУДАЧНЫЕ ТЕСТЫ ({len(failed_tests)}):")
            print("-" * 60)
            for test in failed_tests:
                print(f"  • {test.category}/{test.test_name}: {test.error}")
        
        # Предупреждения
        warning_tests = [r for r in self.results if r.warning]
        if warning_tests:
            print(f"\n⚠️ ПРЕДУПРЕЖДЕНИЯ ({len(warning_tests)}):")
            print("-" * 60)
            for test in warning_tests:
                print(f"  • {test.category}/{test.test_name}: {test.warning}")
        
        print("\n" + "=" * 60)
        
        if summary["failed"] == 0:
            print("🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО!")
        elif summary["passed"] > summary["failed"]:
            print("⚠️ БОЛЬШИНСТВО ТЕСТОВ ПРОЙДЕНО, ЕСТЬ ПРОБЛЕМЫ")
        else:
            print("💥 МНОГО КРИТИЧЕСКИХ ОШИБОК")
        
        print("=" * 60)


async def main():
    """Главная функция QA тестирования."""
    
    print("🤖 QA Тестирование системы Grid на дешевых моделях")
    print("💰 Используем только flash-lite и claude-haiku из OpenRouter")
    print("=" * 60)
    
    runner = QATestRunner()
    
    try:
        # Инициализация
        await runner.setup()
        
        # Запуск тестов
        await runner.run_all_qa_tests()
        
        # Генерация отчета
        runner.print_qa_report()
        
        # Сохранение отчета в файл
        report = runner.generate_qa_report()
        report_file = f"qa_report_{int(time.time())}.json"
        
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        print(f"\n💾 Детальный отчет сохранен: {report_file}")
        
        # Возвращаем код ошибки если есть неудачные тесты
        failed_count = report["summary"]["failed"]
        return 1 if failed_count > 0 else 0
    
    except KeyboardInterrupt:
        print("\n⏸️ QA тестирование прервано пользователем")
        return 1
    
    except Exception as e:
        print(f"\n💥 Критическая ошибка QA тестирования: {e}")
        traceback.print_exc()
        return 1
    
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
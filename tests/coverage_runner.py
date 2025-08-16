#!/usr/bin/env python3
"""
Скрипт для запуска тестов с измерением покрытия кода.
Автоматизирует процесс coverage анализа и генерации отчетов.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def run_command(cmd, description=""):
    """Выполняет команду и выводит результат."""
    if description:
        print(f"🔄 {description}")
    
    print(f"Выполняется: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"❌ Ошибка выполнения: {description}")
        print(f"Stderr: {result.stderr}")
        return False
    
    if result.stdout:
        print(result.stdout)
    
    return True


def install_coverage():
    """Устанавливает coverage если его нет."""
    try:
        import coverage
        print("✅ Coverage уже установлен")
        return True
    except ImportError:
        print("📦 Устанавливаем coverage...")
        return run_command("pip install coverage", "Установка coverage")


def run_tests_with_coverage(test_pattern="", markers="", verbose=False):
    """Запускает тесты с измерением покрытия."""
    # Базовая команда
    cmd_parts = ["coverage", "run", "-m", "pytest"]
    
    # Добавляем параметры pytest
    if verbose:
        cmd_parts.append("-v")
    else:
        cmd_parts.append("-q")
    
    if markers:
        cmd_parts.extend(["-m", markers])
    
    if test_pattern:
        cmd_parts.append(test_pattern)
    else:
        cmd_parts.append("tests/")
    
    cmd = " ".join(cmd_parts)
    
    success = run_command(cmd, "Запуск тестов с измерением покрытия")
    return success


def generate_coverage_report(format_type="terminal"):
    """Генерирует отчет о покрытии."""
    if format_type == "terminal":
        return run_command("coverage report -m", "Генерация отчета в терминале")
    elif format_type == "html":
        success = run_command("coverage html", "Генерация HTML отчета")
        if success:
            html_path = Path("htmlcov/index.html").absolute()
            print(f"📊 HTML отчет сохранен: {html_path}")
            if os.name == 'nt':  # Windows
                print(f"Откройте в браузере: file:///{html_path}")
            else:
                print(f"Откройте в браузере: file://{html_path}")
        return success
    elif format_type == "xml":
        return run_command("coverage xml", "Генерация XML отчета")
    elif format_type == "json":
        return run_command("coverage json", "Генерация JSON отчета")


def show_coverage_summary():
    """Показывает краткую сводку о покрытии."""
    print("\n" + "="*60)
    print("📈 СВОДКА ПОКРЫТИЯ КОДА")
    print("="*60)
    
    # Получаем общий процент покрытия
    result = subprocess.run(
        "coverage report --format=total", 
        shell=True, 
        capture_output=True, 
        text=True
    )
    
    if result.returncode == 0:
        try:
            total_coverage = int(result.stdout.strip())
            if total_coverage >= 90:
                emoji = "🎉"
                status = "ОТЛИЧНО"
            elif total_coverage >= 80:
                emoji = "✅"
                status = "ХОРОШО"
            elif total_coverage >= 70:
                emoji = "⚠️"
                status = "УДОВЛЕТВОРИТЕЛЬНО"
            else:
                emoji = "❌"
                status = "НУЖНО УЛУЧШИТЬ"
            
            print(f"{emoji} Общее покрытие: {total_coverage}% - {status}")
        except ValueError:
            print("⚠️ Не удалось получить общий процент покрытия")
    
    # Показываем детальный отчет
    run_command("coverage report", "")


def main():
    parser = argparse.ArgumentParser(description="Запуск тестов с измерением покрытия")
    parser.add_argument(
        "--pattern", 
        default="",
        help="Паттерн для фильтрации тестов (например, tests/test_agents.py)"
    )
    parser.add_argument(
        "--markers", 
        default="",
        help="Маркеры pytest (например, 'unit', 'not integration')"
    )
    parser.add_argument(
        "--format", 
        choices=["terminal", "html", "xml", "json", "all"],
        default="terminal",
        help="Формат отчета о покрытии"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Подробный вывод"
    )
    parser.add_argument(
        "--no-tests",
        action="store_true",
        help="Только генерация отчетов (без запуска тестов)"
    )
    parser.add_argument(
        "--install-deps",
        action="store_true",
        help="Установить зависимости перед запуском"
    )
    
    args = parser.parse_args()
    
    print("🧪 АВТОМАТИЗИРОВАННЫЙ АНАЛИЗ ПОКРЫТИЯ ТЕСТАМИ")
    print("="*50)
    
    # Устанавливаем зависимости
    if args.install_deps:
        if not install_coverage():
            sys.exit(1)
    
    # Проверяем наличие coverage
    if not install_coverage():
        sys.exit(1)
    
    # Запускаем тесты
    if not args.no_tests:
        success = run_tests_with_coverage(
            test_pattern=args.pattern,
            markers=args.markers,
            verbose=args.verbose
        )
        
        if not success:
            print("❌ Тесты завершились с ошибками")
            sys.exit(1)
    
    # Генерируем отчеты
    print("\n🔄 Генерация отчетов о покрытии...")
    
    if args.format == "all":
        formats = ["terminal", "html", "xml", "json"]
    else:
        formats = [args.format]
    
    for fmt in formats:
        if not generate_coverage_report(fmt):
            print(f"⚠️ Не удалось создать {fmt} отчет")
    
    # Показываем сводку
    show_coverage_summary()
    
    print("\n✅ Анализ покрытия завершен!")
    print("\nРекомендации:")
    print("- Стремитесь к покрытию 80%+ для критических компонентов")
    print("- Добавьте тесты для непокрытых функций")
    print("- Используйте integration тесты для сложных сценариев")


if __name__ == "__main__":
    main() 
#!/usr/bin/env python3
"""
Grid Agent System MCP Server (STDIO)

Правильный MCP сервер для Cursor с STDIO протоколом.
Подключает Grid агентов как MCP инструменты.

Логгирование:
- Логи сохраняются в logs/mcp_server.log
- Ротация: 10MB файлы, до 5 backup копий  
- Уровень логгирования: переменная окружения MCP_LOG_LEVEL (DEBUG, INFO, WARNING, ERROR)
- По умолчанию: INFO уровень

Примеры запуска:
    python grid_mcp_server.py                    # INFO логи
    MCP_LOG_LEVEL=DEBUG python grid_mcp_server.py    # DEBUG логи
"""

import asyncio
import sys
import os
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from logging.handlers import RotatingFileHandler
from datetime import datetime

# Исправляем проблему с буферизацией в Windows
if sys.platform == "win32":
    import msvcrt
    # Отключаем буферизацию для Windows
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

# Добавляем путь к Grid системе
sys.path.insert(0, str(Path(__file__).parent))

# Подавляем лишние логи Grid системы
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# Импорты MCP
from mcp.server.fastmcp import FastMCP

# Grid импорты (подавляем их выводы)
class SilentLogger:
    def write(self, s): return len(s)
    def flush(self): pass

# Подавляем логи при импорте
original_stderr = sys.stderr
original_stdout = sys.stdout
sys.stderr = SilentLogger()
sys.stdout = SilentLogger()

try:
    from core.config import Config
    from core.agent_factory import AgentFactory
except Exception as e:
    print(f"[MCP] Ошибка импорта Grid: {e}", file=sys.stderr)
    # Создаем заглушки для тестирования
    class Config:
        def list_agents(self): return {"test": "Test agent"}
    class AgentFactory:
        async def initialize(self): pass
        async def run_agent(self, agent, message): return f"Test response from {agent}: {message}"
finally:
    sys.stderr = original_stderr
    sys.stdout = original_stdout

def setup_logging():
    """Настройка логгирования в файл для MCP сервера."""
    # Создаем директорию для логов
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    
    # Путь к файлу лога
    log_file = log_dir / "mcp_server.log"
    
    # Определяем уровень логгирования из переменной окружения
    log_level = os.environ.get('MCP_LOG_LEVEL', 'INFO').upper()
    numeric_level = getattr(logging, log_level, logging.INFO)
    
    # Создаем logger для MCP сервера
    logger = logging.getLogger("mcp_server")
    logger.setLevel(numeric_level)
    
    # Убираем существующие handlers
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # Настраиваем RotatingFileHandler
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,  # 10 MB
        backupCount=5,
        encoding='utf-8'
    )
    
    # Формат логов с более детальной информацией
    formatter = logging.Formatter(
        '%(asctime)s - [%(levelname)s] - %(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    
    # Добавляем handler к logger
    logger.addHandler(file_handler)
    
    # Отключаем propagation чтобы не дублировать в root logger
    logger.propagate = False
    
    return logger

# Настраиваем логгирование
mcp_logger = setup_logging()

# Инициализируем MCP сервер
mcp = FastMCP("Grid Agent System")

# Глобальные переменные для Grid компонентов
config: Optional[Config] = None
agent_factory: Optional[AgentFactory] = None

async def initialize_grid():
    """Инициализация Grid системы."""
    global config, agent_factory
    
    if config is not None:
        mcp_logger.info("Grid система уже инициализирована")
        return  # Уже инициализирована
    
    mcp_logger.info("Начинаем инициализацию Grid системы")
    
    try:
        # Подавляем логи во время инициализации
        original_stderr = sys.stderr
        sys.stderr = SilentLogger()
        
        # Загружаем конфигурацию
        config_path = Path(__file__).parent / "config.yaml"
        if not config_path.exists():
            config_path = Path(__file__).parent / "config.yaml.example"
            mcp_logger.warning(f"config.yaml не найден, используем config.yaml.example")
        
        mcp_logger.info(f"Загружаем конфигурацию из {config_path}")
        config = Config(str(config_path))
        agent_factory = AgentFactory(config)
        await agent_factory.initialize()
        
        # Восстанавливаем stderr для отладочной информации
        sys.stderr = original_stderr
        
        # Выводим список доступных агентов
        agents = config.list_agents()
        mcp_logger.info(f"Grid система успешно инициализирована с {len(agents)} агентами")
        
        # Логируем доступных агентов
        for key, desc in agents.items():
            mcp_logger.debug(f"Агент: {key} - {desc}")
        
        print(f"[MCP] Инициализирован с {len(agents)} агентами:", file=sys.stderr)
        for key, desc in agents.items():
            print(f"  - {key}: {desc}", file=sys.stderr)
            
    except Exception as e:
        sys.stderr = original_stderr
        mcp_logger.error(f"Ошибка инициализации Grid системы: {str(e)}", exc_info=True)
        print(f"[MCP] Ошибка инициализации: {e}", file=sys.stderr)
        # Создаем заглушки для работы
        config = Config()
        agent_factory = AgentFactory()

def safe_encode(text: str) -> str:
    """Безопасно кодирует текст для вывода."""
    try:
        # Пытаемся закодировать как UTF-8
        return text.encode('utf-8', errors='replace').decode('utf-8')
    except:
        # Если не получается, заменяем проблемные символы
        return text.encode('ascii', errors='replace').decode('ascii')

async def run_agent_with_timeout(agent_key: str, message: str, timeout: int = None) -> str:
    """Запускает агента без таймаута."""
    start_time = datetime.now()
    mcp_logger.info(f"Запуск агента '{agent_key}' с сообщением: {message[:100]}...")
    
    try:
        # Запускаем агента без таймаута
        result = await agent_factory.run_agent(agent_key, message)
        
        # Измеряем время выполнения
        execution_time = (datetime.now() - start_time).total_seconds()
        mcp_logger.info(f"Агент '{agent_key}' успешно выполнен за {execution_time:.2f}с")
        mcp_logger.debug(f"Результат агента '{agent_key}': {str(result)[:200]}...")
        
        # Безопасно кодируем результат
        safe_result = safe_encode(str(result))
        return safe_result
        
    except Exception as e:
        execution_time = (datetime.now() - start_time).total_seconds()
        error_msg = f"Ошибка выполнения агента {agent_key}: {str(e)}"
        mcp_logger.error(f"Ошибка в агенте '{agent_key}' за {execution_time:.2f}с: {str(e)}", exc_info=True)
        return safe_encode(error_msg)

# Регистрируем инструменты для каждого агента
@mcp.tool()
async def run_assistant(message: str) -> str:
    """Запускает базового помощника для общих задач"""
    mcp_logger.debug("MCP инструмент: run_assistant вызван")
    try:
        await initialize_grid()
        if not agent_factory:
            mcp_logger.error("Агентская фабрика не инициализирована")
            return "Ошибка: система не инициализирована"
        
        return await run_agent_with_timeout("assistant", message)
    except Exception as e:
        mcp_logger.error(f"Критическая ошибка в run_assistant: {str(e)}", exc_info=True)
        return f"Ошибка в run_assistant: {str(e)}"

@mcp.tool()
async def run_file_agent(message: str) -> str:
    """Запускает файлового агента для работы с файлами"""
    mcp_logger.debug("MCP инструмент: run_file_agent вызван")
    try:
        await initialize_grid()
        if not agent_factory:
            mcp_logger.error("Агентская фабрика не инициализирована для file_agent")
            return "Ошибка: система не инициализирована"
        
        return await run_agent_with_timeout("file_agent", message)
    except Exception as e:
        mcp_logger.error(f"Критическая ошибка в run_file_agent: {str(e)}", exc_info=True)
        return f"Ошибка в run_file_agent: {str(e)}"

@mcp.tool()
async def run_git_agent(message: str) -> str:
    """Запускает Git агента для работы с репозиторием"""
    mcp_logger.debug("MCP инструмент: run_git_agent вызван")
    try:
        await initialize_grid()
        if not agent_factory:
            mcp_logger.error("Агентская фабрика не инициализирована для git_agent")
            return "Ошибка: система не инициализирована"
        
        return await run_agent_with_timeout("git_agent", message)
    except Exception as e:
        mcp_logger.error(f"Критическая ошибка в run_git_agent: {str(e)}", exc_info=True)
        return f"Ошибка в run_git_agent: {str(e)}"

@mcp.tool()
async def run_code_agent(message: str) -> str:
    """Запускает кодового агента для работы с программированием"""
    mcp_logger.debug("MCP инструмент: run_code_agent вызван")
    try:
        await initialize_grid()
        if not agent_factory:
            mcp_logger.error("Агентская фабрика не инициализирована для code_agent")
            return "Ошибка: система не инициализирована"
        
        return await run_agent_with_timeout("code_agent", message)
    except Exception as e:
        mcp_logger.error(f"Критическая ошибка в run_code_agent: {str(e)}", exc_info=True)
        return f"Ошибка в run_code_agent: {str(e)}"

@mcp.tool()
async def run_coordinator(message: str) -> str:
    """Запускает координатора для сложных задач"""
    mcp_logger.debug("MCP инструмент: run_coordinator вызван")
    try:
        await initialize_grid()
        if not agent_factory:
            mcp_logger.error("Агентская фабрика не инициализирована для coordinator")
            return "Ошибка: система не инициализирована"
        
        return await run_agent_with_timeout("coordinator", message)
    except Exception as e:
        mcp_logger.error(f"Критическая ошибка в run_coordinator: {str(e)}", exc_info=True)
        return f"Ошибка в run_coordinator: {str(e)}"

@mcp.tool()
async def run_full_agent(message: str) -> str:
    """Запускает универсального агента со всеми инструментами"""
    mcp_logger.debug("MCP инструмент: run_full_agent вызван")
    try:
        await initialize_grid()
        if not agent_factory:
            mcp_logger.error("Агентская фабрика не инициализирована для full_agent")
            return "Ошибка: система не инициализирована"
        
        return await run_agent_with_timeout("full_agent", message)
    except Exception as e:
        mcp_logger.error(f"Критическая ошибка в run_full_agent: {str(e)}", exc_info=True)
        return f"Ошибка в run_full_agent: {str(e)}"

@mcp.tool()
async def run_thinker(message: str) -> str:
    """Запускает мыслящего агента для глубокого анализа"""
    mcp_logger.debug("MCP инструмент: run_thinker вызван")
    try:
        await initialize_grid()
        if not agent_factory:
            mcp_logger.error("Агентская фабрика не инициализирована для thinker")
            return "Ошибка: система не инициализирована"
        
        return await run_agent_with_timeout("thinker", message)
    except Exception as e:
        mcp_logger.error(f"Критическая ошибка в run_thinker: {str(e)}", exc_info=True)
        return f"Ошибка в run_thinker: {str(e)}"

@mcp.tool()
async def list_available_agents() -> str:
    """Возвращает список всех доступных Grid агентов"""
    mcp_logger.debug("MCP инструмент: list_available_agents вызван")
    try:
        await initialize_grid()
        if not config:
            mcp_logger.error("Конфигурация не инициализирована для list_available_agents")
            return "Ошибка: система не инициализирована"
        
        agents = config.list_agents()
        mcp_logger.info(f"Запрошен список агентов, найдено {len(agents)} агентов")
        
        result = "Доступные Grid агенты:\n"
        for key, desc in agents.items():
            result += f"- {key}: {desc}\n"
        return safe_encode(result)
    except Exception as e:
        mcp_logger.error(f"Ошибка получения списка агентов: {str(e)}", exc_info=True)
        return f"Ошибка получения списка агентов: {str(e)}"

@mcp.tool()
async def test_connection() -> str:
    """Тестовый инструмент для проверки соединения"""
    mcp_logger.debug("MCP инструмент: test_connection вызван")
    mcp_logger.info("Тест соединения успешно выполнен")
    return "✅ MCP сервер работает корректно!"

if __name__ == "__main__":
    # Логгируем запуск сервера
    mcp_logger.info("=== Запуск Grid Agent System MCP Server ===")
    mcp_logger.info(f"Операционная система: {sys.platform}")
    mcp_logger.info(f"Рабочая директория: {Path.cwd()}")
    
    print("[MCP] Запуск Grid Agent System MCP Server...", file=sys.stderr)
    
    # Исправляем проблему с буферизацией в Windows
    if sys.platform == "win32":
        mcp_logger.info("Настройка буферизации для Windows")
        # Принудительно отключаем буферизацию
        import os
        os.environ['PYTHONUNBUFFERED'] = '1'
        
        # Пересоздаем stdout и stderr без буферизации
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)
    
    try:
        mcp_logger.info("Запуск MCP сервера через STDIO протокол")
        mcp.run(transport="stdio")
    except Exception as e:
        mcp_logger.critical(f"Критическая ошибка при запуске MCP сервера: {str(e)}", exc_info=True)
        raise
    finally:
        mcp_logger.info("=== Завершение работы Grid Agent System MCP Server ===") 
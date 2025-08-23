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
from typing import Dict, Optional
from logging.handlers import RotatingFileHandler
from datetime import datetime

# Не трогаем буферизацию stdout/stderr, чтобы не ломать STDIO MCP на Windows

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

# Не переназначаем stdout/stderr при импорте, просто пытаемся импортировать и, если нужно, создаём заглушки
try:
    from core.config import Config
    from core.agent_factory import AgentFactory
except Exception as e:
    mcp_logger.error(f"[MCP] Ошибка импорта Grid: {e}")
    class Config:
        def list_agents(self): return {"test": "Test agent"}
        def get_agent(self, key): return type("A", (), {"name": key})
    class AgentFactory:
        async def initialize(self): pass
        async def run_agent(self, agent, message, context_path=None, stream=False): return f"Test response from {agent}: {message}"
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

# Трекер зарегистрированных инструментов для избежания дубликатов
_registered_agent_tools: Dict[str, bool] = {}

async def initialize_grid():
    """Инициализация Grid системы."""
    global config, agent_factory
    
    if config is not None:
        mcp_logger.info("Grid система уже инициализирована")
        return  # Уже инициализирована
    
    mcp_logger.info("Начинаем инициализацию Grid системы")
    
    try:
        # Сохраняем STDIO нетронутым, чтобы MCP корректно работал
        # Загружаем конфигурацию
        config_path = Path(__file__).parent / "config.yaml"
        if not config_path.exists():
            config_path = Path(__file__).parent / "config.yaml.example"
            mcp_logger.warning("config.yaml не найден, используем config.yaml.example")
        
        mcp_logger.info(f"Загружаем конфигурацию из {config_path}")
        config = Config(str(config_path))
        agent_factory = AgentFactory(config)
        await agent_factory.initialize()
        
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

def _normalize_for_stdio(text: str) -> str:
    """Нормализует текст для безопасной отдачи через STDIO MCP: без переводов строк и управляющих символов."""
    try:
        normalized = text.replace('\r', ' ').replace('\n', ' ').strip()
        # Режем избыточно длинные ответы, чтобы не упереться в буферы клиентов
        if len(normalized) > 10000:
            normalized = normalized[:10000] + "..."
        return normalized
    except Exception:
        return text

async def run_agent_with_timeout(agent_key: str, message: str, context_path: Optional[str] = None, stream: bool = False, timeout: int = None) -> str:
    """Запускает агента, опционально со стримом. Таймаут управляется в фабрике агентов."""
    start_time = datetime.now()
    preview = (message or "")[:100]
    mcp_logger.info(f"Запуск агента '{agent_key}' с сообщением: {preview}...")
    
    try:
        # Запускаем агента без переназначения STDIO, чтобы не ломать MCP транспорт
        result = await agent_factory.run_agent(agent_key, message, context_path=context_path, stream=stream)
        
        # Измеряем время выполнения
        execution_time = (datetime.now() - start_time).total_seconds()
        mcp_logger.info(f"Агент '{agent_key}' успешно выполнен за {execution_time:.2f}с")
        mcp_logger.debug(f"Сырой результат агента '{agent_key}': {str(result)[:200]}...")
        
        # Безопасно кодируем результат
        safe_result = safe_encode(str(result))
        normalized = _normalize_for_stdio(safe_result)
        mcp_logger.debug(f"Нормализованный результат (len={len(normalized)}): {normalized[:200]}...")
        # Возвращаем строку (ожидаемый формат по умолчанию для FastMCP)
        return normalized
        
    except Exception as e:
        execution_time = (datetime.now() - start_time).total_seconds()
        error_msg = f"Ошибка выполнения агента {agent_key}: {str(e)}"
        mcp_logger.error(f"Ошибка в агенте '{agent_key}' за {execution_time:.2f}с: {str(e)}", exc_info=True)
        # Возвращаем строку с ошибкой
        return _normalize_for_stdio(safe_encode(error_msg))

# ---------- Динамическая регистрация инструментов для агентов ----------
async def register_agent_tools() -> None:
    """Регистрирует MCP-инструменты для всех агентов из конфигурации."""
    await initialize_grid()
    if not config:
        mcp_logger.error("Конфигурация не инициализирована при регистрации инструмента")
        return
    agents = config.list_agents()
    for agent_key, agent_desc in agents.items():
        if _registered_agent_tools.get(agent_key):
            continue
        try:
            tool_name = f"run_{agent_key}"
            doc = f"Запускает агента '{agent_key}'. {agent_desc}"
            
            # Фабрика для создания инструмента с захваченным agent_key без дополнительных параметров в сигнатуре
            def make_tool_impl(fixed_agent_key: str, fixed_tool_name: str):
                async def tool_impl(message: str, context_path: Optional[str] = None, stream: bool = False):
                    mcp_logger.debug(f"MCP инструмент: {fixed_tool_name} вызван")
                    try:
                        await initialize_grid()
                        if not agent_factory:
                            mcp_logger.error("Агентская фабрика не инициализирована")
                            return [{"type": "text", "text": "Ошибка: система не инициализирована"}]
                        result_text = await run_agent_with_timeout(fixed_agent_key, message, context_path=context_path, stream=stream)
                        mcp_logger.debug(f"{fixed_tool_name} отдаёт (len={len(result_text)}): {result_text[:200]}...")
                        return [{"type": "text", "text": result_text}]
                    except Exception as e:
                        mcp_logger.error(f"Критическая ошибка в {fixed_tool_name}: {str(e)}", exc_info=True)
                        return [{"type": "text", "text": f"Ошибка в {fixed_tool_name}: {str(e)}"}]
                # Настроим метаданные функции до регистрации
                tool_impl.__name__ = fixed_tool_name
                tool_impl.__doc__ = doc
                return tool_impl
            
            # Создаем реализацию инструмента
            tool_impl = make_tool_impl(agent_key, tool_name)
            
            # Регистрируем функцию как MCP инструмент
            mcp.tool()(tool_impl)
            _registered_agent_tools[agent_key] = True
            mcp_logger.info(f"Зарегистрирован MCP инструмент: {tool_name}")
        except Exception as e:
            mcp_logger.error(f"Не удалось зарегистрировать инструмент для агента '{agent_key}': {e}")

# ---------- Универсальные инструменты ----------
@mcp.tool()
async def call_agent(agent: str, message: str, context_path: Optional[str] = None, stream: bool = False):
    """Вызывает любого агента по ключу: agent=<ключ>, message=<запрос>, context_path=<опционально>, stream=<false|true>."""
    mcp_logger.debug("MCP инструмент: call_agent вызван")
    try:
        await initialize_grid()
        if not agent_factory or not config:
            mcp_logger.error("Система не инициализирована для call_agent")
            return [{"type": "text", "text": "Ошибка: система не инициализирована"}]
        agents = config.list_agents()
        if agent not in agents:
            return [{"type": "text", "text": safe_encode(f"Ошибка: агент '{agent}' не найден. Доступные: {', '.join(agents.keys())}") }]
        result_text = await run_agent_with_timeout(agent, message, context_path=context_path, stream=stream)
        mcp_logger.debug(f"call_agent отдаёт (len={len(result_text)}): {result_text[:200]}...")
        return [{"type": "text", "text": result_text}]
    except Exception as e:
        mcp_logger.error(f"Критическая ошибка в call_agent: {str(e)}", exc_info=True)
        return [{"type": "text", "text": f"Ошибка в call_agent: {str(e)}"}]

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
        os.environ['PYTHONUNBUFFERED'] = '1'
        # Используем reconfigure из начала файла; не переопределяем stdout/stderr, чтобы не ломать STDIO MCP
    
    try:
        # Инициализация и динамическая регистрация инструментов до старта
        asyncio.get_event_loop().run_until_complete(initialize_grid())
        asyncio.get_event_loop().run_until_complete(register_agent_tools())
        
        mcp_logger.info("Запуск MCP сервера через STDIO протокол")
        mcp.run(transport="stdio")
    except Exception as e:
        mcp_logger.critical(f"Критическая ошибка при запуске MCP сервера: {str(e)}", exc_info=True)
        raise
    finally:
        mcp_logger.info("=== Завершение работы Grid Agent System MCP Server ===") 
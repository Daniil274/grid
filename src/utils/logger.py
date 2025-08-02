"""
Система логгирования для агентов и инструментов.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict
import json
from datetime import datetime
from dotenv import load_dotenv

class AgentLogger:
    """Централизованная система логгирования для агентов."""
    
    _instance = None
    _initialized = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AgentLogger, cls).__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            self._setup_logging()
            self._initialized = True
    
    def _setup_logging(self):
        """Настройка системы логгирования."""
        # Загружаем .env файл
        load_dotenv()
        
        # Читаем настройки из .env и конфигурации
        debug_env = os.getenv('DEBUG_LOGGING', 'false').lower() == 'true'
        debug_config = os.getenv('DEBUG', 'false').lower() == 'true'
        self.debug_enabled = debug_env or debug_config
        self.log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
        self.log_agents = os.getenv('LOG_AGENTS', 'true').lower() == 'true'
        self.log_tools = os.getenv('LOG_TOOLS', 'true').lower() == 'true'
        self.log_communications = os.getenv('LOG_COMMUNICATIONS', 'true').lower() == 'true'
        self.log_errors = os.getenv('LOG_ERRORS', 'true').lower() == 'true'
        self.log_file = os.getenv('LOG_FILE')
        
        # Создаем основной логгер
        self.logger = logging.getLogger('AgentSystem')
        # Устанавливаем уровень DEBUG для основного логгера, чтобы все сообщения проходили
        self.logger.setLevel(logging.DEBUG)
        
        # Очищаем существующие обработчики
        self.logger.handlers.clear()
        
        # Настраиваем форматирование
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)8s | %(name)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Консольный вывод (только INFO и выше)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
        
        # Файловый вывод с датой и временем в названии (INFO и выше)
        if not self.log_file:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            self.log_file = f"logs/agents_{timestamp}.log"
            
        if self.log_file:
            log_path = Path(self.log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding='utf-8')
            file_handler.setLevel(logging.INFO)  # INFO и выше в основной файл
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
        
        # Отдельный файл для DEBUG логов (полные промпты)
        debug_log_file = f"logs/debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        debug_log_path = Path(debug_log_file)
        debug_log_path.parent.mkdir(parents=True, exist_ok=True)
        debug_file_handler = logging.FileHandler(debug_log_path, encoding='utf-8')
        debug_file_handler.setLevel(logging.DEBUG)  # Все уровни в debug файл
        debug_file_handler.setFormatter(formatter)
        self.logger.addHandler(debug_file_handler)
    
    def _should_log(self, category: str) -> bool:
        """Проверяет, нужно ли логгировать для данной категории."""
        if not self.debug_enabled:
            return False
        
        category_map = {
            'agents': self.log_agents,
            'tools': self.log_tools,
            'communications': self.log_communications,
            'errors': self.log_errors,
            'git_command': self.log_tools,  # Git команды как часть инструментов
            'file_operation': self.log_tools,  # Файловые операции как часть инструментов
            'prompt_building': self.log_agents,  # Построение промптов как часть агентов
            'agent_creation': self.log_agents,  # Создание агентов
            'test': True,  # Тестовые сообщения
            'coordinator_prompt': True,  # Промпты координатора
        }
        
        return category_map.get(category, True)
    
    def log_agent_start(self, agent_name: str, input_data: str):
        """Логгирование запуска агента."""
        if self._should_log('agents'):
            # Создаем отдельный логгер для каждого агента
            agent_logger = logging.getLogger(f'Agent.{agent_name}')
            agent_logger.setLevel(self.logger.level)
            if not agent_logger.handlers:
                for handler in self.logger.handlers:
                    agent_logger.addHandler(handler)
            
            # Показываем краткую информацию о входных данных
            input_summary = input_data[:100].replace('\n', ' ').strip()
            if len(input_data) > 100:
                input_summary += "..."
            
            agent_logger.info(f"🤖 START | 📝 {input_summary}")
            agent_logger.debug(f"   📝 Full Input: {input_data}")
    
    def log_agent_end(self, agent_name: str, output_data: str, duration: float):
        """Логгирование завершения работы агента."""
        if self._should_log('agents'):
            agent_logger = logging.getLogger(f'Agent.{agent_name}')
            agent_logger.setLevel(self.logger.level)
            if not agent_logger.handlers:
                for handler in self.logger.handlers:
                    agent_logger.addHandler(handler)
            
            # Показываем краткую информацию о выходных данных
            output_summary = output_data[:100].replace('\n', ' ').strip()
            if len(output_data) > 100:
                output_summary += "..."
                    
            agent_logger.info(f"✅ END (took {duration:.2f}s) | 📤 {output_summary}")
            agent_logger.debug(f"   📤 Full Output: {output_data}")
    
    def log_agent_error(self, agent_name: str, error: Exception):
        """Логгирование ошибки агента."""
        if self._should_log('errors'):
            agent_logger = logging.getLogger(f'Agent.{agent_name}')
            agent_logger.setLevel(self.logger.level)
            if not agent_logger.handlers:
                for handler in self.logger.handlers:
                    agent_logger.addHandler(handler)
                    
            agent_logger.error(f"❌ ERROR - {str(error)}")
            agent_logger.debug(f"   🔍 Error details:", exc_info=True)
    
    def log_tool_start(self, tool_name: str, args: Dict[str, Any]):
        """Логгирование запуска инструмента."""
        if self._should_log('tools'):
            tool_logger = logging.getLogger(f'Tool.{tool_name}')
            tool_logger.setLevel(self.logger.level)
            if not tool_logger.handlers:
                for handler in self.logger.handlers:
                    tool_logger.addHandler(handler)
            
            # Показываем краткую информацию о аргументах
            args_summary = str(args)[:100].replace('\n', ' ').strip()
            if len(str(args)) > 100:
                args_summary += "..."
                    
            tool_logger.info(f"🔧 START | 📋 {args_summary}")
            tool_logger.debug(f"   📋 Full Args: {json.dumps(args, ensure_ascii=False, indent=2)}")
    
    def log_tool_end(self, tool_name: str, result: str, duration: float):
        """Логгирование завершения работы инструмента."""
        if self._should_log('tools'):
            tool_logger = logging.getLogger(f'Tool.{tool_name}')
            tool_logger.setLevel(self.logger.level)
            if not tool_logger.handlers:
                for handler in self.logger.handlers:
                    tool_logger.addHandler(handler)
            
            # Показываем краткую информацию о результате
            result_summary = result[:100].replace('\n', ' ').strip()
            if len(result) > 100:
                result_summary += "..."
                    
            tool_logger.info(f"✅ END (took {duration:.2f}s) | 📤 {result_summary}")
            tool_logger.debug(f"   📤 Full Result: {result}")
    
    def log_tool_error(self, tool_name: str, error: Exception):
        """Логгирование ошибки инструмента."""
        if self._should_log('errors'):
            tool_logger = logging.getLogger(f'Tool.{tool_name}')
            tool_logger.setLevel(self.logger.level)
            if not tool_logger.handlers:
                for handler in self.logger.handlers:
                    tool_logger.addHandler(handler)
                    
            tool_logger.error(f"❌ ERROR - {str(error)}")
            tool_logger.debug(f"   🔍 Error details:", exc_info=True)
    
    def log_communication(self, sender: str, receiver: str, message_type: str, content: str):
        """Логгирование коммуникации между агентами."""
        if self._should_log('communications'):
            self.logger.info(f"💬 COMM: {sender} → {receiver} ({message_type})")
            self.logger.debug(f"   📄 Content: {content[:200]}{'...' if len(content) > 200 else ''}")
    
    def log_context_change(self, context_name: str, old_value: Any, new_value: Any):
        """Логгирование изменения контекста."""
        if self._should_log('agents'):
            self.logger.debug(f"🔄 CONTEXT_CHANGE: {context_name}")
            self.logger.debug(f"   ⬅️ Old: {str(old_value)[:100]}{'...' if len(str(old_value)) > 100 else ''}")
            self.logger.debug(f"   ➡️ New: {str(new_value)[:100]}{'...' if len(str(new_value)) > 100 else ''}")
    
    def log_custom(self, level: str, category: str, message: str, **kwargs):
        """Кастомное логгирование."""
        if self._should_log(category):
            log_method = getattr(self.logger, level.lower(), self.logger.info)
            log_method(f"🔹 {category.upper()}: {message}")
            
            for key, value in kwargs.items():
                self.logger.debug(f"   {key}: {value}")
        else:
            # Для DEBUG сообщений всегда логируем, если DEBUG включен
            if level.lower() == 'debug' and self.debug_enabled:
                log_method = getattr(self.logger, level.lower(), self.logger.debug)
                log_method(f"🔹 {category.upper()}: {message}")
                
                for key, value in kwargs.items():
                    self.logger.debug(f"   {key}: {value}")
    
    def log_agent_prompt(self, agent_name: str, prompt: str):
        """Логгирование промпта агента."""
        if self._should_log('agents'):
            agent_logger = logging.getLogger(f'Agent.{agent_name}')
            agent_logger.setLevel(self.logger.level)
            if not agent_logger.handlers:
                for handler in self.logger.handlers:
                    agent_logger.addHandler(handler)
            
            # Краткая информация в консоль (INFO уровень)
            prompt_summary = prompt[:200].replace('\n', ' ').strip()
            if len(prompt) > 200:
                prompt_summary += "..."
            agent_logger.info(f"📝 PROMPT ({len(prompt)} chars): {prompt_summary}")
            
            # Полный промпт в файл (DEBUG уровень)
            agent_logger.debug(f"📄 FULL PROMPT FOR {agent_name}:")
            agent_logger.debug(f"   {'='*80}")
            
            # Разбиваем промпт на строки для лучшего отображения
            lines = prompt.split('\n')
            for i, line in enumerate(lines, 1):
                if line.strip():  # Пропускаем пустые строки
                    agent_logger.debug(f"   {i:3d}: {line}")
                else:
                    agent_logger.debug(f"   {i:3d}: <empty>")
            
            agent_logger.debug(f"   {'='*80}")
            agent_logger.debug(f"📄 END OF PROMPT FOR {agent_name}")

# Глобальный экземпляр логгера
logger = AgentLogger()

# Удобные функции для использования
def log_agent_start(agent_name: str, input_data: str):
    """Логгирование запуска агента."""
    logger.log_agent_start(agent_name, input_data)

def log_agent_end(agent_name: str, output_data: str, duration: float):
    """Логгирование завершения работы агента."""
    logger.log_agent_end(agent_name, output_data, duration)

def log_agent_error(agent_name: str, error: Exception):
    """Логгирование ошибки агента."""
    logger.log_agent_error(agent_name, error)

def log_tool_start(tool_name: str, args: Dict[str, Any]):
    """Логгирование запуска инструмента."""
    logger.log_tool_start(tool_name, args)

def log_tool_end(tool_name: str, result: str, duration: float):
    """Логгирование завершения работы инструмента."""
    logger.log_tool_end(tool_name, result, duration)

def log_tool_error(tool_name: str, error: Exception):
    """Логгирование ошибки инструмента."""
    logger.log_tool_error(tool_name, error)

def log_communication(sender: str, receiver: str, message_type: str, content: str):
    """Логгирование коммуникации между агентами."""
    logger.log_communication(sender, receiver, message_type, content)

def log_context_change(context_name: str, old_value: Any, new_value: Any):
    """Логгирование изменения контекста."""
    logger.log_context_change(context_name, old_value, new_value)

def log_custom(level: str, category: str, message: str, **kwargs):
    """Кастомное логгирование."""
    logger.log_custom(level, category, message, **kwargs)

def log_agent_prompt(agent_name: str, prompt: str):
    """Логгирование промпта агента."""
    logger.log_agent_prompt(agent_name, prompt)

def log_tool_performance(tool_name: str, operation: str, duration: float, **kwargs):
    """Логгирование производительности инструментов."""
    if logger._should_log('tools'):
        tool_logger = logging.getLogger(f'Tool.{tool_name}')
        tool_logger.setLevel(logger.logger.level)
        # Проверяем, что хендлеры еще не добавлены
        if not tool_logger.handlers:
            for handler in logger.logger.handlers:
                tool_logger.addHandler(handler)
        
        tool_logger.debug(f"⚡ PERFORMANCE: {operation} took {duration:.3f}s")
        for key, value in kwargs.items():
            tool_logger.debug(f"   {key}: {value}")

def log_tool_usage(tool_name: str, args: Dict[str, Any], success: bool, duration: float):
    """Логгирование использования инструментов для статистики."""
    if logger._should_log('tools'):
        tool_logger = logging.getLogger(f'Tool.{tool_name}')
        tool_logger.setLevel(logger.logger.level)
        # Проверяем, что хендлеры еще не добавлены
        if not tool_logger.handlers:
            for handler in logger.logger.handlers:
                tool_logger.addHandler(handler)
        
        status = "✅ SUCCESS" if success else "❌ FAILED"
        tool_logger.info(f"📊 USAGE: {tool_name} - {status} ({duration:.3f}s)")
        tool_logger.debug(f"   📋 Args: {json.dumps(args, ensure_ascii=False, indent=2)}")
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
        
        # Читаем настройки из .env
        self.debug_enabled = os.getenv('DEBUG_LOGGING', 'false').lower() == 'true'
        self.log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
        self.log_agents = os.getenv('LOG_AGENTS', 'true').lower() == 'true'
        self.log_tools = os.getenv('LOG_TOOLS', 'true').lower() == 'true'
        self.log_communications = os.getenv('LOG_COMMUNICATIONS', 'true').lower() == 'true'
        self.log_errors = os.getenv('LOG_ERRORS', 'true').lower() == 'true'
        self.log_file = os.getenv('LOG_FILE')
        
        # Создаем основной логгер
        self.logger = logging.getLogger('AgentSystem')
        self.logger.setLevel(getattr(logging, self.log_level, logging.INFO))
        
        # Очищаем существующие обработчики
        self.logger.handlers.clear()
        
        # Настраиваем форматирование
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)8s | %(name)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Консольный вывод
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)
        
        # Файловый вывод с датой и временем в названии
        if not self.log_file:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            self.log_file = f"logs/agents_{timestamp}.log"
            
        if self.log_file:
            log_path = Path(self.log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding='utf-8')
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
    
    def _should_log(self, category: str) -> bool:
        """Проверяет, нужно ли логгировать для данной категории."""
        if not self.debug_enabled:
            return False
        
        category_map = {
            'agents': self.log_agents,
            'tools': self.log_tools,
            'communications': self.log_communications,
            'errors': self.log_errors
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
            
            agent_logger.info(f"🤖 START")
            agent_logger.debug(f"   📝 Input: {input_data[:200]}{'...' if len(input_data) > 200 else ''}")
    
    def log_agent_end(self, agent_name: str, output_data: str, duration: float):
        """Логгирование завершения работы агента."""
        if self._should_log('agents'):
            agent_logger = logging.getLogger(f'Agent.{agent_name}')
            agent_logger.setLevel(self.logger.level)
            if not agent_logger.handlers:
                for handler in self.logger.handlers:
                    agent_logger.addHandler(handler)
                    
            agent_logger.info(f"✅ END (took {duration:.2f}s)")
            agent_logger.debug(f"   📤 Output: {output_data[:200]}{'...' if len(output_data) > 200 else ''}")
    
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
                    
            tool_logger.info(f"🔧 START")
            tool_logger.debug(f"   📋 Args: {json.dumps(args, ensure_ascii=False, indent=2)}")
    
    def log_tool_end(self, tool_name: str, result: str, duration: float):
        """Логгирование завершения работы инструмента."""
        if self._should_log('tools'):
            tool_logger = logging.getLogger(f'Tool.{tool_name}')
            tool_logger.setLevel(self.logger.level)
            if not tool_logger.handlers:
                for handler in self.logger.handlers:
                    tool_logger.addHandler(handler)
                    
            tool_logger.info(f"✅ END (took {duration:.2f}s)")
            tool_logger.debug(f"   📤 Result: {result[:200]}{'...' if len(result) > 200 else ''}")
    
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
    
    def log_agent_prompt(self, agent_name: str, prompt: str):
        """Логгирование промпта агента."""
        if self._should_log('agents'):
            agent_logger = logging.getLogger(f'Agent.{agent_name}')
            agent_logger.setLevel(self.logger.level)
            if not agent_logger.handlers:
                for handler in self.logger.handlers:
                    agent_logger.addHandler(handler)
            
            agent_logger.info(f"📝 PROMPT")
            agent_logger.debug(f"   📄 Instructions:")
            
            # Разбиваем промпт на строки для лучшего отображения
            lines = prompt.split('\n')
            for i, line in enumerate(lines, 1):
                if line.strip():  # Пропускаем пустые строки
                    agent_logger.debug(f"   {i:2d}: {line}")
                else:
                    agent_logger.debug(f"   {i:2d}: <empty>")

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
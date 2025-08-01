"""
Простой загрузчик YAML конфигурации.
"""

import os
import yaml
from typing import Dict, Any, Optional
from dataclasses import dataclass
from pathlib import Path

@dataclass
class ModelConfig:
    """Конфигурация модели."""
    name: str
    provider: str
    description: str
    temperature: float
    max_tokens: int
    best_for: list

@dataclass
class ProviderConfig:
    """Конфигурация провайдера."""
    name: str
    base_url: str
    api_key_env: str
    timeout: int = 30
    max_retries: int = 2

@dataclass
class AgentConfig:
    """Конфигурация агента."""
    name: str
    model: str
    tools: list
    base_prompt: str
    custom_prompt: str = None
    description: str = ''
    mcp_enabled: bool = False

class ConfigLoader:
    """Загрузчик конфигурации из YAML."""
    
    def __init__(self, config_path: str = "config.yaml", working_directory: Optional[str] = None):
        self.config_path = config_path
        self._config = None
        self._working_directory = working_directory or os.getcwd()
        self._load_config()
    
    def _load_config(self):
        """Загружает конфигурацию из YAML файла."""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Конфигурационный файл {self.config_path} не найден")
        except yaml.YAMLError as e:
            raise ValueError(f"Ошибка чтения YAML файла: {e}")
    
    def get_working_directory(self) -> str:
        """Возвращает рабочую директорию."""
        return self._config.get('settings', {}).get('working_directory', self._working_directory)
    
    def get_config_directory(self) -> str:
        """Возвращает директорию конфигурации."""
        return self._config.get('settings', {}).get('config_directory', os.path.dirname(self.config_path))
    
    def is_path_override_allowed(self) -> bool:
        """Проверяет разрешено ли переопределение пути."""
        return self._config.get('settings', {}).get('allow_path_override', True)
    
    def set_working_directory(self, path: str):
        """Устанавливает рабочую директорию."""
        if self.is_path_override_allowed():
            self._working_directory = os.path.abspath(path)
        else:
            print("⚠️ Переопределение пути отключено в конфигурации")
    
    def get_absolute_path(self, relative_path: str) -> str:
        """Преобразует относительный путь в абсолютный относительно рабочей директории."""
        if os.path.isabs(relative_path):
            return relative_path
        return os.path.join(self.get_working_directory(), relative_path)
    
    def get_project_root(self) -> str:
        """Возвращает корневую директорию проекта."""
        return self.get_working_directory()
    
    def get_default_model(self) -> str:
        """Возвращает имя модели по умолчанию."""
        return self._config.get('default_model', 'claude-sonnet')
    
    def get_max_history(self) -> int:
        """Возвращает максимальное количество сообщений в истории."""
        return self._config.get('settings', {}).get('max_history', 15)
    
    def is_debug(self) -> bool:
        """Возвращает флаг отладки."""
        return self._config.get('settings', {}).get('debug', False)
    
    def get_provider(self, provider_key: str) -> ProviderConfig:
        """Возвращает конфигурацию провайдера."""
        providers = self._config.get('providers', {})
        if provider_key not in providers:
            raise ValueError(f"Провайдер '{provider_key}' не найден в конфигурации")
        
        p = providers[provider_key]
        return ProviderConfig(
            name=p['name'],
            base_url=p['base_url'],
            api_key_env=p['api_key_env'],
            timeout=p.get('timeout', 30),
            max_retries=p.get('max_retries', 2)
        )
    
    def get_model(self, model_key: str) -> ModelConfig:
        """Возвращает конфигурацию модели."""
        models = self._config.get('models', {})
        if model_key not in models:
            raise ValueError(f"Модель '{model_key}' не найдена в конфигурации")
        
        m = models[model_key]
        return ModelConfig(
            name=m['name'],
            provider=m['provider'],
            description=m['description'],
            temperature=m['temperature'],
            max_tokens=m['max_tokens'],
            best_for=m.get('best_for', [])
        )
    
    def get_prompt(self, prompt_type: str = 'default') -> str:
        """Возвращает системный промпт."""
        prompts = self._config.get('prompts', {})
        return prompts.get(prompt_type, prompts.get('default', 
            'Ты дружелюбный помощник. Отвечай кратко и по делу.'))
    
    def list_models(self) -> Dict[str, str]:
        """Возвращает список доступных моделей с описаниями."""
        models = self._config.get('models', {})
        return {key: config['description'] for key, config in models.items()}
    
    def list_providers(self) -> Dict[str, str]:
        """Возвращает список доступных провайдеров с описаниями."""
        providers = self._config.get('providers', {})
        return {key: config['description'] for key, config in providers.items()}
    
    def get_api_key(self, provider_key: str) -> Optional[str]:
        """Получает API ключ для провайдера из переменных окружения."""
        provider = self.get_provider(provider_key)
        return os.getenv(provider.api_key_env)
    
    def get_agent(self, agent_key: str) -> AgentConfig:
        """Получает конфигурацию агента."""
        agents = self._config.get('agents', {})
        if agent_key not in agents:
            raise ValueError(f"Агент '{agent_key}' не найден в конфигурации")
        
        agent_data = agents[agent_key]
        return AgentConfig(
            name=agent_data.get('name', agent_key),
            model=agent_data.get('model'),
            tools=agent_data.get('tools', []),
            base_prompt=agent_data.get('base_prompt', 'base'),
            custom_prompt=agent_data.get('custom_prompt'),
            description=agent_data.get('description', ''),
            mcp_enabled=agent_data.get('mcp_enabled', False)
        )
    
    def build_agent_prompt(self, agent_key: str) -> str:
        """Строит полный промпт агента на основе его инструментов."""
        agent_config = self.get_agent(agent_key)
        
        # Базовый промпт
        base_prompt = self._config.get('prompt_templates', {}).get(agent_config.base_prompt, '')
        
        # Кастомный промпт имеет приоритет
        if agent_config.custom_prompt:
            base_prompt = agent_config.custom_prompt
        
        # Добавляем описания инструментов
        tool_descriptions = []
        for tool_name in agent_config.tools:
            tool_config = self._config.get('tools', {}).get(tool_name, {})
            if tool_config.get('prompt_addition'):
                tool_descriptions.append(tool_config['prompt_addition'])
        
        # Собираем полный промпт
        parts = [base_prompt]
        if tool_descriptions:
            parts.append("\nДоступные инструменты:")
            parts.extend(tool_descriptions)
        
        final_prompt = "\n".join(parts)
        
        # Логгируем построение промпта
        try:
            from utils.logger import log_custom
            log_custom('debug', 'prompt_building', f"Building prompt for agent '{agent_key}'")
            log_custom('debug', 'prompt_building', f"Base prompt template: '{agent_config.base_prompt}'")
            log_custom('debug', 'prompt_building', f"Has custom prompt: {bool(agent_config.custom_prompt)}")
            log_custom('debug', 'prompt_building', f"Tools count: {len(agent_config.tools)}")
            log_custom('debug', 'prompt_building', f"Tool descriptions count: {len(tool_descriptions)}")
        except ImportError:
            pass  # Логгер может быть недоступен
        
        return final_prompt
    
    def list_agents(self) -> Dict[str, str]:
        """Возвращает список всех агентов."""
        agents = {}
        for key, agent_data in self._config.get('agents', {}).items():
            agents[key] = agent_data.get('description', agent_data.get('name', key))
        return agents
    
    def get_default_agent(self) -> str:
        """Получает агента по умолчанию."""
        return self._config.get('settings', {}).get('default_agent', 'assistant')
    
    def is_mcp_enabled(self) -> bool:
        """Проверяет включен ли MCP глобально."""
        return self._config.get('settings', {}).get('mcp_enabled', False)

# Глобальный экземпляр
config = ConfigLoader()
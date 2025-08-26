"""
Интеграция отслеживания стоимости в Grid систему
Модуль для интеграции с существующими компонентами системы
"""

import os
import logging
import asyncio
from typing import Optional, Dict, Any, Union
from functools import wraps

# Импорты AgentsSDK
from agents.tracing.span_data import GenerationSpanData
from agents.tracing.spans import Span
from agents.models.interface import ModelProvider

# Локальные импорты
from utils.openrouter_cost_tracker import get_cost_tracker, get_cost_logger, track_and_log_cost
from utils.openrouter_provider import OpenRouterProvider, create_openrouter_provider


class CostIntegrationMixin:
    """
    Mixin для добавления отслеживания стоимости в существующие классы
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cost_tracker = get_cost_tracker()
        self._cost_logger = get_cost_logger()
        self.logger = logging.getLogger("grid.cost_integration")
    
    async def track_generation_cost(self, generation_id: str, span_data: Optional[Dict[str, Any]] = None):
        """Отследить стоимость генерации"""
        await track_and_log_cost(generation_id, span_data)


def with_cost_tracking(func):
    """
    Декоратор для добавления отслеживания стоимости к функциям генерации
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        result = await func(*args, **kwargs)
        
        # Попытка извлечь generation_id из результата
        generation_id = None
        if hasattr(result, 'response_id') and result.response_id:
            generation_id = result.response_id
        elif isinstance(result, dict) and 'id' in result:
            generation_id = result['id']
        
        if generation_id:
            # Асинхронно отслеживаем стоимость
            asyncio.create_task(track_and_log_cost(generation_id))
        
        return result
    
    return wrapper


class GridCostIntegration:
    """
    Главный класс для интеграции отслеживания стоимости в Grid
    """
    
    def __init__(self):
        self.logger = logging.getLogger("grid.cost_integration")
        self._cost_tracker = get_cost_tracker()
        self._enabled = self._check_if_enabled()
    
    def _check_if_enabled(self) -> bool:
        """Проверить, включено ли отслеживание стоимости"""
        # Проверяем переменные окружения
        if os.getenv("GRID_COST_TRACKING_DISABLED", "false").lower() == "true":
            return False
        
        # Проверяем наличие API ключа
        if not os.getenv("OPENROUTER_API_KEY"):
            self.logger.warning("OPENROUTER_API_KEY not found, cost tracking disabled")
            return False
        
        return True
    
    def is_enabled(self) -> bool:
        """Проверить, включено ли отслеживание стоимости"""
        return self._enabled
    
    def create_provider_with_cost_tracking(
        self,
        provider_config: Dict[str, Any]
    ) -> ModelProvider:
        """
        Создать provider с отслеживанием стоимости на основе конфигурации
        
        Args:
            provider_config: Конфигурация provider из config.yaml
            
        Returns:
            ModelProvider с поддержкой отслеживания стоимости
        """
        if not self._enabled:
            # Если отслеживание выключено, создаем стандартный provider
            from agents.models.openai_provider import OpenAIProvider
            return OpenAIProvider(
                api_key=provider_config.get("api_key"),
                base_url=provider_config.get("base_url"),
            )
        
        # Проверяем, является ли это OpenRouter provider
        base_url = provider_config.get("base_url", "")
        if "openrouter.ai" in base_url:
            self.logger.info("Creating OpenRouter provider with cost tracking")
            return create_openrouter_provider(
                api_key=provider_config.get("api_key") or os.getenv(provider_config.get("api_key_env", "")),
                base_url=base_url,
                cost_tracking=True
            )
        else:
            # Для других providers создаем стандартный OpenAI provider
            from agents.models.openai_provider import OpenAIProvider
            return OpenAIProvider(
                api_key=provider_config.get("api_key") or os.getenv(provider_config.get("api_key_env", "")),
                base_url=base_url,
            )
    
    async def enhance_span_with_cost_data(
        self,
        span: Span,
        generation_id: Optional[str] = None
    ):
        """
        Улучшить span данными о стоимости
        
        Args:
            span: Span для улучшения
            generation_id: ID генерации для отслеживания стоимости
        """
        if not self._enabled or not generation_id:
            return
        
        try:
            cost = await self._cost_tracker.get_generation_cost(generation_id)
            if cost and hasattr(span, 'span_data'):
                # Добавляем данные о стоимости в span
                if hasattr(span.span_data, '__dict__'):
                    span.span_data.__dict__.update({
                        'openrouter_cost': cost.total_cost,
                        'openrouter_model': cost.model,
                        'openrouter_tokens_prompt': cost.tokens_prompt,
                        'openrouter_tokens_completion': cost.tokens_completion,
                        'openrouter_total_tokens': cost.total_tokens,
                        'openrouter_generation_id': cost.id,
                        'openrouter_created_at': cost.created_at,
                    })
                
                self.logger.debug(f"Enhanced span with cost data: ${cost.total_cost:.6f}")
                
        except Exception as e:
            self.logger.error(f"Error enhancing span with cost data: {e}")


# Глобальный экземпляр интеграции
_global_integration: Optional[GridCostIntegration] = None


def get_cost_integration() -> GridCostIntegration:
    """Получить глобальный экземпляр интеграции стоимости"""
    global _global_integration
    if _global_integration is None:
        _global_integration = GridCostIntegration()
    return _global_integration


def is_cost_tracking_enabled() -> bool:
    """Проверить, включено ли отслеживание стоимости"""
    return get_cost_integration().is_enabled()


async def cleanup_cost_integration():
    """Очистка ресурсов интеграции"""
    global _global_integration
    if _global_integration:
        await _global_integration._cost_tracker.close()
        _global_integration = None


# Утилиты для интеграции с конфигурацией
def should_use_cost_tracking_for_provider(provider_name: str, provider_config: Dict[str, Any]) -> bool:
    """
    Определить, нужно ли использовать отслеживание стоимости для provider
    
    Args:
        provider_name: Имя provider
        provider_config: Конфигурация provider
        
    Returns:
        True если нужно отслеживание стоимости
    """
    if not is_cost_tracking_enabled():
        return False
    
    # Проверяем base_url на предмет OpenRouter
    base_url = provider_config.get("base_url", "")
    return "openrouter.ai" in base_url
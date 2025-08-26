"""
OpenRouter Cost Tracker
Модуль для подсчета токенов и стоимости через OpenRouter API
"""

import os
import asyncio
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime
import httpx
# from core.config import GridConfig  # Убираем чтобы избежать циклических импортов


@dataclass
class GenerationCost:
    """Данные о стоимости генерации"""
    id: str
    total_cost: float
    model: str
    tokens_prompt: int
    tokens_completion: int
    created_at: str
    
    @property
    def total_tokens(self) -> int:
        return self.tokens_prompt + self.tokens_completion


class OpenRouterCostTracker:
    """Трекер стоимости для запросов к OpenRouter"""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        self.base_url = "https://openrouter.ai/api/v1"
        self.logger = logging.getLogger("grid.openrouter_cost")
        self._client: Optional[httpx.AsyncClient] = None
        
    async def _get_client(self) -> httpx.AsyncClient:
        """Получить HTTP клиент с повторным использованием"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10.0
            )
        return self._client
        
    async def get_generation_cost(self, generation_id: str) -> Optional[GenerationCost]:
        """
        Получить информацию о стоимости генерации через OpenRouter API
        
        Args:
            generation_id: ID генерации из OpenRouter
            
        Returns:
            GenerationCost или None если не найдено
        """
        if not self.api_key:
            self.logger.warning("OpenRouter API key not provided, cost tracking disabled")
            return None
            
        if not generation_id:
            self.logger.warning("Generation ID is empty, cannot track cost")
            return None
            
        try:
            client = await self._get_client()
            url = f"{self.base_url}/generation"
            
            response = await client.get(url, params={"id": generation_id})
            response.raise_for_status()
            
            data = response.json()
            if "data" not in data:
                self.logger.warning(f"No data in OpenRouter response for generation {generation_id}")
                return None
                
            gen_data = data["data"]
            return GenerationCost(
                id=gen_data.get("id", generation_id),
                total_cost=gen_data.get("total_cost", 0.0),
                model=gen_data.get("model", "unknown"),
                tokens_prompt=gen_data.get("tokens_prompt", 0),
                tokens_completion=gen_data.get("tokens_completion", 0),
                created_at=gen_data.get("created_at", datetime.now().isoformat()),
            )
            
        except httpx.HTTPError as e:
            self.logger.error(f"HTTP error getting generation cost for {generation_id}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error getting generation cost for {generation_id}: {e}")
            return None
            
    async def close(self):
        """Закрыть HTTP клиент"""
        if self._client:
            await self._client.aclose()
            self._client = None


class CostLogger:
    """Логгер для вывода стоимости в trace и консоль"""
    
    def __init__(self, tracker: OpenRouterCostTracker):
        self.tracker = tracker
        self.logger = logging.getLogger("grid.cost_logging")
        
    def log_cost_to_console(self, cost: GenerationCost):
        """Вывод стоимости в консоль"""
        print(f"💰 OpenRouter Cost: ${cost.total_cost:.6f} | "
              f"Model: {cost.model} | "
              f"Tokens: {cost.tokens_prompt}→{cost.tokens_completion} "
              f"({cost.total_tokens} total)")
              
    def log_cost_to_trace(self, cost: GenerationCost, span_data: Dict[str, Any]):
        """Добавить информацию о стоимости в span данные для tracing"""
        span_data.update({
            "openrouter_cost": cost.total_cost,
            "openrouter_model": cost.model,
            "openrouter_tokens_prompt": cost.tokens_prompt,
            "openrouter_tokens_completion": cost.tokens_completion,
            "openrouter_total_tokens": cost.total_tokens,
            "openrouter_generation_id": cost.id,
            "openrouter_created_at": cost.created_at,
        })
        
    async def log_generation_cost(self, generation_id: str, span_data: Optional[Dict[str, Any]] = None):
        """
        Получить и залогировать стоимость генерации
        
        Args:
            generation_id: ID генерации из OpenRouter
            span_data: Словарь span данных для обновления (опционально)
        """
        cost = await self.tracker.get_generation_cost(generation_id)
        if cost:
            # Вывод в консоль
            self.log_cost_to_console(cost)
            
            # Добавление в trace если предоставлен span_data
            if span_data is not None:
                self.log_cost_to_trace(cost, span_data)
                
            # Структурированное логирование
            self.logger.info(
                f"OpenRouter generation cost tracked",
                extra={
                    "generation_id": cost.id,
                    "total_cost": cost.total_cost,
                    "model": cost.model,
                    "tokens_prompt": cost.tokens_prompt,
                    "tokens_completion": cost.tokens_completion,
                    "total_tokens": cost.total_tokens,
                    "event_type": "openrouter_cost"
                }
            )


# Глобальный экземпляр трекера стоимости
_global_tracker: Optional[OpenRouterCostTracker] = None
_global_cost_logger: Optional[CostLogger] = None


def get_cost_tracker() -> OpenRouterCostTracker:
    """Получить глобальный экземпляр трекера стоимости"""
    global _global_tracker
    if _global_tracker is None:
        _global_tracker = OpenRouterCostTracker()
    return _global_tracker


def get_cost_logger() -> CostLogger:
    """Получить глобальный экземпляр логгера стоимости"""
    global _global_cost_logger
    if _global_cost_logger is None:
        _global_cost_logger = CostLogger(get_cost_tracker())
    return _global_cost_logger


async def track_and_log_cost(generation_id: str, span_data: Optional[Dict[str, Any]] = None):
    """
    Удобная функция для отслеживания и логирования стоимости
    
    Args:
        generation_id: ID генерации из OpenRouter
        span_data: Словарь span данных для обновления (опционально)
    """
    cost_logger = get_cost_logger()
    await cost_logger.log_generation_cost(generation_id, span_data)


# Cleanup функции
async def cleanup_cost_tracking():
    """Очистка ресурсов при завершении работы"""
    global _global_tracker
    if _global_tracker:
        await _global_tracker.close()
        _global_tracker = None
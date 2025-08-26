"""
OpenRouter HTTP Interceptor для получения generation_id
Перехватывает HTTP заголовки от OpenRouter
"""

import logging
from typing import Optional, Dict, Any
import httpx


class OpenRouterInterceptor:
    """Перехватчик для сохранения данных из заголовков OpenRouter"""
    
    def __init__(self):
        self.logger = logging.getLogger("grid.openrouter_interceptor")
        self._last_generation_id: Optional[str] = None
        self._last_headers: Dict[str, str] = {}
    
    def process_response(self, response: httpx.Response) -> None:
        """
        Обработать HTTP ответ от OpenRouter
        
        Args:
            response: HTTP ответ от OpenRouter
        """
        try:
            # Сохраняем заголовки
            self._last_headers = dict(response.headers)
            
            # Логируем все заголовки для отладки
            self.logger.debug(f"OpenRouter response headers: {dict(response.headers)}")
            
            # Извлекаем generation_id из заголовков
            generation_id = response.headers.get('x-openrouter-generation-id')
            if not generation_id:
                # Пробуем другие возможные имена заголовков
                generation_id = (response.headers.get('openrouter-generation-id') or
                               response.headers.get('X-Generation-Id') or 
                               response.headers.get('generation-id'))
                               
            if generation_id:
                self._last_generation_id = generation_id
                self.logger.debug(f"Captured generation_id from headers: {generation_id}")
            else:
                # Пробуем извлечь из JSON тела ответа
                try:
                    if hasattr(response, '_content'):
                        json_data = response.json()
                        if isinstance(json_data, dict) and 'id' in json_data:
                            self._last_generation_id = json_data['id']
                            self.logger.debug(f"Captured generation_id from JSON: {self._last_generation_id}")
                except Exception as e:
                    self.logger.debug(f"Could not extract from JSON: {e}")
                    pass
                    
            if not self._last_generation_id:
                self.logger.warning("Could not extract generation_id from OpenRouter response")
                
        except Exception as e:
            self.logger.error(f"Error processing OpenRouter response: {e}")
    
    def get_last_generation_id(self) -> Optional[str]:
        """Получить последний generation_id"""
        return self._last_generation_id
    
    def get_last_headers(self) -> Dict[str, str]:
        """Получить последние заголовки"""
        return self._last_headers.copy()
    
    def clear(self) -> None:
        """Очистить сохраненные данные"""
        self._last_generation_id = None
        self._last_headers.clear()


# Глобальный экземпляр перехватчика
_global_interceptor: Optional[OpenRouterInterceptor] = None


def get_interceptor() -> OpenRouterInterceptor:
    """Получить глобальный экземпляр перехватчика"""
    global _global_interceptor
    if _global_interceptor is None:
        _global_interceptor = OpenRouterInterceptor()
    return _global_interceptor


class InterceptingHTTPXClient(httpx.AsyncClient):
    """HTTPX клиент с перехватом ответов OpenRouter"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.interceptor = get_interceptor()
        
    async def request(self, *args, **kwargs) -> httpx.Response:
        """Перехватывает запросы и ответы"""
        response = await super().request(*args, **kwargs)
        
        # Проверяем, это запрос к OpenRouter
        if hasattr(response, 'url') and 'openrouter.ai' in str(response.url):
            self.interceptor.process_response(response)
        
        return response
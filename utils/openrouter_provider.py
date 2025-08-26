"""
OpenRouter Provider с отслеживанием стоимости
Wrapper для OpenAI provider с интеграцией OpenRouter cost tracking
"""

import os
import logging
import asyncio
from typing import Optional

# Импорты из AgentsSDK
from agents.models.interface import Model, ModelProvider
from agents.models.openai_provider import OpenAIProvider
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.items import ModelResponse, TResponseInputItem
from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.tool import Tool
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from agents.usage import Usage
from agents.models.chatcmpl_converter import Converter
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from openai import AsyncOpenAI
from openai.types.responses.response_prompt_param import ResponsePromptParam

# Импорты локальных модулей
from utils.openrouter_cost_tracker import OpenRouterCostTracker, get_cost_tracker, get_cost_logger
from utils.openrouter_interceptor import get_interceptor, InterceptingHTTPXClient


class OpenRouterModel(OpenAIChatCompletionsModel):
    """
    Модель OpenAI с интеграцией отслеживания стоимости OpenRouter
    """
    
    def __init__(self, model: str, openai_client: AsyncOpenAI, cost_tracker: Optional[OpenRouterCostTracker] = None):
        super().__init__(model, openai_client)
        self.cost_tracker = cost_tracker or get_cost_tracker()
        self.logger = logging.getLogger("grid.openrouter_model")
        
    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None,
        prompt: ResponsePromptParam | None = None,
    ) -> ModelResponse:
        """Получить ответ от модели с отслеживанием стоимости"""
        
        from agents.tracing import generation_span
        
        # ХАКАЕМ generation_span чтобы захватить span_data
        captured_generation_id = None
        captured_span = None
        
        # Патчим _client чтобы перехватить generation_id
        original_post = self._client.chat.completions.create
        
        async def patched_create(*args, **kwargs):
            result = await original_post(*args, **kwargs)
            nonlocal captured_generation_id
            # OpenRouter возвращает generation_id в поле id
            captured_generation_id = getattr(result, 'id', None)
            return result
        
        # Временно патчим метод
        self._client.chat.completions.create = patched_create
        
        # ХАКАЕМ generation_span чтобы получить доступ к span
        with generation_span(
            model=str(self.model),
            model_config=model_settings.to_json_dict() | {"base_url": str(self._client.base_url)},
            disabled=tracing.is_disabled(),
        ) as span_generation:
            captured_span = span_generation
            
            try:
                # Вызываем _fetch_response напрямую вместо super()
                response = await self._fetch_response(
                    system_instructions, input, model_settings, tools, output_schema,
                    handoffs, span_generation, tracing, stream=False, prompt=prompt
                )
                
                # Обрабатываем ответ как в базовом классе
                message = None
                first_choice = None
                if response.choices and len(response.choices) > 0:
                    first_choice = response.choices[0]
                    message = first_choice.message
                
                usage = (
                    Usage(
                        requests=1,
                        input_tokens=response.usage.prompt_tokens,
                        output_tokens=response.usage.completion_tokens,
                        total_tokens=response.usage.total_tokens,
                        input_tokens_details=InputTokensDetails(
                            cached_tokens=getattr(response.usage.prompt_tokens_details, "cached_tokens", 0) or 0,
                        ),
                        output_tokens_details=OutputTokensDetails(
                            reasoning_tokens=getattr(response.usage.completion_tokens_details, "reasoning_tokens", 0) or 0,
                        ),
                    )
                    if response.usage
                    else Usage()
                )
                
                if tracing.include_data():
                    span_generation.span_data.output = (
                        [message.model_dump()] if message is not None else []
                    )
                span_generation.span_data.usage = {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                }
                
                # ДОБАВЛЯЕМ стоимость в span СРАЗУ после usage - ТЕСТОВЫЕ ДАННЫЕ
                if captured_generation_id and captured_span:
                    self.logger.info(f"Captured real generation_id: {captured_generation_id}")
                    # Добавляем тестовые данные сразу для проверки механизма
                    test_cost_data = {
                        'openrouter_cost': 0.000042,
                        'openrouter_model': str(self.model),
                        'openrouter_tokens_prompt': usage.input_tokens,
                        'openrouter_tokens_completion': usage.output_tokens,
                        'openrouter_total_tokens': usage.total_tokens,
                        'openrouter_generation_id': captured_generation_id,
                    }
                    captured_span.span_data.__dict__.update(test_cost_data)
                    # Format for human-readable display
                    tokens_in = f"{test_cost_data['openrouter_tokens_prompt']:,}"
                    tokens_out = f"{test_cost_data['openrouter_tokens_completion']:,}"
                    
                    self.logger.info(f"Token usage - Input: {tokens_in}, Output: {tokens_out} (cost pending...)")
                    
                    # Пытаемся получить реальную стоимость асинхронно в фоне
                    asyncio.create_task(self._add_cost_to_span(captured_generation_id, captured_span))
                
                items = Converter.message_to_output_items(message) if message is not None else []
                
                model_response = ModelResponse(
                    output=items,
                    usage=usage,
                    response_id=None,
                )
                
                return model_response
                
            finally:
                # Восстанавливаем оригинальный метод
                self._client.chat.completions.create = original_post
    
    def _extract_generation_id(self, response: ModelResponse) -> Optional[str]:
        """
        Извлечь generation_id из ответа модели
        
        Использует перехватчик для получения generation_id из HTTP заголовков OpenRouter
        """
        # Получаем interceptor и проверяем последний generation_id
        interceptor = get_interceptor()
        generation_id = interceptor.get_last_generation_id()
        
        if generation_id:
            self.logger.debug(f"Extracted generation_id from interceptor: {generation_id}")
            return generation_id
        
        # Fallback: проверяем response_id
        if response.response_id:
            self.logger.debug(f"Using response_id as generation_id: {response.response_id}")
            return response.response_id
        
        # Для демонстрации создаем тестовый ID
        import uuid
        test_generation_id = f"gen-test-{uuid.uuid4().hex[:16]}"
        self.logger.debug(f"Generated test generation_id: {test_generation_id}")
        return test_generation_id
    
    async def _track_cost_sync(self, generation_id: str, tracing: ModelTracing, response: ModelResponse):
        """СИНХРОННО получить стоимость и добавить в trace"""
        try:
            cost_logger = get_cost_logger()
            
            # Ждем немного чтобы данные появились в OpenRouter
            await asyncio.sleep(1)
            
            # Получаем стоимость
            cost = await self.cost_tracker.get_generation_cost(generation_id)
            
            if cost:
                # Выводим в консоль
                cost_logger.log_cost_to_console(cost)
                
                # Добавляем в usage ответа если возможно
                if hasattr(response, 'usage') and response.usage:
                    # Добавляем информацию о стоимости прямо в usage
                    if hasattr(response.usage, '__dict__'):
                        response.usage.__dict__.update({
                            'openrouter_cost': cost.total_cost,
                            'openrouter_generation_id': cost.id
                        })
                
                self.logger.info(f"Cost tracking completed: ${cost.total_cost:.6f}")
            else:
                self.logger.warning(f"Cost not available yet for {generation_id}")
                
        except Exception as e:
            self.logger.error(f"Error tracking cost for generation {generation_id}: {e}")
    
    async def _add_cost_to_span(self, generation_id: str, span):
        """Добавить стоимость в активный span"""
        try:
            # Ждем немного чтобы данные появились в OpenRouter
            await asyncio.sleep(1)
            
            # Получаем стоимость
            cost = await self.cost_tracker.get_generation_cost(generation_id)
            
            if cost:
                # Выводим в консоль
                from utils.openrouter_cost_tracker import get_cost_logger
                cost_logger = get_cost_logger()
                cost_logger.log_cost_to_console(cost)
                
                # ДОБАВЛЯЕМ СТОИМОСТЬ ПРЯМО В SPAN DATA
                if hasattr(span, 'span_data') and hasattr(span.span_data, '__dict__'):
                    cost_data = {
                        'openrouter_cost': cost.total_cost,
                        'openrouter_model': cost.model,
                        'openrouter_tokens_prompt': cost.tokens_prompt,
                        'openrouter_tokens_completion': cost.tokens_completion,
                        'openrouter_total_tokens': cost.total_tokens,
                        'openrouter_generation_id': cost.id,
                        'openrouter_created_at': cost.created_at,
                    }
                    span.span_data.__dict__.update(cost_data)
                    self.logger.info(f"Cost data added to span: {cost_data}")
                else:
                    self.logger.warning(f"Cannot add cost to span: span={span}, span_data={getattr(span, 'span_data', None)}")
                
                self.logger.info(f"Cost added to span: ${cost.total_cost:.6f}")
            else:
                self.logger.warning(f"Cost not available for {generation_id}")
                
        except Exception as e:
            self.logger.error(f"Error adding cost to span: {e}")
    
    async def _track_cost_async(self, generation_id: str, tracing: ModelTracing):
        """Асинхронно отследить и залогировать стоимость (устарело)"""
        await self._track_cost_sync(generation_id, tracing, None)


class OpenRouterProvider(ModelProvider):
    """
    Provider для OpenRouter с отслеживанием стоимости
    """
    
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        openai_client: AsyncOpenAI | None = None,
        organization: str | None = None,
        project: str | None = None,
        use_responses: bool | None = None,
        cost_tracking: bool = True,
    ) -> None:
        """
        Создать OpenRouter provider
        
        Args:
            api_key: API key для OpenRouter
            base_url: Base URL для OpenRouter (по умолчанию https://openrouter.ai/api/v1)
            openai_client: Готовый OpenAI client
            organization: Организация
            project: Проект
            use_responses: Использовать ли responses API
            cost_tracking: Включить ли отслеживание стоимости
        """
        # Устанавливаем base_url по умолчанию для OpenRouter
        if base_url is None:
            base_url = "https://openrouter.ai/api/v1"
        
        # Используем API key из переменной окружения если не указан
        if api_key is None:
            api_key = os.getenv("OPENROUTER_API_KEY")
        
        # Создаем кастомный OpenAI клиент с перехватчиком если нужен
        if openai_client is None and base_url and "openrouter.ai" in base_url:
            # Создаем клиент с перехватывающим HTTP клиентом для OpenRouter
            custom_http_client = InterceptingHTTPXClient(
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                timeout=30.0
            )
            openai_client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                http_client=custom_http_client,
                organization=organization,
                project=project,
            )
        
        # Создаем базовый OpenAI provider
        if openai_client is not None:
            # Если клиент уже создан, передаем только его
            self._base_provider = OpenAIProvider(
                openai_client=openai_client,
                use_responses=use_responses,
            )
        else:
            # Иначе передаем параметры для создания клиента
            self._base_provider = OpenAIProvider(
                api_key=api_key,
                base_url=base_url,
                organization=organization,
                project=project,
                use_responses=use_responses,
            )
        
        self.cost_tracking = cost_tracking
        if self.cost_tracking:
            self.cost_tracker = OpenRouterCostTracker(api_key)
        else:
            self.cost_tracker = None
        
        self.logger = logging.getLogger("grid.openrouter_provider")
    
    def get_model(self, model_name: str | None) -> Model:
        """Получить модель с поддержкой отслеживания стоимости"""
        if not self.cost_tracking:
            # Если отслеживание стоимости выключено, возвращаем стандартную модель
            return self._base_provider.get_model(model_name)
        
        # Получаем стандартную модель из базового provider
        base_model = self._base_provider.get_model(model_name)
        
        # Если это OpenAIChatCompletionsModel, оборачиваем в наш класс
        if isinstance(base_model, OpenAIChatCompletionsModel):
            return OpenRouterModel(
                model=base_model.model,
                openai_client=base_model._client,
                cost_tracker=self.cost_tracker
            )
        
        # Для других типов моделей возвращаем как есть
        self.logger.warning(f"Cost tracking not supported for model type {type(base_model)}")
        return base_model


# Утилиты для удобной интеграции
def create_openrouter_provider(
    api_key: str | None = None,
    cost_tracking: bool = True,
    **kwargs
) -> OpenRouterProvider:
    """
    Создать OpenRouter provider с настройками по умолчанию
    
    Args:
        api_key: API key для OpenRouter (если не указан, берется из OPENROUTER_API_KEY)
        cost_tracking: Включить отслеживание стоимости
        **kwargs: Дополнительные параметры для OpenRouterProvider
    """
    return OpenRouterProvider(
        api_key=api_key,
        cost_tracking=cost_tracking,
        **kwargs
    )


def is_openrouter_provider(provider: ModelProvider) -> bool:
    """Проверить, является ли provider OpenRouter provider"""
    return isinstance(provider, OpenRouterProvider)
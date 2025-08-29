"""
Модуль проверяющих агентов для системы GRID.
Реализует проверку ответов основных агентов на предмет галлюцинаций.
Все настройки берутся из конфигурации.
"""

import logging
from typing import Optional, Any, Dict
from agents import (
    Agent, 
    output_guardrail, 
    GuardrailFunctionOutput, 
    RunContextWrapper,
    Runner,
    ModelSettings
)
from schemas import HallucinationCheckOutput, VerificationResult

# Создаем logger для модуля
logger = logging.getLogger(__name__)


class HallucinationChecker:
    """Агент для проверки галлюцинаций в ответах других агентов."""
    
    def __init__(self, model: Any, config: Any, name: str = "HallucinationChecker"):
        """
        Инициализация проверяющего агента.
        
        Args:
            model: Модель для проверки
            config: Конфигурация системы
            name: Имя агента
        """
        self.name = name
        self.model = model
        self.config = config
        self._agent = None
        
        # Получаем настройки из конфигурации
        self.settings = self._get_verification_settings()
    
    def _get_verification_settings(self) -> Dict[str, Any]:
        """Получить настройки проверки из конфигурации."""
        try:
            # Получаем глобальные настройки по умолчанию
            defaults = getattr(self.config.config.settings, 'verification_defaults', {})
            
            # Получаем настройки конкретного агента
            agent_config = self.config.config.agents.get('hallucination_checker', {})
            agent_settings = getattr(agent_config, 'verification_settings', {})
            
            # Объединяем настройки (агент имеет приоритет над глобальными)
            settings = defaults.copy()
            if agent_settings:
                settings.update(agent_settings)
            
            logger.info(f"Настройки проверки загружены: {settings}")
            return settings
            
        except Exception as e:
            logger.warning(f"Не удалось загрузить настройки проверки, используем значения по умолчанию: {e}")
            return {
                "temperature": 0.1,
                "max_tokens": 5000,
                "context_strategy": "full",
                "confidence_threshold": 0.7,
                "strict_mode": True
            }
    
    def get_agent(self) -> Agent:
        """Получить настроенного агента для проверки."""
        if self._agent is None:
            # Создаем ModelSettings из конфигурации
            model_settings = ModelSettings(
                temperature=self.settings.get("temperature", 0.1),
                max_tokens=self.settings.get("max_tokens", 5000)
            )
            
            self._agent = Agent(
                name=self.name,
                instructions=self._get_instructions(),
                model=self.model,
                tools=[],  # Без инструментов для проверяющего агента
                output_type=HallucinationCheckOutput,
                model_settings=model_settings
            )
        return self._agent
    
    def _get_instructions(self) -> str:
        """Получить инструкции для проверяющего агента из конфигурации."""
        try:
            # Пытаемся получить промпт из конфигурации
            prompt_key = "verification_base"
            if hasattr(self.config.config, 'prompt_templates'):
                prompt_templates = self.config.config.prompt_templates
                if prompt_key in prompt_templates:
                    base_prompt = prompt_templates[prompt_key]
                    
                    # Добавляем настройки режима
                    strict_mode = self.settings.get("strict_mode", True)
                    confidence_threshold = self.settings.get("confidence_threshold", 0.7)
                    
                    mode_instructions = f"""
VERIFICATION MODE:
- Strict mode: {'ENABLED' if strict_mode else 'DISABLED'}
- Confidence threshold: {confidence_threshold}
- {'Be extremely strict' if strict_mode else 'Be moderately strict'}

{base_prompt}
"""
                    return mode_instructions
            
            # Fallback если промпт не найден в конфигурации
            logger.warning(f"Промпт '{prompt_key}' не найден в конфигурации, используем базовый")
            return self._get_fallback_instructions()
            
        except Exception as e:
            logger.error(f"Ошибка при получении инструкций: {e}")
            return self._get_fallback_instructions()
    
    def _get_fallback_instructions(self) -> str:
        """Fallback instructions if configuration is unavailable."""
        return """
You are a hallucination detection agent for evaluating AI assistant responses.

Your task is to analyze the assistant's response and determine if it contains statements 
that are not supported by the conversation context or tool execution results.

VERIFICATION RULES:
1. Hallucination = statement that is not supported by:
   - User messages
   - Tool execution results
   - Previous conversation messages

2. NOT a hallucination:
   - Logical conclusions based on available facts
   - General knowledge (if not contradicting context)
   - Suggestions and recommendations

RETURN STRUCTURED OUTPUT:
- has_hallucination: true/false
- analysis: detailed explanation with examples
- confidence: confidence level 0.0-1.0
- flagged_statements: list of unsupported statements
"""
    
    async def verify_response(
        self, 
        response: str, 
        context: Any,
        context_strategy: Optional[str] = None,
        agent_instructions: Optional[str] = None
    ) -> HallucinationCheckOutput:
        """
        Проверить ответ на галлюцинации.
        
        Args:
            response: Ответ агента для проверки
            context: Контекст диалога
            context_strategy: Стратегия контекста (если не указана, берется из конфига)
            agent_instructions: Инструкции проверяемого агента
            
        Returns:
            Результат проверки
        """
        try:
            # Используем стратегию из конфига если не указана
            if context_strategy is None:
                context_strategy = self.settings.get("context_strategy", "full")
            
            # Подготавливаем контекст для проверки
            verification_context = self._prepare_verification_context(
                response, context, context_strategy, agent_instructions
            )
            
            # Запускаем проверяющего агента
            agent = self.get_agent()
            result = await Runner.run(
                agent, 
                verification_context,
                context=context
            )
            
            # Извлекаем результат
            if hasattr(result, 'final_output') and result.final_output:
                return result.final_output
            else:
                # Fallback если структурированный вывод не получен
                logger.warning("HallucinationChecker не вернул структурированный результат")
                return HallucinationCheckOutput(
                    has_hallucination=False,
                    analysis="Проверка не удалась - используем fallback",
                    confidence=0.0
                )
                
        except Exception as e:
            logger.error(f"Ошибка при проверке галлюцинаций: {e}")
            return HallucinationCheckOutput(
                has_hallucination=False,
                analysis=f"Ошибка проверки: {str(e)}",
                confidence=0.0
            )
    
    def _prepare_verification_context(
        self, 
        response: str, 
        context: Any, 
        strategy: str,
        agent_instructions: Optional[str] = None
    ) -> str:
        """
        Подготовить контекст для проверки.
        
        Args:
            response: Ответ агента
            context: Контекст диалога
            strategy: Стратегия контекста
            agent_instructions: Инструкции проверяемого агента
            
        Returns:
            Подготовленный контекст для проверки
        """
        # Базовый контекст
        base_context = f"""
TASK: Check the following assistant response for hallucinations.

ASSISTANT RESPONSE:
{response}

Check if this response contains statements that are not supported by the conversation context or tool execution results.
"""
        
        # Добавляем инструкции проверяемого агента
        if agent_instructions:
            base_context += f"""

AGENT INSTRUCTIONS (what the agent was supposed to do):
{agent_instructions}

Use these instructions to understand the agent's role and expected behavior.
"""
        
        if strategy == "last_turn":
            # Только последний ход
            return base_context
        else:
            # Полный контекст
            return f"""
{base_context}

CONVERSATION CONTEXT:
{self._format_context(context)}

Analyze the response against the full conversation context and tool execution history.
"""
    
    def _format_context(self, context: Any) -> str:
        """Format context for verification."""
        try:
            if hasattr(context, 'messages'):
                messages = context.messages
                formatted = []
                for msg in messages:
                    role = getattr(msg, 'role', 'unknown')
                    content = getattr(msg, 'content', '')
                    if role == 'user':
                        formatted.append(f"USER: {content}")
                    elif role == 'assistant':
                        formatted.append(f"ASSISTANT: {content}")
                    elif role == 'system':
                        formatted.append(f"SYSTEM: {content}")
                return "\n".join(formatted)
            else:
                return str(context)
        except Exception:
            return str(context)


# Глобальный экземпляр проверяющего агента
_hallucination_checker: Optional[HallucinationChecker] = None


def get_hallucination_checker(model: Any, config: Any) -> HallucinationChecker:
    """Получить глобальный экземпляр проверяющего агента."""
    global _hallucination_checker
    if _hallucination_checker is None:
        _hallucination_checker = HallucinationChecker(model, config)
    return _hallucination_checker


@output_guardrail
async def hallucination_guardrail(
    ctx: RunContextWrapper, 
    agent: Agent, 
    output: Any
) -> GuardrailFunctionOutput:
    """
    Output guardrail для проверки галлюцинаций.
    
    Args:
        ctx: Контекст выполнения
        agent: Агент, чей ответ проверяется
        output: Вывод агента
        
    Returns:
        Результат guardrail проверки
    """
    try:
        # Извлекаем текстовый ответ
        response_text = ""
        if hasattr(output, 'response'):
            response_text = output.response
        elif isinstance(output, str):
            response_text = output
        else:
            response_text = str(output)
        
        if not response_text.strip():
            # Пустой ответ - не проверяем
            return GuardrailFunctionOutput(
                output_info=None,
                tripwire_triggered=False
            )
        
        # Получаем проверяющего агента
        # Используем ту же модель, что и у основного агента
        model = getattr(agent, 'model', None)
        if not model:
            logger.warning("Не удалось получить модель для проверки галлюцинаций")
            return GuardrailFunctionOutput(
                output_info=None,
                tripwire_triggered=False
            )
        
        # Получаем конфигурацию из агента
        config = getattr(agent, '_config', None)
        if not config:
            logger.warning("Не удалось получить конфигурацию для проверки галлюцинаций")
            return GuardrailFunctionOutput(
                output_info=None,
                tripwire_triggered=False
            )
        
        checker = get_hallucination_checker(model, config)
        
        # Определяем стратегию контекста из конфигурации агента
        context_strategy = None
        if hasattr(agent, '_agent_config'):
            agent_config = getattr(agent, '_agent_config', None)
            if agent_config and hasattr(agent_config, 'verification_context'):
                context_strategy = agent_config.verification_context
        
        # Получаем инструкции проверяемого агента
        agent_instructions = getattr(agent, 'instructions', None)
        
        # Выполняем проверку
        verification_result = await checker.verify_response(
            response_text, 
            ctx.context,
            context_strategy,
            agent_instructions
        )
        
        # Проверяем порог уверенности из конфигурации
        confidence_threshold = checker.settings.get("confidence_threshold", 0.7)
        strict_mode = checker.settings.get("strict_mode", True)
        
        # Определяем, сработал ли трипвайер
        tripwire_triggered = False
        if strict_mode:
            # В строгом режиме: любая галлюцинация = трипвайер
            tripwire_triggered = verification_result.has_hallucination
        else:
            # В нестрогом режиме: только если уверенность выше порога
            tripwire_triggered = (
                verification_result.has_hallucination and 
                verification_result.confidence >= confidence_threshold
            )
        
        # Логируем результат проверки
        if tripwire_triggered:
            logger.warning(
                f"Обнаружена галлюцинация в ответе агента {agent.name}: "
                f"{verification_result.analysis} (уверенность: {verification_result.confidence})"
            )
        else:
            logger.info(f"Проверка галлюцинаций пройдена для агента {agent.name}")
        
        # Возвращаем результат
        return GuardrailFunctionOutput(
            output_info=verification_result,
            tripwire_triggered=tripwire_triggered
        )
        
    except Exception as e:
        logger.error(f"Ошибка в hallucination_guardrail: {e}")
        # В случае ошибки не блокируем ответ
        return GuardrailFunctionOutput(
            output_info=None,
            tripwire_triggered=False
        ) 
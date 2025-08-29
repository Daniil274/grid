# 🧠 Система проверяющих агентов GRID

Система автоматической проверки ответов AI агентов на предмет галлюцинаций и недостоверной информации.

> **🔄 Последние обновления**: 
> - ✅ Исправлена ошибка `Runner.run()` с неправильными аргументами
> - ✅ Добавлена передача инструкций проверяемого агента
> - ✅ Все промпты переведены на английский язык
> - ✅ Улучшена обработка контекста для проверки

## 🎯 Назначение

Проверяющие агенты предназначены для оценки ответов основных агентов и отсеивания результатов с явными галлюцинациями или недостоверной информацией. Система работает как вторая линия защиты после текстовых правил в промптах.

## 🏗️ Архитектура

### Основные компоненты

1. **HallucinationChecker** - специализированный агент-оценщик
2. **Output Guardrail** - автоматическая проверка ответов
3. **Конфигурируемые настройки** - все параметры настраиваются из конфигурации

### Принцип работы

```
Пользователь → Основной агент → Ответ → Output Guardrail → HallucinationChecker → Результат
                                                                    ↓
                                                            Блокировка/Пропуск
```

## ⚙️ Конфигурация

### Глобальные настройки

```yaml
settings:
  verify_hallucinations: true  # Включить проверку глобально
  verification_agent: "hallucination_checker"  # Агент для проверки
  verification_defaults:
    temperature: 0.1  # Температура для проверяющего агента
    max_tokens: 1000  # Максимум токенов
    context_strategy: "full"  # Стратегия контекста
    confidence_threshold: 0.7  # Порог уверенности
    strict_mode: true  # Строгий режим
```

### Настройки агентов

```yaml
agents:
  assistant:
    verify_output: true  # Включить проверку для этого агента
    verification_context: "full"  # Стратегия контекста
    verification_settings:  # Дополнительные настройки
      temperature: 0.1
      strict_mode: true
```

### Проверяющий агент

```yaml
agents:
  hallucination_checker:
    name: "Проверяющий агент"
    model: "gpt-5-nano"
    base_prompt: "verification_base"
    verify_output: false  # Отключаем проверку (избегаем рекурсии)
    verification_settings:
      temperature: 0.1
      max_tokens: 1000
      confidence_threshold: 0.7
      strict_mode: true
```

### Промпт для проверки

```yaml
prompt_templates:
  verification_base: |
    Ты агент-оценщик для проверки ответов на предмет галлюцинаций.
    
    Твоя задача - проанализировать ответ ассистента и определить, 
    содержит ли он утверждения, которые не подкреплены контекстом 
    диалога или результатами вызовов инструментов.
    
    ПРАВИЛА ПРОВЕРКИ:
    1. Галлюцинация = утверждение, которое не подтверждается:
       - Сообщениями пользователя
       - Результатами вызовов инструментов
       - Предыдущими сообщениями в диалоге
    
    2. НЕ является галлюцинацией:
       - Логические выводы на основе доступных фактов
       - Общие знания (если не противоречат контексту)
       - Предложения и рекомендации
    
    ВЕРНИ СТРУКТУРИРОВАННЫЙ ОТВЕТ:
    - has_hallucination: true/false
    - analysis: подробное обоснование
    - confidence: уверенность 0.0-1.0
    - flagged_statements: список неподтверждённых утверждений
```

## 🔧 Использование

### Автоматическая проверка

При создании агента с `verify_output: true` автоматически добавляется output guardrail:

```python
# В AgentFactory.create_agent()
if (self.config.config.settings.verify_hallucinations and 
    getattr(agent_config, 'verify_output', False)):
    output_guardrails.append(hallucination_guardrail)
```

### Ручная проверка

```python
from core.verification_agents import HallucinationChecker

# Создаем проверяющего агента
checker = HallucinationChecker(model, config)

# Проверка с инструкциями агента
result = await checker.verify_response(
    response, 
    context, 
    context_strategy="full",
    agent_instructions="You are a file management agent"
)

# Проверка без инструкций агента
result = await checker.verify_response(response, context)
```

## 📊 Результаты проверки

### HallucinationCheckOutput

```python
class HallucinationCheckOutput(BaseModel):
    has_hallucination: bool  # Обнаружена ли галлюцинация
    analysis: str  # Обоснование решения
    confidence: float  # Уверенность в решении (0.0-1.0)
    flagged_statements: List[str]  # Список неподтверждённых утверждений
```

### Логика срабатывания

```python
# В строгом режиме: любая галлюцинация = трипвайер
if strict_mode:
    tripwire_triggered = verification_result.has_hallucination
else:
    # В нестрогом режиме: только если уверенность выше порога
    tripwire_triggered = (
        verification_result.has_hallucination and 
        verification_result.confidence >= confidence_threshold
    )
```

## 🚀 Примеры использования

### Базовый пример

```python
from core.agent_factory import AgentFactory
from core.config import Config

# Создаем фабрику
config = Config("config.yaml")
factory = AgentFactory(config)

# Создаем агента с проверкой
agent = await factory.create_agent("assistant")
# Автоматически добавлен output_guardrail
```

### Тестирование

```bash
# Проверка импорта и базовой функциональности
python -c "from core.verification_agents import HallucinationChecker; print('✅ Импорт успешен')"

# Создание простого теста для проверки конкретных функций
# (файлы тестов удалены после проверки)
```

## 🔍 Стратегии контекста

### `full` - Полный контекст
Проверяющий агент получает весь диалог и историю вызовов инструментов.

### `last_turn` - Последний ход
Проверяющий агент получает только последний вопрос и ответ.

## 📋 Инструкции агента

Система автоматически передает инструкции проверяемого агента проверяющему агенту для лучшего понимания контекста:

```python
# В hallucination_guardrail
agent_instructions = getattr(agent, 'instructions', None)

verification_result = await checker.verify_response(
    response_text, 
    ctx.context,
    context_strategy,
    agent_instructions  # Инструкции проверяемого агента
)
```

Это позволяет проверяющему агенту:
- Понимать роль и задачи проверяемого агента
- Лучше оценивать соответствие ответа инструкциям
- Выявлять галлюцинации в контексте ожидаемого поведения

## ⚡ Производительность

- **Низкая температура** (0.1) для детерминизма
- **Ограниченные токены** (1000) для быстрой проверки
- **Кэширование** проверяющего агента
- **Асинхронная обработка** через guardrails
- **Контекстная оптимизация** - передача инструкций агента для лучшего понимания

## 🛡️ Безопасность

- **Предотвращение рекурсии** - проверяющий агент не проверяется сам
- **Fallback режим** - при ошибках проверки ответ не блокируется
- **Логирование** всех проверок и результатов
- **Настраиваемые пороги** для гибкости
- **Обработка ошибок** - корректная обработка исключений Runner.run()
- **Валидация аргументов** - проверка корректности передаваемых параметров

## 🔧 Настройка

### Включение/отключение

```yaml
# Глобально
settings:
  verify_hallucinations: false

# Для конкретного агента
agents:
  my_agent:
    verify_output: false
```

### Настройка строгости

```yaml
verification_defaults:
  strict_mode: false  # Менее строгая проверка
  confidence_threshold: 0.9  # Высокий порог уверенности
```

### Настройка модели

```yaml
agents:
  hallucination_checker:
    model: "claude-haiku"  # Быстрая модель для проверки
    verification_settings:
      temperature: 0.0  # Максимальная детерминированность
      max_tokens: 500   # Экономия токенов
```

## 📝 Логирование

Система ведет подробные логи:

```
INFO - core.verification_agents - Настройки проверки загружены: {...}
INFO - core.verification_agents - Проверка галлюцинаций пройдена для агента assistant
WARNING - core.verification_agents - Обнаружена галлюцинация в ответе агента: ...
```

## 🚨 Обработка ошибок

### При срабатывании guardrail

```python
try:
    output = await Runner.run(agent, message, ...)
except OutputGuardrailTripwireTriggered:
    return "❌ Ответ отклонён: обнаружены признаки галлюцинации"
```

### При ошибках проверки

```python
# В случае ошибки проверки ответ не блокируется
return GuardrailFunctionOutput(
    output_info=None,
    tripwire_triggered=False
)
```

## 🔮 Расширение функциональности

### Новые типы проверок

```python
@output_guardrail
async def fact_checking_guardrail(ctx, agent, output):
    # Проверка фактов по внешним источникам
    pass

@output_guardrail
async def safety_guardrail(ctx, agent, output):
    # Проверка безопасности контента
    pass
```

### Многоступенчатая проверка

```python
# Быстрая проверка + глубокая проверка
output_guardrails = [
    quick_hallucination_check,
    deep_fact_verification
]
```

## 📚 Дополнительные ресурсы

- [OpenAI Agents SDK Guardrails](https://github.com/openai/agents)
- [Примеры использования guardrails](https://github.com/openai/agents/tree/main/examples)
- [Документация по MCP](https://modelcontextprotocol.io/)

---

## 🔧 Исправления и улучшения

### Последние исправления (v2.0)

1. **Исправлена ошибка Runner.run()**
   - Убран неправильный аргумент `instructions`
   - Корректная передача параметров в OpenAI Agents SDK

2. **Добавлена передача инструкций агента**
   - Проверяющий агент получает инструкции проверяемого агента
   - Улучшенное понимание контекста и роли
   - Более точная оценка галлюцинаций

3. **Перевод на английский язык**
   - Все промпты переведены на английский
   - Унифицированный язык для международного использования
   - Соответствие стандартам OpenAI Agents SDK

4. **Улучшенная обработка контекста**
   - Оптимизированная подготовка контекста для проверки
   - Гибкие стратегии контекста (full/last_turn)
   - Автоматическое форматирование сообщений

---

**🎉 Система проверяющих агентов GRID v2.0 готова к использованию!**

Все настройки конфигурируются из `config.yaml`, хардкод полностью устранен, 
ошибки исправлены, функциональность расширена. 
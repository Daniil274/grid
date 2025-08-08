"""
OpenAI format converter utilities for GRID Agent System.
Handles conversion between OpenAI API format and GRID internal formats.
"""

import time
import uuid
from typing import List, Dict, Any, Optional
import re

from api.models.openai_models import (
    ChatMessage, ChatCompletionResponse, ChatCompletionChoice,
    ChatCompletionUsage, CompletionResponse, CompletionChoice,
    ModelInfo, ModelPermission, GridMetadata
)

# Agent to Model mapping
AGENT_MODEL_MAPPING = {
    # GRID агенты
    "grid-coordinator": "coordinator",
    "grid-code-agent": "code_agent", 
    "grid-file-agent": "file_agent",
    "grid-git-agent": "git_agent",
    "grid-security-guardian": "security_guardian",
    "grid-task-analyzer": "task_analyzer",
    "grid-context-quality": "context_quality",
    "grid-researcher": "researcher",
    "grid-thinker": "thinker",
    "grid-assistant": "coordinator",

    # Aliases для совместимости
    "gpt-4": "coordinator",
    "gpt-4-turbo": "coordinator",
    "gpt-3.5-turbo": "coordinator",
    "claude-3": "thinker",
    "claude-3-opus": "thinker",
    "claude-3-sonnet": "coordinator",

    # LM Studio / локальные модели
    "qwen3-4b-instruct-2507": "assistant",

    # Специализированные агенты
    "grid-security": "security_guardian",
    "grid-analysis": "task_analyzer", 
    "grid-development": "code_agent",
    "grid-files": "file_agent",
    "grid-research": "researcher",
    "grid-chat": "coordinator"
}

# Reverse mapping для информации о модели
MODEL_INFO_TEMPLATES = {
    "grid-coordinator": {
        "description": "Координатор агентов GRID - распределяет задачи между специализированными агентами",
        "capabilities": ["task_delegation", "agent_coordination", "general_assistance"],
        "tools": ["file_agent", "git_agent", "researcher", "code_agent"]
    },
    "grid-code-agent": {
        "description": "Специалист по программированию и анализу кода",
        "capabilities": ["code_analysis", "bug_detection", "code_generation", "documentation"],
        "tools": ["file_read", "file_write", "file_edit_patch", "git_status", "git_diff"]
    },
    "grid-file-agent": {
        "description": "Специалист по управлению файлами и файловой системой",
        "capabilities": ["file_management", "directory_operations", "file_search"],
        "tools": ["file_read", "file_write", "file_list", "file_info", "file_search"]
    },
    "grid-git-agent": {
        "description": "Специалист по работе с Git репозиториями",
        "capabilities": ["version_control", "repository_management", "commit_analysis"],
        "tools": ["git_status", "git_log", "git_diff", "git_commit", "git_branch_list"]
    },
    "grid-security-guardian": {
        "description": "Агент безопасности для анализа угроз и защиты системы",
        "capabilities": ["threat_analysis", "security_scanning", "policy_compliance"],
        "tools": ["threat_analysis", "policy_compliance"]
    },
    "grid-task-analyzer": {
        "description": "Анализатор задач для оценки выполнимости и планирования",
        "capabilities": ["task_analysis", "feasibility_assessment", "planning"],
        "tools": ["task_feasibility", "dependency_check"]
    },
    "grid-context-quality": {
        "description": "Агент контроля качества контекста и валидации данных",
        "capabilities": ["context_validation", "quality_assessment", "data_validation"],
        "tools": ["context_validation", "dependency_check", "quality_metrics"]
    },
    "grid-researcher": {
        "description": "Исследователь для анализа файлов и документов",
        "capabilities": ["research", "document_analysis", "information_extraction"],
        "tools": ["file_write", "file_read", "file_search"]
    },
    "grid-thinker": {
        "description": "Агент для глубокого анализа и размышлений",
        "capabilities": ["deep_analysis", "problem_solving", "strategic_thinking"],
        "tools": []
    }
}

class OpenAIConverter:
    """Утилиты для конвертации между OpenAI и GRID форматами."""
    
    @staticmethod
    def model_to_agent(model: str) -> str:
        """Конвертация имени модели в тип агента GRID."""
        return AGENT_MODEL_MAPPING.get(model, "coordinator")
    
    @staticmethod
    def agent_to_model(agent_type: str) -> str:
        """Конвертация типа агента GRID в имя модели."""
        for model, agent in AGENT_MODEL_MAPPING.items():
            if agent == agent_type:
                return model
        return f"grid-{agent_type}"
    
    @staticmethod
    def extract_user_message(messages: List[ChatMessage]) -> str:
        """Извлечение последнего пользовательского сообщения."""
        user_messages = [msg for msg in messages if msg.role == "user"]
        
        if user_messages:
            last = user_messages[-1]
            content = last.content
            # If content is array of parts (OpenAI format), join text parts
            if isinstance(content, list):
                parts = []
                for part in content:
                    # typical shape {"type":"text","text":"..."}
                    if isinstance(part, dict):
                        txt = part.get("text") or part.get("content") or ""
                        if txt:
                            parts.append(txt)
                return "\n".join(parts) if parts else ""
            return content
        
        # Если нет пользовательского сообщения, объединяем все
        parts = []
        for msg in messages:
            c = msg.content
            if isinstance(c, list):
                text = []
                for part in c:
                    if isinstance(part, dict):
                        t = part.get("text") or part.get("content") or ""
                        if t:
                            text.append(t)
                c = "\n".join(text)
            parts.append(f"{msg.role}: {c}")
        return "\n".join(parts)
    
    @staticmethod
    def build_conversation_context(messages: List[ChatMessage]) -> str:
        """Построение контекста разговора для агента."""
        context_parts = []
        
        for msg in messages[:-1]:  # Исключаем последнее сообщение
            if msg.role == "system":
                context_parts.append(f"System: {msg.content}")
            elif msg.role == "user":
                context_parts.append(f"User: {msg.content}")
            elif msg.role == "assistant":
                context_parts.append(f"Assistant: {msg.content}")
        
        return "\n".join(context_parts) if context_parts else ""
    
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Примерная оценка количества токенов в тексте."""
        # Простая эвристика: ~4 символа на токен для русского/английского
        # Учитываем пробелы и знаки препинания
        words = len(text.split())
        chars = len(text)
        
        # Формула учитывает длину слов и общую длину текста
        estimated_tokens = max(words * 0.75, chars / 4)
        return int(estimated_tokens)
    
    @staticmethod
    def agent_result_to_chat_completion(
        result: Any,
        model: str,
        agent_type: str,
        execution_time: float,
        request_id: str,
        session_id: Optional[str] = None
    ) -> ChatCompletionResponse:
        """Конвертация результата агента в OpenAI chat completion."""
        
        # Извлекаем контент из результата
        if hasattr(result, 'content'):
            content = result.content
        elif isinstance(result, str):
            content = result
        else:
            content = str(result)
        
        # Подсчет токенов
        completion_tokens = OpenAIConverter.estimate_tokens(content)
        prompt_tokens = 50  # Примерная базовая оценка
        
        # Создаем response
        response = ChatCompletionResponse(
            id=f"chatcmpl-grid-{request_id}",
            created=int(time.time()),
            model=model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant",
                        content=content
                    ),
                    finish_reason="stop"
                )
            ],
            usage=ChatCompletionUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens
            ),
            grid_metadata=GridMetadata(
                agent_used=agent_type,
                execution_time=execution_time,
                tools_called=getattr(result, 'tools_used', []),
                security_analysis=getattr(result, 'security_info', {}),
                session_id=session_id,
                trace_id=getattr(result, 'trace_id', None),
                working_directory=getattr(result, 'working_directory', None)
            )
        )
        
        return response
    
    @staticmethod
    def chat_completion_to_completion(
        chat_response: ChatCompletionResponse
    ) -> CompletionResponse:
        """Конвертация chat completion в legacy completion формат."""
        
        return CompletionResponse(
            id=chat_response.id.replace("chatcmpl", "cmpl"),
            created=chat_response.created,
            model=chat_response.model,
            choices=[
                CompletionChoice(
                    text=choice.message.content,
                    index=choice.index,
                    logprobs=None,
                    finish_reason=choice.finish_reason
                )
                for choice in chat_response.choices
            ],
            usage=chat_response.usage
        )
    
    @staticmethod
    def create_model_info(model_name: str, agent_type: str) -> ModelInfo:
        """Создание информации о модели из данных агента."""
        
        template = MODEL_INFO_TEMPLATES.get(model_name, {})
        
        return ModelInfo(
            id=model_name,
            object="model",
            created=int(time.time()),
            owned_by="grid-system",
            permission=[
                ModelPermission(
                    id=f"modelperm-{uuid.uuid4().hex[:10]}",
                    created=int(time.time()),
                    allow_create_engine=False,
                    allow_sampling=True,
                    allow_logprobs=False,
                    allow_search_indices=False,
                    allow_view=True,
                    allow_fine_tuning=False,
                    organization="*"
                )
            ],
            root=model_name,
            parent=None,
            description=template.get("description", f"GRID Agent: {agent_type}"),
            agent_type=agent_type,
            capabilities=template.get("capabilities", []),
            tools=template.get("tools", [])
        )
    
    @staticmethod
    def get_available_models(available_agents: Dict[str, Any]) -> List[ModelInfo]:
        """Получение списка доступных моделей из агентов."""
        models = []
        
        for model_name, agent_type in AGENT_MODEL_MAPPING.items():
            if agent_type in available_agents:
                model_info = OpenAIConverter.create_model_info(model_name, agent_type)
                models.append(model_info)
        
        return models
    
    @staticmethod
    def validate_model_name(model: str) -> bool:
        """Проверка валидности имени модели.
        Принимаем любые имена, чтобы поддерживать локальные OpenAI-совместимые провайдеры (LM Studio, Ollama).
        """
        return True
    
    @staticmethod
    def sanitize_content(content: str) -> str:
        """Очистка контента от потенциально проблемных символов."""
        # Удаляем управляющие символы, кроме переводов строк и табов
        content = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', content)
        
        # Ограничиваем максимальную длину ответа
        max_length = 50000  # 50K символов
        if len(content) > max_length:
            content = content[:max_length] + "\n\n[Response truncated due to length]"
        
        return content
    
    @staticmethod
    def format_error_response(error: Exception, request_id: str) -> Dict[str, Any]:
        """Форматирование ошибки в OpenAI формат."""
        error_type = type(error).__name__
        
        # Определяем тип ошибки для OpenAI формата
        if "authentication" in str(error).lower():
            openai_error_type = "invalid_api_key"
        elif "permission" in str(error).lower() or "access" in str(error).lower():
            openai_error_type = "insufficient_quota"
        elif "rate" in str(error).lower() or "limit" in str(error).lower():
            openai_error_type = "rate_limit_exceeded"
        elif "timeout" in str(error).lower():
            openai_error_type = "timeout"
        else:
            openai_error_type = "server_error"
        
        return {
            "error": {
                "message": str(error),
                "type": openai_error_type,
                "param": None,
                "code": error_type
            },
            "id": f"error-grid-{request_id}"
        }
#!/usr/bin/env python3
"""
Standalone GRID Agent System API with built-in mocks.
Self-contained version for testing and demonstration.
"""

import asyncio
import time
import uuid
import json
import logging
from contextlib import asynccontextmanager
from typing import Dict, Any, List, Optional, AsyncIterator
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field
import uvicorn

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Security
security = HTTPBearer(auto_error=False)

# Mock implementations
@dataclass
class MockAgentResult:
    content: str
    tools_used: List[str] = None
    metadata: Dict[str, Any] = None

@dataclass 
class MockStreamChunk:
    content: str
    is_final: bool = False

class MockAgent:
    def __init__(self, agent_type: str):
        self.agent_type = agent_type
        
    async def run(self, message: str, context: Dict[str, Any] = None) -> MockAgentResult:
        await asyncio.sleep(0.5)
        
        responses = {
            "coordinator": f"🎯 Как координатор, я обработаю ваш запрос: {message[:50]}...\n\nДелегирую задачу соответствующему агенту.",
            "code_agent": f"💻 Анализирую код по запросу: {message[:50]}...\n\n```python\n# Пример решения\ndef solve():\n    return 'Готово!'\n```",
            "file_agent": f"📁 Работаю с файлами: {message[:50]}...\n\nОперации:\n- Чтение файлов\n- Запись данных\n- Поиск файлов",
            "security_guardian": f"🛡️ Анализ безопасности: {message[:50]}...\n\n✅ Угроз не обнаружено\n📊 Уровень риска: НИЗКИЙ",
            "task_analyzer": f"📊 Анализ задачи: {message[:50]}...\n\n🔍 Сложность: СРЕДНЯЯ\n⏱️ Время: 15-30 мин\n✨ Выполнимо",
        }
        
        return MockAgentResult(
            content=responses.get(self.agent_type, f"Ответ от {self.agent_type}: {message}"),
            tools_used=[f"mock_{self.agent_type}_tool"]
        )
    
    async def stream(self, message: str, context: Dict[str, Any] = None) -> AsyncIterator[MockStreamChunk]:
        result = await self.run(message, context)
        words = result.content.split()
        
        for i, word in enumerate(words):
            await asyncio.sleep(0.1)
            yield MockStreamChunk(content=word + " ", is_final=(i == len(words) - 1))

class MockAgentFactory:
    def __init__(self):
        self.agents = {
            "coordinator": {"name": "Координатор", "description": "Координатор агентов"},
            "code_agent": {"name": "Кодовый агент", "description": "Специалист по коду"},
            "file_agent": {"name": "Файловый агент", "description": "Управление файлами"},
            "security_guardian": {"name": "Страж безопасности", "description": "Анализ безопасности"},
            "task_analyzer": {"name": "Анализатор задач", "description": "Анализ задач"},
        }
    
    async def create_agent(self, agent_type: str) -> MockAgent:
        return MockAgent(agent_type)
    
    def get_available_agents(self) -> Dict[str, Any]:
        return self.agents

# Pydantic models
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    max_tokens: Optional[int] = None
    temperature: Optional[float] = 0.7
    stream: Optional[bool] = False

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[Dict[str, Any]]
    usage: Dict[str, int]

class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "grid-system"

# Agent mappings
AGENT_MODEL_MAPPING = {
    "grid-coordinator": "coordinator",
    "grid-code-agent": "code_agent", 
    "grid-file-agent": "file_agent",
    "grid-security-guardian": "security_guardian",
    "grid-task-analyzer": "task_analyzer",
    "gpt-4": "coordinator",
    "gpt-3.5-turbo": "coordinator",
}

# Global state
agent_factory = MockAgentFactory()

# Dependencies
async def get_agent_factory():
    return agent_factory

async def get_current_user(credentials = Depends(security)):
    if not credentials:
        return {"user_id": "anonymous", "roles": ["user"]}
    return {"user_id": "user123", "roles": ["user"]}

# App lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Starting GRID API (Standalone)")
    yield
    logger.info("👋 Shutting down GRID API")

# Create app
app = FastAPI(
    title="GRID Agent System API (Standalone)",
    description="OpenAI-compatible API with mock agents",
    version="1.0.0-standalone",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
@app.get("/")
async def root():
    return {
        "message": "GRID Agent System API (Standalone)",
        "version": "1.0.0-standalone",
        "mode": "standalone-mock",
        "docs": "/docs",
        "health": "/health"
    }

@app.get("/health")
async def health():
    return {"status": "healthy", "mode": "standalone"}

@app.get("/v1/models")
async def list_models():
    models = []
    for model_name, agent_type in AGENT_MODEL_MAPPING.items():
        if agent_type in agent_factory.agents:
            models.append(ModelInfo(
                id=model_name,
                created=int(time.time()),
                owned_by="grid-system"
            ))
    
    return {"object": "list", "data": models}

@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    factory = Depends(get_agent_factory),
    user = Depends(get_current_user)
):
    # Get agent type
    agent_type = AGENT_MODEL_MAPPING.get(request.model, "coordinator")
    
    # Get user message
    user_message = ""
    for msg in reversed(request.messages):
        if msg.role == "user":
            user_message = msg.content
            break
    
    if request.stream:
        # Streaming response
        async def generate():
            completion_id = f"chatcmpl-{uuid.uuid4().hex[:10]}"
            created = int(time.time())
            
            try:
                agent = await factory.create_agent(agent_type)
                async for chunk in agent.stream(user_message):
                    chunk_data = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": request.model,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": chunk.content},
                            "finish_reason": "stop" if chunk.is_final else None
                        }]
                    }
                    yield f"data: {json.dumps(chunk_data)}\n\n"
                
                # Final chunk
                final_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": request.model,
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop"
                    }]
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"
                
            except Exception as e:
                error_chunk = {
                    "error": {"message": str(e), "type": "server_error"}
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
                yield "data: [DONE]\n\n"
        
        return StreamingResponse(generate(), media_type="text/event-stream")
    
    else:
        # Synchronous response
        agent = await factory.create_agent(agent_type)
        result = await agent.run(user_message)
        
        response = ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:10]}",
            created=int(time.time()),
            model=request.model,
            choices=[{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result.content
                },
                "finish_reason": "stop"
            }],
            usage={
                "prompt_tokens": 50,
                "completion_tokens": int(len(result.content.split()) * 1.3),
                "total_tokens": int(50 + len(result.content.split()) * 1.3)
            }
        )
        
        return response

@app.get("/v1/agents/")
async def list_agents():
    agents = []
    for agent_type, info in agent_factory.agents.items():
        agents.append({
            "agent_type": agent_type,
            "name": info["name"],
            "description": info["description"],
            "status": "available"
        })
    return agents

@app.post("/v1/auth/login")
async def login(credentials: dict):
    # Mock login
    return {
        "access_token": f"mock-token-{int(time.time())}",
        "token_type": "bearer",
        "expires_in": 3600,
        "user_id": "user123"
    }

if __name__ == "__main__":
    print("🚀 Starting GRID Agent System API (Standalone)")
    print("📖 Documentation: http://localhost:8000/docs")
    print("🔍 Health Check: http://localhost:8000/health")
    print("🤖 Models: http://localhost:8000/v1/models")
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info"
    )
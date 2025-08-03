"""
Mock implementations for testing API without full GRID system.
"""

import asyncio
import time
from typing import Any, Dict, List, AsyncIterator
from dataclasses import dataclass

# Mock Agent Result
@dataclass
class MockAgentResult:
    content: str
    tools_used: List[str] = None
    security_info: Dict[str, Any] = None
    trace_id: str = None
    working_directory: str = None
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.tools_used is None:
            self.tools_used = []
        if self.security_info is None:
            self.security_info = {"threat_level": "LOW", "risk_score": 0.1}
        if self.metadata is None:
            self.metadata = {}

# Mock Stream Chunk
@dataclass 
class MockStreamChunk:
    content: str
    is_final: bool = False

# Mock Agent
class MockAgent:
    def __init__(self, agent_type: str):
        self.agent_type = agent_type
        
    async def run(self, message: str, context: Dict[str, Any] = None) -> MockAgentResult:
        """Mock agent execution."""
        # Simulate processing time
        await asyncio.sleep(0.5 + len(message) / 1000)
        
        # Generate response based on agent type
        responses = {
            "coordinator": f"Как координатор агентов, я помогу вам с задачей: {message[:50]}...",
            "code_agent": f"Анализируя ваш запрос о коде: {message[:50]}...\n\n```python\n# Пример кода\nprint('Hello, World!')\n```",
            "file_agent": f"Работаю с файлами по запросу: {message[:50]}...\n\nСписок файлов:\n- example.txt\n- data.json",
            "security_guardian": f"Анализ безопасности: {message[:50]}...\n\n🛡️ Угроз не обнаружено.\nУровень риска: НИЗКИЙ",
            "task_analyzer": f"Анализ задачи: {message[:50]}...\n\n📊 Сложность: СРЕДНЯЯ\nВремя выполнения: 15-30 минут",
            "git_agent": f"Git операции для: {message[:50]}...\n\n```bash\ngit status\n# On branch main\n# nothing to commit, working tree clean\n```"
        }
        
        response_text = responses.get(self.agent_type, f"Ответ от агента {self.agent_type}: {message}")
        
        return MockAgentResult(
            content=response_text,
            tools_used=[f"mock_{self.agent_type}_tool"],
            trace_id=f"trace_{int(time.time())}"
        )
    
    async def stream(self, message: str, context: Dict[str, Any] = None) -> AsyncIterator[MockStreamChunk]:
        """Mock streaming execution."""
        response = await self.run(message, context)
        words = response.content.split()
        
        # Stream words with small delays
        for i, word in enumerate(words):
            await asyncio.sleep(0.1)
            is_final = (i == len(words) - 1)
            yield MockStreamChunk(content=word + " ", is_final=is_final)

# Mock Agent Factory
class MockSecurityAwareAgentFactory:
    def __init__(self, config=None):
        self.config = config
        self.available_agents = {
            "coordinator": {"name": "Координатор", "model": "mock", "tools": [], "description": "Координатор агентов"},
            "code_agent": {"name": "Кодовый агент", "model": "mock", "tools": ["file_read", "code_analysis"], "description": "Специалист по коду"},
            "file_agent": {"name": "Файловый агент", "model": "mock", "tools": ["file_read", "file_write"], "description": "Управление файлами"},
            "git_agent": {"name": "Git агент", "model": "mock", "tools": ["git_status", "git_log"], "description": "Операции с Git"},
            "security_guardian": {"name": "Страж безопасности", "model": "mock", "tools": ["threat_analysis"], "description": "Анализ безопасности"},
            "task_analyzer": {"name": "Анализатор задач", "model": "mock", "tools": ["task_analysis"], "description": "Анализ задач"},
            "context_quality": {"name": "Контроль качества", "model": "mock", "tools": ["context_validation"], "description": "Контроль качества"},
            "researcher": {"name": "Исследователь", "model": "mock", "tools": ["research"], "description": "Исследования"},
            "thinker": {"name": "Мыслитель", "model": "mock", "tools": [], "description": "Глубокий анализ"}
        }
    
    async def initialize(self):
        """Mock initialization."""
        print("📦 Mock agent factory initialized")
    
    async def cleanup(self):
        """Mock cleanup."""
        print("🧹 Mock agent factory cleaned up")
    
    async def create_agent(self, agent_type: str) -> MockAgent:
        """Create mock agent."""
        if agent_type not in self.available_agents:
            raise ValueError(f"Agent type '{agent_type}' not available")
        return MockAgent(agent_type)
    
    def get_available_agents(self) -> Dict[str, Any]:
        """Get available agents."""
        return self.available_agents.copy()
    
    def get_security_statistics(self) -> Dict[str, Any]:
        """Get mock security statistics."""
        return {
            "total_security_agents": 3,
            "full_analysis_agents": 3,
            "security_only_agents": 0,
            "audit_logging_enabled": True
        }

# Mock Config
class MockConfig:
    def __init__(self, config_path: str = None):
        self.config_path = config_path
        self.settings = MockSettings()
    
    def get_working_directory(self) -> str:
        return "/workspaces/grid"
    
    def get_default_agent(self) -> str:
        return "coordinator"

class MockSettings:
    def __init__(self):
        self.agent_logging = MockAgentLogging()

class MockAgentLogging:
    def __init__(self):
        self.enabled = True
        self.level = "info"
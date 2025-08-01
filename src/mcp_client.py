"""
Заглушка для MCP клиента.
"""

from typing import List, Dict, Any

class MCPClient:
    """Заглушка для MCP клиента."""
    
    def __init__(self, name: str, server_command: List[str], env_vars: Dict[str, str]):
        self.name = name
        self.server_command = server_command
        self.env_vars = env_vars
    
    async def connect(self):
        """Заглушка для подключения."""
        print(f"🔌 MCP клиент '{self.name}' подключен (заглушка)")
    
    async def disconnect(self):
        """Заглушка для отключения."""
        print(f"🔌 MCP клиент '{self.name}' отключен (заглушка)")
    
    async def get_tools(self) -> List[Any]:
        """Заглушка для получения инструментов."""
        print(f"🔧 MCP инструменты для '{self.name}' недоступны (заглушка)")
        return []
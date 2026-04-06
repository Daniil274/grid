import asyncio
import logging
import sys
from pathlib import Path

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from core.config import Config
from core.agent_factory import AgentFactory, ConsoleStreamObserver

async def main():
    # Configure logging to see what's happening
    logging.basicConfig(level=logging.INFO)
    
    print("--- Initializing Config ---")
    cfg = Config()
    
    print("--- Initializing AgentFactory ---")
    factory = AgentFactory(config=cfg)
    
    agent_key = "system_agent"
    message = "В examples/claude-tools аналог claude code. Зарегистрируй систему claude-code на основе этого примера. Реализуй сборщика контекста, координатора и проверяющего, подключи beads."
    
    print(f"--- Running Agent: {agent_key} ---")
    print(f"Message: {message}")
    
    try:
        # Run agent with streaming to console
        response = await factory.run_agent(
            agent_key=agent_key,
            message=message,
            stream=True
        )
        
        print("\n\n--- Final Response ---")
        print(response)
        
    except Exception as e:
        print(f"\n--- Error during execution ---")
        import traceback
        traceback.print_exc()
    finally:
        await factory.cleanup()

if __name__ == "__main__":
    asyncio.run(main())

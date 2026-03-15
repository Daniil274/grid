import asyncio
import logging

import uvicorn

from web_chat.runtime import WebChatRuntime
from web_chat.server import create_app

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    runtime = WebChatRuntime()
    app = create_app(runtime)
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())

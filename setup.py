from setuptools import setup, find_packages

setup(
    name="grid-agent-system",
    version="0.1.0",
    description="GRID Agent System API",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "fastapi==0.104.1",
        "uvicorn[standard]==0.24.0",
        "pydantic==2.5.0",
        "python-jose[cryptography]==3.3.0",
        "PyJWT==2.8.0",
        "passlib[bcrypt]==1.7.4",
        "python-multipart==0.0.6",
        "httpx==0.25.2",
        "aiohttp==3.9.0",
        "psutil==5.9.6",
        "orjson==3.9.10",
        "websockets==12.0",
        "python-socketio==5.10.0",
        "redis==5.0.1",
        "uvloop==0.19.0",
        "gunicorn==21.2.0",
    ],
    extras_require={
        "dev": [
            "pytest==7.4.3",
            "pytest-asyncio==0.21.1",
            "pytest-mock==3.12.0",
        ]
    },
)
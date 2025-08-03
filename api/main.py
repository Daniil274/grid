#!/usr/bin/env python3
"""
GRID Agent System - FastAPI Application
OpenAI-compatible API for managing and interacting with GRID agents.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
import sys

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
import uvicorn

# Add grid package to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import Config
from core.security_agent_factory import SecurityAwareAgentFactory
from utils.logger import Logger
from utils.exceptions import GridError

from api.routers import openai_compatible, agents, system, auth, websocket
from api.middleware.authentication import AuthenticationMiddleware
from api.middleware.security import SecurityMiddleware
from api.middleware.logging import LoggingMiddleware
from api.middleware.rate_limiting import RateLimitingMiddleware

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("Starting GRID Agent System API...")
    
    try:
        # Initialize configuration
        config_path = "config.yaml"
        app.state.config = Config(config_path)
        
        # Initialize agent factory
        app.state.agent_factory = SecurityAwareAgentFactory(app.state.config)
        
        # Initialize logger
        Logger.configure(
            level="INFO",
            enable_console=True,
            log_dir="logs",
            enable_legacy_logs=True
        )
        
        logger.info("GRID Agent System API started successfully")
        
        yield
        
    except Exception as e:
        logger.error(f"Failed to start API: {e}")
        raise
    finally:
        # Cleanup
        logger.info("Shutting down GRID Agent System API...")
        if hasattr(app.state, 'agent_factory'):
            await app.state.agent_factory.cleanup()
        logger.info("GRID Agent System API shutdown complete")

# Create FastAPI application
app = FastAPI(
    title="GRID Agent System API",
    description="OpenAI-compatible API for GRID Agent System with security and monitoring",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # В продакшене ограничить конкретными доменами
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Custom middleware
app.add_middleware(LoggingMiddleware)
app.add_middleware(RateLimitingMiddleware)
app.add_middleware(SecurityMiddleware)
app.add_middleware(AuthenticationMiddleware)

# Exception handlers
@app.exception_handler(GridError)
async def grid_error_handler(request: Request, exc: GridError):
    """Handle GRID-specific errors."""
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": str(exc),
                "type": "grid_error",
                "code": getattr(exc, 'code', 'unknown')
            }
        }
    )

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Handle request validation errors."""
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "message": "Invalid request format",
                "type": "validation_error",
                "details": exc.errors()
            }
        }
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle unexpected errors."""
    logger.error(f"Unexpected error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": "Internal server error",
                "type": "server_error"
            }
        }
    )

# Include routers
app.include_router(
    openai_compatible.router,
    prefix="/v1",
    tags=["OpenAI Compatible"]
)

app.include_router(
    agents.router,
    prefix="/v1/agents",
    tags=["Agents"]
)

app.include_router(
    system.router,
    prefix="/v1/system",
    tags=["System"]
)

app.include_router(
    auth.router,
    prefix="/v1/auth",
    tags=["Authentication"]
)

app.include_router(
    websocket.router,
    prefix="/ws",
    tags=["WebSocket"]
)

# Root endpoints
@app.get("/")
async def root():
    """API root endpoint."""
    return {
        "message": "GRID Agent System API",
        "version": "1.0.0",
        "compatibility": "OpenAI API v1",
        "features": [
            "OpenAI-compatible chat completions",
            "Agent management",
            "Real-time monitoring",
            "Security analysis",
            "Session management"
        ],
        "docs": "/docs",
        "health": "/v1/system/health"
    }

@app.get("/health")
async def health_check():
    """Simple health check endpoint."""
    return {"status": "healthy", "timestamp": asyncio.get_event_loop().time()}

if __name__ == "__main__":
    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
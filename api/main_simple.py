#!/usr/bin/env python3
"""
Simplified GRID Agent System API for testing.
Uses mocks instead of full agent system.
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

# Import mocks instead of real implementations
from api.mocks import MockSecurityAwareAgentFactory, MockConfig
from api.routers import openai_compatible, agents, system, auth

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("Starting GRID Agent System API (Mock Mode)...")
    
    try:
        # Initialize mock configuration
        app.state.config = MockConfig("config.yaml")
        
        # Initialize mock agent factory
        app.state.agent_factory = MockSecurityAwareAgentFactory(app.state.config)
        await app.state.agent_factory.initialize()
        
        logger.info("GRID Agent System API started successfully (Mock Mode)")
        
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
    title="GRID Agent System API (Mock)",
    description="OpenAI-compatible API for GRID Agent System (Testing Mode)",
    version="1.0.0-mock",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Exception handlers
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

# Root endpoints
@app.get("/")
async def root():
    """API root endpoint."""
    return {
        "message": "GRID Agent System API (Mock Mode)",
        "version": "1.0.0-mock",
        "mode": "testing",
        "compatibility": "OpenAI API v1",
        "features": [
            "OpenAI-compatible chat completions",
            "Agent management",
            "System monitoring",
            "Authentication (mock)",
            "Mock agent responses"
        ],
        "docs": "/docs",
        "health": "/health"
    }

@app.get("/health")
async def health_check():
    """Simple health check endpoint."""
    return {
        "status": "healthy", 
        "mode": "mock",
        "timestamp": asyncio.get_event_loop().time()
    }

if __name__ == "__main__":
    uvicorn.run(
        "api.main_simple:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
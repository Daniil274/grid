#!/usr/bin/env python3
"""
Startup script for GRID Agent System API.
"""

import sys
import os
import asyncio
from pathlib import Path
from utils.logger import Logger
logger = Logger(__name__)

# Add current directory to Python path
sys.path.insert(0, str(Path(__file__).parent))

def check_dependencies():
    """Check if required dependencies are installed."""
    try:
        import fastapi
        import uvicorn
        import pydantic
        logger.info("Core dependencies found")
        return True
    except ImportError as e:
        logger.error(f"Missing dependency: {e}")
        logger.info("Please install dependencies: pip install -r requirements-api.txt")
        return False

def check_config():
    """Check if config file exists."""
    config_path = Path("config.yaml")
    if config_path.exists():
        logger.info("Configuration file found")
        return True
    else:
        logger.warning("Configuration file not found: config.yaml")
        return False

def main():
    """Main startup function."""
    logger.info("Starting GRID Agent System API")
    
    # Check dependencies
    if not check_dependencies():
        sys.exit(1)
    
    # Check config
    if not check_config():
        logger.info("Using default configuration...")
    
    # Set environment variables
    os.environ.setdefault("PYTHONPATH", str(Path.cwd()))  # не меняем cwd, чтобы логи писались в ./logs
    
    logger.info("Starting API server...")
    logger.info("Host: 0.0.0.0, Port: 8000, Docs: http://localhost:8000/docs, Health: http://localhost:8000/health")
    
    try:
        # Import and run
        import uvicorn
        uvicorn.run(
            "api.main:app",
            host="0.0.0.0",
            port=8000,
            reload=True,
            log_level="info"
        )
    except KeyboardInterrupt:
        logger.info("API server stopped")
    except Exception as e:
        logger.error(f"Error starting API: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
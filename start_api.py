#!/usr/bin/env python3
"""
Startup script for GRID Agent System API.
"""

import sys
import os
import asyncio
from pathlib import Path

# Add current directory to Python path
sys.path.insert(0, str(Path(__file__).parent))

def check_dependencies():
    """Check if required dependencies are installed."""
    try:
        import fastapi
        import uvicorn
        import pydantic
        print("✅ Core dependencies found")
        return True
    except ImportError as e:
        print(f"❌ Missing dependency: {e}")
        print("Please install dependencies:")
        print("pip install -r requirements-api.txt")
        return False

def check_config():
    """Check if config file exists."""
    config_path = Path("config.yaml")
    if config_path.exists():
        print("✅ Configuration file found")
        return True
    else:
        print("❌ Configuration file not found: config.yaml")
        return False

def main():
    """Main startup function."""
    print("🚀 Starting GRID Agent System API")
    print("=" * 50)
    
    # Check dependencies
    if not check_dependencies():
        sys.exit(1)
    
    # Check config
    if not check_config():
        print("Using default configuration...")
    
    # Set environment variables
    os.environ.setdefault("PYTHONPATH", str(Path.cwd()))
    
    print("\n📡 Starting API server...")
    print("   Host: 0.0.0.0")
    print("   Port: 8000")
    print("   Docs: http://localhost:8000/docs")
    print("   Health: http://localhost:8000/health")
    print("\n🔍 Press Ctrl+C to stop")
    print("=" * 50)
    
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
        print("\n\n👋 API server stopped")
    except Exception as e:
        print(f"\n❌ Error starting API: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
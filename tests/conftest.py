"""
Pytest configuration and shared fixtures for Grid Agent System tests.
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, MagicMock
import yaml
import os


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    temp_path = tempfile.mkdtemp()
    yield Path(temp_path)
    
    # Fix readonly permissions before cleanup on Windows
    def handle_remove_readonly(func, path, exc):
        import stat
        import time
        if os.path.exists(path):
            # First try to change permissions
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except PermissionError:
                # If still permission error, try waiting a bit for file handles to close
                time.sleep(0.1)
                try:
                    os.chmod(path, stat.S_IWRITE)  
                    func(path)
                except:
                    # Last resort - just ignore the file
                    pass
    
    if os.name == 'nt':  # Windows
        shutil.rmtree(temp_path, onerror=handle_remove_readonly)
    else:
        shutil.rmtree(temp_path)


@pytest.fixture
def sample_config():
    """Sample configuration for testing."""
    return {
        "settings": {
            "default_agent": "test_agent",
            "max_history": 10,
            "max_turns": 5,
            "agent_timeout": 30,
            "working_directory": "/tmp/test",
            "config_directory": "/tmp/config",
            "allow_path_override": True,
            "mcp_enabled": False,
            "agent_logging": {
                "enabled": True,
                "level": "INFO"
            }
        },
        "providers": {
            "openai": {
                "name": "openai",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
                "timeout": 30,
                "max_retries": 3
            }
        },
        "models": {
            "gpt-4": {
                "name": "gpt-4",
                "provider": "openai",
                "temperature": 0.7,
                "max_tokens": 4000,
                "use_responses_api": False
            }
        },
        "agents": {
            "test_agent": {
                "name": "Test Agent",
                "model": "gpt-4",
                "tools": ["file_read", "file_write"],
                "base_prompt": "test_prompt",
                "description": "Test agent for testing"
            }
        },
        "tools": {
            "file_read": {
                "type": "function",
                "name": "file_read"
            },
            "file_write": {
                "type": "function", 
                "name": "file_write"
            }
        }
    }


@pytest.fixture
def config_file(temp_dir, sample_config):
    """Create a temporary config file."""
    config_path = temp_dir / "config.yaml"
    with open(config_path, 'w') as f:
        yaml.dump(sample_config, f)
    return config_path


@pytest.fixture
def mock_openai():
    """Mock OpenAI client."""
    return Mock()


@pytest.fixture
def sample_test_file(temp_dir):
    """Create a sample test file."""
    test_file = temp_dir / "test_file.txt"
    test_file.write_text("Hello, World!")
    return test_file


@pytest.fixture
def mock_git_repo(temp_dir):
    """Create a mock git repository."""
    git_dir = temp_dir / ".git"
    git_dir.mkdir()
    return temp_dir


@pytest.fixture(autouse=True)
def setup_test_env():
    """Setup test environment variables."""
    os.environ.setdefault("OPENAI_API_KEY", "test-key")
    yield
    # Cleanup is handled by pytest automatically


@pytest.fixture
def mock_logger():
    """Mock logger for testing."""
    return Mock()


class MockSQLiteSession:
    """Mock SQLiteSession for testing."""
    
    def __init__(self):
        self.messages = []
        self.closed = False
    
    def add_message(self, role, content):
        self.messages.append({"role": role, "content": content})
    
    def get_messages(self):
        return self.messages
    
    def close(self):
        self.closed = True


@pytest.fixture
def mock_session():
    """Mock session for testing."""
    return MockSQLiteSession()
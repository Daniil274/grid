"""
Pytest configuration and shared fixtures for Grid Agent System tests.
"""

import pytest
from unittest.mock import Mock
import yaml
import os


@pytest.fixture
def temp_dir(tmp_path):
    """A temporary directory, removed by pytest."""
    return tmp_path


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


@pytest.fixture
def agent_workspace(tmp_path):
    """Run the test as an agent whose working directory is ``tmp_path``.

    Tool paths are confined to the agent's working directory, so tools called
    with paths under ``tmp_path`` need it bound as that directory.
    """
    from types import SimpleNamespace

    from utils.path_utils import factory_path_context

    factory = SimpleNamespace(
        config=SimpleNamespace(get_working_directory=lambda: str(tmp_path)),
        container_id=None,
    )
    with factory_path_context(factory):
        yield tmp_path


def link(path, target, *, directory=False):
    """Make *path* a link to *target*, as an agent in a container could.

    Windows grants symbolic links only to administrators and developer mode;
    a directory then gets a junction - the link any user can make there - and
    a file link, which has no such stand-in, skips the test.
    """
    from pathlib import Path

    try:
        Path(path).symlink_to(target, target_is_directory=directory)
    except OSError:
        if os.name != "nt":
            raise
        if not directory:
            pytest.skip("symbolic links to files need privileges on this Windows")
        import _winapi

        _winapi.CreateJunction(str(target), str(path))

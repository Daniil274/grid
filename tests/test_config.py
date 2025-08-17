"""
Unit tests for core/config.py module.
"""

import pytest
import os
import yaml
from pathlib import Path
from unittest.mock import patch, Mock

from core.config import Config
from utils.exceptions import ConfigError


class TestConfig:
    """Test Config class functionality."""
    
    def test_config_init_with_valid_file(self, config_file, sample_config):
        """Test config initialization with valid config file."""
        config = Config(str(config_file))
        
        assert config.config_path == config_file
        assert config._config is not None
        assert config.get_default_agent() == "test_agent"
    
    def test_config_init_with_nonexistent_file(self, temp_dir):
        """Test config initialization with non-existent config file."""
        nonexistent_file = temp_dir / "nonexistent.yaml"
        
        with pytest.raises(ConfigError, match="Configuration file .* not found"):
            Config(str(nonexistent_file))
    
    def test_config_init_with_invalid_yaml(self, temp_dir):
        """Test config initialization with invalid YAML."""
        invalid_yaml_file = temp_dir / "invalid.yaml"
        invalid_yaml_file.write_text("invalid: yaml: content: [")
        
        with pytest.raises(ConfigError, match="Invalid YAML format"):
            Config(str(invalid_yaml_file))
    
    def test_config_init_with_invalid_schema(self, temp_dir):
        """Test config initialization with invalid schema."""
        invalid_config_file = temp_dir / "invalid_config.yaml"
        # Use configuration that will definitely fail validation
        invalid_config = {
            "settings": {
                "max_history": -1  # Invalid: must be >= 1
            }
        }
        with open(invalid_config_file, 'w') as f:
            yaml.dump(invalid_config, f)
        
        with pytest.raises(ConfigError, match="Configuration validation failed"):
            Config(str(invalid_config_file))
    
    def test_working_directory_methods(self, config_file):
        """Test working directory related methods."""
        config = Config(str(config_file))
        
        # Test get_working_directory
        working_dir = config.get_working_directory()
        assert working_dir == "/tmp/test"
        
        # Test set_working_directory (when allowed)
        new_dir = "/tmp/new_test"
        config.set_working_directory(new_dir)
        assert config._working_directory == os.path.abspath(new_dir)
    
    def test_working_directory_override_disabled(self, config_file, sample_config):
        """Test working directory override when disabled."""
        # Modify config to disable path override
        sample_config["settings"]["allow_path_override"] = False
        modified_config_file = config_file.parent / "modified_config.yaml"
        with open(modified_config_file, 'w') as f:
            yaml.dump(sample_config, f)
        
        config = Config(str(modified_config_file))
        original_working_dir = config._working_directory
        
        with patch('core.config.logger') as mock_logger:
            config.set_working_directory("/new/path")
            mock_logger.warning.assert_called_with("Path override is disabled in configuration")
            assert config._working_directory == original_working_dir
    
    def test_get_absolute_path(self, config_file):
        """Test get_absolute_path method."""
        config = Config(str(config_file))
        
        # Test with relative path
        relative_path = "test/file.txt"
        absolute_path = config.get_absolute_path(relative_path)
        assert absolute_path == "/tmp/test/test/file.txt"
        
        # Test with already absolute path
        abs_path = "/already/absolute/path.txt"
        result = config.get_absolute_path(abs_path)
        assert result == abs_path
    
    def test_get_config_directory(self, config_file):
        """Test get_config_directory method."""
        config = Config(str(config_file))
        config_dir = config.get_config_directory()
        assert config_dir == "/tmp/config"
    
    def test_provider_methods(self, config_file):
        """Test provider-related methods."""
        config = Config(str(config_file))
        
        # Test get_provider
        provider = config.get_provider("openai")
        assert provider.name == "openai"
        assert provider.base_url == "https://api.openai.com/v1"
        
        # Test get_provider with invalid key
        with pytest.raises(ConfigError, match="Provider 'invalid' not found"):
            config.get_provider("invalid")
    
    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-api-key"})
    def test_get_api_key_from_env(self, config_file):
        """Test getting API key from environment variable."""
        config = Config(str(config_file))
        api_key = config.get_api_key("openai")
        assert api_key == "test-api-key"
    
    def test_get_api_key_missing(self, config_file):
        """Test getting API key when not available."""
        config = Config(str(config_file))
        
        with patch.dict(os.environ, {}, clear=True):
            with patch('core.config.logger') as mock_logger:
                api_key = config.get_api_key("openai")
                assert api_key is None
                mock_logger.warning.assert_called_with("No API key found for provider 'openai'")
    
    def test_model_methods(self, config_file):
        """Test model-related methods."""
        config = Config(str(config_file))
        
        # Test get_model
        model = config.get_model("gpt-4")
        assert model.name == "gpt-4"
        assert model.provider == "openai"
        assert model.temperature == 0.7
        
        # Test get_model with invalid key
        with pytest.raises(ConfigError, match="Model 'invalid' not found"):
            config.get_model("invalid")
    
    def test_agent_methods(self, config_file):
        """Test agent-related methods."""
        config = Config(str(config_file))
        
        # Test get_agent
        agent = config.get_agent("test_agent")
        assert agent.name == "Test Agent"
        assert agent.model == "gpt-4"
        assert "file_read" in agent.tools
        
        # Test get_agent with invalid key
        with pytest.raises(ConfigError, match="Agent 'invalid' not found"):
            config.get_agent("invalid")
        
        # Test get_default_agent
        default_agent = config.get_default_agent()
        assert default_agent == "test_agent"
        
        # Test list_agents
        agents = config.list_agents()
        assert "test_agent" in agents
        assert agents["test_agent"] == "Test agent for testing"
    
    def test_tool_methods(self, config_file):
        """Test tool-related methods."""
        config = Config(str(config_file))
        
        # Test get_tool
        tool = config.get_tool("file_read")
        assert tool.type == "function"
        assert tool.name == "file_read"
        
        # Test get_tool with invalid key
        with pytest.raises(ConfigError, match="Tool 'invalid' not found"):
            config.get_tool("invalid")
    
    def test_reload_config(self, config_file, sample_config):
        """Test config reloading."""
        config = Config(str(config_file))
        original_default_agent = config.get_default_agent()
        
        # Modify config file
        sample_config["settings"]["default_agent"] = "new_agent"
        sample_config["agents"]["new_agent"] = {
            "name": "New Agent",
            "model": "gpt-4",
            "tools": ["file_read"],
            "base_prompt": "test_prompt",
            "description": "New test agent"
        }
        with open(config_file, 'w') as f:
            yaml.dump(sample_config, f)
        
        # Reload and verify change
        config.reload()
        new_default_agent = config.get_default_agent()
        assert new_default_agent != original_default_agent
        assert new_default_agent == "new_agent"
    
    def test_settings_methods(self, config_file):
        """Test settings-related methods."""
        config = Config(str(config_file))
        
        # Test max_history
        assert config.get_max_history() == 10
        
        # Test max_turns
        assert config.get_max_turns() == 5
        
        # Test agent_timeout
        assert config.get_agent_timeout() == 30
        
        # Test mcp_enabled
        assert config.is_mcp_enabled() is False
    
    def test_prompt_template_methods(self, config_file, sample_config):
        """Test prompt template methods."""
        # Add prompt templates to config
        sample_config["prompt_templates"] = {
            "test_prompt": "This is a test prompt template."
        }
        
        modified_config_file = config_file.parent / "prompt_config.yaml"
        with open(modified_config_file, 'w') as f:
            yaml.dump(sample_config, f)
        
        config = Config(str(modified_config_file))
        
        # Test get_prompt_template
        template = config.get_prompt_template("test_prompt")
        assert template == "This is a test prompt template."
        
        # Test get_prompt_template with invalid key
        with pytest.raises(ConfigError, match="Prompt template 'invalid' not found"):
            config.get_prompt_template("invalid")
    
    def test_build_agent_prompt(self, config_file, sample_config):
        """Test building agent prompt."""
        # Add prompt templates and tool configurations
        sample_config["prompt_templates"] = {
            "test_prompt": "Base prompt for test agent."
        }
        sample_config["tools"]["file_read"]["prompt_addition"] = "Can read files."
        sample_config["tools"]["file_write"]["prompt_addition"] = "Can write files."
        
        modified_config_file = config_file.parent / "full_config.yaml"
        with open(modified_config_file, 'w') as f:
            yaml.dump(sample_config, f)
        
        config = Config(str(modified_config_file))
        
        prompt = config.build_agent_prompt("test_agent")
        assert "Base prompt for test agent." in prompt
        assert "Can read files." in prompt
        assert "Can write files." in prompt
        assert "Доступные инструменты:" in prompt
    
    def test_build_agent_prompt_with_custom_prompt(self, config_file, sample_config):
        """Test building agent prompt with custom prompt."""
        sample_config["agents"]["test_agent"]["custom_prompt"] = "Custom prompt for agent."
        
        modified_config_file = config_file.parent / "custom_config.yaml"
        with open(modified_config_file, 'w') as f:
            yaml.dump(sample_config, f)
        
        config = Config(str(modified_config_file))
        
        prompt = config.build_agent_prompt("test_agent")
        assert "Custom prompt for agent." in prompt
    
    def test_config_property_not_loaded(self):
        """Test accessing config property when not loaded."""
        # Create config instance without loading
        config = Config.__new__(Config)
        config._config = None
        
        with pytest.raises(ConfigError, match="Configuration not loaded"):
            _ = config.config
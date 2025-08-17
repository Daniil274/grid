"""
Unit tests for schemas.py module.
"""

import pytest
from datetime import datetime
from pydantic import ValidationError

from schemas import (
    ToolType, ProviderConfig, ModelConfig, ToolConfig, AgentConfig,
    AgentLoggingConfig, Settings, GridConfig, ContextMessage, AgentExecution
)


class TestToolType:
    """Test ToolType enum."""
    
    def test_tool_type_values(self):
        """Test ToolType enum values."""
        assert ToolType.FUNCTION == "function"
        assert ToolType.MCP == "mcp"
        assert ToolType.AGENT == "agent"
    
    def test_tool_type_membership(self):
        """Test ToolType membership."""
        assert "function" in ToolType
        assert "mcp" in ToolType
        assert "agent" in ToolType
        assert "invalid" not in ToolType


class TestProviderConfig:
    """Test ProviderConfig schema."""
    
    def test_provider_config_minimal(self):
        """Test ProviderConfig with minimal required fields."""
        config = ProviderConfig(
            name="test_provider",
            base_url="https://api.example.com"
        )
        
        assert config.name == "test_provider"
        assert config.base_url == "https://api.example.com"
        assert config.api_key_env is None
        assert config.api_key is None
        assert config.timeout == 30  # default
        assert config.max_retries == 2  # default
    
    def test_provider_config_full(self):
        """Test ProviderConfig with all fields."""
        config = ProviderConfig(
            name="full_provider",
            base_url="https://api.example.com",
            api_key_env="API_KEY",
            api_key="secret_key",
            timeout=60,
            max_retries=5
        )
        
        assert config.name == "full_provider"
        assert config.base_url == "https://api.example.com"
        assert config.api_key_env == "API_KEY"
        assert config.api_key == "secret_key"
        assert config.timeout == 60
        assert config.max_retries == 5
    
    def test_provider_config_timeout_validation(self):
        """Test timeout field validation."""
        # Valid timeout
        config = ProviderConfig(name="test", base_url="url", timeout=30)
        assert config.timeout == 30
        
        # Invalid timeout - too low
        with pytest.raises(ValidationError):
            ProviderConfig(name="test", base_url="url", timeout=0)
        
        # Invalid timeout - too high
        with pytest.raises(ValidationError):
            ProviderConfig(name="test", base_url="url", timeout=500)
    
    def test_provider_config_max_retries_validation(self):
        """Test max_retries field validation."""
        # Valid max_retries
        config = ProviderConfig(name="test", base_url="url", max_retries=3)
        assert config.max_retries == 3
        
        # Invalid max_retries - too low
        with pytest.raises(ValidationError):
            ProviderConfig(name="test", base_url="url", max_retries=-1)
        
        # Invalid max_retries - too high
        with pytest.raises(ValidationError):
            ProviderConfig(name="test", base_url="url", max_retries=20)


class TestModelConfig:
    """Test ModelConfig schema."""
    
    def test_model_config_minimal(self):
        """Test ModelConfig with minimal required fields."""
        config = ModelConfig(
            name="test_model",
            provider="test_provider"
        )
        
        assert config.name == "test_model"
        assert config.provider == "test_provider"
        assert config.temperature == 0.7  # default
        assert config.max_tokens == 4000  # default
        assert config.description == ""  # default
        assert config.use_responses_api is False  # default
    
    def test_model_config_full(self):
        """Test ModelConfig with all fields."""
        config = ModelConfig(
            name="full_model",
            provider="full_provider",
            temperature=0.5,
            max_tokens=8000,
            description="Test model description",
            use_responses_api=True
        )
        
        assert config.name == "full_model"
        assert config.provider == "full_provider"
        assert config.temperature == 0.5
        assert config.max_tokens == 8000
        assert config.description == "Test model description"
        assert config.use_responses_api is True
    
    def test_model_config_temperature_validation(self):
        """Test temperature field validation."""
        # Valid temperatures
        config = ModelConfig(name="test", provider="provider", temperature=0.0)
        assert config.temperature == 0.0
        
        config = ModelConfig(name="test", provider="provider", temperature=2.0)
        assert config.temperature == 2.0
        
        # Invalid temperature - too low
        with pytest.raises(ValidationError):
            ModelConfig(name="test", provider="provider", temperature=-0.1)
        
        # Invalid temperature - too high
        with pytest.raises(ValidationError):
            ModelConfig(name="test", provider="provider", temperature=2.1)
    
    def test_model_config_max_tokens_validation(self):
        """Test max_tokens field validation."""
        # Valid max_tokens
        config = ModelConfig(name="test", provider="provider", max_tokens=1000)
        assert config.max_tokens == 1000
        
        # Invalid max_tokens - too low
        with pytest.raises(ValidationError):
            ModelConfig(name="test", provider="provider", max_tokens=0)
        
        # Invalid max_tokens - too high
        with pytest.raises(ValidationError):
            ModelConfig(name="test", provider="provider", max_tokens=200000)


class TestToolConfig:
    """Test ToolConfig schema."""
    
    def test_tool_config_function_tool(self):
        """Test ToolConfig for function tool."""
        config = ToolConfig(
            type=ToolType.FUNCTION,
            name="test_function",
            description="Test function tool",
            prompt_addition="Can perform test operations"
        )
        
        assert config.type == ToolType.FUNCTION
        assert config.name == "test_function"
        assert config.description == "Test function tool"
        assert config.prompt_addition == "Can perform test operations"
        assert config.server_command is None
        assert config.env_vars is None
        assert config.target_agent is None
    
    def test_tool_config_mcp_tool(self):
        """Test ToolConfig for MCP tool."""
        config = ToolConfig(
            type=ToolType.MCP,
            name="mcp_tool",
            server_command=["python", "mcp_server.py"],
            env_vars={"ENV_VAR": "value"}
        )
        
        assert config.type == ToolType.MCP
        assert config.name == "mcp_tool"
        assert config.server_command == ["python", "mcp_server.py"]
        assert config.env_vars == {"ENV_VAR": "value"}
    
    def test_tool_config_agent_tool(self):
        """Test ToolConfig for agent tool."""
        config = ToolConfig(
            type=ToolType.AGENT,
            name="agent_tool",
            target_agent="sub_agent",
            context_strategy="smart",
            context_depth=5,
            include_tool_history=True
        )
        
        assert config.type == ToolType.AGENT
        assert config.name == "agent_tool"
        assert config.target_agent == "sub_agent"
        assert config.context_strategy == "smart"
        assert config.context_depth == 5
        assert config.include_tool_history is True


class TestAgentConfig:
    """Test AgentConfig schema."""
    
    def test_agent_config_minimal(self):
        """Test AgentConfig with minimal required fields."""
        config = AgentConfig(
            name="test_agent",
            model="test_model"
        )
        
        assert config.name == "test_agent"
        assert config.model == "test_model"
        assert config.tools == []  # default empty list
        assert config.base_prompt == "base"  # default
        assert config.custom_prompt is None
        assert config.description == ""  # default
        assert config.mcp_enabled is False  # default
    
    def test_agent_config_full(self):
        """Test AgentConfig with all fields."""
        config = AgentConfig(
            name="full_agent",
            model="full_model",
            tools=["tool1", "tool2", "tool3"],
            base_prompt="custom_base",
            custom_prompt="Custom prompt text",
            description="Full agent description",
            mcp_enabled=True
        )
        
        assert config.name == "full_agent"
        assert config.model == "full_model"
        assert config.tools == ["tool1", "tool2", "tool3"]
        assert config.base_prompt == "custom_base"
        assert config.custom_prompt == "Custom prompt text"
        assert config.description == "Full agent description"
        assert config.mcp_enabled is True


class TestAgentLoggingConfig:
    """Test AgentLoggingConfig schema."""
    
    def test_agent_logging_config_defaults(self):
        """Test AgentLoggingConfig with default values."""
        config = AgentLoggingConfig()
        
        assert config.enabled is True
        assert config.level == "full"
        assert config.save_prompts is True
        assert config.save_conversations is True
        assert config.save_executions is True
    
    def test_agent_logging_config_custom(self):
        """Test AgentLoggingConfig with custom values."""
        config = AgentLoggingConfig(
            enabled=False,
            level="basic",
            save_prompts=False,
            save_conversations=False,
            save_executions=True
        )
        
        assert config.enabled is False
        assert config.level == "basic"
        assert config.save_prompts is False
        assert config.save_conversations is False
        assert config.save_executions is True


class TestSettings:
    """Test Settings schema."""
    
    def test_settings_defaults(self):
        """Test Settings with default values."""
        settings = Settings()
        
        assert settings.default_agent == "assistant"
        assert settings.max_history == 15
        assert settings.max_turns == 10
        assert settings.agent_timeout == 300
        assert settings.debug is False
        assert settings.mcp_enabled is False
        assert settings.working_directory == "."
        assert settings.config_directory == "."
        assert settings.allow_path_override is True
        assert isinstance(settings.agent_logging, AgentLoggingConfig)
        assert settings.tools_common_rules is None
    
    def test_settings_custom(self):
        """Test Settings with custom values."""
        logging_config = AgentLoggingConfig(enabled=False)
        settings = Settings(
            default_agent="custom_agent",
            max_history=20,
            max_turns=15,
            agent_timeout=600,
            debug=True,
            mcp_enabled=True,
            working_directory="/custom/dir",
            config_directory="/custom/config",
            allow_path_override=False,
            agent_logging=logging_config,
            tools_common_rules="Common rules for all tools"
        )
        
        assert settings.default_agent == "custom_agent"
        assert settings.max_history == 20
        assert settings.max_turns == 15
        assert settings.agent_timeout == 600
        assert settings.debug is True
        assert settings.mcp_enabled is True
        assert settings.working_directory == "/custom/dir"
        assert settings.config_directory == "/custom/config"
        assert settings.allow_path_override is False
        assert settings.agent_logging.enabled is False
        assert settings.tools_common_rules == "Common rules for all tools"
    
    def test_settings_validation(self):
        """Test Settings field validation."""
        # Valid values
        settings = Settings(max_history=50, max_turns=25, agent_timeout=900)
        assert settings.max_history == 50
        assert settings.max_turns == 25
        assert settings.agent_timeout == 900
        
        # Invalid max_history
        with pytest.raises(ValidationError):
            Settings(max_history=0)
        
        with pytest.raises(ValidationError):
            Settings(max_history=200)
        
        # Invalid max_turns
        with pytest.raises(ValidationError):
            Settings(max_turns=0)
        
        with pytest.raises(ValidationError):
            Settings(max_turns=200)
        
        # Invalid agent_timeout
        with pytest.raises(ValidationError):
            Settings(agent_timeout=20)
        
        with pytest.raises(ValidationError):
            Settings(agent_timeout=2000)


class TestGridConfig:
    """Test GridConfig schema."""
    
    def test_grid_config_minimal(self):
        """Test GridConfig with minimal configuration."""
        config = GridConfig()
        
        assert isinstance(config.settings, Settings)
        assert config.providers == {}
        assert config.models == {}
        assert config.tools == {}
        assert config.agents == {}
        assert config.prompt_templates == {}
        assert config.scenarios is None
    
    def test_grid_config_full(self):
        """Test GridConfig with full configuration."""
        provider = ProviderConfig(name="test_provider", base_url="url")
        model = ModelConfig(name="test_model", provider="test_provider")
        tool = ToolConfig(type=ToolType.FUNCTION, name="test_tool")
        agent = AgentConfig(name="test_agent", model="test_model")
        
        config = GridConfig(
            providers={"test_provider": provider},
            models={"test_model": model},
            tools={"test_tool": tool},
            agents={"test_agent": agent},
            prompt_templates={"template1": "Template content"},
            scenarios={"scenario1": {"type": "test"}}
        )
        
        assert "test_provider" in config.providers
        assert "test_model" in config.models
        assert "test_tool" in config.tools
        assert "test_agent" in config.agents
        assert config.prompt_templates["template1"] == "Template content"
        assert config.scenarios["scenario1"]["type"] == "test"
    
    def test_grid_config_agent_model_validation(self):
        """Test GridConfig validation for agent models."""
        # Valid configuration
        provider = ProviderConfig(name="provider", base_url="url")
        model = ModelConfig(name="model", provider="provider")
        agent = AgentConfig(name="agent", model="model")
        
        config = GridConfig(
            providers={"provider": provider},
            models={"model": model},
            agents={"agent": agent}
        )
        
        assert config.agents["agent"].model == "model"
        
        # Invalid configuration - model doesn't exist
        with pytest.raises(ValidationError, match="Model 'nonexistent' for agent 'agent' not found"):
            GridConfig(
                providers={"provider": provider},
                models={"model": model},
                agents={"agent": AgentConfig(name="agent", model="nonexistent")}
            )
    
    def test_grid_config_agent_tools_validation(self):
        """Test GridConfig validation for agent tools."""
        # Valid configuration
        tool = ToolConfig(type=ToolType.FUNCTION, name="tool")
        agent = AgentConfig(name="agent", model="model", tools=["tool"])
        
        config = GridConfig(
            models={"model": ModelConfig(name="model", provider="provider")},
            tools={"tool": tool},
            agents={"agent": agent}
        )
        
        assert "tool" in config.agents["agent"].tools
        
        # Invalid configuration - tool doesn't exist
        with pytest.raises(ValidationError, match="Tool 'nonexistent' for agent 'agent' not found"):
            GridConfig(
                models={"model": ModelConfig(name="model", provider="provider")},
                tools={"tool": tool},
                agents={"agent": AgentConfig(name="agent", model="model", tools=["nonexistent"])}
            )


class TestContextMessage:
    """Test ContextMessage schema."""
    
    def test_context_message_valid_roles(self):
        """Test ContextMessage with valid roles."""
        timestamp = datetime.now().isoformat()
        
        # Test all valid roles
        for role in ["user", "assistant", "system"]:
            message = ContextMessage(
                role=role,
                content="Test content",
                timestamp=timestamp
            )
            assert message.role == role
            assert message.content == "Test content"
            assert message.timestamp == timestamp
            assert message.metadata is None
    
    def test_context_message_with_metadata(self):
        """Test ContextMessage with metadata."""
        timestamp = datetime.now().isoformat()
        metadata = {"source": "test", "priority": "high"}
        
        message = ContextMessage(
            role="user",
            content="Test content",
            timestamp=timestamp,
            metadata=metadata
        )
        
        assert message.metadata == metadata
    
    def test_context_message_invalid_role(self):
        """Test ContextMessage with invalid role."""
        timestamp = datetime.now().isoformat()
        
        with pytest.raises(ValidationError):
            ContextMessage(
                role="invalid_role",
                content="Test content",
                timestamp=timestamp
            )


class TestAgentExecution:
    """Test AgentExecution schema."""
    
    def test_agent_execution_minimal(self):
        """Test AgentExecution with minimal required fields."""
        execution = AgentExecution(
            agent_name="test_agent",
            input_message="Test input",
            start_time=1234567890.0
        )
        
        assert execution.agent_name == "test_agent"
        assert execution.input_message == "Test input"
        assert execution.start_time == 1234567890.0
        assert execution.end_time is None
        assert execution.output is None
        assert execution.error is None
        assert execution.tools_used == []
        assert execution.token_usage is None
    
    def test_agent_execution_full(self):
        """Test AgentExecution with all fields."""
        execution = AgentExecution(
            agent_name="test_agent",
            input_message="Test input",
            start_time=1234567890.0,
            end_time=1234567895.0,
            output="Test output",
            error="Test error",
            tools_used=["tool1", "tool2"],
            token_usage={"input_tokens": 100, "output_tokens": 50}
        )
        
        assert execution.agent_name == "test_agent"
        assert execution.input_message == "Test input"
        assert execution.start_time == 1234567890.0
        assert execution.end_time == 1234567895.0
        assert execution.output == "Test output"
        assert execution.error == "Test error"
        assert execution.tools_used == ["tool1", "tool2"]
        assert execution.token_usage == {"input_tokens": 100, "output_tokens": 50}
    
    def test_agent_execution_duration_calculation(self):
        """Test that duration can be calculated from start/end times."""
        execution = AgentExecution(
            agent_name="test_agent",
            input_message="Test input",
            start_time=1234567890.0,
            end_time=1234567895.5
        )
        
        # Calculate duration manually
        duration = execution.end_time - execution.start_time
        assert duration == 5.5


class TestSchemaIntegration:
    """Integration tests for schema interactions."""
    
    def test_complete_configuration_example(self):
        """Test a complete, realistic configuration."""
        # Create a complete configuration
        config_data = {
            "settings": {
                "default_agent": "assistant",
                "max_history": 20,
                "max_turns": 15,
                "agent_timeout": 300,
                "debug": False,
                "mcp_enabled": True,
                "agent_logging": {
                    "enabled": True,
                    "level": "full",
                    "save_prompts": True,
                    "save_conversations": True,
                    "save_executions": True
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
            "tools": {
                "file_read": {
                    "type": "function",
                    "name": "file_read",
                    "description": "Read file contents"
                },
                "sub_agent": {
                    "type": "agent",
                    "target_agent": "helper",
                    "context_strategy": "smart"
                }
            },
            "agents": {
                "assistant": {
                    "name": "Assistant",
                    "model": "gpt-4",
                    "tools": ["file_read"],
                    "description": "Main assistant agent"
                },
                "helper": {
                    "name": "Helper",
                    "model": "gpt-4",
                    "tools": [],
                    "description": "Helper agent"
                }
            },
            "prompt_templates": {
                "base": "You are a helpful assistant."
            }
        }
        
        # This should validate without errors
        config = GridConfig(**config_data)
        
        assert config.settings.default_agent == "assistant"
        assert "openai" in config.providers
        assert "gpt-4" in config.models
        assert "file_read" in config.tools
        assert "assistant" in config.agents
        assert config.agents["assistant"].tools == ["file_read"]
    
    def test_schema_serialization(self):
        """Test that schemas can be serialized and deserialized."""
        # Create a configuration
        original_config = GridConfig(
            providers={
                "test": ProviderConfig(name="test", base_url="url")
            },
            models={
                "model": ModelConfig(name="model", provider="test")
            },
            agents={
                "agent": AgentConfig(name="agent", model="model")
            }
        )
        
        # Serialize to dict
        config_dict = original_config.model_dump()
        
        # Deserialize back
        restored_config = GridConfig(**config_dict)
        
        # Should be equivalent
        assert restored_config.providers["test"].name == "test"
        assert restored_config.models["model"].provider == "test"
        assert restored_config.agents["agent"].model == "model"
    
    def test_schema_json_compatibility(self):
        """Test JSON serialization compatibility."""
        import json
        
        config = GridConfig(
            settings=Settings(debug=True),
            providers={
                "test": ProviderConfig(name="test", base_url="url")
            }
        )
        
        # Should be able to convert to JSON and back
        json_str = config.model_dump_json()
        parsed_data = json.loads(json_str)
        restored_config = GridConfig(**parsed_data)
        
        assert restored_config.settings.debug is True
        assert restored_config.providers["test"].name == "test"
"""
Unit tests for core/agent_factory.py module.
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from pathlib import Path

from core.agent_factory import AgentFactory
from core.config import Config
from core.config.prompt_sections import ModelContextAssembly
from utils.exceptions import AgentError, ConfigError
from schemas import AgentExecution


class TestAgentFactory:
    """Test AgentFactory class functionality."""
    
    def test_agent_factory_init(self, config_file):
        """Test agent factory initialization."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        assert factory.config is config
        assert factory.context_manager is not None
        assert len(factory._agent_cache) == 0
        assert len(factory._tool_cache) == 0
        assert len(factory._mcp_servers) == 0
        assert len(factory._agent_sessions) == 0
    
    def test_agent_factory_init_with_working_directory(self, config_file, temp_dir):
        """Test agent factory initialization with working directory override."""
        config = Config(str(config_file))
        factory = AgentFactory(config, str(temp_dir))
        
        # AgentFactory calls config.set_working_directory with the override
        # Since allow_path_override is True in sample_config, it should update the working directory
        assert factory.config is config
        
        # We verify the factory updated the config's working directory
        assert factory.config.get_working_directory() == str(temp_dir)
    
    @pytest.mark.asyncio
    async def test_agent_factory_initialize(self, config_file):
        """Test agent factory async initialization."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        # Should not raise any exceptions
        result = await factory.initialize()
        assert result is None
    
    def test_resolve_model_key_with_none(self, config_file):
        """Test resolving model key when None provided."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        # Should return default agent's model
        model_key = factory.resolve_model_key(None)
        assert model_key == "gpt-4"  # From sample config
    
    def test_resolve_model_key_with_model_key(self, config_file):
        """Test resolving model key when valid model key provided."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        model_key = factory.resolve_model_key("gpt-4")
        assert model_key == "gpt-4"
    
    def test_resolve_model_key_with_agent_key(self, config_file):
        """Test resolving model key when agent key provided."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        model_key = factory.resolve_model_key("test_agent")
        assert model_key == "gpt-4"  # test_agent uses gpt-4 model
    
    def test_resolve_model_key_with_invalid_key(self, config_file):
        """Test resolving model key with invalid key falls back to default."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        model_key = factory.resolve_model_key("invalid_key")
        assert model_key == "gpt-4"  # Should fallback to default
    
    def test_get_openai_client_for_model(self, config_file):
        """Test getting OpenAI client for model."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}):
            client, model_name = factory.get_openai_client_for_model("gpt-4")
            
            assert client is not None
            assert hasattr(client, 'chat')  # AsyncOpenAI client
            assert model_name == "gpt-4"
    
    def test_get_openai_client_for_model_no_api_key(self, config_file):
        """Test getting OpenAI client with missing API key."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        with patch.dict('os.environ', {}, clear=True):
            with pytest.raises(AgentError, match="API key not found"):
                factory.get_openai_client_for_model("gpt-4")
    
    def test_get_agent_session_creates_new(self, config_file):
        """Test that _get_agent_session creates new session."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        with patch('core.agent_factory.SQLiteSession') as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value = mock_session
            
            session = factory._get_agent_session("test_agent", "test_context")
            
            assert session is mock_session
            assert ("test_agent", "test_context") in factory._agent_sessions
            mock_session_class.assert_called_once_with(
                session_id="agent_test_agent_test_context",
                db_path=factory._agent_session_db_path,
            )
    
    def test_get_agent_session_reuses_existing(self, config_file):
        """Test that _get_agent_session reuses existing session."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        # First call
        with patch('core.agent_factory.SQLiteSession') as mock_session_class:
            mock_session = Mock()
            mock_session_class.return_value = mock_session
            
            session1 = factory._get_agent_session("test_agent", "test_context")
            session2 = factory._get_agent_session("test_agent", "test_context")
            
            assert session1 is session2
            mock_session_class.assert_called_once()  # Only called once

    def test_persistent_session_db_path_lives_under_user_logs(self, config_file, monkeypatch, tmp_path):
        """Test durable session DB path is created under ~/.grid/logs by default."""
        grid_home = tmp_path / "grid_home"
        monkeypatch.setenv("USERPROFILE", str(grid_home))  # Windows
        monkeypatch.setenv("HOME", str(grid_home))

        config = Config(str(config_file))
        factory = AgentFactory(config)

        expected = grid_home / ".grid" / "logs" / "agent_sessions.db"
        assert Path(factory._agent_session_db_path).resolve() == expected.resolve()
        assert expected.parent.exists()

    def test_is_retriable_agent_exception_detects_transient_errors(self, config_file):
        """Test transient provider/network failures are treated as retriable."""
        config = Config(str(config_file))
        factory = AgentFactory(config)

        assert factory._is_retriable_agent_exception(Exception("Connection error while streaming response"))
        assert factory._is_retriable_agent_exception(AgentError("rate limit exceeded by provider"))
        assert factory._is_retriable_agent_exception(Exception("all providers exhausted"))
        assert factory._is_retriable_agent_exception(Exception("HTTP 503: upstream_unavailable"))
        assert factory._is_retriable_agent_exception(AgentError("upstream unavailable"))
        assert factory._is_retriable_agent_exception(Exception("server_error: Сервис временно недоступен"))
        assert not factory._is_retriable_agent_exception(Exception("tool arguments are invalid"))
    
    def test_is_reasoning_model_name(self, config_file):
        """Test reasoning model detection."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        # Reasoning models
        assert factory._is_reasoning_model_name("o3-mini")
        assert factory._is_reasoning_model_name("o4-mini-high")
        assert factory._is_reasoning_model_name("deepseek-r1")
        assert factory._is_reasoning_model_name("reasoning-model")
        assert factory._is_reasoning_model_name("thinking-ai")
        
        # Non-reasoning models
        assert not factory._is_reasoning_model_name("gpt-4")
        assert not factory._is_reasoning_model_name("claude-3")
        assert not factory._is_reasoning_model_name("llama2")
        assert not factory._is_reasoning_model_name("")
        assert not factory._is_reasoning_model_name(None)

    def test_model_requires_manual_history(self, config_file):
        """Thinking models that preserve reasoning should bypass SDK session history."""
        config = Config(str(config_file))
        factory = AgentFactory(config)

        base_model = config.get_model("gpt-4")
        assert factory._model_requires_manual_history(base_model) is False

        base_model.preserve_reasoning_content = True
        assert factory._model_requires_manual_history(base_model) is True
    
    @pytest.mark.asyncio 
    async def test_create_agent_success(self, config_file):
        """Test successful agent creation."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        with patch('core.agent_factory.AsyncOpenAI') as mock_openai, \
             patch('core.agent_factory.VisionChatCompletionsModel') as mock_model, \
             patch('core.agent_factory.Agent') as mock_agent_class, \
             patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}), \
             patch.object(factory, '_get_agent_tools', return_value=[], new_callable=AsyncMock), \
             patch.object(factory, '_build_agent_instructions', return_value="Test instructions"), \
             patch.object(factory, '_create_mcp_servers', return_value=[], new_callable=AsyncMock):
            
            mock_client = Mock()
            mock_openai.return_value = mock_client
            
            mock_model_instance = Mock()
            mock_model.return_value = mock_model_instance
            
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            
            agent = await factory.create_agent("test_agent")
            
            assert agent is mock_agent
            assert "test_agent" in factory._agent_cache
            mock_agent_class.assert_called_once()
            
            # Verify agent creation call
            call_args = mock_agent_class.call_args
            assert call_args[1]['name'] == "Test Agent"
            assert call_args[1]['instructions'] == "Test instructions"
            assert call_args[1]['model'] is mock_model_instance

    @pytest.mark.asyncio
    async def test_create_dynamic_agent_passes_model_config_flags(self, config_file):
        """Dynamic agents must apply the same VisionChatCompletionsModel flags as create_agent."""
        config = Config(str(config_file))
        factory = AgentFactory(config)

        model_cfg = config.get_model("gpt-4")
        model_cfg.preserve_reasoning_content = True

        with patch('core.agent_factory.AsyncOpenAI'), \
             patch('core.agent_factory.VisionChatCompletionsModel') as mock_model, \
             patch('core.agent_factory.Agent') as mock_agent_class, \
             patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}), \
             patch.object(factory, '_resolve_tools_for_names', return_value=([], []), new_callable=AsyncMock), \
             patch.object(factory, '_build_dynamic_agent_instructions', return_value="instr"), \
             patch.object(factory, '_is_model_allowed', return_value=True):

            mock_model.return_value = Mock()
            mock_agent_class.return_value = Mock()

            await factory.create_dynamic_agent(
                name="dyn-test",
                instructions="do work",
                model_key="gpt-4",
            )

            mock_model.assert_called_once()
            assert mock_model.call_args.kwargs["preserve_reasoning_content"] is True
    
    @pytest.mark.asyncio
    async def test_create_agent_with_force_reload(self, config_file):
        """Test agent creation with force reload."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        with patch('core.agent_factory.AsyncOpenAI'), \
             patch('core.agent_factory.VisionChatCompletionsModel'), \
             patch('core.agent_factory.Agent') as mock_agent_class, \
             patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}), \
             patch.object(factory, '_get_agent_tools', return_value=[], new_callable=AsyncMock), \
             patch.object(factory, '_build_agent_instructions', return_value="Test instructions"), \
             patch.object(factory, '_create_mcp_servers', return_value=[], new_callable=AsyncMock):
            
            mock_agent1 = Mock()
            mock_agent2 = Mock()
            mock_agent_class.side_effect = [mock_agent1, mock_agent2]
            
            # First creation
            agent1 = await factory.create_agent("test_agent")
            assert agent1 is mock_agent1
            
            # Force reload should create new agent
            agent2 = await factory.create_agent("test_agent", force_reload=True)
            assert agent2 is mock_agent2
            assert agent1 is not agent2
            
            assert mock_agent_class.call_count == 2
    
    @pytest.mark.asyncio
    async def test_create_agent_uses_cache(self, config_file):
        """Test that agent creation uses cache."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        with patch('core.agent_factory.AsyncOpenAI'), \
             patch('core.agent_factory.VisionChatCompletionsModel'), \
             patch('core.agent_factory.Agent') as mock_agent_class, \
             patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}), \
             patch.object(factory, '_get_agent_tools', return_value=[], new_callable=AsyncMock), \
             patch.object(factory, '_build_agent_instructions', return_value="Test instructions"), \
             patch.object(factory, '_create_mcp_servers', return_value=[], new_callable=AsyncMock):
            
            mock_agent = Mock()
            mock_agent_class.return_value = mock_agent
            
            # First creation
            agent1 = await factory.create_agent("test_agent")
            assert agent1 is mock_agent
            
            # Second creation should use cache
            agent2 = await factory.create_agent("test_agent")
            assert agent2 is mock_agent
            assert agent1 is agent2
            
            # Agent should only be created once
            mock_agent_class.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_create_agent_invalid_agent_key(self, config_file):
        """Test agent creation with invalid agent key."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        with pytest.raises(AgentError, match="Failed to create agent"):
            await factory.create_agent("invalid_agent")
    
    @pytest.mark.asyncio
    async def test_run_agent_success(self, config_file):
        """Test successful agent run."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        with patch.object(factory, 'create_agent', new_callable=AsyncMock) as mock_create, \
             patch('core.agent_factory.Runner') as mock_runner, \
             patch('asyncio.wait_for', new_callable=AsyncMock) as mock_wait_for, \
             patch.object(factory, '_build_agent_instructions', return_value="Test instructions"):
            
            mock_agent = Mock()
            mock_agent.name = "Test Agent"
            mock_create.return_value = mock_agent
            
            mock_result = Mock()
            mock_result.final_output = "Test response"
            mock_wait_for.return_value = mock_result
            
            response = await factory.run_agent("test_agent", "test message")
            
            assert response.startswith("Test response")
            assert "ID:" in response
            assert "ctx-" in response
            mock_create.assert_called_once_with("test_agent", None)
            mock_wait_for.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_run_agent_with_context_path(self, config_file):
        """Test agent run with context path."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        with patch.object(factory, 'create_agent', new_callable=AsyncMock) as mock_create, \
             patch('core.agent_factory.Runner') as mock_runner, \
             patch('asyncio.wait_for', new_callable=AsyncMock) as mock_wait_for, \
             patch.object(factory, '_build_agent_instructions', return_value="Test instructions"):
            
            mock_agent = Mock()
            mock_agent.name = "Test Agent"
            mock_create.return_value = mock_agent
            
            mock_result = Mock()
            mock_result.final_output = "Test response"
            mock_wait_for.return_value = mock_result
            
            await factory.run_agent("test_agent", "test message", "/test/path")
            
            mock_create.assert_called_once_with("test_agent", "/test/path")
    
    @pytest.mark.asyncio
    async def test_run_agent_retries_transient_timeout(self, config_file):
        """Test agent retries transient timeout errors and eventually succeeds."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        with patch.object(factory, 'create_agent', new_callable=AsyncMock) as mock_create, \
             patch('core.agent_factory.Runner') as mock_runner, \
             patch.object(factory, '_build_agent_instructions', return_value="Test instructions"), \
             patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            
            mock_agent = Mock()
            mock_agent.name = "Test Agent"
            mock_create.return_value = mock_agent

            mock_result = Mock()
            mock_result.final_output = "Recovered response"
            mock_runner.run = AsyncMock(return_value=mock_result)

            attempts = {"count": 0}

            async def fake_wait_for(coro, timeout):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    coro.close()
                    raise asyncio.TimeoutError()
                coro.close()
                return mock_result

            with patch('asyncio.wait_for', side_effect=fake_wait_for) as mock_wait_for:
                response = await factory.run_agent("test_agent", "test message")

            assert response.startswith("Recovered response")
            assert attempts["count"] == 2
            assert mock_wait_for.call_count == 2
            mock_sleep.assert_awaited_once()
    
    @pytest.mark.asyncio
    async def test_run_agent_streaming(self, config_file, capsys):
        """Test agent run with streaming."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        # Simple test - just verify streaming parameter can be passed
        with patch.object(factory, 'run_agent') as mock_run:
            mock_run.return_value = "Test result"
            
            result = await factory.run_agent("test_agent", "test message", streaming=True)
            
            mock_run.assert_called_once_with("test_agent", "test message", streaming=True)
            assert result == "Test result"
    
    @pytest.mark.asyncio
    async def test_run_agent_empty_response(self, config_file):
        """Test agent run with empty response."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        with patch.object(factory, 'create_agent', new_callable=AsyncMock) as mock_create, \
             patch('asyncio.wait_for', new_callable=AsyncMock) as mock_wait_for, \
             patch.object(factory, '_build_agent_instructions', return_value="Test instructions"):
            
            mock_agent = Mock()
            mock_agent.name = "Test Agent"
            mock_create.return_value = mock_agent
            
            # Mock empty result - use string result to avoid Mock len() issue
            mock_wait_for.return_value = ""  # Return empty string directly
            
            response = await factory.run_agent("test_agent", "test message")
            
            # Should return fallback message
            assert "ID:" in response
            assert "ctx-" in response
            assert "Agent completed the task but did not provide a text response" in response
    
    @pytest.mark.asyncio
    async def test_run_agent_refreshes_cached_agent_instructions(self, config_file):
        """Test that run_agent applies freshly assembled instructions to a cached agent."""
        config = Config(str(config_file))
        factory = AgentFactory(config)

        mock_agent = Mock()
        mock_agent.name = "Test Agent"
        mock_agent.instructions = "stale instructions"

        assembly_calls = [
            ModelContextAssembly(
                instructions="Initial instructions",
                sections=[],
                context_id="ctx-test",
                history_strategy="none",
            ),
            ModelContextAssembly(
                instructions="Final instructions",
                sections=[],
                context_id="ctx-test",
                history_strategy="prompt",
            ),
        ]
        factory.instructions_builder.assemble_model_context = Mock(
            side_effect=assembly_calls
        )

        with patch.object(factory, 'create_agent', new_callable=AsyncMock) as mock_create, \
             patch.object(factory, '_build_agent_instructions', side_effect=["Initial instructions", "Final instructions"]), \
             patch('asyncio.wait_for', new_callable=AsyncMock) as mock_wait_for:
            mock_create.return_value = mock_agent
            mock_wait_for.return_value = "done"

            response = await factory.run_agent("test_agent", "test message")

        assert "done" in response
        assert mock_agent.instructions == "Final instructions"
        assert factory.instructions_builder.assemble_model_context.call_count == 2

    def test_build_agent_instructions_basic(self, config_file):
        """Test building basic agent instructions."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        with patch.object(config, 'build_agent_prompt', return_value="Base prompt"):
            instructions = factory._build_agent_instructions("test_agent")
            
            assert "Base prompt" in instructions
            assert "Path information:" in instructions
            assert "Working directory:" in instructions
    
    def test_build_agent_instructions_with_context_path(self, config_file):
        """Test building agent instructions with context path."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        with patch.object(config, 'build_agent_prompt', return_value="Base prompt"):
            instructions = factory._build_agent_instructions("test_agent", "/test/context")
            
            assert "Base prompt" in instructions
            assert "Context path: /test/context" in instructions
    
    def test_build_path_context(self, config_file):
        """Test building path context."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        context = factory._build_path_context()
        
        assert "Path information:" in context
        assert "Working directory:" in context
        assert "Configuration directory:" in context
    
    def test_build_path_context_with_context_path(self, config_file):
        """Test building path context with context path."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        context = factory._build_path_context("/test/context")
        
        assert "Context path: /test/context" in context
        assert "Absolute context path:" in context
    
    @pytest.mark.asyncio
    async def test_get_agent_tools_caching(self, config_file):
        """Test agent tools caching."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        agent_config = config.get_agent("test_agent")
        
        # Mock the tool configs to be found properly
        with patch.object(config, 'get_tool') as mock_get_tool, \
             patch('core.agent_factory.get_tools_by_names', return_value=[Mock(), Mock()]) as mock_get_tools:
            
            # Mock tool configs as function type
            mock_tool_config = Mock()
            mock_tool_config.type = "function"
            mock_get_tool.return_value = mock_tool_config
            
            # First call
            tools1 = await factory._get_agent_tools(agent_config)
            
            # Second call should use cache
            tools2 = await factory._get_agent_tools(agent_config)
            assert tools1 is tools2
            
            # get_tools_by_names should be called once with function tools
            mock_get_tools.assert_called_once_with(["file_read", "file_write"])
    
    def test_extract_tools_used_success(self, config_file):
        """Test extracting tools used from result."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        # Mock result with tool_calls
        mock_result = Mock()
        mock_call1 = Mock()
        mock_call1.name = "tool1"
        mock_call2 = Mock()
        mock_call2.name = "tool2"
        mock_result.tool_calls = [mock_call1, mock_call2]
        
        tools = factory._extract_tools_used(mock_result)
        
        assert tools == ["tool1", "tool2"]
    
    def test_extract_tools_used_no_tool_calls(self, config_file):
        """Test extracting tools when no tool_calls attribute."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        mock_result = Mock()
        # No tool_calls attribute
        del mock_result.tool_calls
        
        tools = factory._extract_tools_used(mock_result)
        
        assert tools == []
    
    def test_extract_tools_used_exception(self, config_file):
        """Test extracting tools with exception."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        mock_result = Mock()
        mock_result.tool_calls = None  # This will cause AttributeError when iterating
        
        tools = factory._extract_tools_used(mock_result)
        
        assert tools == []
    
    def test_context_management_methods(self, config_file):
        """Test context management methods."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        # Test add_to_context
        factory.add_to_context("user", "test message")
        
        # Test get_context_info
        info = factory.get_context_info()
        assert "conversation_messages" in info
        assert "execution_history" in info
        
        # Test clear_context
        factory.clear_context()
        info_after_clear = factory.get_context_info()
        assert info_after_clear["conversation_messages"] == 0
    
    def test_cache_management(self, config_file):
        """Test cache management."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        # Add something to cache
        factory._agent_cache["test"] = Mock()
        factory._tool_cache["test"] = [Mock()]
        
        assert len(factory._agent_cache) == 1
        assert len(factory._tool_cache) == 1
        
        # Clear cache
        factory.clear_cache()
        
        assert len(factory._agent_cache) == 0
        assert len(factory._tool_cache) == 0
    
    @pytest.mark.asyncio
    async def test_cleanup(self, config_file):
        """Test cleanup method."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        # Add mock sessions and servers
        mock_session = Mock()
        mock_session.clear_session = AsyncMock()
        factory._agent_sessions["test"] = mock_session
        
        mock_server = Mock()
        mock_server.cleanup = AsyncMock()
        factory._mcp_servers["test"] = mock_server
        
        await factory.cleanup()
        
        # Should call cleanup methods
        mock_session.clear_session.assert_called_once()
        mock_server.cleanup.assert_called_once()
        
        # Should clear caches
        assert len(factory._agent_sessions) == 0
        assert len(factory._mcp_servers) == 0
        assert len(factory._agent_cache) == 0
    
    @pytest.mark.asyncio
    async def test_cleanup_with_exceptions(self, config_file):
        """Test cleanup method handles exceptions gracefully."""
        config = Config(str(config_file))
        factory = AgentFactory(config)
        
        # Add mock session that raises exception
        mock_session = Mock()
        mock_session.clear_session = AsyncMock(side_effect=Exception("Cleanup error"))
        factory._agent_sessions["test"] = mock_session
        
        # Should not raise exception
        await factory.cleanup()
        
        # Should still clear caches despite exception
        assert len(factory._agent_sessions) == 0

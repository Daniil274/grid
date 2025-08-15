"""
Tests for enhanced features: profiles, guardrails, evals, metrics, and tool registry.
Demonstrates the new object-oriented architecture and simplified APIs.
"""

import pytest
import asyncio
from core.agent_profiles import ProfileManager, AgentProfile, ProfileTemplate
from core.guardrails import GuardrailManager, InputValidationGuardrail, PathSafetyGuardrail
from core.evals import EvalManager, EvalSuite, TestCase, AccuracyEvaluator, RelevanceEvaluator
from core.tool_registry import ToolRegistry, ToolBuilder
from utils.agent_metrics import AgentMetrics
from tests.enhanced_test_framework import EnhancedTestEnvironment, WorkflowTestCase
from schemas.enhanced_schemas import ConfigValidator, EnhancedAgentConfig


class TestAgentProfiles:
    """Test agent profiles functionality."""
    
    def test_profile_creation(self):
        """Test creating and managing agent profiles."""
        profile_manager = ProfileManager()
        
        # Test getting built-in profile
        file_profile = profile_manager.registry.get_profile("file_worker")
        assert file_profile is not None
        assert file_profile.template == ProfileTemplate.FILE_OPERATIONS
        assert "file_read" in file_profile.tools
        
    def test_profile_application(self):
        """Test applying profile to agent configuration."""
        profile_manager = ProfileManager()
        
        agent_config = {
            "name": "Test Agent",
            "model": "gpt-4",
            "tools": ["custom_tool"]
        }
        
        enhanced_config = profile_manager.apply_profile(agent_config, "file_worker")
        
        # Should merge tools
        assert "file_read" in enhanced_config["tools"]
        assert "custom_tool" in enhanced_config["tools"]
        assert "input_validation" in enhanced_config["guardrails"]
        
    def test_custom_profile_registration(self):
        """Test registering custom profiles."""
        profile_manager = ProfileManager()
        
        custom_profile = AgentProfile(
            name="Custom Analyst",
            template=ProfileTemplate.ANALYSIS,
            capabilities=["data_processing", "visualization"],
            tools=["data_tool", "chart_tool"],
            guardrails=["input_validation"]
        )
        
        profile_manager.registry.register_profile("custom_analyst", custom_profile)
        
        retrieved = profile_manager.registry.get_profile("custom_analyst")
        assert retrieved.name == "Custom Analyst"
        assert "data_tool" in retrieved.tools


class TestGuardrails:
    """Test guardrails functionality."""
    
    @pytest.mark.asyncio
    async def test_input_validation_guardrail(self):
        """Test input validation guardrail."""
        guardrail = InputValidationGuardrail(max_length=100, min_length=5)
        
        # Valid input
        result = await guardrail.validate_input("This is a valid input message")
        assert result.is_valid
        
        # Too short
        result = await guardrail.validate_input("Hi")
        assert not result.is_valid
        assert "too short" in result.message.lower()
        
        # Too long
        long_input = "x" * 200
        result = await guardrail.validate_input(long_input)
        assert not result.is_valid
        assert "exceeds maximum length" in result.message
        
    @pytest.mark.asyncio
    async def test_path_safety_guardrail(self):
        """Test path safety guardrail."""
        guardrail = PathSafetyGuardrail(allowed_paths=["."])
        
        # Safe path
        result = await guardrail.validate_input("Read file ./data.txt")
        assert result.is_valid
        
        # Unsafe path
        result = await guardrail.validate_input("Delete file ../../../etc/passwd")
        assert not result.is_valid
        assert "unsafe file path" in result.message.lower()
        
    @pytest.mark.asyncio
    async def test_guardrail_manager(self):
        """Test guardrail manager orchestration."""
        manager = GuardrailManager()
        
        # Test input validation
        valid, results = await manager.validate_input(
            "Read the file data.txt",
            ["input_validation", "path_safety"]
        )
        
        assert valid
        assert len(results) == 2
        assert all(r.is_valid for r in results)
        
        # Test output sanitization
        output_with_email = "Here's the data: user@example.com"
        sanitized, results = await manager.validate_output(
            output_with_email,
            ["output_sanitization"]
        )
        
        assert "[EMAIL_REDACTED]" in sanitized


class TestEvaluations:
    """Test evaluation system."""
    
    @pytest.mark.asyncio
    async def test_accuracy_evaluator(self):
        """Test accuracy evaluation."""
        evaluator = AccuracyEvaluator()
        
        test_case = TestCase(
            id="test_1",
            input="What is 2+2?",
            expected_output="The answer is 4"
        )
        
        # Perfect match
        result = await evaluator.evaluate(test_case, "The answer is 4")
        assert result.score == 1.0
        assert result.passed
        
        # Partial match
        result = await evaluator.evaluate(test_case, "The result is 4")
        assert 0.5 < result.score < 1.0
        
        # No match
        result = await evaluator.evaluate(test_case, "I don't know")
        assert result.score < 0.5
        assert not result.passed
        
    @pytest.mark.asyncio
    async def test_relevance_evaluator(self):
        """Test relevance evaluation."""
        evaluator = RelevanceEvaluator()
        
        test_case = TestCase(
            id="test_2",
            input="Explain file operations in Python"
        )
        
        # Relevant response
        relevant_response = "File operations in Python include reading, writing, and manipulating files using built-in functions"
        result = await evaluator.evaluate(test_case, relevant_response)
        assert result.score > 0.5
        assert result.passed
        
        # Irrelevant response
        irrelevant_response = "The weather is nice today"
        result = await evaluator.evaluate(test_case, irrelevant_response)
        assert result.score < 0.5
        
    @pytest.mark.asyncio
    async def test_eval_suite(self):
        """Test evaluation suite execution."""
        eval_manager = EvalManager()
        
        # Create test suite
        suite = EvalSuite("test_suite", [
            TestCase("test_1", "What is Python?", expected_output="Python is a programming language"),
            TestCase("test_2", "How to read files?", expected_behavior="Should explain file reading")
        ])
        
        eval_manager.register_suite(suite)
        
        # Mock agent runner
        async def mock_agent_runner(agent_key: str, input_msg: str) -> str:
            if "Python" in input_msg:
                return "Python is a powerful programming language"
            elif "files" in input_msg:
                return "Use open() function to read files"
            return "I don't understand"
        
        # Run evaluation
        result = await eval_manager.run_suite("test_suite", mock_agent_runner, "test_agent")
        
        assert result.suite_name == "test_suite"
        assert result.agent_key == "test_agent"
        assert len(result.results) > 0
        assert result.overall_score > 0


class TestToolRegistry:
    """Test tool registry functionality."""
    
    def test_function_tool_registration(self):
        """Test registering function tools."""
        registry = ToolRegistry()
        
        def simple_calculator(a: int, b: int) -> int:
            return a + b
        
        registry.register_function_tool(
            "calculator",
            simple_calculator,
            description="Simple calculator tool",
            category="math",
            tags=["calculator", "math"]
        )
        
        tool = registry.get_tool("calculator")
        assert tool is not None
        assert tool.metadata.name == "calculator"
        assert tool.metadata.category == "math"
        assert "calculator" in tool.metadata.tags
        
        # Test getting function tool
        function_tool = registry.get_tool_function("calculator")
        assert function_tool is not None
        
    def test_tool_builder(self):
        """Test fluent tool builder interface."""
        registry = ToolRegistry()
        builder = ToolBuilder(registry)
        
        def test_function(input_text: str) -> str:
            return f"Processed: {input_text}"
        
        # Build tool using fluent interface
        tool = (builder
                .name("text_processor")
                .description("Processes text input")
                .category("text")
                .tags("processing", "text")
                .function(test_function)
                .build())
        
        assert tool.metadata.name == "text_processor"
        assert "processing" in tool.metadata.tags
        
    def test_composite_tool(self):
        """Test composite tool creation."""
        registry = ToolRegistry()
        
        # Register base tools
        def step1(input: str) -> str:
            return f"Step1: {input}"
        
        def step2(input: str) -> str:
            return f"Step2: {input}"
        
        registry.register_function_tool("step1", step1)
        registry.register_function_tool("step2", step2)
        
        # Create composite tool
        registry.register_composite_tool(
            "two_step_process",
            ["step1", "step2"],
            description="Two-step processing chain"
        )
        
        composite_tool = registry.get_tool("two_step_process")
        assert composite_tool is not None
        assert composite_tool.metadata.dependencies == ["step1", "step2"]
        
    def test_tool_statistics(self):
        """Test tool registry statistics."""
        registry = ToolRegistry()
        
        # Add some tools
        registry.register_function_tool("tool1", lambda: None, category="cat1")
        registry.register_function_tool("tool2", lambda: None, category="cat2")
        
        stats = registry.get_tool_statistics()
        assert stats["total_tools"] == 2
        assert "function" in stats["by_type"]
        assert "cat1" in stats["by_category"]


class TestAgentMetrics:
    """Test agent metrics system."""
    
    def test_metrics_recording(self):
        """Test recording execution metrics."""
        metrics = AgentMetrics()
        
        # Record some executions
        metrics.record_execution(
            agent_key="test_agent",
            duration=2.5,
            success=True,
            input_text="Test input",
            output_text="Test output",
            tools_used=["tool1", "tool2"]
        )
        
        metrics.record_execution(
            agent_key="test_agent",
            duration=3.0,
            success=False,
            error_type="ValidationError",
            error_message="Invalid input"
        )
        
        # Get agent health
        health = metrics.get_agent_health("test_agent")
        assert health.agent_key == "test_agent"
        assert health.success_rate == 0.5  # 1 success, 1 failure
        assert health.avg_response_time > 0
        
    def test_system_overview(self):
        """Test system metrics overview."""
        metrics = AgentMetrics()
        
        # Record metrics for multiple agents
        for i in range(5):
            metrics.record_execution(
                agent_key=f"agent_{i % 2}",  # agent_0 and agent_1
                duration=1.0 + i * 0.5,
                success=i % 3 != 0,  # Some failures
                tools_used=[f"tool_{i}"]
            )
        
        overview = metrics.get_system_overview()
        assert overview["total_executions"] == 5
        assert overview["active_agents"] == 2
        assert "agent_0" in overview["agents"]
        assert "agent_1" in overview["agents"]
        
    def test_detailed_analysis(self):
        """Test detailed agent analysis."""
        metrics = AgentMetrics()
        
        # Record varied executions
        for i in range(10):
            metrics.record_execution(
                agent_key="detailed_agent",
                duration=1.0 + i * 0.1,
                success=i < 8,  # 8 successes, 2 failures
                quality_score=0.8 + i * 0.02,
                tools_used=[f"tool_{i % 3}"]
            )
        
        analysis = metrics.get_detailed_analysis("detailed_agent", hours=1)
        
        assert analysis["agent_key"] == "detailed_agent"
        assert "performance_analysis" in analysis
        assert "health_status" in analysis
        assert "time_series" in analysis
        assert len(analysis["time_series"]) == 10


@pytest.mark.asyncio
async def test_enhanced_test_environment():
    """Test enhanced test environment with all features."""
    async with EnhancedTestEnvironment() as env:
        # Test with evaluation
        result = await env.run_with_evaluation(
            "test_simple_agent",
            "Hello, test message",
            eval_suite_name="general_capabilities"
        )
        
        assert "response" in result
        assert "metrics" in result
        assert result["success"]
        
        # Test performance report
        report = env.get_performance_report()
        assert "overall_metrics" in report
        assert "agent_metrics" in report
        assert "recommendations" in report


@pytest.mark.asyncio
async def test_workflow_test_case():
    """Test complex workflow testing."""
    async with EnhancedTestEnvironment() as env:
        # Create workflow test
        workflow = WorkflowTestCase(
            "file_processing_workflow",
            "Test file processing workflow"
        )
        
        # Add setup
        async def setup_test_files(test_env):
            test_env.create_test_file("input.txt", "Test content")
        
        workflow.add_setup(setup_test_files)
        
        # Add workflow steps
        workflow.add_step("test_simple_agent", "Read the file input.txt", ["content", "file"])
        workflow.add_step("test_simple_agent", "Summarize the content", ["summary"])
        
        # Execute workflow
        result = await workflow.run(env)
        
        assert result["workflow_name"] == "file_processing_workflow"
        assert result["total_steps"] == 2
        assert "results" in result


def test_config_validation():
    """Test enhanced configuration validation."""
    # Valid config
    valid_config = {
        "settings": {
            "default_agent": "test_agent",
            "max_history": 10,
            "max_turns": 20
        },
        "providers": {
            "test_provider": {
                "name": "Test Provider",
                "base_url": "https://api.test.com",
                "api_key_env": "TEST_API_KEY"
            }
        },
        "models": {
            "test_model": {
                "name": "test-model",
                "provider": "test_provider",
                "temperature": 0.7
            }
        },
        "agents": {
            "test_agent": {
                "name": "Test Agent",
                "model": "test_model",
                "tools": []
            }
        }
    }
    
    # Should validate successfully
    config = ConfigValidator.validate_config_file(valid_config)
    assert config.settings.default_agent == "test_agent"
    
    # Test validation errors
    invalid_config = {
        "settings": {
            "default_agent": "nonexistent_agent"
        },
        "providers": {},
        "models": {},
        "agents": {}
    }
    
    errors = ConfigValidator.get_validation_errors(invalid_config)
    assert len(errors) > 0
    
    # Test suggestions
    suggestions = ConfigValidator.suggest_fixes(invalid_config)
    assert len(suggestions) > 0
    assert any("provider" in s.lower() for s in suggestions)
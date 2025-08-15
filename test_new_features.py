#!/usr/bin/env python3
"""
Standalone testing script for new enhanced features.
Tests each component independently without complex dependencies.
"""

import asyncio
import sys
import traceback
from typing import List, Dict, Any


def test_agent_profiles():
    """Test agent profiles functionality."""
    print("\n🔍 Testing Agent Profiles...")
    
    try:
        from core.agent_profiles import ProfileManager, AgentProfile, ProfileTemplate
        
        # Test profile creation
        profile_manager = ProfileManager()
        
        # Test getting built-in profile
        file_profile = profile_manager.registry.get_profile("file_worker")
        assert file_profile is not None, "File worker profile should exist"
        assert file_profile.template == ProfileTemplate.FILE_OPERATIONS, "Should be file operations template"
        assert "file_read" in file_profile.tools, "Should include file_read tool"
        
        # Test profile application
        agent_config = {
            "name": "Test Agent",
            "model": "gpt-4",
            "tools": ["custom_tool"]
        }
        
        enhanced_config = profile_manager.apply_profile(agent_config, "file_worker")
        assert "file_read" in enhanced_config["tools"], "Should merge file tools"
        assert "custom_tool" in enhanced_config["tools"], "Should keep existing tools"
        assert "input_validation" in enhanced_config["guardrails"], "Should add guardrails"
        
        # Test custom profile registration
        custom_profile = AgentProfile(
            name="Custom Analyst",
            template=ProfileTemplate.ANALYSIS,
            capabilities=["data_processing", "visualization"],
            tools=["data_tool", "chart_tool"],
            guardrails=["input_validation"]
        )
        
        profile_manager.registry.register_profile("custom_analyst", custom_profile)
        retrieved = profile_manager.registry.get_profile("custom_analyst")
        assert retrieved.name == "Custom Analyst", "Should retrieve custom profile"
        assert "data_tool" in retrieved.tools, "Should have custom tools"
        
        print("✅ Agent Profiles: All tests passed!")
        return True
        
    except Exception as e:
        print(f"❌ Agent Profiles: Test failed - {e}")
        traceback.print_exc()
        return False


async def test_guardrails():
    """Test guardrails functionality."""
    print("\n🛡️ Testing Guardrails...")
    
    try:
        from core.guardrails import GuardrailManager, InputValidationGuardrail, PathSafetyGuardrail
        
        # Test input validation guardrail
        guardrail = InputValidationGuardrail(max_length=100, min_length=5)
        
        # Valid input
        result = await guardrail.validate_input("This is a valid input message")
        assert result.is_valid, "Valid input should pass"
        
        # Too short
        result = await guardrail.validate_input("Hi")
        assert not result.is_valid, "Short input should fail"
        assert "too short" in result.message.lower(), "Should mention length issue"
        
        # Too long
        long_input = "x" * 200
        result = await guardrail.validate_input(long_input)
        assert not result.is_valid, "Long input should fail"
        assert "exceeds maximum length" in result.message, "Should mention length limit"
        
        # Test path safety guardrail
        path_guardrail = PathSafetyGuardrail(allowed_paths=["."])
        
        # Safe path
        result = await path_guardrail.validate_input("Read file ./data.txt")
        assert result.is_valid, "Safe path should pass"
        
        # Unsafe path  
        result = await path_guardrail.validate_input("Delete file ../../../etc/passwd")
        assert not result.is_valid, "Unsafe path should fail"
        assert "unsafe file path" in result.message.lower(), "Should mention path safety"
        
        # Test guardrail manager
        manager = GuardrailManager()
        
        valid, results = await manager.validate_input(
            "Read the file data.txt",
            ["input_validation", "path_safety"]
        )
        assert valid, "Valid input should pass all guardrails"
        assert len(results) == 2, "Should run both guardrails"
        assert all(r.is_valid for r in results), "All results should be valid"
        
        # Test output sanitization
        output_with_email = "Here's the data: user@example.com"
        sanitized, results = await manager.validate_output(
            output_with_email,
            ["output_sanitization"]
        )
        assert "[EMAIL_REDACTED]" in sanitized, "Should redact email address"
        
        print("✅ Guardrails: All tests passed!")
        return True
        
    except Exception as e:
        print(f"❌ Guardrails: Test failed - {e}")
        traceback.print_exc()
        return False


async def test_evaluations():
    """Test evaluation system."""
    print("\n📊 Testing Evaluation System...")
    
    try:
        from core.evals import EvalManager, EvalSuite, TestCase, AccuracyEvaluator, RelevanceEvaluator
        
        # Test accuracy evaluator
        evaluator = AccuracyEvaluator()
        
        test_case = TestCase(
            id="test_1",
            input="What is 2+2?",
            expected_output="The answer is 4"
        )
        
        # Perfect match
        result = await evaluator.evaluate(test_case, "The answer is 4")
        assert result.score == 1.0, "Perfect match should score 1.0"
        assert result.passed, "Perfect match should pass"
        
        # Partial match
        result = await evaluator.evaluate(test_case, "The result is 4")
        assert 0.5 < result.score < 1.0, "Partial match should score between 0.5 and 1.0"
        
        # No match
        result = await evaluator.evaluate(test_case, "I don't know")
        assert result.score < 0.5, "No match should score low"
        assert not result.passed, "No match should not pass"
        
        # Test relevance evaluator
        relevance_evaluator = RelevanceEvaluator()
        
        test_case_2 = TestCase(
            id="test_2",
            input="Explain file operations in Python"
        )
        
        # Relevant response
        relevant_response = "File operations in Python include reading, writing, and manipulating files using built-in functions"
        result = await relevance_evaluator.evaluate(test_case_2, relevant_response)
        assert result.score > 0.5, "Relevant response should score high"
        assert result.passed, "Relevant response should pass"
        
        # Irrelevant response
        irrelevant_response = "The weather is nice today"
        result = await relevance_evaluator.evaluate(test_case_2, irrelevant_response)
        assert result.score < 0.5, "Irrelevant response should score low"
        
        # Test eval suite
        eval_manager = EvalManager()
        
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
        
        assert result.suite_name == "test_suite", "Should preserve suite name"
        assert result.agent_key == "test_agent", "Should preserve agent key"
        assert len(result.results) > 0, "Should have evaluation results"
        assert result.overall_score > 0, "Should have positive overall score"
        
        print("✅ Evaluation System: All tests passed!")
        return True
        
    except Exception as e:
        print(f"❌ Evaluation System: Test failed - {e}")
        traceback.print_exc()
        return False


def test_tool_registry():
    """Test tool registry functionality."""
    print("\n🔧 Testing Tool Registry...")
    
    try:
        from core.tool_registry import ToolRegistry, ToolBuilder
        
        registry = ToolRegistry()
        
        # Test function tool registration
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
        assert tool is not None, "Should retrieve registered tool"
        assert tool.metadata.name == "calculator", "Should preserve tool name"
        assert tool.metadata.category == "math", "Should preserve category"
        assert "calculator" in tool.metadata.tags, "Should preserve tags"
        
        # Test getting function tool
        function_tool = registry.get_tool_function("calculator")
        assert function_tool is not None, "Should create function tool"
        
        # Test tool builder
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
        
        assert tool.metadata.name == "text_processor", "Builder should set name"
        assert "processing" in tool.metadata.tags, "Builder should set tags"
        
        # Test composite tool
        def step1(input: str) -> str:
            return f"Step1: {input}"
        
        def step2(input: str) -> str:
            return f"Step2: {input}"
        
        registry.register_function_tool("step1", step1)
        registry.register_function_tool("step2", step2)
        
        registry.register_composite_tool(
            "two_step_process",
            ["step1", "step2"],
            description="Two-step processing chain"
        )
        
        composite_tool = registry.get_tool("two_step_process")
        assert composite_tool is not None, "Should create composite tool"
        assert composite_tool.metadata.dependencies == ["step1", "step2"], "Should track dependencies"
        
        # Test tool statistics
        stats = registry.get_tool_statistics()
        assert stats["total_tools"] >= 4, "Should count all registered tools"
        assert "function" in stats["by_type"], "Should categorize by type"
        assert "math" in stats["by_category"], "Should categorize by category"
        
        print("✅ Tool Registry: All tests passed!")
        return True
        
    except Exception as e:
        print(f"❌ Tool Registry: Test failed - {e}")
        traceback.print_exc()
        return False


def test_agent_metrics():
    """Test agent metrics system."""
    print("\n📈 Testing Agent Metrics...")
    
    try:
        from utils.agent_metrics import AgentMetrics
        
        metrics = AgentMetrics()
        
        # Test metrics recording
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
        
        # Test agent health
        health = metrics.get_agent_health("test_agent")
        assert health.agent_key == "test_agent", "Should track correct agent"
        assert health.success_rate == 0.5, "Should calculate success rate (1 success, 1 failure)"
        assert health.avg_response_time > 0, "Should calculate average response time"
        
        # Test system overview
        for i in range(5):
            metrics.record_execution(
                agent_key=f"agent_{i % 2}",  # agent_0 and agent_1
                duration=1.0 + i * 0.5,
                success=i % 3 != 0,  # Some failures
                tools_used=[f"tool_{i}"]
            )
        
        overview = metrics.get_system_overview()
        assert overview["total_executions"] >= 5, "Should count all executions"
        assert overview["active_agents"] >= 2, "Should track multiple agents"
        assert "agent_0" in overview["agents"], "Should include agent_0"
        
        # Test detailed analysis
        for i in range(10):
            metrics.record_execution(
                agent_key="detailed_agent",
                duration=1.0 + i * 0.1,
                success=i < 8,  # 8 successes, 2 failures
                quality_score=0.8 + i * 0.02,
                tools_used=[f"tool_{i % 3}"]
            )
        
        analysis = metrics.get_detailed_analysis("detailed_agent", hours=1)
        assert analysis["agent_key"] == "detailed_agent", "Should analyze correct agent"
        assert "performance_analysis" in analysis, "Should include performance analysis"
        assert "health_status" in analysis, "Should include health status"
        assert "time_series" in analysis, "Should include time series data"
        assert len(analysis["time_series"]) == 10, "Should include all metrics"
        
        print("✅ Agent Metrics: All tests passed!")
        return True
        
    except Exception as e:
        print(f"❌ Agent Metrics: Test failed - {e}")
        traceback.print_exc()
        return False


def test_config_validation():
    """Test enhanced configuration validation."""
    print("\n⚙️ Testing Configuration Validation...")
    
    try:
        from schemas.enhanced_schemas import ConfigValidator, EnhancedAgentConfig
        
        # Test valid config
        valid_config = {
            "settings": {
                "default_agent": "test_agent",
                "max_history": 10,
                "max_turns": 20,
                "working_directory": ".",
                "config_directory": "."
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
        assert config.settings.default_agent == "test_agent", "Should preserve default agent"
        
        # Test individual agent config
        agent_config = {
            "name": "Test Agent",
            "model": "test_model",
            "tools": ["tool1", "tool2"],
            "max_tools_per_turn": 5
        }
        
        validated_agent = ConfigValidator.validate_agent_config(agent_config)
        assert validated_agent.name == "Test Agent", "Should validate agent config"
        assert validated_agent.max_tools_per_turn == 5, "Should preserve custom settings"
        
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
        assert len(errors) > 0, "Should detect validation errors"
        
        # Test suggestions
        suggestions = ConfigValidator.suggest_fixes(invalid_config)
        assert len(suggestions) > 0, "Should provide fix suggestions"
        assert any("provider" in s.lower() for s in suggestions), "Should suggest adding providers"
        
        print("✅ Configuration Validation: All tests passed!")
        return True
        
    except Exception as e:
        print(f"❌ Configuration Validation: Test failed - {e}")
        traceback.print_exc()
        return False


async def main():
    """Run all tests."""
    print("🚀 Testing Grid System Enhancements")
    print("=" * 50)
    
    test_results = []
    
    # Run all tests
    test_results.append(test_agent_profiles())
    test_results.append(await test_guardrails())
    test_results.append(await test_evaluations())
    test_results.append(test_tool_registry())
    test_results.append(test_agent_metrics())
    test_results.append(test_config_validation())
    
    # Summary
    passed = sum(test_results)
    total = len(test_results)
    
    print("\n" + "=" * 50)
    print(f"📊 Test Summary: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All enhancements are working correctly!")
        return 0
    else:
        print(f"⚠️ {total - passed} tests failed. Please check the errors above.")
        return 1


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n❌ Tests interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 Unexpected error: {e}")
        traceback.print_exc()
        sys.exit(1)
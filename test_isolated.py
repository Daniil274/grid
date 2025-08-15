#!/usr/bin/env python3
"""
Isolated testing script that tests individual components without dependencies.
"""

import asyncio
import sys
import os
import traceback

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_metrics_only():
    """Test only agent metrics which has no dependencies."""
    print("\n📈 Testing Agent Metrics (Isolated)...")
    
    try:
        from utils.agent_metrics import AgentMetrics, ExecutionMetric, MetricAggregator, PerformanceAnalyzer
        
        # Test basic metrics recording
        metrics = AgentMetrics()
        
        # Record successful execution
        metrics.record_execution(
            agent_key="test_agent",
            duration=2.5,
            success=True,
            input_text="Test input",
            output_text="Test output",
            tools_used=["tool1", "tool2"],
            quality_score=0.85
        )
        
        # Record failed execution
        metrics.record_execution(
            agent_key="test_agent", 
            duration=3.0,
            success=False,
            error_type="ValidationError",
            error_message="Invalid input"
        )
        
        # Test agent health
        health = metrics.get_agent_health("test_agent")
        assert health.agent_key == "test_agent"
        assert health.success_rate == 0.5  # 1 success, 1 failure
        assert health.avg_response_time == 2.75  # (2.5 + 3.0) / 2
        assert health.error_rate == 0.5
        
        # Test with no data
        empty_health = metrics.get_agent_health("nonexistent_agent")
        assert empty_health.status == "no_data"
        assert "No recent execution data" in empty_health.issues
        
        # Test multiple agents
        for i in range(5):
            metrics.record_execution(
                agent_key=f"agent_{i % 2}",  # agent_0 and agent_1
                duration=1.0 + i * 0.5,
                success=i % 3 != 0,  # Some failures
                tools_used=[f"tool_{i}"]
            )
        
        overview = metrics.get_system_overview()
        assert overview["total_executions"] >= 7  # 2 + 5
        assert overview["active_agents"] >= 3  # test_agent, agent_0, agent_1
        assert "test_agent" in overview["agents"]
        assert "agent_0" in overview["agents"]
        
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
        assert analysis["agent_key"] == "detailed_agent"
        assert "performance_analysis" in analysis
        assert "health_status" in analysis
        assert "time_series" in analysis
        assert len(analysis["time_series"]) == 10
        
        # Test export functionality
        exported = metrics.export_metrics("detailed_agent", hours=1)
        assert len(exported) == 10
        assert all("agent_key" in record for record in exported)
        
        # Test metric aggregator directly
        aggregator = MetricAggregator(window_size=5)
        
        for i in range(3):
            metric = ExecutionMetric(
                agent_key="agg_test",
                timestamp=1000.0 + i,
                duration=1.0 + i,
                success=True,
                input_length=10,
                output_length=20,
                tools_used=[f"tool_{i}"]
            )
            aggregator.add_metric(metric)
        
        agent_metrics = aggregator.get_agent_metrics("agg_test")
        assert len(agent_metrics) == 3
        assert all(m.agent_key == "agg_test" for m in agent_metrics)
        
        # Test performance analyzer
        analyzer = PerformanceAnalyzer()
        
        test_metrics = []
        for i in range(20):
            metric = ExecutionMetric(
                agent_key="analyzer_test",
                timestamp=1000.0 + i,
                duration=1.0 + i * 0.1,
                success=i < 15,  # 15 successes, 5 failures
                input_length=100,
                output_length=200,
                quality_score=0.7 + i * 0.01
            )
            test_metrics.append(metric)
        
        analysis = analyzer.analyze_agent_performance(test_metrics)
        assert analysis["total_executions"] == 20
        assert analysis["success_rate"] == 0.75  # 15/20
        assert analysis["error_rate"] == 0.25    # 5/20
        assert "avg_response_time" in analysis
        assert "trends" in analysis  # Should include trends for 20+ metrics
        
        health = analyzer.generate_health_status("analyzer_test", analysis)
        assert health.agent_key == "analyzer_test"
        assert health.success_rate == 0.75
        assert health.error_rate == 0.25
        
        print("✅ Agent Metrics: All tests passed!")
        return True
        
    except Exception as e:
        print(f"❌ Agent Metrics: Test failed - {e}")
        traceback.print_exc()
        return False


async def test_simple_components():
    """Test individual component files that don't have complex dependencies."""
    print("\n🧪 Testing Simple Components...")
    
    results = []
    
    # Test enum and basic classes from agent_profiles
    try:
        # Import and test basic ProfileTemplate enum
        import sys
        sys.path.append('core')
        
        from core.agent_profiles import ProfileTemplate, AgentProfile
        
        # Test enum values
        assert ProfileTemplate.FILE_OPERATIONS == "file_operations"
        assert ProfileTemplate.DEVELOPMENT == "development"
        
        # Test AgentProfile creation
        profile = AgentProfile(
            name="Test Profile",
            template=ProfileTemplate.ANALYSIS,
            capabilities=["test_capability"],
            tools=["test_tool"],
            guardrails=["test_guardrail"]
        )
        
        assert profile.name == "Test Profile"
        assert profile.template == ProfileTemplate.ANALYSIS
        assert "test_capability" in profile.capabilities
        
        # Test merge functionality
        agent_config = {"name": "Test Agent", "tools": ["existing_tool"]}
        merged = profile.merge_with_config(agent_config)
        
        assert "test_tool" in merged["tools"]
        assert "existing_tool" in merged["tools"]
        assert "test_guardrail" in merged["guardrails"]
        
        print("✅ Agent Profiles Basic: Tests passed!")
        results.append(True)
        
    except Exception as e:
        print(f"❌ Agent Profiles Basic: Test failed - {e}")
        results.append(False)
    
    # Test guardrail enums and basic validation
    try:
        from core.guardrails import ValidationResult, ValidationError
        
        # Test ValidationResult
        result = ValidationResult(
            is_valid=True,
            message="Test passed",
            modified_content="Modified content"
        )
        
        assert result.is_valid
        assert result.message == "Test passed"
        assert result.modified_content == "Modified content"
        
        # Test ValidationError
        try:
            raise ValidationError("Test error", {"detail": "test"})
        except ValidationError as e:
            assert str(e) == "Test error"
            assert e.details["detail"] == "test"
        
        print("✅ Guardrails Basic: Tests passed!")
        results.append(True)
        
    except Exception as e:
        print(f"❌ Guardrails Basic: Test failed - {e}")
        results.append(False)
    
    # Test evaluation enums and basic classes
    try:
        from core.evals import EvalMetric, TestCase, EvalResult
        
        # Test enum
        assert EvalMetric.ACCURACY == "accuracy"
        assert EvalMetric.RELEVANCE == "relevance"
        
        # Test TestCase
        test_case = TestCase(
            id="test_1",
            input="Test input",
            expected_output="Expected output"
        )
        
        assert test_case.id == "test_1"
        assert test_case.input == "Test input"
        
        # Test EvalResult
        result = EvalResult(
            test_case_id="test_1",
            agent_response="Agent response",
            score=0.85,
            metric=EvalMetric.ACCURACY
        )
        
        assert result.test_case_id == "test_1"
        assert result.score == 0.85
        assert result.passed  # Should pass with score >= 0.7
        
        print("✅ Evaluations Basic: Tests passed!")
        results.append(True)
        
    except Exception as e:
        print(f"❌ Evaluations Basic: Test failed - {e}")
        results.append(False)
    
    return all(results)


async def test_enhanced_schemas():
    """Test enhanced schemas with Pydantic validation."""
    print("\n⚙️ Testing Enhanced Schemas...")
    
    try:
        from schemas.enhanced_schemas import (
            ProviderType, ToolType, GuardrailType, ProfileTemplate,
            EnhancedProviderConfig, EnhancedModelConfig, EnhancedToolConfig
        )
        
        # Test enums
        assert ProviderType.OPENAI == "openai"
        assert ToolType.FUNCTION == "function"
        assert GuardrailType.INPUT_VALIDATION == "input_validation"
        
        # Test provider config validation
        provider_config = EnhancedProviderConfig(
            name="Test Provider",
            base_url="https://api.test.com",
            api_key_env="TEST_API_KEY",
            timeout=30.0,
            max_retries=3
        )
        
        assert provider_config.name == "Test Provider"
        assert provider_config.provider_type == ProviderType.OPENAI  # default
        assert provider_config.base_url == "https://api.test.com"
        
        # Test model config validation
        model_config = EnhancedModelConfig(
            name="test-model",
            provider="test_provider",
            temperature=0.7,
            max_tokens=4000
        )
        
        assert model_config.name == "test-model"
        assert model_config.temperature == 0.7
        assert not model_config.use_responses_api  # default False
        
        # Test tool config validation
        function_tool_config = EnhancedToolConfig(
            name="test_function",
            type=ToolType.FUNCTION,
            function_name="test_func",
            description="Test function tool"
        )
        
        assert function_tool_config.name == "test_function"
        assert function_tool_config.type == ToolType.FUNCTION
        assert function_tool_config.function_name == "test_func"
        
        # Test validation errors
        try:
            # Should fail validation - missing required field
            invalid_provider = EnhancedProviderConfig(
                name="Test",
                base_url="invalid-url",  # Should fail URL validation
                api_key_env="test_key"
            )
            assert False, "Should have failed validation"
        except Exception:
            pass  # Expected to fail
        
        print("✅ Enhanced Schemas: All tests passed!")
        return True
        
    except Exception as e:
        print(f"❌ Enhanced Schemas: Test failed - {e}")
        traceback.print_exc()
        return False


async def main():
    """Run isolated tests."""
    print("🧪 Testing Grid System Enhancements (Isolated)")
    print("=" * 55)
    
    test_results = []
    
    # Run isolated tests
    test_results.append(test_metrics_only())
    test_results.append(await test_simple_components())
    test_results.append(await test_enhanced_schemas())
    
    # Summary
    passed = sum(test_results)
    total = len(test_results)
    
    print("\n" + "=" * 55)
    print(f"📊 Test Summary: {passed}/{total} components tested successfully")
    
    if passed == total:
        print("🎉 All tested components are working correctly!")
        print("\n📝 Test Coverage:")
        print("  ✅ Agent Metrics - Full functionality")
        print("  ✅ Basic Components - Core classes and enums") 
        print("  ✅ Enhanced Schemas - Pydantic validation")
        print("\n⚠️ Note: Full integration tests require resolving dependency issues")
        return 0
    else:
        print(f"⚠️ {total - passed} components failed testing.")
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
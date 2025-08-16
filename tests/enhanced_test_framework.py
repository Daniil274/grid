"""
Enhanced testing framework with metrics, evals, and advanced agent testing capabilities.
Extends the base test framework with quality assessment and performance monitoring.
"""

import asyncio
import time
import tempfile
import os
from typing import Dict, List, Any, Optional
from unittest.mock import AsyncMock, patch

from core.config import Config
from core.agent_factory import AgentFactory
from core.evals import EvalManager, EvalSuite, TestCase, EvalSuiteResult
from core.guardrails import GuardrailManager
from core.agent_profiles import ProfileManager
from tests.test_framework import TestEnvironment, TestResult


class MetricsCollector:
    """Collects and analyzes performance metrics during testing."""
    
    def __init__(self):
        self.metrics = {
            "response_times": [],
            "memory_usage": [],
            "tool_usage": {},
            "error_counts": {},
            "success_rates": {}
        }
    
    def record_execution(
        self, 
        agent_key: str, 
        duration: float, 
        success: bool, 
        tools_used: List[str] = None,
        memory_usage: float = 0.0
    ):
        """Record execution metrics."""
        self.metrics["response_times"].append({
            "agent": agent_key,
            "duration": duration,
            "timestamp": time.time()
        })
        
        if memory_usage > 0:
            self.metrics["memory_usage"].append({
                "agent": agent_key,
                "memory": memory_usage,
                "timestamp": time.time()
            })
        
        # Track tool usage
        if tools_used:
            for tool in tools_used:
                if tool not in self.metrics["tool_usage"]:
                    self.metrics["tool_usage"][tool] = 0
                self.metrics["tool_usage"][tool] += 1
        
        # Track success rates
        if agent_key not in self.metrics["success_rates"]:
            self.metrics["success_rates"][agent_key] = {"success": 0, "total": 0}
        
        self.metrics["success_rates"][agent_key]["total"] += 1
        if success:
            self.metrics["success_rates"][agent_key]["success"] += 1
        else:
            if agent_key not in self.metrics["error_counts"]:
                self.metrics["error_counts"][agent_key] = 0
            self.metrics["error_counts"][agent_key] += 1
    
    def get_agent_metrics(self, agent_key: str) -> Dict[str, Any]:
        """Get metrics for a specific agent."""
        agent_response_times = [
            m["duration"] for m in self.metrics["response_times"] 
            if m["agent"] == agent_key
        ]
        
        success_data = self.metrics["success_rates"].get(agent_key, {"success": 0, "total": 0})
        success_rate = success_data["success"] / success_data["total"] if success_data["total"] > 0 else 0
        
        return {
            "agent_key": agent_key,
            "avg_response_time": sum(agent_response_times) / len(agent_response_times) if agent_response_times else 0,
            "min_response_time": min(agent_response_times) if agent_response_times else 0,
            "max_response_time": max(agent_response_times) if agent_response_times else 0,
            "success_rate": success_rate,
            "total_executions": success_data["total"],
            "error_count": self.metrics["error_counts"].get(agent_key, 0)
        }
    
    def get_overall_metrics(self) -> Dict[str, Any]:
        """Get overall system metrics."""
        all_response_times = [m["duration"] for m in self.metrics["response_times"]]
        
        return {
            "total_executions": len(self.metrics["response_times"]),
            "avg_response_time": sum(all_response_times) / len(all_response_times) if all_response_times else 0,
            "most_used_tools": sorted(
                self.metrics["tool_usage"].items(), 
                key=lambda x: x[1], 
                reverse=True
            )[:5],
            "agents_tested": list(self.metrics["success_rates"].keys()),
            "total_errors": sum(self.metrics["error_counts"].values())
        }


class EnhancedTestEnvironment(TestEnvironment):
    """Enhanced test environment with eval support and metrics collection."""
    
    def __init__(self, config_path: str = None, eval_enabled: bool = True):
        super().__init__(config_path)
        self.eval_enabled = eval_enabled
        self.metrics_collector = MetricsCollector()
        self.eval_manager = EvalManager()
        self.guardrail_manager = GuardrailManager()
        self.profile_manager = ProfileManager()
        self._setup_default_eval_suites()
    
    def _setup_default_eval_suites(self):
        """Setup default evaluation suites for common agent types."""
        # File operations suite
        file_ops_suite = EvalSuite("file_operations", [
            TestCase(
                id="file_read_test",
                input="Read the contents of test.txt",
                expected_behavior="Should read file and return contents",
                metadata={"required_elements": ["content", "file"], "optimal_length": 200}
            ),
            TestCase(
                id="file_write_test", 
                input="Write 'Hello World' to output.txt",
                expected_behavior="Should create file with specified content",
                metadata={"required_elements": ["written", "success"], "optimal_length": 150}
            )
        ])
        
        # Code analysis suite
        code_analysis_suite = EvalSuite("code_analysis", [
            TestCase(
                id="code_review_test",
                input="Analyze this Python function for issues: def test(): pass",
                expected_behavior="Should identify function issues and suggest improvements",
                metadata={"required_elements": ["function", "analysis", "suggestions"], "optimal_length": 300}
            ),
            TestCase(
                id="code_security_test",
                input="Check this code for security issues: eval(user_input)",
                expected_behavior="Should identify security vulnerabilities",
                metadata={"required_elements": ["security", "vulnerability", "eval"], "optimal_length": 250}
            )
        ])
        
        # General capability suite
        general_suite = EvalSuite("general_capabilities", [
            TestCase(
                id="task_understanding_test",
                input="List the files in the current directory and explain what each does",
                expected_behavior="Should list files and provide explanations",
                metadata={"required_elements": ["files", "directory", "explanation"], "optimal_length": 400}
            ),
            TestCase(
                id="complex_reasoning_test",
                input="If I have 3 Python files and need to create tests for each, what's the best approach?",
                expected_behavior="Should provide structured testing approach",
                metadata={"required_elements": ["testing", "approach", "files"], "optimal_length": 350}
            )
        ])
        
        self.eval_manager.register_suite(file_ops_suite)
        self.eval_manager.register_suite(code_analysis_suite)
        self.eval_manager.register_suite(general_suite)
    
    async def run_with_evaluation(
        self, 
        agent_key: str, 
        message: str, 
        eval_suite_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Run agent with automatic quality evaluation."""
        start_time = time.time()
        
        try:
            # Run agent
            response = await self.agent_factory.run_agent(agent_key, message)
            duration = time.time() - start_time
            success = True
            
            # Collect basic metrics
            tools_used = []
            recent_executions = self.agent_factory.get_recent_executions(1)
            if recent_executions:
                tools_used = recent_executions[0].tools_used or []
            
            self.metrics_collector.record_execution(
                agent_key, duration, success, tools_used
            )
            
            result = {
                "response": response,
                "duration": duration,
                "success": success,
                "metrics": self.metrics_collector.get_agent_metrics(agent_key)
            }
            
            # Run evaluation if enabled
            if self.eval_enabled and eval_suite_name:
                eval_result = await self._run_evaluation(agent_key, eval_suite_name)
                result["evaluation"] = eval_result
            
            return result
            
        except Exception as e:
            duration = time.time() - start_time
            self.metrics_collector.record_execution(agent_key, duration, False)
            
            return {
                "response": f"Error: {str(e)}",
                "duration": duration,
                "success": False,
                "error": str(e),
                "metrics": self.metrics_collector.get_agent_metrics(agent_key)
            }
    
    async def _run_evaluation(self, agent_key: str, suite_name: str) -> Dict[str, Any]:
        """Run evaluation suite against agent."""
        try:
            async def agent_runner(key: str, input_msg: str) -> str:
                return await self.agent_factory.run_agent(key, input_msg)
            
            eval_result = await self.eval_manager.run_suite(suite_name, agent_runner, agent_key)
            
            return {
                "suite_name": eval_result.suite_name,
                "overall_score": eval_result.overall_score,
                "passed_count": eval_result.passed_count,
                "failed_count": eval_result.failed_count,
                "total_duration": eval_result.total_duration,
                "details": [
                    {
                        "test_id": r.test_case_id,
                        "metric": r.metric.value,
                        "score": r.score,
                        "passed": r.passed
                    }
                    for r in eval_result.results
                ]
            }
            
        except Exception as e:
            return {
                "error": f"Evaluation failed: {str(e)}",
                "suite_name": suite_name
            }
    
    async def run_agent_profile_test(
        self, 
        profile_key: str, 
        agent_config: Dict[str, Any], 
        test_message: str
    ) -> Dict[str, Any]:
        """Test agent with applied profile."""
        # Apply profile to config
        enhanced_config = self.profile_manager.enhance_agent_config("test_agent", {
            **agent_config,
            "profile": profile_key
        })
        
        # Temporarily update config
        original_agents = self.config.data.get("agents", {}).copy()
        self.config.data.setdefault("agents", {})["test_agent"] = enhanced_config
        
        try:
            # Run test with enhanced config
            result = await self.run_with_evaluation("test_agent", test_message)
            result["profile_applied"] = profile_key
            result["enhanced_config"] = enhanced_config
            return result
            
        finally:
            # Restore original config
            self.config.data["agents"] = original_agents
    
    async def run_guardrail_test(
        self, 
        agent_key: str, 
        message: str, 
        guardrail_names: List[str]
    ) -> Dict[str, Any]:
        """Test agent with guardrail validation."""
        # Validate input
        input_valid, input_results = await self.guardrail_manager.validate_input(
            message, guardrail_names
        )
        
        if not input_valid:
            return {
                "response": "Input validation failed",
                "success": False,
                "input_validation": {
                    "valid": False,
                    "results": [r.__dict__ for r in input_results]
                }
            }
        
        # Run agent
        response = await self.agent_factory.run_agent(agent_key, message)
        
        # Validate output
        sanitized_output, output_results = await self.guardrail_manager.validate_output(
            response, guardrail_names
        )
        
        return {
            "response": sanitized_output,
            "original_response": response,
            "success": True,
            "input_validation": {
                "valid": True,
                "results": [r.__dict__ for r in input_results]
            },
            "output_validation": {
                "results": [r.__dict__ for r in output_results],
                "modified": sanitized_output != response
            }
        }
    
    def get_performance_report(self) -> Dict[str, Any]:
        """Generate comprehensive performance report."""
        overall_metrics = self.metrics_collector.get_overall_metrics()
        
        # Agent-specific metrics
        agent_metrics = {}
        for agent_key in overall_metrics["agents_tested"]:
            agent_metrics[agent_key] = self.metrics_collector.get_agent_metrics(agent_key)
        
        # Evaluation summary
        eval_summary = self.eval_manager.get_agent_performance_summary("test_agent")
        
        return {
            "timestamp": time.time(),
            "overall_metrics": overall_metrics,
            "agent_metrics": agent_metrics,
            "evaluation_summary": eval_summary,
            "recommendations": self._generate_recommendations(overall_metrics, agent_metrics)
        }
    
    def _generate_recommendations(
        self, 
        overall_metrics: Dict[str, Any], 
        agent_metrics: Dict[str, Any]
    ) -> List[str]:
        """Generate performance improvement recommendations."""
        recommendations = []
        
        # Response time recommendations
        avg_response_time = overall_metrics.get("avg_response_time", 0)
        if avg_response_time > 10:
            recommendations.append("Consider optimizing agent response times - average exceeds 10 seconds")
        
        # Error rate recommendations
        total_errors = overall_metrics.get("total_errors", 0)
        total_executions = overall_metrics.get("total_executions", 1)
        error_rate = total_errors / total_executions
        
        if error_rate > 0.1:
            recommendations.append("High error rate detected - review agent configurations and error handling")
        
        # Tool usage recommendations
        most_used_tools = overall_metrics.get("most_used_tools", [])
        if len(most_used_tools) < 3:
            recommendations.append("Limited tool usage detected - consider expanding agent capabilities")
        
        # Agent-specific recommendations
        for agent_key, metrics in agent_metrics.items():
            if metrics["success_rate"] < 0.8:
                recommendations.append(f"Agent '{agent_key}' has low success rate - review configuration")
            
            if metrics["avg_response_time"] > 15:
                recommendations.append(f"Agent '{agent_key}' has slow response times - optimize performance")
        
        return recommendations


class WorkflowTestCase:
    """Test case for complex multi-step workflows."""
    
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self.steps = []
        self.setup_actions = []
        self.teardown_actions = []
    
    def add_setup(self, action):
        """Add setup action."""
        self.setup_actions.append(action)
    
    def add_step(self, agent_key: str, message: str, expected_keywords: List[str] = None):
        """Add workflow step."""
        self.steps.append({
            "agent_key": agent_key,
            "message": message,
            "expected_keywords": expected_keywords or []
        })
    
    def add_teardown(self, action):
        """Add teardown action."""
        self.teardown_actions.append(action)
    
    async def run(self, env: EnhancedTestEnvironment) -> Dict[str, Any]:
        """Execute the workflow test case."""
        results = []
        
        try:
            # Setup
            for action in self.setup_actions:
                await action(env)
            
            # Execute steps
            for i, step in enumerate(self.steps):
                step_start = time.time()
                
                try:
                    result = await env.run_with_evaluation(
                        step["agent_key"], 
                        step["message"],
                        eval_suite_name="general_capabilities"
                    )
                    
                    step_result = {
                        "step": i + 1,
                        "agent_key": step["agent_key"],
                        "message": step["message"],
                        "response": result["response"],
                        "duration": time.time() - step_start,
                        "success": result["success"]
                    }
                    
                    # Check expected keywords
                    if step["expected_keywords"]:
                        keywords_found = [
                            kw for kw in step["expected_keywords"] 
                            if kw.lower() in result["response"].lower()
                        ]
                        step_result["keywords_found"] = keywords_found
                        step_result["keywords_expected"] = step["expected_keywords"]
                        step_result["keywords_match"] = len(keywords_found) == len(step["expected_keywords"])
                    
                    if "evaluation" in result:
                        step_result["evaluation_score"] = result["evaluation"]["overall_score"]
                    
                    results.append(step_result)
                    
                except Exception as e:
                    results.append({
                        "step": i + 1,
                        "agent_key": step["agent_key"],
                        "message": step["message"],
                        "error": str(e),
                        "duration": time.time() - step_start,
                        "success": False
                    })
            
            # Calculate overall success
            successful_steps = sum(1 for r in results if r["success"])
            overall_success = successful_steps == len(self.steps)
            
            return {
                "workflow_name": self.name,
                "overall_success": overall_success,
                "steps_completed": successful_steps,
                "total_steps": len(self.steps),
                "results": results,
                "total_duration": sum(r["duration"] for r in results)
            }
            
        finally:
            # Teardown
            for action in self.teardown_actions:
                try:
                    await action(env)
                except Exception:
                    pass  # Don't fail the test due to teardown issues
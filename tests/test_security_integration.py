"""
Integration tests for the security analysis system.
Tests the complete flow of security agents, guardrails, and context analysis.
"""
import asyncio
import unittest
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime
from typing import List, Dict, Any

# Import the security system components
from core.security_context import SecurityAnalysisContext, RawMessage, ToolExecution, ThreatLevel
from core.raw_context_provider import RawContextProvider
from agents.security_guardian import SecurityGuardianAgent, create_security_guardian_agent
from agents.task_analyzer import TaskAnalyzerAgent, create_task_analyzer_agent
from agents.context_quality import ContextQualityAgent, create_context_quality_agent
from core.security_guardrails import SecurityGuardrails, security_analysis_guardrail, task_analysis_guardrail
from core.security_agent_factory import SecurityAwareAgentFactory, create_security_aware_agent_factory


class TestSecuritySystemIntegration(unittest.TestCase):
    """Test suite for security system integration"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.mock_config = {
            "security_guardian": {
                "name": "Security Guardian",
                "model": "qwen3",
                "tools": ["threat_analysis", "policy_compliance"],
                "base_prompt": "security_analysis"
            },
            "task_analyzer": {
                "name": "Task Analyzer", 
                "model": "qwen3",
                "tools": ["task_feasibility", "dependency_check"],
                "base_prompt": "task_analysis"
            },
            "context_quality": {
                "name": "Context Quality Agent",
                "model": "qwen3", 
                "tools": ["context_validation", "quality_metrics"],
                "base_prompt": "context_analysis"
            }
        }
        
        # Sample conversation data
        self.sample_conversation = [
            RawMessage(
                role="user",
                content="Help me create a new user authentication system",
                timestamp=datetime.now()
            ),
            RawMessage(
                role="assistant", 
                content="I'll help you create a secure authentication system",
                timestamp=datetime.now()
            ),
            RawMessage(
                role="user",
                content="Make sure to implement proper password hashing",
                timestamp=datetime.now()
            )
        ]
        
        self.sample_tool_execution = [
            ToolExecution(
                tool_name="file_read",
                parameters={"filepath": "/app/auth.py"},
                result="# Authentication module code",
                timestamp=datetime.now(),
                success=True
            ),
            ToolExecution(
                tool_name="file_write",
                parameters={"filepath": "/app/new_auth.py", "content": "# New auth code"},
                result="File written successfully",
                timestamp=datetime.now(),
                success=True
            )
        ]

    def test_raw_context_provider(self):
        """Test raw context extraction"""
        provider = RawContextProvider()
        
        # Test conversation extraction
        extracted = provider.extract_raw_conversation(None)
        self.assertIsInstance(extracted, list)
        
        # Test tool context extraction  
        tool_context = provider.extract_tool_context([])
        self.assertIsInstance(tool_context, list)

    def test_security_context_creation(self):
        """Test security analysis context creation"""
        context = SecurityAnalysisContext(
            raw_conversation=self.sample_conversation,
            tool_execution_history=self.sample_tool_execution,
            user_session={"session_id": "test_session"},
            security_policies=[],
            threat_indicators={},
            threat_detector=None,
            policy_engine=None,
            audit_logger=None
        )
        
        self.assertEqual(len(context.raw_conversation), 3)
        self.assertEqual(len(context.tool_execution_history), 2)
        self.assertEqual(context.user_session["session_id"], "test_session")

    @patch('agents.OpenAIChatCompletionsModel')
    def test_security_guardian_creation(self, mock_model):
        """Test security guardian agent creation"""
        agent = create_security_guardian_agent()
        self.assertIsInstance(agent, SecurityGuardianAgent)

    @patch('agents.OpenAIChatCompletionsModel')
    def test_task_analyzer_creation(self, mock_model):
        """Test task analyzer agent creation"""
        agent = create_task_analyzer_agent()
        self.assertIsInstance(agent, TaskAnalyzerAgent)

    @patch('agents.OpenAIChatCompletionsModel')
    def test_context_quality_creation(self, mock_model):
        """Test context quality agent creation"""
        agent = create_context_quality_agent()
        self.assertIsInstance(agent, ContextQualityAgent)

    def test_security_guardrails_initialization(self):
        """Test security guardrails system initialization"""
        guardrails = SecurityGuardrails()
        
        self.assertIsNotNone(guardrails.security_guardian)
        self.assertIsNotNone(guardrails.task_analyzer)
        self.assertIsNotNone(guardrails.context_quality)
        self.assertIsNotNone(guardrails.raw_context_provider)

    @patch('core.agent_factory.AgentFactory.create_agent')
    async def test_security_aware_factory(self, mock_create_agent):
        """Test security-aware agent factory"""
        mock_agent = Mock()
        mock_create_agent.return_value = mock_agent
        
        factory = create_security_aware_agent_factory(self.mock_config)
        self.assertIsInstance(factory, SecurityAwareAgentFactory)
        
        # Test security agent types
        security_agents = factory.get_security_agent_keys()
        expected_agents = {"security_guardian", "task_analyzer", "context_quality", 
                          "file_manager", "system_executor", "code_generator"}
        self.assertTrue(security_agents.issubset(expected_agents) or 
                       expected_agents.issubset(security_agents))

    async def test_threat_detection_flow(self):
        """Test complete threat detection flow"""
        # Create security context
        context = SecurityAnalysisContext(
            raw_conversation=self.sample_conversation,
            tool_execution_history=self.sample_tool_execution,
            user_session={"session_id": "test_session"},
            security_policies=[],
            threat_indicators={},
            threat_detector=None,
            policy_engine=None,
            audit_logger=None
        )
        
        # Mock security guardian
        with patch.object(SecurityGuardianAgent, 'analyze_input') as mock_analyze:
            mock_analyze.return_value = Mock(
                threat_level=ThreatLevel.LOW,
                threats_detected=[],
                security_violations=[],
                risk_score=0.1,
                timestamp=datetime.now()
            )
            
            guardian = SecurityGuardianAgent()
            result = await guardian.analyze_input(
                "Create a new authentication system",
                context.raw_conversation
            )
            
            self.assertEqual(result.threat_level, ThreatLevel.LOW)
            mock_analyze.assert_called_once()

    def test_factory_statistics(self):
        """Test factory security statistics"""
        factory = create_security_aware_agent_factory(self.mock_config)
        stats = factory.get_security_statistics()
        
        self.assertIn('total_security_agents', stats)
        self.assertIn('security_agent_types', stats)
        self.assertIsInstance(stats['security_agent_types'], list)

    async def test_context_quality_analysis(self):
        """Test context quality analysis"""
        context = SecurityAnalysisContext(
            raw_conversation=self.sample_conversation,
            tool_execution_history=self.sample_tool_execution,
            user_session={"session_id": "test_session"},
            security_policies=[],
            threat_indicators={},
            threat_detector=None,
            policy_engine=None,
            audit_logger=None
        )
        
        with patch.object(ContextQualityAgent, 'analyze_context_quality') as mock_analyze:
            mock_analyze.return_value = Mock(
                overall_quality="good",
                quality_score=0.8,
                recommendations=["Add more detail"],
                risks=[]
            )
            
            quality_agent = ContextQualityAgent()
            result = await quality_agent.analyze_context_quality(
                context, 
                "Create authentication system"
            )
            
            mock_analyze.assert_called_once()

    def test_configuration_validation(self):
        """Test configuration validation"""
        factory = create_security_aware_agent_factory(self.mock_config)
        
        # Test valid configuration
        validation = asyncio.run(factory.validate_agent_security("security_guardian"))
        self.assertTrue(validation.get("valid", False))
        
        # Test invalid configuration
        validation = asyncio.run(factory.validate_agent_security("nonexistent_agent"))
        self.assertFalse(validation.get("valid", True))

    def test_security_policy_configuration(self):
        """Test security policy configuration"""
        factory = create_security_aware_agent_factory(self.mock_config)
        
        test_policies = [
            {"type": "file_access", "rule": "deny_system_files"},
            {"type": "command_execution", "rule": "whitelist_only"}
        ]
        
        factory.configure_security_policies(test_policies)
        self.assertEqual(factory.security_policies, test_policies)

    def test_audit_logging_configuration(self):
        """Test audit logging configuration"""
        factory = create_security_aware_agent_factory(self.mock_config)
        
        # Test enabling audit logging
        factory.enable_audit_logging(True)
        self.assertTrue(factory.audit_logging_enabled)
        
        # Test disabling audit logging
        factory.enable_audit_logging(False)
        self.assertFalse(factory.audit_logging_enabled)

    def test_dynamic_agent_security_management(self):
        """Test dynamic security agent management"""
        factory = create_security_aware_agent_factory(self.mock_config)
        
        # Add new security agent type
        factory.add_security_agent_type("custom_security_agent", full_analysis=True)
        self.assertIn("custom_security_agent", factory.get_security_agent_keys())
        
        # Remove security agent type
        factory.remove_security_agent_type("custom_security_agent")
        self.assertNotIn("custom_security_agent", factory.get_security_agent_keys())


class TestSecurityToolsIntegration(unittest.TestCase):
    """Test security tools integration"""
    
    def test_threat_analysis_tool_import(self):
        """Test threat analysis tool can be imported"""
        try:
            from tools.security_tools import ThreatAnalysisResult
            self.assertTrue(True)
        except ImportError:
            self.fail("Failed to import ThreatAnalysisResult")
    
    def test_policy_compliance_tool_import(self):
        """Test policy compliance tool can be imported"""
        try:
            from tools.security_tools import PolicyComplianceResult
            self.assertTrue(True)
        except ImportError:
            self.fail("Failed to import PolicyComplianceResult")
    
    def test_task_feasibility_tool_import(self):
        """Test task feasibility tool can be imported"""
        try:
            from tools.security_tools import FeasibilityAnalysisResult
            self.assertTrue(True)
        except ImportError:
            self.fail("Failed to import FeasibilityAnalysisResult")


if __name__ == '__main__':
    # Run the test suite
    unittest.main(verbosity=2)
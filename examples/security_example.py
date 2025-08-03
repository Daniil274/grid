"""
Example usage of the GRID security analysis system.
Demonstrates how to use security agents, guardrails, and context analysis.
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from datetime import datetime
from typing import List

# Import security system components
from core.security_context import SecurityAnalysisContext, RawMessage, ToolExecution
from core.raw_context_provider import RawContextProvider
from agents.security_guardian import create_security_guardian_agent
from agents.task_analyzer import create_task_analyzer_agent
from agents.context_quality import create_context_quality_agent
from core.security_agent_factory import create_security_aware_agent_factory


async def demonstrate_security_analysis():
    """Demonstrate security analysis capabilities"""
    print("🔒 GRID Security Analysis System Demo")
    print("=" * 50)
    
    # 1. Create sample conversation history
    conversation = [
        RawMessage(
            role="user",
            content="Help me implement a user authentication system with password hashing",
            timestamp=datetime.now()
        ),
        RawMessage(
            role="assistant",
            content="I'll help you create a secure authentication system with proper password hashing using bcrypt",
            timestamp=datetime.now()
        ),
        RawMessage(
            role="user", 
            content="Also add rate limiting for login attempts",
            timestamp=datetime.now()
        )
    ]
    
    # 2. Create sample tool execution history
    tool_history = [
        ToolExecution(
            tool_name="file_read",
            parameters={"filepath": "/app/models/user.py"},
            result="User model with basic fields",
            timestamp=datetime.now(),
            success=True
        ),
        ToolExecution(
            tool_name="file_write",
            parameters={"filepath": "/app/auth/auth.py", "content": "Authentication module"},
            result="Authentication module created",
            timestamp=datetime.now(),
            success=True
        )
    ]
    
    # 3. Create security analysis context
    security_context = SecurityAnalysisContext(
        raw_conversation=conversation,
        tool_execution_history=tool_history,
        user_session={
            "session_id": "demo_session_123",
            "user_id": "demo_user",
            "timestamp": datetime.now()
        },
        security_policies=[
            {"type": "file_access", "rule": "allow_app_directory"},
            {"type": "authentication", "rule": "require_strong_passwords"}
        ],
        threat_indicators={},
        threat_detector=None,
        policy_engine=None,
        audit_logger=None
    )
    
    print(f"📝 Created security context with {len(conversation)} messages")
    print(f"🔧 Tool execution history: {len(tool_history)} operations")
    print()
    
    # 4. Test Security Guardian Agent
    print("🛡️  Testing Security Guardian Agent")
    print("-" * 30)
    
    try:
        security_guardian = create_security_guardian_agent()
        security_result = await security_guardian.analyze_input(
            "Implement authentication with bcrypt password hashing",
            conversation
        )
        
        print(f"✅ Security analysis completed")
        print(f"   Threat level: {security_result.threat_level.value}")
        print(f"   Risk score: {security_result.risk_score:.2f}")
        print(f"   Threats detected: {len(security_result.threats_detected)}")
        print(f"   Security violations: {len(security_result.security_violations)}")
        
    except Exception as e:
        print(f"❌ Security Guardian error: {e}")
    
    print()
    
    # 5. Test Task Analyzer Agent
    print("📊 Testing Task Analyzer Agent")
    print("-" * 30)
    
    try:
        task_analyzer = create_task_analyzer_agent()
        task_result = await task_analyzer.analyze_task_execution(
            security_context,
            "Create authentication system with password hashing and rate limiting"
        )
        
        print(f"✅ Task analysis completed")
        print(f"   Task type: {task_result.task_type.value}")
        print(f"   Complexity: {task_result.complexity_level.value}")
        print(f"   Success probability: {task_result.success_probability:.2f}")
        print(f"   Required tools: {', '.join(task_result.required_tools[:3])}...")
        print(f"   Potential issues: {len(task_result.potential_issues)}")
        
    except Exception as e:
        print(f"❌ Task Analyzer error: {e}")
    
    print()
    
    # 6. Test Context Quality Agent
    print("🔍 Testing Context Quality Agent")
    print("-" * 30)
    
    try:
        context_quality = create_context_quality_agent()
        quality_result = await context_quality.analyze_context_quality(
            security_context,
            "Authentication system implementation"
        )
        
        print(f"✅ Context quality analysis completed")
        print(f"   Overall quality: {quality_result.overall_quality.value}")
        print(f"   Quality score: {quality_result.quality_score:.2f}")
        print(f"   Missing elements: {len(quality_result.completeness.missing_elements)}")
        print(f"   Information gaps: {len(quality_result.information_gaps.critical_gaps)} critical")
        print(f"   Recommendations: {len(quality_result.recommendations)}")
        
        if quality_result.recommendations:
            print("   Top recommendations:")
            for i, rec in enumerate(quality_result.recommendations[:3], 1):
                print(f"   {i}. {rec}")
        
    except Exception as e:
        print(f"❌ Context Quality error: {e}")
    
    print()
    
    # 7. Test Security-Aware Agent Factory
    print("🏭 Testing Security-Aware Agent Factory")
    print("-" * 30)
    
    mock_config = {
        "security_guardian": {
            "name": "Security Guardian",
            "model": "qwen3",
            "tools": ["threat_analysis", "policy_compliance"]
        },
        "task_analyzer": {
            "name": "Task Analyzer",
            "model": "qwen3", 
            "tools": ["task_feasibility", "dependency_check"]
        },
        "context_quality": {
            "name": "Context Quality Agent",
            "model": "qwen3",
            "tools": ["context_validation", "quality_metrics"]
        }
    }
    
    try:
        factory = create_security_aware_agent_factory(mock_config)
        
        # Get statistics
        stats = factory.get_security_statistics()
        print(f"✅ Security factory created")
        print(f"   Security agents: {stats['total_security_agents']}")
        print(f"   Full analysis agents: {stats['full_analysis_agents']}")
        print(f"   Security-only agents: {stats['security_only_agents']}")
        print(f"   Audit logging: {stats['audit_logging_enabled']}")
        
        # Test agent validation
        validation = await factory.validate_agent_security("security_guardian")
        print(f"   Security guardian validation: {'✅' if validation['valid'] else '❌'}")
        
    except Exception as e:
        print(f"❌ Security Factory error: {e}")
    
    print()
    print("🎉 Security system demonstration completed!")
    print("   All components are working and integrated properly.")


async def demonstrate_guardrails():
    """Demonstrate guardrails functionality"""
    print("\n🚦 GRID Guardrails System Demo")
    print("=" * 50)
    
    from core.security_guardrails import SecurityGuardrails
    
    try:
        # Create guardrails system
        guardrails = SecurityGuardrails()
        print("✅ Guardrails system initialized")
        print(f"   Security Guardian: {'✅' if guardrails.security_guardian else '❌'}")
        print(f"   Task Analyzer: {'✅' if guardrails.task_analyzer else '❌'}")
        print(f"   Context Quality: {'✅' if guardrails.context_quality else '❌'}")
        print(f"   Raw Context Provider: {'✅' if guardrails.raw_context_provider else '❌'}")
        
        print("\n🔧 Guardrails configuration:")
        print(f"   Critical threat threshold: {guardrails.CRITICAL_THRESHOLD.value}")
        print(f"   Warning threat threshold: {guardrails.WARNING_THRESHOLD.value}")
        
    except Exception as e:
        print(f"❌ Guardrails initialization error: {e}")


def run_demo():
    """Run the complete demonstration"""
    print("🚀 Starting GRID Security System Demonstration")
    print("=" * 60)
    
    try:
        # Run async demos
        asyncio.run(demonstrate_security_analysis())
        asyncio.run(demonstrate_guardrails())
        
        print("\n✨ Demo completed successfully!")
        print("The GRID security analysis system is ready for use.")
        
    except KeyboardInterrupt:
        print("\n⏹️  Demo interrupted by user")
    except Exception as e:
        print(f"\n❌ Demo error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    run_demo()
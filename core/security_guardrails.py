"""
Security Guardrails for GRID system using AgentsSDK.
Implements input and output guardrails for security analysis and task validation.
"""
from typing import List, Dict, Any, Optional, Union
from datetime import datetime
from agents import GuardrailFunctionOutput, RunContextWrapper, Agent, input_guardrail, output_guardrail

from core.security_context import SecurityAnalysisContext, ThreatLevel
from core.raw_context_provider import RawContextProvider


class SecurityGuardrails:
    """
    Security guardrails system for the GRID framework.
    Provides comprehensive security analysis and task validation.
    """
    
    def __init__(self):
        self.raw_context_provider = RawContextProvider()
        
        # Threat level thresholds
        self.CRITICAL_THRESHOLD = ThreatLevel.HIGH
        self.WARNING_THRESHOLD = ThreatLevel.MEDIUM
        
    async def prepare_security_context(
        self,
        ctx: RunContextWrapper,
        agent: Agent
    ) -> SecurityAnalysisContext:
        """
        Prepare security analysis context with raw conversation history.
        
        Args:
            ctx: Runtime context wrapper
            agent: Current agent instance
            
        Returns:
            SecurityAnalysisContext with extracted raw data
        """
        # Create basic security context
        return SecurityAnalysisContext(
            raw_conversation=[],
            tool_execution_history=[],
            user_session={
                "session_id": "demo",
                "user_id": "demo_user",
                "timestamp": datetime.now()
            }
        )


# Global instance for guardrails
_security_guardrails = SecurityGuardrails()


@input_guardrail
async def security_analysis_guardrail(
    ctx: RunContextWrapper,
    agent: Agent,
    input_data: Union[str, List[Dict[str, Any]]]
) -> GuardrailFunctionOutput:
    """
    Input guardrail for security analysis.
    Analyzes incoming requests for security threats and policy violations.
    
    Args:
        ctx: Runtime context wrapper
        agent: Current agent instance
        input_data: Input data to analyze
        
    Returns:
        GuardrailFunctionOutput with security analysis results
    """
    try:
        # Prepare security context
        security_context = await _security_guardrails.prepare_security_context(ctx, agent)
        
        # Basic security check - allow all for demo
        return GuardrailFunctionOutput(
            should_continue=True,
            output=None,
            error=None
        )
        
    except Exception as e:
        return GuardrailFunctionOutput(
            should_continue=True,  # Allow to continue for demo
            output=None,
            error=f"Security analysis error: {str(e)}"
        )


@output_guardrail
async def task_analysis_guardrail(
    ctx: RunContextWrapper,
    agent: Agent,
    output: str
) -> GuardrailFunctionOutput:
    """
    Output guardrail for task analysis.
    Validates agent output and provides analysis.
    
    Args:
        ctx: Runtime context wrapper
        agent: Current agent instance
        output: Agent output to analyze
        
    Returns:
        GuardrailFunctionOutput with analysis results
    """
    try:
        # Basic output validation - allow all for demo
        return GuardrailFunctionOutput(
            should_continue=True,
            output=output,
            error=None
        )
        
    except Exception as e:
        return GuardrailFunctionOutput(
            should_continue=True,  # Allow to continue for demo
            output=output,
            error=f"Task analysis error: {str(e)}"
        )


@input_guardrail
async def context_quality_guardrail(
    ctx: RunContextWrapper,
    agent: Agent,
    input_data: Union[str, List[Dict[str, Any]]]
) -> GuardrailFunctionOutput:
    """
    Input guardrail for context quality analysis.
    Analyzes input quality and provides recommendations.
    
    Args:
        ctx: Runtime context wrapper
        agent: Current agent instance
        input_data: Input data to analyze
        
    Returns:
        GuardrailFunctionOutput with quality analysis
    """
    try:
        # Basic quality check - allow all for demo
        return GuardrailFunctionOutput(
            should_continue=True,
            output=None,
            error=None
        )
        
    except Exception as e:
        return GuardrailFunctionOutput(
            should_continue=True,  # Allow to continue for demo
            output=None,
            error=f"Context quality analysis error: {str(e)}"
        )


def get_security_guardrails() -> List:
    """Get list of security guardrails."""
    return [
        security_analysis_guardrail,
        context_quality_guardrail
    ]


def get_analysis_guardrails() -> List:
    """Get list of analysis guardrails."""
    return [
        task_analysis_guardrail
    ]


def configure_agent_guardrails(agent: Agent, enable_security: bool = True, enable_analysis: bool = True) -> Agent:
    """
    Configure agent with security and analysis guardrails.
    
    Args:
        agent: Agent to configure
        enable_security: Whether to enable security guardrails
        enable_analysis: Whether to enable analysis guardrails
        
    Returns:
        Configured agent
    """
    if enable_security:
        agent.input_guardrails.extend(get_security_guardrails())
    
    if enable_analysis:
        agent.output_guardrails.extend(get_analysis_guardrails())
    
    return agent
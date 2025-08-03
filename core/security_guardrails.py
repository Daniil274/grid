"""
Security Guardrails for GRID system using AgentsSDK.
Implements input and output guardrails for security analysis and task validation.
"""
from typing import List, Dict, Any, Optional, Union
from datetime import datetime
from agents import GuardrailFunctionOutput, RunContextWrapper, Agent, input_guardrail, output_guardrail
from agents.types import TResponse, TResponseInputItem

from core.security_context import SecurityAnalysisContext, ThreatLevel
from agents.security_guardian import SecurityGuardianAgent, create_security_guardian_agent
from agents.task_analyzer import TaskAnalyzerAgent, create_task_analyzer_agent
from agents.context_quality import ContextQualityAgent, create_context_quality_agent
from core.raw_context_provider import RawContextProvider


class SecurityGuardrails:
    """
    Security guardrails system for the GRID framework.
    Provides comprehensive security analysis and task validation.
    """
    
    def __init__(self):
        self.security_guardian = create_security_guardian_agent()
        self.task_analyzer = create_task_analyzer_agent()
        self.context_quality = create_context_quality_agent()
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
        # Extract raw conversation without agent prompts
        raw_conversation = self.raw_context_provider.extract_raw_conversation(
            ctx.context if hasattr(ctx, 'context') else None
        )
        
        # Extract tool execution history
        tool_execution_history = self.raw_context_provider.extract_tool_context(
            ctx.context.execution_history if hasattr(ctx.context, 'execution_history') else []
        )
        
        # Create security context
        return SecurityAnalysisContext(
            raw_conversation=raw_conversation,
            tool_execution_history=tool_execution_history,
            user_session={
                "session_id": getattr(ctx.context, 'session_id', 'unknown'),
                "user_id": getattr(ctx.context, 'user_id', 'unknown'),
                "timestamp": datetime.now()
            },
            security_policies=[],  # To be loaded from configuration
            threat_indicators={},
            threat_detector=self.security_guardian,
            policy_engine=None,  # To be implemented
            audit_logger=None    # To be implemented
        )


# Global instance for guardrails
_security_guardrails = SecurityGuardrails()


@input_guardrail
async def security_analysis_guardrail(
    ctx: RunContextWrapper,
    agent: Agent,
    input_data: Union[str, List[TResponseInputItem]]
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
        
        # Convert input to string for analysis
        input_text = ""
        if isinstance(input_data, str):
            input_text = input_data
        elif isinstance(input_data, list):
            input_text = " ".join(
                item.text if hasattr(item, 'text') else str(item) 
                for item in input_data
            )
        
        # Run security analysis
        security_result = await _security_guardrails.security_guardian.analyze_input(
            input_text, 
            security_context.raw_conversation
        )
        
        # Check threat level
        threat_triggered = security_result.threat_level.value >= _security_guardrails.CRITICAL_THRESHOLD.value
        
        # Prepare output info
        output_info = {
            "security_analysis": {
                "threat_level": security_result.threat_level.value,
                "threats_detected": [threat.threat_type for threat in security_result.threats_detected],
                "security_violations": security_result.security_violations,
                "risk_score": security_result.risk_score,
                "analysis_timestamp": security_result.timestamp.isoformat()
            }
        }
        
        # Add warning if medium threat
        if security_result.threat_level == ThreatLevel.MEDIUM:
            output_info["security_warning"] = "Medium security risk detected"
        
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=threat_triggered
        )
        
    except Exception as e:
        # Return safe failure state
        return GuardrailFunctionOutput(
            output_info={
                "security_analysis_error": str(e),
                "fallback_action": "Allow with caution"
            },
            tripwire_triggered=False  # Allow execution but log error
        )


@output_guardrail
async def task_analysis_guardrail(
    ctx: RunContextWrapper,
    agent: Agent,
    output: TResponse
) -> GuardrailFunctionOutput:
    """
    Output guardrail for task analysis and quality validation.
    Analyzes task execution results and context quality.
    
    Args:
        ctx: Runtime context wrapper
        agent: Current agent instance
        output: Agent output to analyze
        
    Returns:
        GuardrailFunctionOutput with task analysis results
    """
    try:
        # Prepare security context
        security_context = await _security_guardrails.prepare_security_context(ctx, agent)
        
        # Extract output text
        output_text = ""
        if hasattr(output, 'content'):
            output_text = str(output.content)
        elif hasattr(output, 'text'):
            output_text = str(output.text)
        else:
            output_text = str(output)
        
        # Run task analysis
        task_analysis = await _security_guardrails.task_analyzer.analyze_task_execution(
            security_context,
            output_text
        )
        
        # Run context quality analysis
        context_quality = await _security_guardrails.context_quality.analyze_context_quality(
            security_context,
            output_text
        )
        
        # Prepare comprehensive output info
        output_info = {
            "task_analysis": {
                "task_type": task_analysis.task_type.value,
                "complexity_level": task_analysis.complexity_level.value,
                "estimated_duration": task_analysis.estimated_duration.total_seconds(),
                "success_probability": task_analysis.success_probability,
                "required_tools": task_analysis.required_tools,
                "potential_issues": task_analysis.potential_issues,
                "analysis_timestamp": task_analysis.timestamp.isoformat()
            },
            "context_quality": {
                "overall_quality": context_quality.overall_quality.value,
                "quality_score": context_quality.quality_score,
                "missing_elements": context_quality.completeness.missing_elements,
                "critical_gaps": context_quality.information_gaps.critical_gaps,
                "recommendations": context_quality.recommendations,
                "risks": context_quality.risks
            }
        }
        
        # Check if task analysis indicates problems
        tripwire_triggered = (
            task_analysis.success_probability < 0.3 or
            len(context_quality.information_gaps.critical_gaps) > 0 or
            context_quality.quality_score < 0.4
        )
        
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=tripwire_triggered
        )
        
    except Exception as e:
        # Return safe failure state
        return GuardrailFunctionOutput(
            output_info={
                "task_analysis_error": str(e),
                "fallback_action": "Continue with monitoring"
            },
            tripwire_triggered=False  # Allow continuation but log error
        )


@input_guardrail
async def context_quality_guardrail(
    ctx: RunContextWrapper,
    agent: Agent,
    input_data: Union[str, List[TResponseInputItem]]
) -> GuardrailFunctionOutput:
    """
    Additional input guardrail for context quality validation.
    Ensures sufficient context quality for task execution.
    
    Args:
        ctx: Runtime context wrapper
        agent: Current agent instance
        input_data: Input data to analyze
        
    Returns:
        GuardrailFunctionOutput with context quality results
    """
    try:
        # Prepare security context
        security_context = await _security_guardrails.prepare_security_context(ctx, agent)
        
        # Convert input to string
        input_text = ""
        if isinstance(input_data, str):
            input_text = input_data
        elif isinstance(input_data, list):
            input_text = " ".join(
                item.text if hasattr(item, 'text') else str(item) 
                for item in input_data
            )
        
        # Analyze context quality
        quality_report = await _security_guardrails.context_quality.analyze_context_quality(
            security_context,
            input_text
        )
        
        # Determine if context is insufficient
        insufficient_context = (
            quality_report.quality_score < 0.3 or
            len(quality_report.information_gaps.critical_gaps) > 0
        )
        
        output_info = {
            "context_quality_check": {
                "quality_level": quality_report.overall_quality.value,
                "quality_score": quality_report.quality_score,
                "recommendations": quality_report.recommendations,
                "suggested_questions": quality_report.information_gaps.suggested_questions
            }
        }
        
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=insufficient_context
        )
        
    except Exception as e:
        # Return safe failure state
        return GuardrailFunctionOutput(
            output_info={
                "context_quality_error": str(e)
            },
            tripwire_triggered=False
        )


# Utility functions for guardrail configuration
def get_security_guardrails() -> List:
    """Get list of security input guardrails"""
    return [security_analysis_guardrail, context_quality_guardrail]


def get_analysis_guardrails() -> List:
    """Get list of analysis output guardrails"""
    return [task_analysis_guardrail]


def configure_agent_guardrails(agent: Agent, enable_security: bool = True, enable_analysis: bool = True) -> Agent:
    """
    Configure agent with appropriate guardrails.
    
    Args:
        agent: Agent to configure
        enable_security: Enable security input guardrails
        enable_analysis: Enable analysis output guardrails
        
    Returns:
        Agent with configured guardrails
    """
    input_guardrails = []
    output_guardrails = []
    
    if enable_security:
        input_guardrails.extend(get_security_guardrails())
    
    if enable_analysis:
        output_guardrails.extend(get_analysis_guardrails())
    
    return agent.clone(
        input_guardrails=input_guardrails,
        output_guardrails=output_guardrails
    )


# Export main components
__all__ = [
    "SecurityGuardrails",
    "security_analysis_guardrail",
    "task_analysis_guardrail", 
    "context_quality_guardrail",
    "get_security_guardrails",
    "get_analysis_guardrails",
    "configure_agent_guardrails"
]
"""
Security analysis tools for the GRID system.
Provides threat analysis, policy compliance, and task feasibility tools.
"""

from typing import Dict, List, Any, Literal
from pydantic import BaseModel, Field
from datetime import datetime

from agents import function_tool, RunContextWrapper
from core.security_context import (
    SecurityAnalysisContext, SecurityEvent, ThreatLevel, 
    SecurityEventType, ThreatIndicator, SecurityPolicy
)


class ThreatAnalysisResult(BaseModel):
    """Result of threat analysis."""
    threat_level: ThreatLevel
    threats_detected: List[Dict[str, Any]]
    risk_score: float = Field(ge=0.0, le=10.0)
    recommendations: List[str]
    analysis_timestamp: datetime = Field(default_factory=datetime.now)


class PolicyComplianceResult(BaseModel):
    """Result of policy compliance check."""
    compliant: bool
    violations: List[Dict[str, Any]]
    compliance_score: float = Field(ge=0.0, le=1.0)
    policy_recommendations: List[str]
    check_timestamp: datetime = Field(default_factory=datetime.now)


class FeasibilityAnalysisResult(BaseModel):
    """Result of task feasibility analysis."""
    feasible: bool
    feasibility_score: float = Field(ge=0.0, le=1.0)
    required_tools: List[str]
    missing_dependencies: List[str]
    estimated_complexity: Literal["low", "medium", "high", "very_high"]
    alternative_approaches: List[str]
    analysis_timestamp: datetime = Field(default_factory=datetime.now)


class ContextQualityResult(BaseModel):
    """Result of context quality analysis."""
    quality_score: float = Field(ge=0.0, le=1.0)
    completeness: float = Field(ge=0.0, le=1.0)
    coherence: float = Field(ge=0.0, le=1.0)
    issues: List[str]
    recommendations: List[str]
    analysis_timestamp: datetime = Field(default_factory=datetime.now)


@function_tool
async def threat_analysis_tool(
    ctx: RunContextWrapper[SecurityAnalysisContext],
    content: str,
    analysis_type: Literal["command", "file_access", "network", "general"] = "general"
) -> ThreatAnalysisResult:
    """
    Analyze content for security threats and vulnerabilities.
    
    Args:
        content: Content to analyze for threats
        analysis_type: Type of analysis to perform
        
    Returns:
        Detailed threat analysis result
    """
    security_context = ctx.context
    
    # Perform threat detection
    threats = []
    if security_context.threat_detector:
        detected_events = await security_context.threat_detector.analyze_content(
            content, 
            {"analysis_type": analysis_type}
        )
        threats.extend([{
            "event_id": event.event_id,
            "type": event.event_type.value,
            "level": event.threat_level.value,
            "description": event.description,
            "details": event.details
        } for event in detected_events])
    
    # Additional analysis based on type
    if analysis_type == "command":
        command_threats = _analyze_command_threats(content)
        threats.extend(command_threats)
    elif analysis_type == "file_access":
        file_threats = _analyze_file_access_threats(content)
        threats.extend(file_threats)
    elif analysis_type == "network":
        network_threats = _analyze_network_threats(content)
        threats.extend(network_threats)
    
    # Calculate risk score
    risk_score = _calculate_risk_score(threats)
    
    # Determine overall threat level
    if risk_score >= 8.0:
        threat_level = ThreatLevel.CRITICAL
    elif risk_score >= 6.0:
        threat_level = ThreatLevel.HIGH
    elif risk_score >= 4.0:
        threat_level = ThreatLevel.MEDIUM
    elif risk_score >= 2.0:
        threat_level = ThreatLevel.LOW
    else:
        threat_level = ThreatLevel.NONE
    
    # Generate recommendations
    recommendations = _generate_threat_recommendations(threats, analysis_type)
    
    # Log security events
    if security_context.audit_logger and threats:
        security_events = []
        for threat in threats:
            event = SecurityEvent(
                event_id=f"analysis_{datetime.now().timestamp()}",
                event_type=SecurityEventType.THREAT_DETECTED,
                threat_level=ThreatLevel(threat.get("level", "low")),
                timestamp=datetime.now(),
                source="threat_analysis_tool",
                description=threat.get("description", "Threat detected"),
                details=threat
            )
            security_events.append(event)
        
        await security_context.audit_logger.log_multiple_events(security_events)
    
    return ThreatAnalysisResult(
        threat_level=threat_level,
        threats_detected=threats,
        risk_score=risk_score,
        recommendations=recommendations
    )


@function_tool
async def policy_compliance_check(
    ctx: RunContextWrapper[SecurityAnalysisContext],
    action: str,
    resources: List[str]
) -> PolicyComplianceResult:
    """
    Check if action complies with security policies.
    
    Args:
        action: Action to check for compliance
        resources: Resources involved in the action
        
    Returns:
        Policy compliance analysis result
    """
    security_context = ctx.context
    
    violations = []
    if security_context.policy_engine:
        violation_events = await security_context.policy_engine.check_compliance(
            action, 
            resources,
            {"check_timestamp": datetime.now()}
        )
        
        violations.extend([{
            "policy_id": event.details.get("policy_id"),
            "rule": event.details.get("rule"),
            "severity": event.threat_level.value,
            "description": event.description
        } for event in violation_events])
    
    # Additional compliance checks
    additional_violations = _additional_compliance_checks(action, resources)
    violations.extend(additional_violations)
    
    # Calculate compliance score
    compliance_score = _calculate_compliance_score(violations)
    compliant = compliance_score >= 0.8 and len(violations) == 0
    
    # Generate policy recommendations
    recommendations = _generate_policy_recommendations(violations, action)
    
    # Log violations
    if security_context.audit_logger and violations:
        security_events = []
        for violation in violations:
            event = SecurityEvent(
                event_id=f"compliance_{datetime.now().timestamp()}",
                event_type=SecurityEventType.POLICY_VIOLATION,
                threat_level=ThreatLevel(violation.get("severity", "medium")),
                timestamp=datetime.now(),
                source="policy_compliance_tool",
                description=violation.get("description", "Policy violation detected"),
                details=violation
            )
            security_events.append(event)
        
        await security_context.audit_logger.log_multiple_events(security_events)
    
    return PolicyComplianceResult(
        compliant=compliant,
        violations=violations,
        compliance_score=compliance_score,
        policy_recommendations=recommendations
    )


@function_tool
async def task_feasibility_analysis(
    ctx: RunContextWrapper[SecurityAnalysisContext],
    task_description: str,
    available_tools: List[str]
) -> FeasibilityAnalysisResult:
    """
    Analyze feasibility of task execution.
    
    Args:
        task_description: Description of task to analyze
        available_tools: List of available tools
        
    Returns:
        Task feasibility analysis result
    """
    security_context = ctx.context
    
    # Analyze task requirements
    required_tools = _extract_required_tools(task_description)
    missing_dependencies = [tool for tool in required_tools if tool not in available_tools]
    
    # Calculate feasibility score
    feasibility_score = _calculate_feasibility_score(
        task_description, 
        required_tools, 
        missing_dependencies,
        security_context
    )
    
    # Determine complexity
    complexity = _estimate_complexity(task_description, required_tools)
    
    # Check if task is feasible
    feasible = (
        feasibility_score >= 0.6 and 
        len(missing_dependencies) == 0 and
        complexity != "very_high"
    )
    
    # Generate alternative approaches
    alternatives = _generate_alternative_approaches(
        task_description, 
        available_tools, 
        missing_dependencies
    )
    
    return FeasibilityAnalysisResult(
        feasible=feasible,
        feasibility_score=feasibility_score,
        required_tools=required_tools,
        missing_dependencies=missing_dependencies,
        estimated_complexity=complexity,
        alternative_approaches=alternatives
    )


@function_tool
async def context_quality_analysis(
    ctx: RunContextWrapper[SecurityAnalysisContext]
) -> ContextQualityResult:
    """
    Analyze quality and completeness of security context.
    
    Returns:
        Context quality analysis result
    """
    security_context = ctx.context
    
    # Import here to avoid circular dependencies
    from core.raw_context_provider import RawContextProvider
    provider = RawContextProvider()
    
    # Get quality metrics
    metrics = provider.get_context_quality_metrics(security_context)
    
    # Calculate overall quality score
    quality_score = (
        metrics["context_completeness"] * 0.3 +
        metrics["temporal_coherence"] * 0.2 +
        metrics["content_quality"] * 0.3 +
        min(metrics["conversation_depth"], 1.0) * 0.2
    )
    
    # Identify issues
    issues = []
    recommendations = []
    
    if metrics["context_completeness"] < 0.5:
        issues.append("Incomplete conversation context")
        recommendations.append("Ensure both user input and assistant responses are present")
    
    if metrics["temporal_coherence"] < 0.5:
        issues.append("Poor temporal coherence in conversation")
        recommendations.append("Check for reasonable time gaps between messages")
    
    if metrics["content_quality"] < 0.5:
        issues.append("Low content quality")
        recommendations.append("Ensure messages contain sufficient contextual information")
    
    if metrics["message_count"] < 2:
        issues.append("Insufficient conversation history")
        recommendations.append("More conversation turns needed for comprehensive analysis")
    
    if metrics["tool_execution_count"] == 0:
        issues.append("No tool usage recorded")
        recommendations.append("Tool execution history helps improve security analysis")
    
    return ContextQualityResult(
        quality_score=quality_score,
        completeness=metrics["context_completeness"],
        coherence=metrics["temporal_coherence"],
        issues=issues,
        recommendations=recommendations
    )


# Helper functions

def _analyze_command_threats(content: str) -> List[Dict[str, Any]]:
    """Analyze command-specific threats."""
    threats = []
    content_lower = content.lower()
    
    # Dangerous commands
    dangerous_commands = {
        "rm -rf": {"level": "critical", "description": "Recursive force deletion command"},
        "sudo rm": {"level": "high", "description": "Privileged deletion command"},
        "chmod 777": {"level": "medium", "description": "Overly permissive file permissions"},
        "curl | sh": {"level": "high", "description": "Remote script execution"},
        "wget | bash": {"level": "high", "description": "Remote script execution"},
        "dd if=": {"level": "high", "description": "Low-level disk operations"}
    }
    
    for cmd, info in dangerous_commands.items():
        if cmd in content_lower:
            threats.append({
                "type": "dangerous_command",
                "level": info["level"],
                "description": f"Dangerous command detected: {info['description']}",
                "pattern": cmd
            })
    
    return threats


def _analyze_file_access_threats(content: str) -> List[Dict[str, Any]]:
    """Analyze file access threats."""
    threats = []
    content_lower = content.lower()
    
    # Sensitive file patterns
    sensitive_paths = {
        "/etc/passwd": {"level": "high", "description": "System password file access"},
        "/etc/shadow": {"level": "critical", "description": "System shadow password file"},
        "~/.ssh": {"level": "high", "description": "SSH key directory access"},
        "/root": {"level": "high", "description": "Root directory access"},
        ".env": {"level": "medium", "description": "Environment variables file"}
    }
    
    for path, info in sensitive_paths.items():
        if path in content_lower:
            threats.append({
                "type": "sensitive_file_access",
                "level": info["level"],
                "description": f"Sensitive file access: {info['description']}",
                "path": path
            })
    
    return threats


def _analyze_network_threats(content: str) -> List[Dict[str, Any]]:
    """Analyze network-related threats."""
    threats = []
    content_lower = content.lower()
    
    # Network threat patterns
    network_patterns = {
        "nc -l": {"level": "high", "description": "Network listener creation"},
        "nmap": {"level": "medium", "description": "Network scanning tool"},
        "telnet": {"level": "medium", "description": "Unencrypted network protocol"},
        "ftp": {"level": "low", "description": "Unencrypted file transfer"}
    }
    
    for pattern, info in network_patterns.items():
        if pattern in content_lower:
            threats.append({
                "type": "network_threat",
                "level": info["level"],
                "description": f"Network threat: {info['description']}",
                "pattern": pattern
            })
    
    return threats


def _calculate_risk_score(threats: List[Dict[str, Any]]) -> float:
    """Calculate overall risk score from threats."""
    if not threats:
        return 0.0
    
    level_scores = {
        "none": 0.0,
        "low": 2.0,
        "medium": 4.0,
        "high": 7.0,
        "critical": 10.0
    }
    
    total_score = sum(level_scores.get(threat.get("level", "low"), 2.0) for threat in threats)
    max_possible = len(threats) * 10.0
    
    return min(total_score / max_possible * 10.0, 10.0) if max_possible > 0 else 0.0


def _generate_threat_recommendations(threats: List[Dict[str, Any]], analysis_type: str) -> List[str]:
    """Generate threat mitigation recommendations."""
    recommendations = []
    
    if not threats:
        recommendations.append("No immediate threats detected. Continue monitoring.")
        return recommendations
    
    threat_levels = [threat.get("level", "low") for threat in threats]
    
    if "critical" in threat_levels:
        recommendations.append("CRITICAL: Immediate action required - block execution and review security policies")
    
    if "high" in threat_levels:
        recommendations.append("HIGH: Carefully review and validate actions before execution")
    
    if analysis_type == "command":
        recommendations.append("Validate all commands against approved command list")
        recommendations.append("Consider using restricted shell environment")
    
    if analysis_type == "file_access":
        recommendations.append("Implement file access controls and monitoring")
        recommendations.append("Review file permissions and ownership")
    
    if analysis_type == "network":
        recommendations.append("Monitor network connections and traffic")
        recommendations.append("Use encrypted protocols where possible")
    
    return recommendations


def _additional_compliance_checks(action: str, resources: List[str]) -> List[Dict[str, Any]]:
    """Perform additional compliance checks."""
    violations = []
    action_lower = action.lower()
    
    # Check for privileged operations
    if any(word in action_lower for word in ["sudo", "root", "admin", "privilege"]):
        violations.append({
            "type": "privilege_escalation",
            "severity": "high",
            "description": "Privileged operation detected"
        })
    
    # Check for system modifications
    if any(word in action_lower for word in ["install", "update", "modify", "delete"]):
        system_resources = [r for r in resources if any(
            sys_path in str(r).lower() 
            for sys_path in ["/etc", "/usr", "/bin", "/sbin", "/root"]
        )]
        if system_resources:
            violations.append({
                "type": "system_modification",
                "severity": "medium",
                "description": "System file modification detected"
            })
    
    return violations


def _calculate_compliance_score(violations: List[Dict[str, Any]]) -> float:
    """Calculate compliance score based on violations."""
    if not violations:
        return 1.0
    
    severity_weights = {
        "low": 0.1,
        "medium": 0.3,
        "high": 0.6,
        "critical": 1.0
    }
    
    total_penalty = sum(
        severity_weights.get(v.get("severity", "medium"), 0.3) 
        for v in violations
    )
    
    return max(0.0, 1.0 - (total_penalty / len(violations)))


def _generate_policy_recommendations(violations: List[Dict[str, Any]], action: str) -> List[str]:
    """Generate policy compliance recommendations."""
    recommendations = []
    
    if not violations:
        recommendations.append("Action complies with all security policies")
        return recommendations
    
    violation_types = [v.get("type", "unknown") for v in violations]
    
    if "privilege_escalation" in violation_types:
        recommendations.append("Review and justify need for privileged access")
        recommendations.append("Consider using principle of least privilege")
    
    if "system_modification" in violation_types:
        recommendations.append("System modifications require additional approval")
        recommendations.append("Create backup before modifying system files")
    
    recommendations.append("Document justification for policy exceptions")
    recommendations.append("Review action against organizational security policies")
    
    return recommendations


def _extract_required_tools(task_description: str) -> List[str]:
    """Extract required tools from task description."""
    tools = []
    task_lower = task_description.lower()
    
    # Tool mapping based on task keywords
    tool_mappings = {
        "file": ["file_read", "file_write", "file_list"],
        "git": ["git_status", "git_commit", "git_log"],
        "search": ["file_search"],
        "network": ["file_search"],
        "database": ["database_query"],
        "code": ["file_edit_patch", "file_read"]
    }
    
    for keyword, tool_list in tool_mappings.items():
        if keyword in task_lower:
            tools.extend(tool_list)
    
    return list(set(tools))


def _calculate_feasibility_score(
    task: str, 
    required_tools: List[str], 
    missing_deps: List[str],
    context: SecurityAnalysisContext
) -> float:
    """Calculate task feasibility score."""
    base_score = 1.0
    
    # Penalize missing dependencies
    if missing_deps:
        base_score -= 0.3 * (len(missing_deps) / max(len(required_tools), 1))
    
    # Consider context quality
    if context.raw_conversation:
        context_quality = len(context.raw_conversation) / 10.0  # Normalize
        base_score *= min(context_quality, 1.0)
    else:
        base_score *= 0.5  # No context
    
    # Consider task complexity
    task_complexity = _estimate_task_complexity_score(task)
    base_score *= (1.0 - task_complexity * 0.2)
    
    return max(0.0, min(base_score, 1.0))


def _estimate_complexity(task: str, tools: List[str]) -> Literal["low", "medium", "high", "very_high"]:
    """Estimate task complexity."""
    complexity_score = 0
    
    # Tool count factor
    complexity_score += len(tools) * 0.1
    
    # Task description complexity
    task_lower = task.lower()
    complex_keywords = ["integrate", "deploy", "configure", "optimize", "migrate"]
    complexity_score += sum(0.2 for keyword in complex_keywords if keyword in task_lower)
    
    if complexity_score >= 1.5:
        return "very_high"
    elif complexity_score >= 1.0:
        return "high"
    elif complexity_score >= 0.5:
        return "medium"
    else:
        return "low"


def _estimate_task_complexity_score(task: str) -> float:
    """Estimate task complexity as normalized score."""
    task_lower = task.lower()
    
    simple_keywords = ["read", "list", "show", "display"]
    medium_keywords = ["write", "create", "update", "modify"]
    complex_keywords = ["integrate", "deploy", "configure", "analyze"]
    
    if any(keyword in task_lower for keyword in complex_keywords):
        return 0.8
    elif any(keyword in task_lower for keyword in medium_keywords):
        return 0.5
    elif any(keyword in task_lower for keyword in simple_keywords):
        return 0.2
    else:
        return 0.6  # Default medium complexity


def _generate_alternative_approaches(
    task: str, 
    available_tools: List[str], 
    missing_deps: List[str]
) -> List[str]:
    """Generate alternative approaches for task completion."""
    alternatives = []
    
    if missing_deps:
        alternatives.append(f"Install missing dependencies: {', '.join(missing_deps)}")
    
    # Suggest tool substitutions
    tool_substitutions = {
        "file_edit_patch": ["file_write", "manual_editing"],
        "web_search": ["manual_research", "documentation_lookup"],
        "git_commit": ["manual_git_operations"]
    }
    
    for missing_tool in missing_deps:
        if missing_tool in tool_substitutions:
            substitutes = [sub for sub in tool_substitutions[missing_tool] if sub in available_tools]
            if substitutes:
                alternatives.append(f"Use {', '.join(substitutes)} instead of {missing_tool}")
    
    # General alternatives
    alternatives.append("Break task into smaller, simpler steps")
    alternatives.append("Use manual intervention where tools are unavailable")
    alternatives.append("Seek additional tools or permissions")
    
    return alternatives
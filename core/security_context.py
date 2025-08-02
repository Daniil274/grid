"""
Security Analysis Context Framework for GRID system.
Provides contextual security framework for agents analysis without prompts.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Union
from datetime import datetime
from enum import Enum

from schemas import AgentExecution


class ThreatLevel(Enum):
    """Threat severity levels."""
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SecurityEventType(Enum):
    """Types of security events."""
    THREAT_DETECTED = "threat_detected"
    POLICY_VIOLATION = "policy_violation"
    SUSPICIOUS_COMMAND = "suspicious_command"
    UNAUTHORIZED_ACCESS = "unauthorized_access"
    DATA_EXFILTRATION = "data_exfiltration"


@dataclass
class RawMessage:
    """Raw message without agent prompts or instructions."""
    role: str
    content: str
    timestamp: datetime
    message_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolExecution:
    """Tool execution record for security analysis."""
    tool_name: str
    parameters: Dict[str, Any]
    result: Any
    start_time: datetime
    end_time: Optional[datetime] = None
    agent_name: Optional[str] = None
    success: bool = True
    error: Optional[str] = None


@dataclass
class SecurityPolicy:
    """Security policy definition."""
    policy_id: str
    name: str
    description: str
    rules: List[str]
    severity: ThreatLevel
    enabled: bool = True


@dataclass
class ThreatIndicator:
    """Threat indicator for pattern matching."""
    indicator_id: str
    pattern: str
    threat_type: SecurityEventType
    severity: ThreatLevel
    description: str
    regex_pattern: bool = False


@dataclass
class UserSession:
    """User session information for security context."""
    session_id: str
    user_id: Optional[str] = None
    start_time: datetime = field(default_factory=datetime.now)
    permissions: List[str] = field(default_factory=list)
    working_directory: Optional[str] = None
    environment: Dict[str, str] = field(default_factory=dict)


@dataclass
class SecurityEvent:
    """Security event record."""
    event_id: str
    event_type: SecurityEventType
    threat_level: ThreatLevel
    timestamp: datetime
    source: str
    description: str
    details: Dict[str, Any]
    mitigated: bool = False


class ThreatDetectionService:
    """Service for detecting security threats."""
    
    def __init__(self, indicators: List[ThreatIndicator]):
        self.indicators = indicators
    
    async def analyze_content(self, content: str, context: Dict[str, Any] = None) -> List[SecurityEvent]:
        """Analyze content for security threats."""
        events = []
        
        for indicator in self.indicators:
            if self._matches_indicator(content, indicator):
                event = SecurityEvent(
                    event_id=f"threat_{datetime.now().timestamp()}",
                    event_type=indicator.threat_type,
                    threat_level=indicator.severity,
                    timestamp=datetime.now(),
                    source="threat_detection",
                    description=indicator.description,
                    details={
                        "indicator_id": indicator.indicator_id,
                        "matched_content": content,
                        "context": context or {}
                    }
                )
                events.append(event)
        
        return events
    
    def _matches_indicator(self, content: str, indicator: ThreatIndicator) -> bool:
        """Check if content matches threat indicator."""
        if indicator.regex_pattern:
            import re
            return bool(re.search(indicator.pattern, content, re.IGNORECASE))
        else:
            return indicator.pattern.lower() in content.lower()


class PolicyComplianceEngine:
    """Service for checking policy compliance."""
    
    def __init__(self, policies: List[SecurityPolicy]):
        self.policies = [p for p in policies if p.enabled]
    
    async def check_compliance(
        self, 
        action: str, 
        resources: List[str], 
        context: Dict[str, Any] = None
    ) -> List[SecurityEvent]:
        """Check if action complies with security policies."""
        events = []
        
        for policy in self.policies:
            violations = self._check_policy_violations(action, resources, policy, context)
            events.extend(violations)
        
        return events
    
    def _check_policy_violations(
        self, 
        action: str, 
        resources: List[str], 
        policy: SecurityPolicy,
        context: Dict[str, Any]
    ) -> List[SecurityEvent]:
        """Check specific policy for violations."""
        violations = []
        
        for rule in policy.rules:
            if self._violates_rule(action, resources, rule, context):
                event = SecurityEvent(
                    event_id=f"policy_{datetime.now().timestamp()}",
                    event_type=SecurityEventType.POLICY_VIOLATION,
                    threat_level=policy.severity,
                    timestamp=datetime.now(),
                    source="policy_compliance",
                    description=f"Policy violation: {policy.name} - {rule}",
                    details={
                        "policy_id": policy.policy_id,
                        "rule": rule,
                        "action": action,
                        "resources": resources,
                        "context": context or {}
                    }
                )
                violations.append(event)
        
        return violations
    
    def _violates_rule(
        self, 
        action: str, 
        resources: List[str], 
        rule: str, 
        context: Dict[str, Any]
    ) -> bool:
        """Check if action violates specific rule."""
        # Simplified rule checking - in practice this would be more sophisticated
        rule_lower = rule.lower()
        action_lower = action.lower()
        
        # Example rules:
        # "no_delete_system_files"
        if "no_delete" in rule_lower and "delete" in action_lower:
            return any("system" in str(r).lower() or "/etc" in str(r) for r in resources)
        
        # "no_network_access"
        if "no_network" in rule_lower and ("curl" in action_lower or "wget" in action_lower):
            return True
        
        return False


class SecurityAuditLogger:
    """Service for logging security events and audit trails."""
    
    def __init__(self, log_path: Optional[str] = None):
        self.log_path = log_path or "logs/security_audit.json"
        self.events: List[SecurityEvent] = []
    
    async def log_event(self, event: SecurityEvent):
        """Log security event."""
        self.events.append(event)
        
        if self.log_path:
            await self._persist_events()
    
    async def log_multiple_events(self, events: List[SecurityEvent]):
        """Log multiple security events."""
        self.events.extend(events)
        
        if self.log_path:
            await self._persist_events()
    
    async def _persist_events(self):
        """Persist events to file."""
        import json
        import os
        
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        
        serializable_events = []
        for event in self.events:
            serializable_events.append({
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "threat_level": event.threat_level.value,
                "timestamp": event.timestamp.isoformat(),
                "source": event.source,
                "description": event.description,
                "details": event.details,
                "mitigated": event.mitigated
            })
        
        with open(self.log_path, 'w') as f:
            json.dump(serializable_events, f, indent=2)


@dataclass
class SecurityAnalysisContext:
    """
    Main security analysis context containing all components needed
    for comprehensive security analysis without agent prompts.
    """
    # Core conversation data without prompts
    raw_conversation: List[RawMessage] = field(default_factory=list)
    
    # Tool execution tracking
    tool_execution_history: List[ToolExecution] = field(default_factory=list)
    
    # System information
    user_session: Optional[UserSession] = None
    security_policies: List[SecurityPolicy] = field(default_factory=list)
    threat_indicators: List[ThreatIndicator] = field(default_factory=list)
    
    # Security services
    threat_detector: Optional[ThreatDetectionService] = None
    policy_engine: Optional[PolicyComplianceEngine] = None
    audit_logger: Optional[SecurityAuditLogger] = None
    
    # Context metadata
    context_id: str = field(default_factory=lambda: f"ctx_{datetime.now().timestamp()}")
    created_at: datetime = field(default_factory=datetime.now)
    
    def __post_init__(self):
        """Initialize security services if not provided."""
        if self.threat_detector is None:
            self.threat_detector = ThreatDetectionService(self.threat_indicators)
        
        if self.policy_engine is None:
            self.policy_engine = PolicyComplianceEngine(self.security_policies)
        
        if self.audit_logger is None:
            self.audit_logger = SecurityAuditLogger()
    
    def add_raw_message(self, role: str, content: str, metadata: Dict[str, Any] = None):
        """Add raw message to conversation history."""
        message = RawMessage(
            role=role,
            content=content,
            timestamp=datetime.now(),
            message_id=f"msg_{len(self.raw_conversation)}",
            metadata=metadata or {}
        )
        self.raw_conversation.append(message)
    
    def add_tool_execution(self, execution: ToolExecution):
        """Add tool execution to history."""
        self.tool_execution_history.append(execution)
    
    def get_conversation_summary(self) -> Dict[str, Any]:
        """Get summary of conversation for analysis."""
        return {
            "message_count": len(self.raw_conversation),
            "user_messages": len([m for m in self.raw_conversation if m.role == "user"]),
            "assistant_messages": len([m for m in self.raw_conversation if m.role == "assistant"]),
            "tool_executions": len(self.tool_execution_history),
            "last_activity": max(
                (m.timestamp for m in self.raw_conversation), 
                default=self.created_at
            ),
            "context_age": (datetime.now() - self.created_at).total_seconds()
        }
    
    async def analyze_security_threats(self, content: str) -> List[SecurityEvent]:
        """Analyze content for security threats."""
        if self.threat_detector:
            return await self.threat_detector.analyze_content(
                content, 
                {"conversation_summary": self.get_conversation_summary()}
            )
        return []
    
    async def check_policy_compliance(
        self, 
        action: str, 
        resources: List[str]
    ) -> List[SecurityEvent]:
        """Check policy compliance for action."""
        if self.policy_engine:
            return await self.policy_engine.check_compliance(
                action, 
                resources, 
                {"conversation_summary": self.get_conversation_summary()}
            )
        return []
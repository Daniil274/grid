"""
Security Guardian Agent - Threat detection and security analysis.
Provides comprehensive security analysis without requiring agent prompts.
"""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field
from datetime import datetime

from agents import Agent, OpenAIChatCompletionsModel, function_tool, RunContextWrapper
from core.security_context import SecurityAnalysisContext, ThreatLevel, SecurityEvent
from tools.security_tools import ThreatAnalysisResult


class SecurityAnalysisInput(BaseModel):
    """Input for security analysis."""
    content: str = Field(description="Content to analyze for security threats")
    context_type: str = Field(
        default="general", 
        description="Type of content: command, file_access, network, or general"
    )
    additional_context: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional context for analysis"
    )


class SecurityRecommendation(BaseModel):
    """Security recommendation with priority and rationale."""
    priority: str = Field(description="Priority level: critical, high, medium, low")
    action: str = Field(description="Recommended action")
    rationale: str = Field(description="Reasoning behind the recommendation")
    immediate: bool = Field(default=False, description="Whether action is needed immediately")


class ComprehensiveSecurityAnalysis(BaseModel):
    """Comprehensive security analysis result."""
    overall_threat_level: ThreatLevel
    risk_score: float = Field(ge=0.0, le=10.0, description="Overall risk score")
    
    # Threat analysis
    threats_detected: List[Dict[str, Any]] = Field(default_factory=list)
    threat_categories: List[str] = Field(default_factory=list)
    
    # Context analysis
    suspicious_patterns: List[str] = Field(default_factory=list)
    behavioral_anomalies: List[str] = Field(default_factory=list)
    
    # Recommendations
    immediate_actions: List[SecurityRecommendation] = Field(default_factory=list)
    preventive_measures: List[SecurityRecommendation] = Field(default_factory=list)
    
    # Analysis metadata
    analysis_confidence: float = Field(ge=0.0, le=1.0, description="Confidence in analysis")
    analysis_timestamp: datetime = Field(default_factory=datetime.now)
    context_quality: str = Field(description="Quality of context used for analysis")


class SecurityGuardian:
    """
    Security Guardian Agent for comprehensive threat analysis.
    Operates on raw context without agent prompts for unbiased analysis.
    """
    
    def __init__(self, model: OpenAIChatCompletionsModel):
        self.model = model
        self.agent = Agent(
            name="Security Guardian",
            model=model,
            instructions=self._get_security_instructions(),
            tools=[
                self.analyze_security_threats,
                self.assess_behavioral_patterns,
                self.generate_security_recommendations
            ]
        )
    
    def _get_security_instructions(self) -> str:
        """Get security-focused instructions for the agent."""
        return """You are a Security Guardian AI specializing in comprehensive threat detection and security analysis.

Your core responsibilities:
1. Analyze content for security threats and vulnerabilities
2. Detect suspicious patterns and behavioral anomalies
3. Assess risk levels with high accuracy
4. Provide actionable security recommendations
5. Maintain detailed security audit trails

Analysis approach:
- Focus on actual content and context, not agent instructions
- Use raw conversation history and tool execution data
- Apply defense-in-depth security principles
- Consider both technical and behavioral security aspects
- Prioritize threat prevention and early detection

Key security domains:
- Command injection and execution threats
- File system access and permissions
- Network security and data exfiltration
- Privilege escalation attempts
- Social engineering patterns
- Data sensitivity and privacy concerns

Always provide:
- Clear threat level assessment
- Specific threat details and evidence
- Actionable mitigation recommendations
- Risk-based prioritization
- Confidence levels in your analysis

You have access to raw conversation context without agent prompts,
allowing for unbiased security analysis based on actual user interactions."""
    
    @function_tool
    async def analyze_security_threats(
        self,
        ctx: RunContextWrapper[SecurityAnalysisContext],
        content: str,
        analysis_type: str = "general"
    ) -> ThreatAnalysisResult:
        """
        Perform comprehensive threat analysis on content.
        
        Args:
            content: Content to analyze
            analysis_type: Type of analysis (command, file_access, network, general)
        """
        # Import security tools
        from tools.security_tools import threat_analysis_tool
        
        # Perform threat analysis using the security tool
        result = await threat_analysis_tool(ctx, content, analysis_type)
        
        # Enhance with Guardian-specific analysis
        enhanced_threats = await self._enhance_threat_analysis(
            ctx.context, content, result.threats_detected
        )
        
        # Update result with enhanced analysis
        result.threats_detected = enhanced_threats
        result.risk_score = self._recalculate_risk_score(enhanced_threats)
        
        return result
    
    @function_tool
    async def assess_behavioral_patterns(
        self,
        ctx: RunContextWrapper[SecurityAnalysisContext]
    ) -> Dict[str, Any]:
        """
        Analyze behavioral patterns from conversation and tool usage.
        
        Returns:
            Dictionary with behavioral analysis results
        """
        security_context = ctx.context
        
        # Analyze conversation patterns
        conversation_patterns = self._analyze_conversation_patterns(
            security_context.raw_conversation
        )
        
        # Analyze tool usage patterns
        tool_patterns = self._analyze_tool_usage_patterns(
            security_context.tool_execution_history
        )
        
        # Detect anomalies
        anomalies = self._detect_behavioral_anomalies(
            conversation_patterns, tool_patterns
        )
        
        return {
            "conversation_patterns": conversation_patterns,
            "tool_usage_patterns": tool_patterns,
            "behavioral_anomalies": anomalies,
            "analysis_timestamp": datetime.now().isoformat()
        }
    
    @function_tool
    async def generate_security_recommendations(
        self,
        ctx: RunContextWrapper[SecurityAnalysisContext],
        threat_analysis: Dict[str, Any],
        behavioral_analysis: Dict[str, Any]
    ) -> List[SecurityRecommendation]:
        """
        Generate comprehensive security recommendations.
        
        Args:
            threat_analysis: Results from threat analysis
            behavioral_analysis: Results from behavioral analysis
            
        Returns:
            List of prioritized security recommendations
        """
        recommendations = []
        
        # Threat-based recommendations
        threat_recs = self._generate_threat_recommendations(threat_analysis)
        recommendations.extend(threat_recs)
        
        # Behavioral-based recommendations
        behavioral_recs = self._generate_behavioral_recommendations(behavioral_analysis)
        recommendations.extend(behavioral_recs)
        
        # Context-based recommendations
        context_recs = self._generate_context_recommendations(ctx.context)
        recommendations.extend(context_recs)
        
        # Sort by priority and immediacy
        recommendations.sort(key=lambda x: (
            {"critical": 0, "high": 1, "medium": 2, "low": 3}[x.priority],
            not x.immediate
        ))
        
        return recommendations
    
    async def comprehensive_security_analysis(
        self,
        ctx: RunContextWrapper[SecurityAnalysisContext],
        analysis_input: SecurityAnalysisInput
    ) -> ComprehensiveSecurityAnalysis:
        """
        Perform comprehensive security analysis combining all analysis types.
        
        Args:
            analysis_input: Input for security analysis
            
        Returns:
            Comprehensive security analysis result
        """
        # Perform threat analysis
        threat_result = await self.analyze_security_threats(
            ctx, 
            analysis_input.content, 
            analysis_input.context_type
        )
        
        # Perform behavioral analysis
        behavioral_result = await self.assess_behavioral_patterns(ctx)
        
        # Generate recommendations
        recommendations = await self.generate_security_recommendations(
            ctx, 
            threat_result.model_dump(),
            behavioral_result
        )
        
        # Calculate overall metrics
        overall_threat_level = threat_result.threat_level
        risk_score = threat_result.risk_score
        
        # Enhance with behavioral risk
        behavioral_risk = self._calculate_behavioral_risk(behavioral_result)
        risk_score = min(10.0, risk_score + behavioral_risk)
        
        # Update threat level based on combined analysis
        if risk_score >= 8.0:
            overall_threat_level = ThreatLevel.CRITICAL
        elif risk_score >= 6.0:
            overall_threat_level = ThreatLevel.HIGH
        elif risk_score >= 4.0:
            overall_threat_level = ThreatLevel.MEDIUM
        elif risk_score >= 2.0:
            overall_threat_level = ThreatLevel.LOW
        else:
            overall_threat_level = ThreatLevel.NONE
        
        # Extract threat categories
        threat_categories = list(set([
            threat.get("type", "unknown") 
            for threat in threat_result.threats_detected
        ]))
        
        # Extract suspicious patterns and anomalies
        suspicious_patterns = self._extract_suspicious_patterns(
            threat_result.threats_detected
        )
        behavioral_anomalies = behavioral_result.get("behavioral_anomalies", [])
        
        # Separate immediate vs preventive recommendations
        immediate_actions = [r for r in recommendations if r.immediate]
        preventive_measures = [r for r in recommendations if not r.immediate]
        
        # Calculate analysis confidence
        confidence = self._calculate_analysis_confidence(
            ctx.context, threat_result, behavioral_result
        )
        
        # Assess context quality
        context_quality = self._assess_context_quality(ctx.context)
        
        return ComprehensiveSecurityAnalysis(
            overall_threat_level=overall_threat_level,
            risk_score=risk_score,
            threats_detected=threat_result.threats_detected,
            threat_categories=threat_categories,
            suspicious_patterns=suspicious_patterns,
            behavioral_anomalies=behavioral_anomalies,
            immediate_actions=immediate_actions,
            preventive_measures=preventive_measures,
            analysis_confidence=confidence,
            context_quality=context_quality
        )
    
    # Helper methods
    
    async def _enhance_threat_analysis(
        self,
        context: SecurityAnalysisContext,
        content: str,
        basic_threats: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Enhance basic threat analysis with Guardian-specific intelligence."""
        enhanced_threats = basic_threats.copy()
        
        # Add context-aware threat enhancement
        if context.raw_conversation:
            conversation_threats = self._analyze_conversation_threats(
                context.raw_conversation, content
            )
            enhanced_threats.extend(conversation_threats)
        
        # Add tool execution threat analysis
        if context.tool_execution_history:
            tool_threats = self._analyze_tool_execution_threats(
                context.tool_execution_history, content
            )
            enhanced_threats.extend(tool_threats)
        
        return enhanced_threats
    
    def _analyze_conversation_threats(self, conversation, content) -> List[Dict[str, Any]]:
        """Analyze threats based on conversation context."""
        threats = []
        
        # Look for escalation patterns
        user_messages = [msg for msg in conversation if msg.role == "user"]
        if len(user_messages) > 3:
            recent_messages = [msg.content.lower() for msg in user_messages[-3:]]
            
            # Check for privilege escalation requests
            escalation_keywords = ["sudo", "root", "admin", "permission", "access"]
            escalation_count = sum(
                1 for msg in recent_messages 
                for keyword in escalation_keywords 
                if keyword in msg
            )
            
            if escalation_count >= 2:
                threats.append({
                    "type": "privilege_escalation_pattern",
                    "level": "high",
                    "description": "Pattern of escalating privilege requests detected",
                    "evidence": f"Multiple privilege requests in recent conversation"
                })
        
        return threats
    
    def _analyze_tool_execution_threats(self, tool_history, content) -> List[Dict[str, Any]]:
        """Analyze threats based on tool execution history."""
        threats = []
        
        # Check for suspicious tool usage patterns
        recent_tools = [exec.tool_name for exec in tool_history[-10:]]  # Last 10 executions
        
        # File system manipulation pattern
        file_tools = ["file_write", "file_edit_patch", "file_list"]
        file_usage = sum(1 for tool in recent_tools if tool in file_tools)
        
        if file_usage >= 5:
            threats.append({
                "type": "excessive_file_manipulation",
                "level": "medium",
                "description": "Excessive file system manipulation detected",
                "evidence": f"Used file tools {file_usage} times recently"
            })
        
        # Git manipulation pattern
        git_tools = ["git_commit", "git_add_file", "git_checkout_branch"]
        git_usage = sum(1 for tool in recent_tools if tool in git_tools)
        
        if git_usage >= 3:
            threats.append({
                "type": "repository_manipulation",
                "level": "medium",
                "description": "Intensive repository manipulation detected",
                "evidence": f"Used git tools {git_usage} times recently"
            })
        
        return threats
    
    def _recalculate_risk_score(self, enhanced_threats: List[Dict[str, Any]]) -> float:
        """Recalculate risk score with enhanced threats."""
        if not enhanced_threats:
            return 0.0
        
        level_scores = {
            "none": 0.0,
            "low": 2.0,
            "medium": 4.0,
            "high": 7.0,
            "critical": 10.0
        }
        
        total_score = sum(
            level_scores.get(threat.get("level", "low"), 2.0) 
            for threat in enhanced_threats
        )
        
        # Apply Guardian-specific risk amplification for pattern-based threats
        pattern_threats = [t for t in enhanced_threats if "pattern" in t.get("type", "")]
        if pattern_threats:
            total_score *= 1.2  # 20% amplification for pattern-based threats
        
        return min(total_score / len(enhanced_threats), 10.0)
    
    def _analyze_conversation_patterns(self, conversation) -> Dict[str, Any]:
        """Analyze patterns in conversation."""
        if not conversation:
            return {"message_count": 0, "patterns": []}
        
        patterns = {
            "message_count": len(conversation),
            "user_messages": len([m for m in conversation if m.role == "user"]),
            "assistant_messages": len([m for m in conversation if m.role == "assistant"]),
            "average_message_length": sum(len(m.content) for m in conversation) / len(conversation),
            "patterns": []
        }
        
        # Detect repetitive requests
        user_contents = [m.content.lower() for m in conversation if m.role == "user"]
        if len(user_contents) > 2:
            # Simple repetition detection
            repeated_patterns = []
            for i, content in enumerate(user_contents[:-1]):
                for j, other_content in enumerate(user_contents[i+1:], i+1):
                    similarity = self._calculate_similarity(content, other_content)
                    if similarity > 0.7:
                        repeated_patterns.append(f"Similar requests at positions {i} and {j}")
            
            if repeated_patterns:
                patterns["patterns"].append({
                    "type": "repetitive_requests",
                    "instances": repeated_patterns
                })
        
        return patterns
    
    def _analyze_tool_usage_patterns(self, tool_history) -> Dict[str, Any]:
        """Analyze tool usage patterns."""
        if not tool_history:
            return {"execution_count": 0, "patterns": []}
        
        tool_counts = {}
        for execution in tool_history:
            tool_counts[execution.tool_name] = tool_counts.get(execution.tool_name, 0) + 1
        
        patterns = {
            "execution_count": len(tool_history),
            "unique_tools": len(tool_counts),
            "most_used_tools": sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:5],
            "patterns": []
        }
        
        # Detect excessive tool usage
        for tool_name, count in tool_counts.items():
            if count > 10:
                patterns["patterns"].append({
                    "type": "excessive_tool_usage",
                    "tool": tool_name,
                    "count": count
                })
        
        return patterns
    
    def _detect_behavioral_anomalies(self, conversation_patterns, tool_patterns) -> List[str]:
        """Detect behavioral anomalies."""
        anomalies = []
        
        # Check for imbalanced conversation
        if conversation_patterns.get("user_messages", 0) > conversation_patterns.get("assistant_messages", 0) * 3:
            anomalies.append("Excessive user requests without assistant responses")
        
        # Check for tool usage without conversation
        if tool_patterns.get("execution_count", 0) > 5 and conversation_patterns.get("message_count", 0) < 3:
            anomalies.append("High tool usage with minimal conversation")
        
        # Check for repetitive patterns
        conv_patterns_list = conversation_patterns.get("patterns", [])
        if any(p.get("type") == "repetitive_requests" for p in conv_patterns_list):
            anomalies.append("Repetitive request patterns detected")
        
        tool_patterns_list = tool_patterns.get("patterns", [])
        if any(p.get("type") == "excessive_tool_usage" for p in tool_patterns_list):
            anomalies.append("Excessive tool usage patterns detected")
        
        return anomalies
    
    def _generate_threat_recommendations(self, threat_analysis) -> List[SecurityRecommendation]:
        """Generate recommendations based on threat analysis."""
        recommendations = []
        
        threats = threat_analysis.get("threats_detected", [])
        threat_level = threat_analysis.get("threat_level", "none")
        
        if threat_level in ["critical", "high"]:
            recommendations.append(SecurityRecommendation(
                priority="critical",
                action="Immediately halt execution and review all detected threats",
                rationale="High-risk threats detected that require immediate attention",
                immediate=True
            ))
        
        # Specific threat-based recommendations
        threat_types = [t.get("type", "") for t in threats]
        
        if "dangerous_command" in threat_types:
            recommendations.append(SecurityRecommendation(
                priority="high",
                action="Validate all commands against security policies before execution",
                rationale="Dangerous commands detected in analysis"
            ))
        
        if "privilege_escalation" in threat_types:
            recommendations.append(SecurityRecommendation(
                priority="high",
                action="Review and justify privilege escalation requirements",
                rationale="Privilege escalation attempts detected"
            ))
        
        return recommendations
    
    def _generate_behavioral_recommendations(self, behavioral_analysis) -> List[SecurityRecommendation]:
        """Generate recommendations based on behavioral analysis."""
        recommendations = []
        
        anomalies = behavioral_analysis.get("behavioral_anomalies", [])
        
        if "Excessive user requests without assistant responses" in anomalies:
            recommendations.append(SecurityRecommendation(
                priority="medium",
                action="Implement conversation flow controls",
                rationale="Unusual conversation patterns detected"
            ))
        
        if "High tool usage with minimal conversation" in anomalies:
            recommendations.append(SecurityRecommendation(
                priority="medium",
                action="Review tool usage authorization and logging",
                rationale="Automated or scripted tool usage suspected"
            ))
        
        return recommendations
    
    def _generate_context_recommendations(self, context) -> List[SecurityRecommendation]:
        """Generate recommendations based on context quality."""
        recommendations = []
        
        if not context.security_policies:
            recommendations.append(SecurityRecommendation(
                priority="medium",
                action="Implement comprehensive security policies",
                rationale="No security policies configured for analysis"
            ))
        
        if not context.audit_logger:
            recommendations.append(SecurityRecommendation(
                priority="high",
                action="Enable security audit logging",
                rationale="Security events are not being logged"
            ))
        
        return recommendations
    
    def _calculate_behavioral_risk(self, behavioral_analysis) -> float:
        """Calculate additional risk from behavioral analysis."""
        base_risk = 0.0
        
        anomalies = behavioral_analysis.get("behavioral_anomalies", [])
        base_risk += len(anomalies) * 0.5
        
        # Pattern-based risk
        patterns = behavioral_analysis.get("conversation_patterns", {}).get("patterns", [])
        patterns.extend(behavioral_analysis.get("tool_usage_patterns", {}).get("patterns", []))
        
        for pattern in patterns:
            if pattern.get("type") == "excessive_tool_usage":
                base_risk += 1.0
            elif pattern.get("type") == "repetitive_requests":
                base_risk += 0.5
        
        return min(base_risk, 3.0)  # Cap at 3.0 additional risk
    
    def _extract_suspicious_patterns(self, threats) -> List[str]:
        """Extract suspicious pattern descriptions."""
        patterns = []
        
        for threat in threats:
            threat_type = threat.get("type", "")
            if "pattern" in threat_type:
                patterns.append(threat.get("description", "Unknown pattern"))
        
        return patterns
    
    def _calculate_analysis_confidence(self, context, threat_result, behavioral_result) -> float:
        """Calculate confidence in security analysis."""
        confidence_factors = []
        
        # Context quality factor
        if context.raw_conversation:
            conv_quality = min(len(context.raw_conversation) / 10.0, 1.0)
            confidence_factors.append(conv_quality)
        else:
            confidence_factors.append(0.3)
        
        # Tool execution history factor
        if context.tool_execution_history:
            tool_quality = min(len(context.tool_execution_history) / 5.0, 1.0)
            confidence_factors.append(tool_quality)
        else:
            confidence_factors.append(0.5)
        
        # Threat detection confidence
        threats_count = len(threat_result.threats_detected)
        threat_confidence = min(threats_count / 3.0 + 0.5, 1.0)
        confidence_factors.append(threat_confidence)
        
        return sum(confidence_factors) / len(confidence_factors)
    
    def _assess_context_quality(self, context) -> str:
        """Assess quality of security context."""
        quality_score = 0
        
        if context.raw_conversation:
            quality_score += 2
        if context.tool_execution_history:
            quality_score += 2
        if context.security_policies:
            quality_score += 1
        if context.user_session:
            quality_score += 1
        
        if quality_score >= 5:
            return "excellent"
        elif quality_score >= 4:
            return "good"
        elif quality_score >= 2:
            return "adequate"
        else:
            return "poor"
    
    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """Calculate similarity between two texts (simple implementation)."""
        words1 = set(text1.split())
        words2 = set(text2.split())
        
        if not words1 and not words2:
            return 1.0
        if not words1 or not words2:
            return 0.0
        
        intersection = len(words1.intersection(words2))
        union = len(words1.union(words2))
        
        return intersection / union if union > 0 else 0.0
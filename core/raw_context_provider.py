"""
Raw Context Provider for extracting clean conversation history and tool execution data
without agent prompts and instructions.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime
import re

from core.context import ContextManager
from core.security_context import RawMessage, ToolExecution, SecurityAnalysisContext
from schemas import AgentExecution


class RawContextProvider:
    """
    Provides clean context extraction without agent prompts and instructions.
    Extracts raw user messages and tool execution history for security analysis.
    """
    
    def __init__(self):
        # Patterns to identify and filter out agent prompts/instructions
        self.prompt_patterns = [
            r"^(You are|Ты|You're|Your role)",
            r"^(Instructions?|Инструкци|Guidelines?|Rules?)",
            r"^(System:|Система:|Assistant:|Помощник:)",
            r"^(Base prompt|Базовый промпт)",
            r"^(Tool addition|Добавление инструмента)",
            r"^(Available tools|Доступные инструменты)",
            r"^(Context information|Контекстная информация)",
            r"^(Working directory|Рабочая директория)"
        ]
        
        # Patterns to identify tool calls and results
        self.tool_call_patterns = [
            r"call_(\w+)\(",
            r"(\w+)_tool\(",
            r"Tool:\s*(\w+)",
            r"Инструмент:\s*(\w+)"
        ]
    
    def extract_raw_conversation(self, context_manager: ContextManager) -> List[RawMessage]:
        """
        Extract clean conversation history without agent prompts.
        
        Args:
            context_manager: Context manager with conversation history
            
        Returns:
            List of RawMessage objects with clean content
        """
        raw_messages = []
        
        # Get conversation history from context manager
        history = context_manager.get_conversation_history()
        
        for i, message in enumerate(history):
            # Skip if this looks like a system prompt or agent instruction
            if self._is_agent_prompt(message.get("content", "")):
                continue
            
            # Extract clean content
            clean_content = self._clean_message_content(message.get("content", ""))
            
            if clean_content.strip():  # Only add non-empty messages
                raw_message = RawMessage(
                    role=message.get("role", "unknown"),
                    content=clean_content,
                    timestamp=message.get("timestamp", datetime.now()),
                    message_id=f"raw_{i}",
                    metadata={
                        "original_length": len(message.get("content", "")),
                        "cleaned_length": len(clean_content),
                        "source": "conversation_history"
                    }
                )
                raw_messages.append(raw_message)
        
        return raw_messages
    
    def extract_tool_context(self, execution_history: List[AgentExecution]) -> List[ToolExecution]:
        """
        Extract tool execution history with parameters and results.
        
        Args:
            execution_history: List of agent executions
            
        Returns:
            List of ToolExecution objects
        """
        tool_executions = []
        
        for execution in execution_history:
            # Extract tool calls from the execution
            tools_used = self._extract_tools_from_execution(execution)
            
            for tool_info in tools_used:
                tool_execution = ToolExecution(
                    tool_name=tool_info["name"],
                    parameters=tool_info.get("parameters", {}),
                    result=tool_info.get("result"),
                    start_time=execution.start_time,
                    end_time=execution.end_time,
                    agent_name=execution.agent_name,
                    success=execution.error is None,
                    error=execution.error
                )
                tool_executions.append(tool_execution)
        
        return tool_executions
    
    def create_security_context(
        self, 
        context_manager: ContextManager,
        execution_history: List[AgentExecution],
        user_session_info: Optional[Dict[str, Any]] = None
    ) -> SecurityAnalysisContext:
        """
        Create comprehensive security analysis context.
        
        Args:
            context_manager: Context manager with conversation history
            execution_history: Agent execution history
            user_session_info: Optional user session information
            
        Returns:
            Complete SecurityAnalysisContext
        """
        # Extract raw conversation and tools
        raw_conversation = self.extract_raw_conversation(context_manager)
        tool_executions = self.extract_tool_context(execution_history)
        
        # Create security context
        security_context = SecurityAnalysisContext(
            raw_conversation=raw_conversation,
            tool_execution_history=tool_executions
        )
        
        # Add user session if provided
        if user_session_info:
            from core.security_context import UserSession
            security_context.user_session = UserSession(
                session_id=user_session_info.get("session_id", "default"),
                user_id=user_session_info.get("user_id"),
                permissions=user_session_info.get("permissions", []),
                working_directory=user_session_info.get("working_directory"),
                environment=user_session_info.get("environment", {})
            )
        
        return security_context
    
    def _is_agent_prompt(self, content: str) -> bool:
        """Check if content appears to be an agent prompt or instruction."""
        content_lower = content.lower().strip()
        
        # Check against known prompt patterns
        for pattern in self.prompt_patterns:
            if re.match(pattern, content, re.IGNORECASE | re.MULTILINE):
                return True
        
        # Check for common prompt keywords
        prompt_keywords = [
            "instructions", "инструкции", "guidelines", "rules", "правила",
            "you are", "ты", "your role", "твоя роль", "system message",
            "системное сообщение", "base prompt", "базовый промпт"
        ]
        
        for keyword in prompt_keywords:
            if keyword in content_lower:
                return True
        
        # Check for tool addition messages
        if any(phrase in content_lower for phrase in [
            "added tool", "добавлен инструмент", "available tools", 
            "доступные инструменты", "tool addition", "добавление инструмента"
        ]):
            return True
        
        return False
    
    def _clean_message_content(self, content: str) -> str:
        """Clean message content by removing agent-specific formatting."""
        # Remove common agent formatting
        cleaned = content
        
        # Remove tool call formatting but preserve the essence
        tool_call_replacements = [
            (r"🔧\s*Tool:\s*(\w+)", r"Used tool: \1"),
            (r"🔧\s*Инструмент:\s*(\w+)", r"Использован инструмент: \1"),
            (r"Tool call:\s*(\w+)", r"Used: \1"),
            (r"Вызов инструмента:\s*(\w+)", r"Использован: \1")
        ]
        
        for pattern, replacement in tool_call_replacements:
            cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
        
        # Remove excessive whitespace
        cleaned = re.sub(r'\n\s*\n', '\n\n', cleaned)
        cleaned = cleaned.strip()
        
        return cleaned
    
    def _extract_tools_from_execution(self, execution: AgentExecution) -> List[Dict[str, Any]]:
        """Extract tool usage information from execution."""
        tools_used = []
        
        # Check tools_used field if available
        if hasattr(execution, 'tools_used') and execution.tools_used:
            for tool_name in execution.tools_used:
                tools_used.append({
                    "name": tool_name,
                    "parameters": {},
                    "result": None
                })
        
        # Parse tool calls from input/output if available
        if execution.input_message:
            tools_from_input = self._parse_tool_calls(execution.input_message)
            tools_used.extend(tools_from_input)
        
        if execution.output:
            tools_from_output = self._parse_tool_calls(execution.output)
            tools_used.extend(tools_from_output)
        
        return tools_used
    
    def _parse_tool_calls(self, text: str) -> List[Dict[str, Any]]:
        """Parse tool calls from text content."""
        tools = []
        
        for pattern in self.tool_call_patterns:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                tool_name = match.group(1) if match.groups() else "unknown"
                
                # Try to extract parameters (simplified)
                param_start = match.end()
                param_end = text.find(')', param_start)
                parameters = {}
                
                if param_end > param_start:
                    param_text = text[param_start:param_end]
                    parameters = self._parse_parameters(param_text)
                
                tools.append({
                    "name": tool_name,
                    "parameters": parameters,
                    "result": None
                })
        
        return tools
    
    def _parse_parameters(self, param_text: str) -> Dict[str, Any]:
        """Parse parameters from tool call text (simplified parsing)."""
        parameters = {}
        
        # Simple parameter parsing - in practice this would be more robust
        param_text = param_text.strip()
        if not param_text:
            return parameters
        
        # Handle simple key=value pairs
        pairs = param_text.split(',')
        for pair in pairs:
            if '=' in pair:
                key, value = pair.split('=', 1)
                key = key.strip().strip('"\'')
                value = value.strip().strip('"\'')
                parameters[key] = value
        
        return parameters
    
    def get_context_quality_metrics(self, security_context: SecurityAnalysisContext) -> Dict[str, Any]:
        """
        Calculate context quality metrics for analysis.
        
        Args:
            security_context: Security analysis context
            
        Returns:
            Dictionary with quality metrics
        """
        conversation = security_context.raw_conversation
        tools = security_context.tool_execution_history
        
        metrics = {
            "message_count": len(conversation),
            "tool_execution_count": len(tools),
            "unique_tools_used": len(set(t.tool_name for t in tools)),
            "conversation_depth": self._calculate_conversation_depth(conversation),
            "context_completeness": self._calculate_completeness(conversation, tools),
            "temporal_coherence": self._calculate_temporal_coherence(conversation),
            "content_quality": self._assess_content_quality(conversation)
        }
        
        return metrics
    
    def _calculate_conversation_depth(self, messages: List[RawMessage]) -> float:
        """Calculate conversation depth based on turn-taking."""
        if len(messages) < 2:
            return 0.0
        
        turns = 0
        last_role = None
        
        for message in messages:
            if message.role != last_role:
                turns += 1
                last_role = message.role
        
        return turns / len(messages)
    
    def _calculate_completeness(self, messages: List[RawMessage], tools: List[ToolExecution]) -> float:
        """Calculate context completeness score."""
        if not messages:
            return 0.0
        
        # Basic completeness metrics
        has_user_input = any(m.role == "user" for m in messages)
        has_assistant_response = any(m.role == "assistant" for m in messages)
        has_tool_usage = len(tools) > 0
        has_recent_activity = any(
            (datetime.now() - m.timestamp).total_seconds() < 3600 
            for m in messages
        )
        
        score = sum([has_user_input, has_assistant_response, has_tool_usage, has_recent_activity]) / 4
        return score
    
    def _calculate_temporal_coherence(self, messages: List[RawMessage]) -> float:
        """Calculate temporal coherence of conversation."""
        if len(messages) < 2:
            return 1.0
        
        timestamps = [m.timestamp for m in messages]
        timestamps.sort()
        
        # Check for reasonable time gaps
        gaps = []
        for i in range(1, len(timestamps)):
            gap = (timestamps[i] - timestamps[i-1]).total_seconds()
            gaps.append(gap)
        
        # Calculate coherence based on reasonable conversation timing
        reasonable_gaps = sum(1 for gap in gaps if 1 <= gap <= 3600)  # 1 second to 1 hour
        return reasonable_gaps / len(gaps) if gaps else 1.0
    
    def _assess_content_quality(self, messages: List[RawMessage]) -> float:
        """Assess content quality of messages."""
        if not messages:
            return 0.0
        
        quality_scores = []
        
        for message in messages:
            content = message.content.strip()
            
            # Basic quality metrics
            length_score = min(len(content) / 100, 1.0)  # Normalized length
            word_count = len(content.split())
            word_score = min(word_count / 10, 1.0)  # Normalized word count
            
            # Check for meaningful content (not just commands)
            has_context = any(word in content.lower() for word in [
                "please", "help", "can you", "would", "could", "пожалуйста", 
                "помоги", "можешь", "хочу", "нужно"
            ])
            context_score = 1.0 if has_context else 0.5
            
            message_quality = (length_score + word_score + context_score) / 3
            quality_scores.append(message_quality)
        
        return sum(quality_scores) / len(quality_scores)
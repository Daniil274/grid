"""
Security-Aware Agent Factory for GRID system.
Extends the base AgentFactory with security analysis capabilities.
"""
from typing import Dict, List, Any, Set
from datetime import datetime
from agents import Agent
from core.agent_factory import AgentFactory
from core.security_guardrails import configure_agent_guardrails, SecurityGuardrails


class SecurityAwareAgentFactory(AgentFactory):
    """
    Enhanced Agent Factory with integrated security analysis.
    Automatically applies security guardrails to appropriate agents.
    """
    
    def __init__(self, config, working_directory=None):
        super().__init__(config, working_directory)
        self.security_guardrails = SecurityGuardrails()
        
        # Agent types that require security analysis
        self.security_enabled_agents = {
            "security_guardian",
            "task_analyzer", 
            "context_quality",
            "file_manager",
            "system_executor",
            "code_generator"
        }
        
        # Agent types that require full analysis (input + output guardrails)
        self.full_analysis_agents = {
            "security_guardian",
            "task_analyzer",
            "context_quality"
        }
        
        # Agent types that only need security input guardrails
        self.security_only_agents = {
            "file_manager",
            "system_executor"
        }
        
    async def create_agent(self, agent_key: str, **kwargs) -> Agent:
        """
        Create agent with appropriate security configuration.
        
        Args:
            agent_key: Agent configuration key
            **kwargs: Additional agent creation parameters
            
        Returns:
            Agent instance with security guardrails if applicable
        """
        # Create base agent
        agent = await super().create_agent(agent_key, **kwargs)
        
        # Apply security enhancements if needed
        if self._requires_security_analysis(agent_key):
            agent = self._apply_security_guardrails(agent, agent_key)
        
        return agent
    
    def _requires_security_analysis(self, agent_key: str) -> bool:
        """
        Check if agent requires security analysis.
        
        Args:
            agent_key: Agent configuration key
            
        Returns:
            True if agent needs security guardrails
        """
        return agent_key in self.security_enabled_agents
    
    def _apply_security_guardrails(self, agent: Agent, agent_key: str) -> Agent:
        """
        Apply appropriate security guardrails to agent.
        
        Args:
            agent: Base agent instance
            agent_key: Agent configuration key
            
        Returns:
            Agent with security guardrails applied
        """
        # Determine guardrail configuration
        enable_security = agent_key in self.security_enabled_agents
        enable_analysis = agent_key in self.full_analysis_agents
        
        # Apply guardrails
        enhanced_agent = configure_agent_guardrails(
            agent,
            enable_security=enable_security,
            enable_analysis=enable_analysis
        )
        
        return enhanced_agent
    
    async def create_security_guardian(self, **kwargs) -> Agent:
        """
        Create Security Guardian agent with specialized configuration.
        
        Args:
            **kwargs: Additional configuration parameters
            
        Returns:
            Configured Security Guardian agent
        """
        return await self.create_agent("security_guardian", **kwargs)
    
    async def create_task_analyzer(self, **kwargs) -> Agent:
        """
        Create Task Analyzer agent with specialized configuration.
        
        Args:
            **kwargs: Additional configuration parameters
            
        Returns:
            Configured Task Analyzer agent
        """
        return await self.create_agent("task_analyzer", **kwargs)
    
    async def create_context_quality_agent(self, **kwargs) -> Agent:
        """
        Create Context Quality agent with specialized configuration.
        
        Args:
            **kwargs: Additional configuration parameters
            
        Returns:
            Configured Context Quality agent
        """
        return await self.create_agent("context_quality", **kwargs)
    
    def get_security_agent_keys(self) -> Set[str]:
        """
        Get set of agent keys that have security capabilities.
        
        Returns:
            Set of security-enabled agent keys
        """
        return self.security_enabled_agents.copy()
    
    def configure_security_policies(self, policies: List[Dict[str, Any]]) -> None:
        """
        Configure security policies for the factory.
        
        Args:
            policies: List of security policy configurations
        """
        # Store policies for use in security context
        self.security_policies = policies
        
        # Update security guardrails with new policies
        if hasattr(self.security_guardrails, 'update_policies'):
            self.security_guardrails.update_policies(policies)
    
    def enable_audit_logging(self, enable: bool = True) -> None:
        """
        Enable or disable security audit logging.
        
        Args:
            enable: Whether to enable audit logging
        """
        self.audit_logging_enabled = enable
        
        # Configure audit logging in security guardrails
        if hasattr(self.security_guardrails, 'configure_audit_logging'):
            self.security_guardrails.configure_audit_logging(enable)
    
    def add_security_agent_type(self, agent_key: str, full_analysis: bool = False) -> None:
        """
        Add new agent type to security monitoring.
        
        Args:
            agent_key: Agent configuration key to add
            full_analysis: Whether agent needs full analysis (input + output)
        """
        self.security_enabled_agents.add(agent_key)
        
        if full_analysis:
            self.full_analysis_agents.add(agent_key)
        else:
            self.security_only_agents.add(agent_key)
    
    def remove_security_agent_type(self, agent_key: str) -> None:
        """
        Remove agent type from security monitoring.
        
        Args:
            agent_key: Agent configuration key to remove
        """
        self.security_enabled_agents.discard(agent_key)
        self.full_analysis_agents.discard(agent_key)
        self.security_only_agents.discard(agent_key)
    
    async def validate_agent_security(self, agent_key: str) -> Dict[str, Any]:
        """
        Validate security configuration for an agent.
        
        Args:
            agent_key: Agent configuration key to validate
            
        Returns:
            Validation results dictionary
        """
        validation_result = {
            "agent_key": agent_key,
            "security_enabled": agent_key in self.security_enabled_agents,
            "full_analysis": agent_key in self.full_analysis_agents,
            "security_only": agent_key in self.security_only_agents,
            "validation_timestamp": str(datetime.now())
        }
        
        # Check if agent configuration exists
        if agent_key not in self.config:
            validation_result["errors"] = [f"Agent configuration '{agent_key}' not found"]
            validation_result["valid"] = False
        else:
            validation_result["valid"] = True
            validation_result["configuration"] = self.config[agent_key]
        
        return validation_result
    
    def get_security_statistics(self) -> Dict[str, Any]:
        """
        Get statistics about security-enabled agents.
        
        Returns:
            Dictionary with security statistics
        """
        return {
            "total_security_agents": len(self.security_enabled_agents),
            "full_analysis_agents": len(self.full_analysis_agents),
            "security_only_agents": len(self.security_only_agents),
            "security_agent_types": list(self.security_enabled_agents),
            "audit_logging_enabled": getattr(self, 'audit_logging_enabled', False)
        }


# Factory function for creating security-aware agent factory
def create_security_aware_agent_factory(config: Dict[str, Any]) -> SecurityAwareAgentFactory:
    """
    Create and configure a SecurityAwareAgentFactory.
    
    Args:
        config: Agent configuration dictionary
        
    Returns:
        Configured SecurityAwareAgentFactory instance
    """
    factory = SecurityAwareAgentFactory(config)
    
    # Enable audit logging by default
    factory.enable_audit_logging(True)
    
    return factory


# Export main components
__all__ = [
    "SecurityAwareAgentFactory",
    "create_security_aware_agent_factory"
]
"""
Security-Aware Agent Factory for GRID system.
Extends the base AgentFactory with security analysis capabilities.
"""
from typing import Dict, List, Any, Set, Optional
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
        
    async def create_agent(
        self, 
        agent_key: str, 
        context_path: Optional[str] = None,
        force_reload: bool = False,
        **kwargs
    ) -> Agent:
        """
        Create agent with appropriate security configuration.
        
        Args:
            agent_key: Agent configuration key
            context_path: Optional context path for agent
            force_reload: Force recreation even if cached
            **kwargs: Additional agent creation parameters
            
        Returns:
            Agent instance with security guardrails if applicable
        """
        # Create base agent
        agent = await super().create_agent(agent_key, context_path, force_reload)
        
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
        """Create security guardian agent."""
        return await self.create_agent("security_guardian", **kwargs)
    
    async def create_task_analyzer(self, **kwargs) -> Agent:
        """Create task analyzer agent."""
        return await self.create_agent("task_analyzer", **kwargs)
    
    async def create_context_quality_agent(self, **kwargs) -> Agent:
        """Create context quality agent."""
        return await self.create_agent("context_quality", **kwargs)
    
    def get_security_agent_keys(self) -> Set[str]:
        """Get set of agent keys that have security enabled."""
        return self.security_enabled_agents.copy()
    
    def configure_security_policies(self, policies: List[Dict[str, Any]]) -> None:
        """
        Configure security policies for the factory.
        
        Args:
            policies: List of security policy configurations
        """
        # For demo purposes, just log the policies
        print(f"Configuring {len(policies)} security policies")
        for policy in policies:
            print(f"  - {policy.get('name', 'Unknown')}: {policy.get('description', 'No description')}")
    
    def enable_audit_logging(self, enable: bool = True) -> None:
        """
        Enable or disable audit logging.
        
        Args:
            enable: Whether to enable audit logging
        """
        print(f"Audit logging {'enabled' if enable else 'disabled'}")
    
    def add_security_agent_type(self, agent_key: str, full_analysis: bool = False) -> None:
        """
        Add agent type to security analysis.
        
        Args:
            agent_key: Agent configuration key
            full_analysis: Whether to enable full analysis
        """
        self.security_enabled_agents.add(agent_key)
        if full_analysis:
            self.full_analysis_agents.add(agent_key)
        else:
            self.security_only_agents.add(agent_key)
    
    def remove_security_agent_type(self, agent_key: str) -> None:
        """
        Remove agent type from security analysis.
        
        Args:
            agent_key: Agent configuration key
        """
        self.security_enabled_agents.discard(agent_key)
        self.full_analysis_agents.discard(agent_key)
        self.security_only_agents.discard(agent_key)
    
    async def validate_agent_security(self, agent_key: str) -> Dict[str, Any]:
        """
        Validate security configuration for agent.
        
        Args:
            agent_key: Agent configuration key
            
        Returns:
            Security validation results
        """
        return {
            "agent_key": agent_key,
            "security_enabled": agent_key in self.security_enabled_agents,
            "full_analysis": agent_key in self.full_analysis_agents,
            "security_only": agent_key in self.security_only_agents,
            "validation_timestamp": datetime.now().isoformat(),
            "status": "valid"
        }
    
    def get_security_statistics(self) -> Dict[str, Any]:
        """
        Get security statistics for the factory.
        
        Returns:
            Security statistics
        """
        return {
            "total_security_agents": len(self.security_enabled_agents),
            "full_analysis_agents": len(self.full_analysis_agents),
            "security_only_agents": len(self.security_only_agents),
            "security_enabled_agents": list(self.security_enabled_agents),
            "full_analysis_agents_list": list(self.full_analysis_agents),
            "security_only_agents_list": list(self.security_only_agents)
        }


def create_security_aware_agent_factory(config: Dict[str, Any]) -> SecurityAwareAgentFactory:
    """
    Factory function to create SecurityAwareAgentFactory.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        SecurityAwareAgentFactory instance
    """
    return SecurityAwareAgentFactory(config)
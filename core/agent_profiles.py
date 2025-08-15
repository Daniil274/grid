"""
Agent Profiles system for simplified agent configuration.
Provides predefined templates for common agent patterns.
"""

from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum


class ProfileTemplate(Enum):
    """Predefined agent templates."""
    FILE_OPERATIONS = "file_operations"
    DEVELOPMENT = "development"
    ANALYSIS = "analysis"
    COORDINATION = "coordination"
    SECURITY = "security"


@dataclass
class AgentProfile:
    """Profile definition for agent capabilities and constraints."""
    
    name: str
    template: ProfileTemplate
    capabilities: List[str]
    tools: List[str]
    guardrails: List[str]
    max_tools_per_turn: int = 3
    context_strategy: str = "conversation"
    base_instructions: str = ""
    
    def merge_with_config(self, agent_config: Dict[str, Any]) -> Dict[str, Any]:
        """Merge profile with specific agent configuration."""
        merged = {
            "tools": self.tools.copy(),
            "guardrails": self.guardrails.copy(),
            "max_tools_per_turn": self.max_tools_per_turn,
            "context_strategy": self.context_strategy,
            "base_instructions": self.base_instructions
        }
        
        # Override with specific config
        merged.update(agent_config)
        
        # Merge lists instead of replacing
        if "tools" in agent_config:
            merged["tools"] = list(set(self.tools + agent_config["tools"]))
        if "guardrails" in agent_config:
            merged["guardrails"] = list(set(self.guardrails + agent_config["guardrails"]))
            
        return merged


class ProfileRegistry:
    """Registry for managing agent profiles."""
    
    def __init__(self):
        self._profiles = self._initialize_default_profiles()
    
    def _initialize_default_profiles(self) -> Dict[str, AgentProfile]:
        """Initialize built-in agent profiles."""
        return {
            "file_worker": AgentProfile(
                name="File Operations Worker",
                template=ProfileTemplate.FILE_OPERATIONS,
                capabilities=["read", "write", "search", "edit"],
                tools=["file_read", "file_write", "file_list", "file_search"],
                guardrails=["input_validation", "path_safety"],
                base_instructions="You are a specialized file operations assistant. Focus on efficient and safe file management."
            ),
            
            "code_assistant": AgentProfile(
                name="Development Assistant",
                template=ProfileTemplate.DEVELOPMENT,
                capabilities=["code_analysis", "git_operations", "testing", "documentation"],
                tools=["file_read", "file_write", "git_status", "git_commit", "code_analyzer"],
                guardrails=["input_validation", "code_safety", "git_validation"],
                base_instructions="You are a development assistant specializing in code analysis, testing, and version control."
            ),
            
            "data_analyst": AgentProfile(
                name="Data Analysis Specialist",
                template=ProfileTemplate.ANALYSIS,
                capabilities=["data_processing", "visualization", "reporting"],
                tools=["file_read", "data_processor", "chart_generator"],
                guardrails=["input_validation", "data_privacy"],
                base_instructions="You are a data analysis specialist focused on processing and visualizing data insights."
            ),
            
            "coordinator": AgentProfile(
                name="Task Coordinator",
                template=ProfileTemplate.COORDINATION,
                capabilities=["task_delegation", "workflow_management", "agent_orchestration"],
                tools=["file_agent", "git_agent", "analysis_agent"],
                guardrails=["input_validation", "task_validation"],
                max_tools_per_turn=5,
                base_instructions="You are a task coordinator responsible for delegating work to specialized agents."
            ),
            
            "security_guardian": AgentProfile(
                name="Security Guardian",
                template=ProfileTemplate.SECURITY,
                capabilities=["security_analysis", "threat_detection", "compliance_check"],
                tools=["security_scanner", "compliance_checker"],
                guardrails=["input_validation", "security_filtering", "output_sanitization"],
                base_instructions="You are a security specialist focused on identifying and mitigating security risks."
            )
        }
    
    def get_profile(self, profile_key: str) -> Optional[AgentProfile]:
        """Get profile by key."""
        return self._profiles.get(profile_key)
    
    def register_profile(self, key: str, profile: AgentProfile):
        """Register a new profile."""
        self._profiles[key] = profile
    
    def list_profiles(self) -> Dict[str, AgentProfile]:
        """List all available profiles."""
        return self._profiles.copy()
    
    def get_profiles_by_template(self, template: ProfileTemplate) -> List[AgentProfile]:
        """Get all profiles matching a template."""
        return [p for p in self._profiles.values() if p.template == template]


class ProfileManager:
    """Manager for applying profiles to agent configurations."""
    
    def __init__(self, registry: Optional[ProfileRegistry] = None):
        self.registry = registry or ProfileRegistry()
    
    def apply_profile(self, agent_config: Dict[str, Any], profile_key: str) -> Dict[str, Any]:
        """Apply a profile to agent configuration."""
        profile = self.registry.get_profile(profile_key)
        if not profile:
            raise ValueError(f"Profile '{profile_key}' not found")
        
        return profile.merge_with_config(agent_config)
    
    def validate_profile_config(self, config: Dict[str, Any]) -> bool:
        """Validate that profile configuration is valid."""
        if "profile" not in config:
            return True  # No profile specified, use as-is
        
        profile_key = config["profile"]
        profile = self.registry.get_profile(profile_key)
        
        if not profile:
            return False
        
        # Validate required capabilities are supported
        required_capabilities = config.get("required_capabilities", [])
        for capability in required_capabilities:
            if capability not in profile.capabilities:
                return False
        
        return True
    
    def enhance_agent_config(self, agent_key: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Enhance agent configuration with profile if specified."""
        if "profile" not in config:
            return config
        
        profile_key = config.pop("profile")
        enhanced_config = self.apply_profile(config, profile_key)
        
        # Add profile metadata
        enhanced_config["_profile_applied"] = profile_key
        enhanced_config["_enhanced_by"] = "ProfileManager"
        
        return enhanced_config
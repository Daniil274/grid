"""
GRID-specific agent management endpoints.
Provides extended functionality beyond OpenAI compatibility.
"""

import logging
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field

from api.dependencies import get_agent_factory, get_current_user, require_permission
from core.security_agent_factory import SecurityAwareAgentFactory

logger = logging.getLogger(__name__)
router = APIRouter()

# Request/Response models
class AgentInfo(BaseModel):
    """Agent information."""
    agent_type: str = Field(..., description="Agent type identifier")
    name: str = Field(..., description="Human-readable agent name")
    description: str = Field(..., description="Agent description")
    capabilities: List[str] = Field(..., description="Agent capabilities")
    tools: List[str] = Field(..., description="Available tools")
    status: str = Field(..., description="Agent status")
    model: str = Field(..., description="Underlying model")

class AgentExecutionRequest(BaseModel):
    """Request to execute an agent."""
    message: str = Field(..., description="Message to send to agent")
    context: Optional[Dict[str, Any]] = Field(None, description="Execution context")
    session_id: Optional[str] = Field(None, description="Session identifier")
    timeout: Optional[int] = Field(300, description="Execution timeout")

class AgentExecutionResponse(BaseModel):
    """Response from agent execution."""
    agent_type: str = Field(..., description="Agent that processed request")
    result: str = Field(..., description="Agent response")
    execution_time: float = Field(..., description="Execution time in seconds")
    tools_used: List[str] = Field(..., description="Tools used during execution")
    session_id: Optional[str] = Field(None, description="Session identifier")
    metadata: Dict[str, Any] = Field(..., description="Additional metadata")

class AgentCapabilitiesResponse(BaseModel):
    """Agent capabilities information."""
    agent_type: str = Field(..., description="Agent type")
    capabilities: List[str] = Field(..., description="Agent capabilities")
    tools: List[str] = Field(..., description="Available tools")
    supported_formats: List[str] = Field(..., description="Supported input/output formats")
    limitations: List[str] = Field(..., description="Known limitations")

@router.get("/", response_model=List[AgentInfo])
async def list_agents(
    agent_factory: SecurityAwareAgentFactory = Depends(get_agent_factory),
    user: Dict[str, Any] = Depends(get_current_user)
):
    """
    List all available GRID agents with their capabilities.
    """
    
    try:
        # Get available agents
        available_agents = agent_factory.get_available_agents()
        
        agents_info = []
        for agent_type, agent_config in available_agents.items():
            
            # Get agent information
            agent_info = AgentInfo(
                agent_type=agent_type,
                name=agent_config.get("name", agent_type),
                description=agent_config.get("description", f"GRID {agent_type} agent"),
                capabilities=_get_agent_capabilities(agent_type, agent_config),
                tools=agent_config.get("tools", []),
                status="available",
                model=agent_config.get("model", "unknown")
            )
            
            agents_info.append(agent_info)
        
        logger.info(f"Listed {len(agents_info)} available agents for user {user.get('user_id')}")
        return agents_info
        
    except Exception as e:
        logger.error(f"Error listing agents: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Failed to list agents", "details": str(e)}
        )

@router.get("/{agent_type}", response_model=AgentInfo)
async def get_agent_info(
    agent_type: str,
    agent_factory: SecurityAwareAgentFactory = Depends(get_agent_factory),
    user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Get detailed information about a specific agent.
    """
    
    try:
        # Check if agent exists
        available_agents = agent_factory.get_available_agents()
        if agent_type not in available_agents:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": f"Agent '{agent_type}' not found"}
            )
        
        agent_config = available_agents[agent_type]
        
        agent_info = AgentInfo(
            agent_type=agent_type,
            name=agent_config.get("name", agent_type),
            description=agent_config.get("description", f"GRID {agent_type} agent"),
            capabilities=_get_agent_capabilities(agent_type, agent_config),
            tools=agent_config.get("tools", []),
            status="available",
            model=agent_config.get("model", "unknown")
        )
        
        logger.debug(f"Retrieved info for agent: {agent_type}")
        return agent_info
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting agent info: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Failed to get agent info", "details": str(e)}
        )

@router.post("/{agent_type}/execute", response_model=AgentExecutionResponse)
async def execute_agent(
    agent_type: str,
    request: AgentExecutionRequest,
    agent_factory: SecurityAwareAgentFactory = Depends(get_agent_factory),
    user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Execute a specific agent with a message.
    """
    
    try:
        # Check if agent exists
        available_agents = agent_factory.get_available_agents()
        if agent_type not in available_agents:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": f"Agent '{agent_type}' not found"}
            )
        
        # Prepare context
        context = {
            "user_id": user.get("user_id"),
            "session_id": request.session_id,
            **(request.context or {})
        }
        
        # Create and execute agent
        import time
        start_time = time.time()
        
        agent = await agent_factory.create_agent(agent_type)
        result = await agent.run(request.message, context)
        
        execution_time = time.time() - start_time
        
        # Prepare response
        response = AgentExecutionResponse(
            agent_type=agent_type,
            result=result.content if hasattr(result, 'content') else str(result),
            execution_time=execution_time,
            tools_used=getattr(result, 'tools_used', []),
            session_id=request.session_id,
            metadata={
                "model": available_agents[agent_type].get("model"),
                "timestamp": time.time(),
                "context_size": len(str(context))
            }
        )
        
        logger.info(f"Agent {agent_type} executed in {execution_time:.2f}s for user {user.get('user_id')}")
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error executing agent {agent_type}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Agent execution failed", "details": str(e)}
        )

@router.get("/{agent_type}/capabilities", response_model=AgentCapabilitiesResponse)
async def get_agent_capabilities(
    agent_type: str,
    agent_factory: SecurityAwareAgentFactory = Depends(get_agent_factory),
    user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Get detailed capabilities of a specific agent.
    """
    
    try:
        # Check if agent exists
        available_agents = agent_factory.get_available_agents()
        if agent_type not in available_agents:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": f"Agent '{agent_type}' not found"}
            )
        
        agent_config = available_agents[agent_type]
        
        # Get detailed capabilities
        capabilities = _get_detailed_capabilities(agent_type, agent_config)
        tools = agent_config.get("tools", [])
        supported_formats = ["text", "json"]
        limitations = _get_agent_limitations(agent_type)
        
        response = AgentCapabilitiesResponse(
            agent_type=agent_type,
            capabilities=capabilities,
            tools=tools,
            supported_formats=supported_formats,
            limitations=limitations
        )
        
        logger.debug(f"Retrieved capabilities for agent: {agent_type}")
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting agent capabilities: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Failed to get agent capabilities", "details": str(e)}
        )

@router.post("/{agent_type}/validate")
async def validate_agent_request(
    agent_type: str,
    request: AgentExecutionRequest,
    agent_factory: SecurityAwareAgentFactory = Depends(get_agent_factory),
    user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Validate a request without executing it.
    Useful for checking if a request would be accepted.
    """
    
    try:
        # Check if agent exists
        available_agents = agent_factory.get_available_agents()
        if agent_type not in available_agents:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": f"Agent '{agent_type}' not found"}
            )
        
        # Validate message
        if not request.message or not request.message.strip():
            return {
                "valid": False,
                "error": "Message cannot be empty",
                "suggestions": ["Provide a non-empty message"]
            }
        
        # Check message length
        if len(request.message) > 50000:  # 50K character limit
            return {
                "valid": False,
                "error": "Message too long",
                "suggestions": ["Reduce message length to under 50,000 characters"]
            }
        
        # Validate context if provided
        suggestions = []
        if request.context:
            context_size = len(str(request.context))
            if context_size > 100000:  # 100K character limit for context
                suggestions.append("Context is very large, consider reducing it")
        
        # Check timeout
        if request.timeout and (request.timeout < 1 or request.timeout > 600):
            suggestions.append("Timeout should be between 1 and 600 seconds")
        
        return {
            "valid": True,
            "agent_type": agent_type,
            "estimated_execution_time": _estimate_execution_time(agent_type, request.message),
            "suggestions": suggestions
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error validating request: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Validation failed", "details": str(e)}
        )

# Helper functions
def _get_agent_capabilities(agent_type: str, agent_config: Dict[str, Any]) -> List[str]:
    """Get basic capabilities for an agent."""
    
    capabilities_map = {
        "coordinator": ["task_delegation", "agent_coordination", "general_assistance"],
        "code_agent": ["code_analysis", "bug_detection", "code_generation", "documentation"],
        "file_agent": ["file_management", "directory_operations", "file_search"],
        "git_agent": ["version_control", "repository_management", "commit_analysis"],
        "security_guardian": ["threat_analysis", "security_scanning", "policy_compliance"],
        "task_analyzer": ["task_analysis", "feasibility_assessment", "planning"],
        "context_quality": ["context_validation", "quality_assessment", "data_validation"],
        "researcher": ["research", "document_analysis", "information_extraction"],
        "thinker": ["deep_analysis", "problem_solving", "strategic_thinking"]
    }
    
    return capabilities_map.get(agent_type, ["general_assistance"])

def _get_detailed_capabilities(agent_type: str, agent_config: Dict[str, Any]) -> List[str]:
    """Get detailed capabilities for an agent."""
    
    base_capabilities = _get_agent_capabilities(agent_type, agent_config)
    
    # Add tool-based capabilities
    tools = agent_config.get("tools", [])
    tool_capabilities = []
    
    for tool in tools:
        if "file" in tool:
            tool_capabilities.append("file_operations")
        elif "git" in tool:
            tool_capabilities.append("git_operations")
        elif "security" in tool or "threat" in tool:
            tool_capabilities.append("security_operations")
    
    return list(set(base_capabilities + tool_capabilities))

def _get_agent_limitations(agent_type: str) -> List[str]:
    """Get known limitations for an agent."""
    
    limitations_map = {
        "coordinator": ["Cannot execute tools directly", "Requires other agents for specialized tasks"],
        "code_agent": ["Limited to text-based code analysis", "Cannot compile or execute code"],
        "file_agent": ["Cannot access files outside working directory", "Limited by file permissions"],
        "git_agent": ["Cannot push to remote repositories", "Limited to local git operations"],
        "security_guardian": ["Analysis based on patterns", "May have false positives"],
        "task_analyzer": ["Estimates may vary", "Cannot guarantee actual execution time"],
        "context_quality": ["Analysis is heuristic-based", "May miss subtle context issues"],
        "researcher": ["Limited to provided documents", "Cannot access external resources"],
        "thinker": ["No access to external tools", "Responses based on training data"]
    }
    
    return limitations_map.get(agent_type, ["General AI limitations apply"])

def _estimate_execution_time(agent_type: str, message: str) -> float:
    """Estimate execution time for an agent."""
    
    # Base execution times (in seconds)
    base_times = {
        "coordinator": 2.0,
        "code_agent": 5.0,
        "file_agent": 3.0,
        "git_agent": 4.0,
        "security_guardian": 3.0,
        "task_analyzer": 4.0,
        "context_quality": 2.0,
        "researcher": 6.0,
        "thinker": 8.0
    }
    
    base_time = base_times.get(agent_type, 5.0)
    
    # Adjust based on message length
    message_factor = min(len(message) / 1000, 3.0)  # Cap at 3x
    
    return base_time + message_factor
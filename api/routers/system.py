"""
System monitoring and health endpoints for GRID Agent System.
"""

import time
import psutil
import asyncio
import logging
from typing import Dict, Any, List
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from datetime import datetime

from api.dependencies import get_agent_factory, get_current_user, require_role
from core.security_agent_factory import SecurityAwareAgentFactory

logger = logging.getLogger(__name__)
router = APIRouter()

# Response models
class HealthResponse(BaseModel):
    """System health status."""
    status: str = Field(..., description="Overall system status")
    timestamp: str = Field(..., description="Health check timestamp")
    uptime: float = Field(..., description="System uptime in seconds")
    version: str = Field(..., description="API version")

class SystemStats(BaseModel):
    """System statistics."""
    cpu_usage: float = Field(..., description="CPU usage percentage")
    memory_usage: float = Field(..., description="Memory usage percentage")
    memory_used_gb: float = Field(..., description="Memory used in GB")
    memory_total_gb: float = Field(..., description="Total memory in GB")
    disk_usage: float = Field(..., description="Disk usage percentage")
    disk_free_gb: float = Field(..., description="Free disk space in GB")
    active_connections: int = Field(..., description="Active connections")
    uptime_seconds: float = Field(..., description="System uptime")

class AgentStats(BaseModel):
    """Agent usage statistics."""
    total_agents: int = Field(..., description="Total number of agents")
    active_sessions: int = Field(..., description="Active agent sessions")
    total_executions: int = Field(..., description="Total executions")
    executions_last_hour: int = Field(..., description="Executions in last hour")
    average_execution_time: float = Field(..., description="Average execution time")
    agent_usage: Dict[str, int] = Field(..., description="Usage by agent type")

class SecurityStatus(BaseModel):
    """Security system status."""
    threat_level: str = Field(..., description="Current threat level")
    active_threats: int = Field(..., description="Number of active threats")
    blocked_requests: int = Field(..., description="Blocked requests count")
    security_events_last_hour: int = Field(..., description="Security events in last hour")
    guardrails_active: bool = Field(..., description="Guardrails system status")

class SystemInfo(BaseModel):
    """Complete system information."""
    health: HealthResponse
    system_stats: SystemStats
    agent_stats: AgentStats
    security_status: SecurityStatus

# Store for tracking stats (in production, use Redis or database)
_stats_storage = {
    "total_executions": 0,
    "executions_history": [],
    "security_events": [],
    "blocked_requests": 0,
    "start_time": time.time()
}

@router.get("/health", response_model=HealthResponse)
async def get_system_health():
    """
    Get basic system health status.
    Public endpoint for monitoring.
    """
    
    uptime = time.time() - _stats_storage["start_time"]
    
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now().isoformat(),
        uptime=uptime,
        version="1.0.0"
    )

@router.get("/stats", response_model=SystemStats)
async def get_system_stats(
    user: Dict[str, Any] = Depends(require_role("admin"))
):
    """
    Get detailed system statistics.
    Requires admin role.
    """
    
    try:
        # Get system metrics
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Get network connections (approximate)
        connections = len(psutil.net_connections())
        
        uptime = time.time() - _stats_storage["start_time"]
        
        return SystemStats(
            cpu_usage=cpu_percent,
            memory_usage=memory.percent,
            memory_used_gb=memory.used / (1024**3),
            memory_total_gb=memory.total / (1024**3),
            disk_usage=disk.percent,
            disk_free_gb=disk.free / (1024**3),
            active_connections=connections,
            uptime_seconds=uptime
        )
        
    except Exception as e:
        logger.error(f"Error getting system stats: {e}")
        # Return default values if system metrics fail
        return SystemStats(
            cpu_usage=0.0,
            memory_usage=0.0,
            memory_used_gb=0.0,
            memory_total_gb=0.0,
            disk_usage=0.0,
            disk_free_gb=0.0,
            active_connections=0,
            uptime_seconds=time.time() - _stats_storage["start_time"]
        )

@router.get("/agents/stats", response_model=AgentStats)
async def get_agent_stats(
    agent_factory: SecurityAwareAgentFactory = Depends(get_agent_factory),
    user: Dict[str, Any] = Depends(require_role("admin"))
):
    """
    Get agent usage statistics.
    Requires admin role.
    """
    
    try:
        # Get available agents
        available_agents = agent_factory.get_available_agents()
        
        # Calculate stats from stored data
        current_time = time.time()
        hour_ago = current_time - 3600
        
        executions_last_hour = len([
            ex for ex in _stats_storage["executions_history"]
            if ex["timestamp"] > hour_ago
        ])
        
        # Calculate average execution time
        recent_executions = [
            ex for ex in _stats_storage["executions_history"]
            if ex["timestamp"] > current_time - 86400  # Last 24 hours
        ]
        
        avg_execution_time = 0.0
        if recent_executions:
            avg_execution_time = sum(ex["duration"] for ex in recent_executions) / len(recent_executions)
        
        # Agent usage stats
        agent_usage = {}
        for execution in recent_executions:
            agent_type = execution.get("agent_type", "unknown")
            agent_usage[agent_type] = agent_usage.get(agent_type, 0) + 1
        
        return AgentStats(
            total_agents=len(available_agents),
            active_sessions=0,  # Implement session tracking
            total_executions=_stats_storage["total_executions"],
            executions_last_hour=executions_last_hour,
            average_execution_time=avg_execution_time,
            agent_usage=agent_usage
        )
        
    except Exception as e:
        logger.error(f"Error getting agent stats: {e}")
        return AgentStats(
            total_agents=0,
            active_sessions=0,
            total_executions=0,
            executions_last_hour=0,
            average_execution_time=0.0,
            agent_usage={}
        )

@router.get("/security/status", response_model=SecurityStatus)
async def get_security_status(
    agent_factory: SecurityAwareAgentFactory = Depends(get_agent_factory),
    user: Dict[str, Any] = Depends(require_role("admin"))
):
    """
    Get security system status.
    Requires admin role.
    """
    
    try:
        # Check if security guardrails are active
        security_stats = agent_factory.get_security_statistics()
        guardrails_active = security_stats.get("total_security_agents", 0) > 0
        
        # Calculate security metrics
        current_time = time.time()
        hour_ago = current_time - 3600
        
        security_events_last_hour = len([
            event for event in _stats_storage["security_events"]
            if event["timestamp"] > hour_ago
        ])
        
        # Determine threat level based on recent activity
        threat_level = "LOW"
        if security_events_last_hour > 10:
            threat_level = "MEDIUM"
        if security_events_last_hour > 50:
            threat_level = "HIGH"
        
        return SecurityStatus(
            threat_level=threat_level,
            active_threats=0,  # Implement threat tracking
            blocked_requests=_stats_storage["blocked_requests"],
            security_events_last_hour=security_events_last_hour,
            guardrails_active=guardrails_active
        )
        
    except Exception as e:
        logger.error(f"Error getting security status: {e}")
        return SecurityStatus(
            threat_level="UNKNOWN",
            active_threats=0,
            blocked_requests=0,
            security_events_last_hour=0,
            guardrails_active=False
        )

@router.get("/info", response_model=SystemInfo)
async def get_system_info(
    agent_factory: SecurityAwareAgentFactory = Depends(get_agent_factory),
    user: Dict[str, Any] = Depends(require_role("admin"))
):
    """
    Get complete system information.
    Requires admin role.
    """
    
    # Get all components
    health = await get_system_health()
    system_stats = await get_system_stats(user)
    agent_stats = await get_agent_stats(agent_factory, user)
    security_status = await get_security_status(agent_factory, user)
    
    return SystemInfo(
        health=health,
        system_stats=system_stats,
        agent_stats=agent_stats,
        security_status=security_status
    )

@router.post("/restart")
async def restart_system(
    user: Dict[str, Any] = Depends(require_role("admin"))
):
    """
    Restart the system (graceful shutdown and restart).
    Requires admin role.
    """
    
    logger.warning(f"System restart requested by user {user.get('user_id')}")
    
    # In a real implementation, this would trigger a graceful shutdown
    # For now, just log the request
    
    return {
        "status": "restart_initiated",
        "message": "System restart has been initiated",
        "timestamp": datetime.now().isoformat(),
        "initiated_by": user.get("user_id")
    }

@router.get("/logs")
async def get_system_logs(
    lines: int = 100,
    level: str = "INFO",
    user: Dict[str, Any] = Depends(require_role("admin"))
):
    """
    Get recent system logs.
    Requires admin role.
    """
    
    try:
        # In a real implementation, read from log files
        # For now, return mock logs
        
        logs = []
        current_time = time.time()
        
        for i in range(lines):
            log_time = current_time - (i * 60)  # One log per minute
            logs.append({
                "timestamp": datetime.fromtimestamp(log_time).isoformat(),
                "level": level,
                "message": f"System log entry {i + 1}",
                "component": "api.main" if i % 2 == 0 else "core.agents"
            })
        
        return {
            "logs": logs,
            "total_lines": len(logs),
            "level_filter": level,
            "timestamp": datetime.now().isoformat()
        }
        
    except Exception as e:
        logger.error(f"Error getting logs: {e}")
        return {
            "error": "Failed to retrieve logs",
            "details": str(e)
        }

# Utility functions for tracking stats
def record_execution(agent_type: str, duration: float):
    """Record an agent execution for statistics."""
    _stats_storage["total_executions"] += 1
    _stats_storage["executions_history"].append({
        "agent_type": agent_type,
        "duration": duration,
        "timestamp": time.time()
    })
    
    # Keep only last 1000 executions
    if len(_stats_storage["executions_history"]) > 1000:
        _stats_storage["executions_history"] = _stats_storage["executions_history"][-1000:]

def record_security_event(event_type: str, details: Dict[str, Any]):
    """Record a security event for statistics."""
    _stats_storage["security_events"].append({
        "type": event_type,
        "details": details,
        "timestamp": time.time()
    })
    
    # Keep only last 1000 events
    if len(_stats_storage["security_events"]) > 1000:
        _stats_storage["security_events"] = _stats_storage["security_events"][-1000:]

def record_blocked_request():
    """Record a blocked request."""
    _stats_storage["blocked_requests"] += 1
"""
FastAPI dependencies for GRID Agent System API.
Provides dependency injection for common components.
"""

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Dict, Any
import logging

from core.security_agent_factory import SecurityAwareAgentFactory
from core.config import Config

logger = logging.getLogger(__name__)

# Security scheme
security = HTTPBearer(auto_error=False)

async def get_config(request: Request) -> Config:
    """Get application configuration."""
    if not hasattr(request.app.state, 'config'):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Configuration not initialized"
        )
    return request.app.state.config

async def get_agent_factory(request: Request) -> SecurityAwareAgentFactory:
    """Get agent factory from application state."""
    if not hasattr(request.app.state, 'agent_factory'):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Agent factory not initialized"
        )
    return request.app.state.agent_factory

async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Dict[str, Any]:
    """
    Get current user from authentication token.
    For now, returns a mock user - will be replaced with actual auth.
    """
    
    # Skip auth for health checks and docs
    if request.url.path in ["/health", "/", "/docs", "/redoc", "/openapi.json"]:
        return {"user_id": "anonymous", "roles": ["user"]}
    
    # If no credentials provided, return anonymous user
    if not credentials:
        return {"user_id": "anonymous", "roles": ["user"]}
    
    # Implement actual JWT validation
    # For now, accept any token and return mock user
    token = credentials.credentials
    
    # Basic token validation (just check if it's not empty)
    if not token or len(token) < 10:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Mock user data - replace with actual user lookup
    user_data = {
        "user_id": "user123",
        "username": "testuser", 
        "email": "test@example.com",
        "roles": ["user"],
        "permissions": ["agent:run", "session:create"]
    }
    
    logger.debug(f"Authenticated user: {user_data['user_id']}")
    return user_data

async def get_current_user_optional(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Optional[Dict[str, Any]]:
    """Get current user, but don't raise error if not authenticated."""
    try:
        return await get_current_user(request, credentials)
    except HTTPException:
        return None

def require_permission(permission: str):
    """Dependency that requires a specific permission."""
    async def check_permission(user: Dict[str, Any] = Depends(get_current_user)):
        user_permissions = user.get("permissions", [])
        if permission not in user_permissions and "admin" not in user.get("roles", []):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission}' required"
            )
        return user
    return check_permission

def require_role(role: str):
    """Dependency that requires a specific role."""
    async def check_role(user: Dict[str, Any] = Depends(get_current_user)):
        user_roles = user.get("roles", [])
        if role not in user_roles and "admin" not in user_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role}' required"
            )
        return user
    return check_role

async def get_request_id(request: Request) -> str:
    """Get or generate request ID for tracking."""
    import uuid
    
    # Try to get from header first
    request_id = request.headers.get("X-Request-ID")
    if not request_id:
        request_id = str(uuid.uuid4())
    
    # Store in request state for use in other dependencies
    request.state.request_id = request_id
    return request_id

async def get_client_info(request: Request) -> Dict[str, Any]:
    """Extract client information from request."""
    return {
        "ip": request.client.host if request.client else "unknown",
        "user_agent": request.headers.get("User-Agent", "unknown"),
        "referer": request.headers.get("Referer"),
        "forwarded_for": request.headers.get("X-Forwarded-For"),
        "real_ip": request.headers.get("X-Real-IP")
    }
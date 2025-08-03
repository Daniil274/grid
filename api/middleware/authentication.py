"""
Authentication middleware for GRID Agent System API.
Handles JWT token validation and user context.
"""

import time
import logging
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Callable

logger = logging.getLogger(__name__)

class AuthenticationMiddleware(BaseHTTPMiddleware):
    """Middleware for handling authentication."""
    
    def __init__(self, app, skip_paths: list = None):
        super().__init__(app)
        
        # Paths that don't require authentication
        self.skip_paths = skip_paths or [
            "/",
            "/health",
            "/docs",
            "/redoc", 
            "/openapi.json",
            "/favicon.ico"
        ]
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process authentication for requests."""
        start_time = time.time()
        
        # Skip authentication for certain paths
        if any(request.url.path.startswith(path) for path in self.skip_paths):
            response = await call_next(request)
            return response
        
        # Extract authorization header
        auth_header = request.headers.get("Authorization")
        
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            
            # Validate token (simplified for now)
            if await self._validate_token(token):
                # Add user context to request state
                request.state.authenticated = True
                request.state.user_id = "user123"  # TODO: Extract from token
                request.state.token = token
            else:
                request.state.authenticated = False
        else:
            request.state.authenticated = False
        
        # Continue with request
        response = await call_next(request)
        
        # Add timing header
        process_time = time.time() - start_time
        response.headers["X-Process-Time"] = str(process_time)
        
        return response
    
    async def _validate_token(self, token: str) -> bool:
        """Validate JWT token."""
        # TODO: Implement actual JWT validation
        # For now, just check if token is not empty and has minimum length
        return token and len(token) >= 10
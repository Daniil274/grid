"""
Logging middleware for GRID Agent System API.
Provides structured logging for all API requests and responses.
"""

import time
import uuid
import logging
import json
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Callable

logger = logging.getLogger(__name__)

class LoggingMiddleware(BaseHTTPMiddleware):
    """Middleware for request/response logging."""
    
    def __init__(self, app):
        super().__init__(app)
        
        # Paths to skip detailed logging
        self.skip_detailed_logging = [
            "/health",
            "/favicon.ico"
        ]
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Log request and response details."""
        
        # Generate request ID if not provided
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        
        # Start timing
        start_time = time.time()
        
        # Log request
        await self._log_request(request, request_id)
        
        # Process request
        response = await call_next(request)
        
        # Calculate processing time
        process_time = time.time() - start_time
        
        # Add headers
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time"] = f"{process_time:.4f}"
        
        # Log response
        await self._log_response(request, response, request_id, process_time)
        
        return response
    
    async def _log_request(self, request: Request, request_id: str):
        """Log incoming request."""
        
        # Skip detailed logging for certain paths
        if any(request.url.path.startswith(path) for path in self.skip_detailed_logging):
            return
        
        # Extract client info
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")
        
        # Extract user info if available
        user_id = getattr(request.state, 'user_id', 'anonymous')
        
        log_data = {
            "event": "api_request",
            "request_id": request_id,
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "query_params": dict(request.query_params),
            "client_ip": client_ip,
            "user_agent": user_agent,
            "user_id": user_id,
            "content_type": request.headers.get("content-type"),
            "content_length": request.headers.get("content-length"),
        }
        
        # Log request body for certain endpoints (be careful with sensitive data)
        if request.method in ["POST", "PUT", "PATCH"] and request.url.path.startswith("/v1/"):
            try:
                # Don't log large bodies
                content_length = int(request.headers.get("content-length", 0))
                if content_length < 10000:  # 10KB limit
                    # Note: This consumes the request body, which might cause issues
                    # In production, consider using a different approach
                    pass
            except Exception as e:
                logger.debug(f"Failed to log request body: {e}")
        
        logger.info(json.dumps(log_data))
    
    async def _log_response(self, request: Request, response: Response, request_id: str, process_time: float):
        """Log outgoing response."""
        
        # Skip detailed logging for certain paths
        if any(request.url.path.startswith(path) for path in self.skip_detailed_logging):
            return
        
        # Extract user info if available
        user_id = getattr(request.state, 'user_id', 'anonymous')
        
        log_data = {
            "event": "api_response",
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "process_time": process_time,
            "user_id": user_id,
            "response_size": response.headers.get("content-length"),
        }
        
        # Log level based on status code
        if response.status_code >= 500:
            logger.error(json.dumps(log_data))
        elif response.status_code >= 400:
            logger.warning(json.dumps(log_data))
        else:
            logger.info(json.dumps(log_data))
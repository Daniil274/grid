"""
Rate limiting middleware for GRID Agent System API.
Implements sliding window rate limiting with different limits for different endpoints.
"""

import time
import asyncio
from collections import defaultdict, deque
from fastapi import Request, Response, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Callable, Dict, Tuple
import logging

logger = logging.getLogger(__name__)

class RateLimitingMiddleware(BaseHTTPMiddleware):
    """Middleware for rate limiting API requests."""
    
    def __init__(self, app):
        super().__init__(app)
        
        # Rate limits: {endpoint_pattern: (requests_per_minute, requests_per_hour)}
        self.rate_limits = {
            # OpenAI compatible endpoints - higher limits
            "/v1/chat/completions": (60, 1000),
            "/v1/completions": (60, 1000),
            "/v1/models": (120, 2000),
            
            # Agent management
            "/v1/agents": (30, 500),
            
            # Authentication
            "/v1/auth": (20, 100),
            
            # System endpoints
            "/v1/system": (30, 300),
            
            # Default for other endpoints
            "default": (30, 500)
        }
        
        # Storage for request tracking: {client_id: {endpoint: deque of timestamps}}
        self.request_history: Dict[str, Dict[str, deque]] = defaultdict(lambda: defaultdict(deque))
        
        # Cleanup interval
        self.last_cleanup = time.time()
        self.cleanup_interval = 300  # 5 minutes
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Apply rate limiting to requests."""
        
        # Skip rate limiting for certain paths
        if self._should_skip_rate_limiting(request):
            return await call_next(request)
        
        # Get client identifier
        client_id = self._get_client_id(request)
        
        # Get endpoint pattern
        endpoint = self._get_endpoint_pattern(request.url.path)
        
        # Check rate limits
        if await self._is_rate_limited(client_id, endpoint):
            logger.warning(f"Rate limit exceeded for client {client_id} on endpoint {endpoint}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": {
                        "message": "Rate limit exceeded",
                        "type": "rate_limit_exceeded",
                        "code": "rate_limit"
                    }
                },
                headers={
                    "Retry-After": "60",
                    "X-RateLimit-Endpoint": endpoint
                }
            )
        
        # Record the request
        self._record_request(client_id, endpoint)
        
        # Periodic cleanup
        await self._cleanup_old_requests()
        
        # Process request
        response = await call_next(request)
        
        # Add rate limit headers
        self._add_rate_limit_headers(response, client_id, endpoint)
        
        return response
    
    def _should_skip_rate_limiting(self, request: Request) -> bool:
        """Check if rate limiting should be skipped for this request."""
        skip_paths = [
            "/health",
            "/",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/favicon.ico"
        ]
        
        return any(request.url.path.startswith(path) for path in skip_paths)
    
    def _get_client_id(self, request: Request) -> str:
        """Get client identifier for rate limiting."""
        # Try to get user ID from authentication
        user_id = getattr(request.state, 'user_id', None)
        if user_id and user_id != 'anonymous':
            return f"user:{user_id}"
        
        # Fall back to IP address
        client_ip = request.client.host if request.client else "unknown"
        
        # Check for forwarded IP
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            client_ip = forwarded_for.split(",")[0].strip()
        
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            client_ip = real_ip
        
        return f"ip:{client_ip}"
    
    def _get_endpoint_pattern(self, path: str) -> str:
        """Get rate limit pattern for endpoint."""
        for pattern in self.rate_limits:
            if pattern != "default" and path.startswith(pattern):
                return pattern
        return "default"
    
    async def _is_rate_limited(self, client_id: str, endpoint: str) -> bool:
        """Check if client is rate limited for endpoint."""
        current_time = time.time()
        
        # Get rate limits for this endpoint
        per_minute, per_hour = self.rate_limits.get(endpoint, self.rate_limits["default"])
        
        # Get request history for this client and endpoint
        request_times = self.request_history[client_id][endpoint]
        
        # Clean old requests outside the time windows
        minute_ago = current_time - 60
        hour_ago = current_time - 3600
        
        # Remove requests older than 1 hour
        while request_times and request_times[0] < hour_ago:
            request_times.popleft()
        
        # Count requests in last minute and hour
        requests_last_minute = sum(1 for t in request_times if t > minute_ago)
        requests_last_hour = len(request_times)
        
        # Check limits
        return (requests_last_minute >= per_minute or 
                requests_last_hour >= per_hour)
    
    def _record_request(self, client_id: str, endpoint: str):
        """Record a request for rate limiting."""
        current_time = time.time()
        self.request_history[client_id][endpoint].append(current_time)
    
    async def _cleanup_old_requests(self):
        """Periodically clean up old request records."""
        current_time = time.time()
        
        # Only cleanup every cleanup_interval seconds
        if current_time - self.last_cleanup < self.cleanup_interval:
            return
        
        self.last_cleanup = current_time
        hour_ago = current_time - 3600
        
        # Clean up old requests
        clients_to_remove = []
        for client_id, endpoints in self.request_history.items():
            endpoints_to_remove = []
            
            for endpoint, request_times in endpoints.items():
                # Remove old requests
                while request_times and request_times[0] < hour_ago:
                    request_times.popleft()
                
                # Mark empty endpoints for removal
                if not request_times:
                    endpoints_to_remove.append(endpoint)
            
            # Remove empty endpoints
            for endpoint in endpoints_to_remove:
                del endpoints[endpoint]
            
            # Mark empty clients for removal
            if not endpoints:
                clients_to_remove.append(client_id)
        
        # Remove empty clients
        for client_id in clients_to_remove:
            del self.request_history[client_id]
        
        logger.debug(f"Rate limit cleanup completed. Active clients: {len(self.request_history)}")
    
    def _add_rate_limit_headers(self, response: Response, client_id: str, endpoint: str):
        """Add rate limit headers to response."""
        current_time = time.time()
        minute_ago = current_time - 60
        hour_ago = current_time - 3600
        
        # Get rate limits
        per_minute, per_hour = self.rate_limits.get(endpoint, self.rate_limits["default"])
        
        # Get request history
        request_times = self.request_history[client_id][endpoint]
        
        # Count current usage
        requests_last_minute = sum(1 for t in request_times if t > minute_ago)
        requests_last_hour = sum(1 for t in request_times if t > hour_ago)
        
        # Add headers
        response.headers["X-RateLimit-Limit-Minute"] = str(per_minute)
        response.headers["X-RateLimit-Limit-Hour"] = str(per_hour)
        response.headers["X-RateLimit-Used-Minute"] = str(requests_last_minute)
        response.headers["X-RateLimit-Used-Hour"] = str(requests_last_hour)
        response.headers["X-RateLimit-Remaining-Minute"] = str(max(0, per_minute - requests_last_minute))
        response.headers["X-RateLimit-Remaining-Hour"] = str(max(0, per_hour - requests_last_hour))
        
        # Add reset time (next minute)
        next_reset = int(current_time) + (60 - int(current_time) % 60)
        response.headers["X-RateLimit-Reset"] = str(next_reset)
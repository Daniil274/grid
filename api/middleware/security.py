"""
Security middleware for GRID Agent System API.
Handles security headers, input validation, and threat detection.
"""

import time
import logging
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Callable
import json

logger = logging.getLogger(__name__)

class SecurityMiddleware(BaseHTTPMiddleware):
    """Middleware for security enforcement."""
    
    def __init__(self, app):
        super().__init__(app)
        
        # Security headers to add
        self.security_headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", 
            "X-XSS-Protection": "1; mode=block",
            "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Content-Security-Policy": "default-src 'self'"
        }
        
        # Rate limiting tracking
        self.request_counts = {}
        self.rate_limit_window = 60  # 1 minute
        self.rate_limit_max = 100    # 100 requests per minute
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process security for requests."""
        
        # Check rate limiting
        if await self._check_rate_limit(request):
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded"
            )
        
        # Validate request size
        if await self._check_request_size(request):
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Request too large"
            )
        
        # Process request
        response = await call_next(request)
        
        # Add security headers
        for header, value in self.security_headers.items():
            response.headers[header] = value
        
        # Add GRID-specific headers
        response.headers["X-GRID-API-Version"] = "1.0.0"
        response.headers["X-Powered-By"] = "GRID-Agent-System"
        
        return response
    
    async def _check_rate_limit(self, request: Request) -> bool:
        """Check if request exceeds rate limit."""
        client_ip = request.client.host if request.client else "unknown"
        current_time = time.time()
        
        # Clean old entries
        cutoff_time = current_time - self.rate_limit_window
        self.request_counts = {
            ip: [(timestamp, count) for timestamp, count in requests 
                 if timestamp > cutoff_time]
            for ip, requests in self.request_counts.items()
        }
        
        # Check current IP
        if client_ip not in self.request_counts:
            self.request_counts[client_ip] = []
        
        # Count recent requests
        recent_requests = sum(count for _, count in self.request_counts[client_ip])
        
        if recent_requests >= self.rate_limit_max:
            logger.warning(f"Rate limit exceeded for IP: {client_ip}")
            return True
        
        # Add current request
        self.request_counts[client_ip].append((current_time, 1))
        return False
    
    async def _check_request_size(self, request: Request) -> bool:
        """Check if request size is within limits."""
        max_size = 10 * 1024 * 1024  # 10MB
        
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > max_size:
            logger.warning(f"Request too large: {content_length} bytes")
            return True
        
        return False
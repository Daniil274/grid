"""
Authentication and authorization endpoints for GRID Agent System.
"""

import jwt
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field, EmailStr
from passlib.context import CryptContext

from api.dependencies import get_current_user_optional

logger = logging.getLogger(__name__)
router = APIRouter()

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT settings (in production, use environment variables)
SECRET_KEY = "your-secret-key-change-in-production"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

# Request/Response models
class UserLogin(BaseModel):
    """User login request."""
    username: str = Field(..., description="Username or email")
    password: str = Field(..., description="Password")

class UserRegister(BaseModel):
    """User registration request."""
    username: str = Field(..., min_length=3, max_length=50, description="Username")
    email: EmailStr = Field(..., description="Email address")
    password: str = Field(..., min_length=8, description="Password")
    full_name: Optional[str] = Field(None, description="Full name")

class Token(BaseModel):
    """JWT token response."""
    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field("bearer", description="Token type")
    expires_in: int = Field(..., description="Token expiration in seconds")
    user_id: str = Field(..., description="User identifier")

class UserProfile(BaseModel):
    """User profile information."""
    user_id: str = Field(..., description="User identifier")
    username: str = Field(..., description="Username")
    email: str = Field(..., description="Email address")
    full_name: Optional[str] = Field(None, description="Full name")
    roles: list[str] = Field(..., description="User roles")
    permissions: list[str] = Field(..., description="User permissions")
    created_at: str = Field(..., description="Account creation date")
    last_login: Optional[str] = Field(None, description="Last login date")

class TokenRefresh(BaseModel):
    """Token refresh request."""
    refresh_token: str = Field(..., description="Refresh token")

class PasswordChange(BaseModel):
    """Password change request."""
    current_password: str = Field(..., description="Current password")
    new_password: str = Field(..., min_length=8, description="New password")

# Mock user database (in production, use real database)
MOCK_USERS = {
    "testuser": {
        "user_id": "user123",
        "username": "testuser",
        "email": "test@example.com",
        "full_name": "Test User",
        "password_hash": pwd_context.hash("password123"),
        "roles": ["user"],
        "permissions": ["agent:run", "session:create"],
        "created_at": "2024-01-01T00:00:00",
        "last_login": None,
        "active": True
    },
    "admin": {
        "user_id": "admin123",
        "username": "admin",
        "email": "admin@example.com",
        "full_name": "Admin User",
        "password_hash": pwd_context.hash("admin123"),
        "roles": ["admin", "user"],
        "permissions": ["*"],
        "created_at": "2024-01-01T00:00:00",
        "last_login": None,
        "active": True
    }
}

@router.post("/login", response_model=Token)
async def login(user_login: UserLogin):
    """
    Authenticate user and return JWT token.
    """
    
    try:
        # Find user
        user = None
        for stored_user in MOCK_USERS.values():
            if (stored_user["username"] == user_login.username or 
                stored_user["email"] == user_login.username):
                user = stored_user
                break
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password"
            )
        
        # Verify password
        if not pwd_context.verify(user_login.password, user["password_hash"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password"
            )
        
        # Check if user is active
        if not user.get("active", True):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Account is disabled"
            )
        
        # Update last login
        user["last_login"] = datetime.now().isoformat()
        
        # Create JWT token
        token_data = {
            "sub": user["user_id"],
            "username": user["username"],
            "email": user["email"],
            "roles": user["roles"],
            "permissions": user["permissions"],
            "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
            "iat": datetime.utcnow(),
            "type": "access"
        }
        
        access_token = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)
        
        logger.info(f"User {user['username']} logged in successfully")
        
        return Token(
            access_token=access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user_id=user["user_id"]
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication service error"
        )

@router.post("/register", response_model=Token)
async def register(user_register: UserRegister):
    """
    Register a new user account.
    """
    
    try:
        # Check if username already exists
        for stored_user in MOCK_USERS.values():
            if (stored_user["username"] == user_register.username or 
                stored_user["email"] == user_register.email):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Username or email already exists"
                )
        
        # Create new user
        user_id = f"user{int(time.time())}"
        password_hash = pwd_context.hash(user_register.password)
        
        new_user = {
            "user_id": user_id,
            "username": user_register.username,
            "email": user_register.email,
            "full_name": user_register.full_name,
            "password_hash": password_hash,
            "roles": ["user"],
            "permissions": ["agent:run", "session:create"],
            "created_at": datetime.now().isoformat(),
            "last_login": None,
            "active": True
        }
        
        # Store user (in production, save to database)
        MOCK_USERS[user_register.username] = new_user
        
        # Create JWT token
        token_data = {
            "sub": user_id,
            "username": user_register.username,
            "email": user_register.email,
            "roles": new_user["roles"],
            "permissions": new_user["permissions"],
            "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
            "iat": datetime.utcnow(),
            "type": "access"
        }
        
        access_token = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)
        
        logger.info(f"New user {user_register.username} registered successfully")
        
        return Token(
            access_token=access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user_id=user_id
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Registration error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Registration service error"
        )

@router.get("/profile", response_model=UserProfile)
async def get_profile(
    current_user: Dict[str, Any] = Depends(get_current_user_optional)
):
    """
    Get current user profile.
    """
    
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )
    
    try:
        # Find user in mock database
        user = None
        for stored_user in MOCK_USERS.values():
            if stored_user["user_id"] == current_user.get("user_id"):
                user = stored_user
                break
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        return UserProfile(
            user_id=user["user_id"],
            username=user["username"],
            email=user["email"],
            full_name=user.get("full_name"),
            roles=user["roles"],
            permissions=user["permissions"],
            created_at=user["created_at"],
            last_login=user.get("last_login")
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Profile error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Profile service error"
        )

@router.post("/refresh", response_model=Token)
async def refresh_token(token_refresh: TokenRefresh):
    """
    Refresh JWT token.
    """
    
    try:
        # Decode refresh token
        payload = jwt.decode(token_refresh.refresh_token, SECRET_KEY, algorithms=[ALGORITHM])
        
        if payload.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type"
            )
        
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token"
            )
        
        # Find user
        user = None
        for stored_user in MOCK_USERS.values():
            if stored_user["user_id"] == user_id:
                user = stored_user
                break
        
        if not user or not user.get("active", True):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or disabled"
            )
        
        # Create new access token
        token_data = {
            "sub": user["user_id"],
            "username": user["username"],
            "email": user["email"],
            "roles": user["roles"],
            "permissions": user["permissions"],
            "exp": datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
            "iat": datetime.utcnow(),
            "type": "access"
        }
        
        access_token = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)
        
        return Token(
            access_token=access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user_id=user["user_id"]
        )
        
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token expired"
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Token refresh service error"
        )

@router.post("/change-password")
async def change_password(
    password_change: PasswordChange,
    current_user: Dict[str, Any] = Depends(get_current_user_optional)
):
    """
    Change user password.
    """
    
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )
    
    try:
        # Find user
        user = None
        for stored_user in MOCK_USERS.values():
            if stored_user["user_id"] == current_user.get("user_id"):
                user = stored_user
                break
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        # Verify current password
        if not pwd_context.verify(password_change.current_password, user["password_hash"]):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect"
            )
        
        # Update password
        user["password_hash"] = pwd_context.hash(password_change.new_password)
        
        logger.info(f"Password changed for user {user['username']}")
        
        return {"message": "Password changed successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Password change error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Password change service error"
        )

@router.post("/logout")
async def logout(
    current_user: Dict[str, Any] = Depends(get_current_user_optional)
):
    """
    Logout user (client should discard token).
    """
    
    if current_user:
        logger.info(f"User {current_user.get('username', 'unknown')} logged out")
    
    return {"message": "Logged out successfully"}

@router.get("/verify")
async def verify_token(
    current_user: Dict[str, Any] = Depends(get_current_user_optional)
):
    """
    Verify if current token is valid.
    """
    
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )
    
    return {
        "valid": True,
        "user_id": current_user.get("user_id"),
        "username": current_user.get("username"),
        "roles": current_user.get("roles", [])
    }
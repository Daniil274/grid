"""
WebSocket endpoints for real-time communication with GRID agents.
Provides streaming responses and real-time agent interaction.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Dict, Any, List
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, status
from fastapi.websockets import WebSocketState

from api.dependencies import get_agent_factory, get_websocket_user
from core.security_agent_factory import SecurityAwareAgentFactory
from utils.exceptions import GridError

logger = logging.getLogger(__name__)
router = APIRouter()

class ConnectionManager:
    """WebSocket connection manager for handling multiple client connections."""
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.user_sessions: Dict[str, List[str]] = {}  # user_id -> connection_ids
    
    async def connect(self, websocket: WebSocket, connection_id: str, user_id: str):
        """Accept new WebSocket connection."""
        await websocket.accept()
        self.active_connections[connection_id] = websocket
        
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = []
        self.user_sessions[user_id].append(connection_id)
        
        logger.info(f"WebSocket connected: {connection_id} for user {user_id}")
    
    def disconnect(self, connection_id: str, user_id: str):
        """Remove WebSocket connection."""
        if connection_id in self.active_connections:
            del self.active_connections[connection_id]
        
        if user_id in self.user_sessions:
            self.user_sessions[user_id] = [
                conn_id for conn_id in self.user_sessions[user_id] 
                if conn_id != connection_id
            ]
            if not self.user_sessions[user_id]:
                del self.user_sessions[user_id]
        
        logger.info(f"WebSocket disconnected: {connection_id}")
    
    async def send_personal_message(self, message: dict, connection_id: str):
        """Send message to specific connection."""
        if connection_id in self.active_connections:
            websocket = self.active_connections[connection_id]
            if websocket.client_state == WebSocketState.CONNECTED:
                try:
                    await websocket.send_text(json.dumps(message))
                except Exception as e:
                    logger.error(f"Error sending message to {connection_id}: {e}")
    
    async def broadcast_to_user(self, message: dict, user_id: str):
        """Send message to all connections of a user."""
        if user_id in self.user_sessions:
            for connection_id in self.user_sessions[user_id].copy():
                await self.send_personal_message(message, connection_id)
    
    def get_user_connections(self, user_id: str) -> List[str]:
        """Get all connection IDs for a user."""
        return self.user_sessions.get(user_id, [])

# Global connection manager
manager = ConnectionManager()

@router.websocket("/agents/{agent_type}")
async def websocket_agent_endpoint(
    websocket: WebSocket,
    agent_type: str,
    agent_factory: SecurityAwareAgentFactory = Depends(get_agent_factory)
):
    """
    WebSocket endpoint for real-time agent interaction.
    
    Message format:
    {
        "type": "message",
        "content": "user message",
        "session_id": "optional_session_id",
        "context": {...}
    }
    
    Response format:
    {
        "type": "response|chunk|error|status",
        "content": "response content",
        "chunk_id": 123,
        "agent": "agent_type",
        "execution_time": 1.23,
        "tools_used": [...],
        "session_id": "session_id"
    }
    """
    
    connection_id = str(uuid.uuid4())
    user_id = "anonymous"  # In production, get from auth
    
    try:
        await manager.connect(websocket, connection_id, user_id)
        
        # Send welcome message
        await manager.send_personal_message({
            "type": "status",
            "content": f"Connected to {agent_type} agent",
            "agent": agent_type,
            "connection_id": connection_id
        }, connection_id)
        
        while True:
            try:
                # Receive message from client
                data = await websocket.receive_text()
                message = json.loads(data)
                
                if message.get("type") != "message":
                    await manager.send_personal_message({
                        "type": "error",
                        "content": "Invalid message type. Expected 'message'",
                        "agent": agent_type
                    }, connection_id)
                    continue
                
                # Validate agent exists
                available_agents = agent_factory.get_available_agents()
                if agent_type not in available_agents:
                    await manager.send_personal_message({
                        "type": "error",
                        "content": f"Agent '{agent_type}' not found",
                        "agent": agent_type
                    }, connection_id)
                    continue
                
                # Process message with agent
                await process_agent_message(
                    agent_factory, agent_type, message, connection_id, user_id
                )
                
            except json.JSONDecodeError:
                await manager.send_personal_message({
                    "type": "error",
                    "content": "Invalid JSON format",
                    "agent": agent_type
                }, connection_id)
            except Exception as e:
                logger.error(f"Error processing WebSocket message: {e}")
                await manager.send_personal_message({
                    "type": "error",
                    "content": f"Processing error: {str(e)}",
                    "agent": agent_type
                }, connection_id)
    
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {connection_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        manager.disconnect(connection_id, user_id)

async def process_agent_message(
    agent_factory: SecurityAwareAgentFactory,
    agent_type: str,
    message: dict,
    connection_id: str,
    user_id: str
):
    """Process agent message and stream response."""
    
    start_time = time.time()
    
    try:
        # Send processing status
        await manager.send_personal_message({
            "type": "status",
            "content": "Processing...",
            "agent": agent_type
        }, connection_id)
        
        # Prepare context
        context = {
            "user_id": user_id,
            "session_id": message.get("session_id"),
            "websocket_connection": connection_id,
            **(message.get("context", {}))
        }
        
        # Create agent
        agent = await agent_factory.create_agent(agent_type)
        user_message = message.get("content", "")
        
        # Check if agent supports streaming
        if hasattr(agent, 'stream') and callable(getattr(agent, 'stream')):
            # Stream response
            chunk_count = 0
            async for chunk in agent.stream(user_message, context):
                chunk_count += 1
                
                await manager.send_personal_message({
                    "type": "chunk",
                    "content": chunk.content if hasattr(chunk, 'content') else str(chunk),
                    "chunk_id": chunk_count,
                    "agent": agent_type,
                    "session_id": context.get("session_id"),
                    "is_final": getattr(chunk, 'is_final', False)
                }, connection_id)
                
                # Check if final chunk
                if hasattr(chunk, 'is_final') and chunk.is_final:
                    break
                
                # Safety limit
                if chunk_count > 10000:
                    logger.warning("WebSocket streaming chunk limit reached")
                    break
        else:
            # Non-streaming agent
            result = await agent.run(user_message, context)
            
            await manager.send_personal_message({
                "type": "response",
                "content": result.content if hasattr(result, 'content') else str(result),
                "agent": agent_type,
                "session_id": context.get("session_id"),
                "tools_used": getattr(result, 'tools_used', [])
            }, connection_id)
        
        # Send completion status
        execution_time = time.time() - start_time
        await manager.send_personal_message({
            "type": "status",
            "content": "Completed",
            "agent": agent_type,
            "execution_time": execution_time
        }, connection_id)
        
    except GridError as e:
        await manager.send_personal_message({
            "type": "error",
            "content": f"Agent error: {str(e)}",
            "agent": agent_type,
            "error_type": "grid_error"
        }, connection_id)
    except Exception as e:
        logger.error(f"Error in agent processing: {e}")
        await manager.send_personal_message({
            "type": "error",
            "content": f"Unexpected error: {str(e)}",
            "agent": agent_type,
            "error_type": "server_error"
        }, connection_id)

@router.websocket("/broadcast")
async def websocket_broadcast_endpoint(websocket: WebSocket):
    """WebSocket endpoint for system broadcasts and notifications."""
    
    connection_id = str(uuid.uuid4())
    user_id = "broadcast"
    
    try:
        await manager.connect(websocket, connection_id, user_id)
        
        await manager.send_personal_message({
            "type": "status",
            "content": "Connected to broadcast channel",
            "connection_id": connection_id
        }, connection_id)
        
        # Keep connection alive and handle pings
        while True:
            try:
                data = await websocket.receive_text()
                message = json.loads(data)
                
                if message.get("type") == "ping":
                    await manager.send_personal_message({
                        "type": "pong",
                        "timestamp": time.time()
                    }, connection_id)
                
            except json.JSONDecodeError:
                await manager.send_personal_message({
                    "type": "error",
                    "content": "Invalid JSON format"
                }, connection_id)
    
    except WebSocketDisconnect:
        logger.info(f"Broadcast WebSocket disconnected: {connection_id}")
    finally:
        manager.disconnect(connection_id, user_id)

# Utility functions for external use
async def broadcast_system_message(message: str, message_type: str = "system"):
    """Broadcast system message to all connected clients."""
    broadcast_message = {
        "type": message_type,
        "content": message,
        "timestamp": time.time(),
        "system": True
    }
    
    # Send to all active connections
    for connection_id in list(manager.active_connections.keys()):
        await manager.send_personal_message(broadcast_message, connection_id)

async def send_user_notification(user_id: str, message: str, notification_type: str = "notification"):
    """Send notification to specific user."""
    notification = {
        "type": notification_type,
        "content": message,
        "timestamp": time.time()
    }
    
    await manager.broadcast_to_user(notification, user_id)

# Health check for WebSocket connections
def get_connection_stats() -> dict:
    """Get WebSocket connection statistics."""
    return {
        "total_connections": len(manager.active_connections),
        "unique_users": len(manager.user_sessions),
        "connections_per_user": {
            user_id: len(connections) 
            for user_id, connections in manager.user_sessions.items()
        }
    }
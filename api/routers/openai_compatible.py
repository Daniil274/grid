"""
OpenAI-compatible API endpoints for GRID Agent System.
Implements /v1/chat/completions, /v1/completions, and /v1/models endpoints.
"""

import asyncio
import time
import uuid
import logging
from typing import AsyncGenerator, Dict, Any
from fastapi import APIRouter, HTTPException, Depends, Request, status
from fastapi.responses import StreamingResponse
import json

from api.models.openai_models import (
    ChatCompletionRequest, ChatCompletionResponse,
    CompletionRequest, CompletionResponse,
    ModelList, ModelInfo, ChatCompletionChunk,
    ErrorResponse
)
from api.dependencies import get_agent_factory, get_current_user, get_request_id
from api.utils.openai_converter import OpenAIConverter
from core.security_agent_factory import SecurityAwareAgentFactory
from utils.exceptions import GridError

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/chat/completions")
async def create_chat_completion(
    request: ChatCompletionRequest,
    agent_factory: SecurityAwareAgentFactory = Depends(get_agent_factory),
    user: Dict[str, Any] = Depends(get_current_user),
    request_id: str = Depends(get_request_id)
):
    """
    Create a chat completion using OpenAI-compatible format.
    
    This endpoint accepts OpenAI-format requests and routes them to appropriate GRID agents
    based on the model name specified in the request.
    """
    
    try:
        # Validate model name (we accept any; further routing below)
        # Convert model to agent type with preference for direct agent keys
        available = agent_factory.get_available_agents()
        if request.model in available:
            agent_type = request.model
        else:
            agent_type = OpenAIConverter.model_to_agent(request.model)
        
        # Extract user message and context
        user_message = OpenAIConverter.extract_user_message(request.messages)
        conversation_context = OpenAIConverter.build_conversation_context(request.messages)
        
        # Prepare execution context
        context = {
            "user_id": user.get("user_id"),
            "session_id": request.grid_context.session_id if request.grid_context else None,
            "working_directory": request.grid_context.working_directory if request.grid_context else None,
            "tools_enabled": request.grid_context.tools_enabled if request.grid_context else True,
            "security_level": request.grid_context.security_level if request.grid_context else "standard",
            "timeout": request.grid_context.timeout if request.grid_context else 300,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "conversation_context": conversation_context,
            "openai_compatible": True,
            "request_id": request_id
        }
        
        # Add any additional context data
        if request.grid_context and request.grid_context.context_data:
            context.update(request.grid_context.context_data)
        
        logger.info(f"Chat completion request: model={request.model}, agent={agent_type}, user={user.get('user_id')}")
        
        if request.stream:
            # Streaming response
            return StreamingResponse(
                _stream_chat_completion(
                    agent_factory, agent_type, user_message, context, request, request_id
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no"  # Disable nginx buffering
                }
            )
        else:
            # Synchronous response
            return await _create_chat_completion(
                agent_factory, agent_type, user_message, context, request, request_id
            )
            
    except HTTPException:
        raise
    except GridError as e:
        logger.error(f"GRID error in chat completion: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=OpenAIConverter.format_error_response(e, request_id)
        )
    except Exception as e:
        logger.error(f"Unexpected error in chat completion: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=OpenAIConverter.format_error_response(e, request_id)
        )

async def _create_chat_completion(
    agent_factory: SecurityAwareAgentFactory,
    agent_type: str,
    user_message: str,
    context: Dict[str, Any],
    request: ChatCompletionRequest,
    request_id: str
) -> ChatCompletionResponse:
    """Create synchronous chat completion."""
    
    start_time = time.time()
    
    try:
        # Create agent
        logger.debug(f"Creating agent: {agent_type}")
        agent = await agent_factory.create_agent(agent_type)
        
        # Execute request with timeout
        timeout = context.get("timeout", 300)
        if hasattr(agent, 'run') and callable(getattr(agent, 'run')):
            result = await asyncio.wait_for(
                agent.run(user_message, context),
                timeout=timeout
            )
        else:
            # Fallback: use factory to execute agent (real SDK path)
            output_text = await asyncio.wait_for(
                agent_factory.run_agent(agent_type, user_message),
                timeout=timeout
            )
            result = output_text
        
        execution_time = time.time() - start_time
        logger.info(f"Agent execution completed in {execution_time:.2f}s")
        
        # Convert result to OpenAI format
        response = OpenAIConverter.agent_result_to_chat_completion(
            result=result,
            model=request.model,
            agent_type=agent_type,
            execution_time=execution_time,
            request_id=request_id,
            session_id=context.get("session_id")
        )
        
        return response
        
    except asyncio.TimeoutError:
        logger.error(f"Agent execution timeout after {timeout}s")
        raise GridError(f"Agent execution timeout after {timeout} seconds")
    except Exception as e:
        logger.error(f"Agent execution error: {e}")
        raise

async def _stream_chat_completion(
    agent_factory: SecurityAwareAgentFactory,
    agent_type: str,
    user_message: str,
    context: Dict[str, Any],
    request: ChatCompletionRequest,
    request_id: str
) -> AsyncGenerator[str, None]:
    """Create streaming chat completion."""
    
    completion_id = f"chatcmpl-grid-{request_id[:10]}"
    created = int(time.time())
    
    try:
        logger.debug(f"Starting streaming execution for agent: {agent_type}")
        
        # Create agent
        agent = await agent_factory.create_agent(agent_type)
        
        # Check if agent supports streaming
        if not hasattr(agent, 'stream') or not callable(getattr(agent, 'stream')):
            # Fallback to non-streaming for agents that don't support it
            logger.warning(f"Agent {agent_type} doesn't support streaming, falling back to sync")
            
            # Use factory execution when no stream capability
            output_text = await agent_factory.run_agent(agent_type, user_message)
            content = output_text
            
            # Send content in chunks to simulate streaming
            words = content.split()
            for i, word in enumerate(words):
                chunk_data = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": request.model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": word + (" " if i < len(words) - 1 else "")},
                        "finish_reason": None
                    }]
                }
                
                yield f"data: {json.dumps(chunk_data)}\n\n"
                
                # Small delay to simulate real streaming
                await asyncio.sleep(0.05)
            
            # Final chunk
            final_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": request.model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop"
                }]
            }
            
            yield f"data: {json.dumps(final_chunk)}\n\n"
            yield "data: [DONE]\n\n"
            return
        
        # True streaming support
        chunk_count = 0
        async for chunk in agent.stream(user_message, context):
            chunk_count += 1
            
            # Prepare chunk data
            chunk_data = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": request.model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": chunk.content} if hasattr(chunk, 'content') and chunk.content else {},
                    "finish_reason": "stop" if (hasattr(chunk, 'is_final') and chunk.is_final) else None
                }]
            }
            
            yield f"data: {json.dumps(chunk_data)}\n\n"
            
            # Check if this is the final chunk
            if hasattr(chunk, 'is_final') and chunk.is_final:
                break
            
            # Safety check to prevent infinite streaming
            if chunk_count > 10000:
                logger.warning("Streaming chunk limit reached")
                break
        
        # Send final chunk if not already sent
        final_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk", 
            "created": created,
            "model": request.model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop"
            }]
        }
        
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"
        
        logger.info(f"Streaming completed with {chunk_count} chunks")
        
    except asyncio.TimeoutError:
        error_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request.model,
            "error": {
                "message": "Request timeout",
                "type": "timeout_error"
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"
        
    except Exception as e:
        logger.error(f"Streaming error: {e}")
        error_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": request.model,
            "error": {
                "message": str(e),
                "type": "server_error"
            }
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"

@router.post("/completions")
async def create_completion(
    request: CompletionRequest,
    agent_factory: SecurityAwareAgentFactory = Depends(get_agent_factory),
    user: Dict[str, Any] = Depends(get_current_user),
    request_id: str = Depends(get_request_id)
):
    """
    Create a completion using legacy OpenAI format.
    Converts to chat completion format internally.
    """
    
    try:
        # Convert to chat completion format
        chat_request = ChatCompletionRequest(
            model=request.model,
            messages=[{"role": "user", "content": request.prompt}],
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            stream=request.stream,
            stop=request.stop,
            presence_penalty=request.presence_penalty,
            frequency_penalty=request.frequency_penalty,
            user=request.user
        )
        
        if request.stream:
            # Streaming completion
            return StreamingResponse(
                _stream_completion(chat_request, agent_factory, user, request_id),
                media_type="text/event-stream"
            )
        else:
            # Synchronous completion
            agent_type = OpenAIConverter.model_to_agent(request.model)
            context = {
                "user_id": user.get("user_id"),
                "openai_compatible": True,
                "request_id": request_id
            }
            
            chat_response = await _create_chat_completion(
                agent_factory, agent_type, request.prompt, context, chat_request, request_id
            )
            
            # Convert to legacy format
            return OpenAIConverter.chat_completion_to_completion(chat_response)
            
    except Exception as e:
        logger.error(f"Completion error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=OpenAIConverter.format_error_response(e, request_id)
        )

async def _stream_completion(
    chat_request: ChatCompletionRequest,
    agent_factory: SecurityAwareAgentFactory,
    user: Dict[str, Any],
    request_id: str
) -> AsyncGenerator[str, None]:
    """Stream completion in legacy format."""
    
    # This would need similar implementation to _stream_chat_completion
    # but converting to legacy completion chunk format
    # For brevity, implementing basic version
    
    completion_id = f"cmpl-grid-{request_id[:10]}"
    created = int(time.time())
    
    # Convert chat streaming to completion streaming
    agent_type = OpenAIConverter.model_to_agent(chat_request.model)
    context = {"user_id": user.get("user_id"), "request_id": request_id}
    
    try:
        agent = await agent_factory.create_agent(agent_type)
        user_message = chat_request.messages[-1].content
        
        if hasattr(agent, 'stream'):
            async for chunk in agent.stream(user_message, context):
                chunk_data = {
                    "id": completion_id,
                    "object": "text_completion",
                    "created": created,
                    "model": chat_request.model,
                    "choices": [{
                        "text": chunk.content if hasattr(chunk, 'content') else "",
                        "index": 0,
                        "finish_reason": "stop" if (hasattr(chunk, 'is_final') and chunk.is_final) else None
                    }]
                }
                
                yield f"data: {json.dumps(chunk_data)}\n\n"
                
                if hasattr(chunk, 'is_final') and chunk.is_final:
                    break
        
        yield "data: [DONE]\n\n"
        
    except Exception as e:
        logger.error(f"Completion streaming error: {e}")
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

@router.get("/models", response_model=ModelList)
async def list_models(
    agent_factory: SecurityAwareAgentFactory = Depends(get_agent_factory)
):
    """
    List available models (GRID agents).
    Returns OpenAI-compatible model list.
    """
    
    try:
        # Get available agents
        available_agents = agent_factory.get_available_agents()
        
        # Convert to model list
        models = OpenAIConverter.get_available_models(available_agents)
        
        logger.debug(f"Returning {len(models)} available models")
        
        return ModelList(object="list", data=models)
        
    except Exception as e:
        logger.error(f"Error listing models: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": {"message": "Failed to list models", "type": "server_error"}}
        )

@router.get("/models/{model}", response_model=ModelInfo)
async def retrieve_model(
    model: str,
    agent_factory: SecurityAwareAgentFactory = Depends(get_agent_factory)
):
    """
    Retrieve information about a specific model (GRID agent).
    """
    
    try:
        # Validate model exists
        if not OpenAIConverter.validate_model_name(model):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": {
                        "message": f"Model '{model}' not found",
                        "type": "invalid_request_error",
                        "param": "model",
                        "code": "model_not_found"
                    }
                }
            )
        
        # Get corresponding agent type
        agent_type = OpenAIConverter.model_to_agent(model)
        
        # Check if agent is available
        available_agents = agent_factory.get_available_agents()
        if agent_type not in available_agents:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": {
                        "message": f"Agent '{agent_type}' not available",
                        "type": "service_unavailable_error", 
                        "code": "agent_unavailable"
                    }
                }
            )
        
        # Create model info
        model_info = OpenAIConverter.create_model_info(model, agent_type)
        
        logger.debug(f"Retrieved model info for: {model}")
        return model_info
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving model {model}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": {"message": "Failed to retrieve model", "type": "server_error"}}
        )
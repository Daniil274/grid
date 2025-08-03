/**
 * TypeScript type definitions for GRID Agent System API
 */

// Base types
export interface Agent {
  agent_type: string;
  name: string;
  description: string;
  capabilities: string[];
  tools: string[];
  status: 'available' | 'busy' | 'error' | 'offline';
  model: string;
}

export interface AgentExecution {
  agent_type: string;
  result: string;
  execution_time: number;
  tools_used: string[];
  session_id?: string;
  metadata: Record<string, any>;
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp?: string;
  metadata?: Record<string, any>;
}

export interface ChatSession {
  session_id: string;
  messages: ChatMessage[];
  created_at: string;
  updated_at: string;
  agent_type?: string;
  user_id: string;
}

// OpenAI-compatible types
export interface OpenAIChatRequest {
  model: string;
  messages: ChatMessage[];
  max_tokens?: number;
  temperature?: number;
  top_p?: number;
  stream?: boolean;
  stop?: string | string[];
  presence_penalty?: number;
  frequency_penalty?: number;
  user?: string;
  grid_context?: {
    session_id?: string;
    working_directory?: string;
    tools_enabled?: boolean;
    security_level?: string;
    timeout?: number;
    context_data?: Record<string, any>;
  };
}

export interface OpenAIChatResponse {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: Array<{
    index: number;
    message: ChatMessage;
    finish_reason: string;
  }>;
  usage: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
  grid_metadata?: {
    agent_used: string;
    execution_time: number;
    tools_called: string[];
    security_analysis: Record<string, any>;
    session_id?: string;
    trace_id?: string;
    working_directory?: string;
  };
}

export interface ModelInfo {
  id: string;
  object: string;
  created: number;
  owned_by: string;
  description?: string;
  agent_type?: string;
  capabilities?: string[];
  tools?: string[];
}

// WebSocket types
export interface WebSocketMessage {
  type: 'message' | 'response' | 'chunk' | 'error' | 'status' | 'ping' | 'pong';
  content?: string;
  chunk_id?: number;
  agent?: string;
  execution_time?: number;
  tools_used?: string[];
  session_id?: string;
  connection_id?: string;
  is_final?: boolean;
  error_type?: string;
  timestamp?: number;
}

export interface ConnectionStatus {
  connected: boolean;
  connection_id?: string;
  agent_type?: string;
  last_ping?: number;
  reconnect_attempts?: number;
}

// UI State types
export interface AppState {
  selectedAgent: string | null;
  activeSession: string | null;
  connectionStatus: ConnectionStatus;
  theme: 'light' | 'dark';
  sidebarOpen: boolean;
  isLoading: boolean;
  error: string | null;
}

export interface AgentMetrics {
  agent_type: string;
  total_executions: number;
  avg_execution_time: number;
  success_rate: number;
  last_execution: string;
  error_count: number;
  tools_usage: Record<string, number>;
}

export interface SystemStats {
  total_agents: number;
  active_connections: number;
  total_sessions: number;
  system_uptime: number;
  memory_usage: number;
  cpu_usage: number;
  agent_metrics: AgentMetrics[];
}

// Error types
export interface APIError {
  error: {
    message: string;
    type: string;
    param?: string;
    code?: string;
  };
  id?: string;
}

// Request/Response types for specific endpoints
export interface AgentExecuteRequest {
  message: string;
  context?: Record<string, any>;
  session_id?: string;
  timeout?: number;
}

export interface AgentCapabilities {
  agent_type: string;
  capabilities: string[];
  tools: string[];
  supported_formats: string[];
  limitations: string[];
}

export interface ValidationResult {
  valid: boolean;
  error?: string;
  suggestions?: string[];
  agent_type?: string;
  estimated_execution_time?: number;
}

// Hook return types
export interface UseAgentsResult {
  agents: Agent[];
  isLoading: boolean;
  error: string | null;
  refetch: () => void;
}

export interface UseChatResult {
  messages: ChatMessage[];
  sendMessage: (content: string) => Promise<void>;
  isLoading: boolean;
  error: string | null;
  clearMessages: () => void;
}
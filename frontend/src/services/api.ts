/**
 * API service for communicating with GRID Agent System backend
 */

import axios, { AxiosInstance, AxiosResponse } from 'axios';
import { 
  Agent, 
  AgentExecution, 
  OpenAIChatRequest, 
  OpenAIChatResponse,
  ModelInfo,
  AgentExecuteRequest,
  AgentCapabilities,
  ValidationResult,
  SystemStats,
  APIError 
} from '@/types/api';

class APIService {
  private client: AxiosInstance;
  private baseURL: string;

  constructor() {
    this.baseURL = import.meta.env.VITE_API_URL || '/api';
    
    this.client = axios.create({
      baseURL: this.baseURL,
      timeout: 30000,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Request interceptor for auth tokens
    this.client.interceptors.request.use(
      (config) => {
        const token = localStorage.getItem('auth_token');
        if (token) {
          config.headers.Authorization = `Bearer ${token}`;
        }
        return config;
      },
      (error) => Promise.reject(error)
    );

    // Response interceptor for error handling
    this.client.interceptors.response.use(
      (response) => response,
      (error) => {
        if (error.response?.status === 401) {
          // Handle unauthorized - redirect to login
          localStorage.removeItem('auth_token');
          window.location.href = '/login';
        }
        return Promise.reject(this.formatError(error));
      }
    );
  }

  private formatError(error: any): APIError {
    if (error.response?.data?.error) {
      return error.response.data;
    }
    
    return {
      error: {
        message: error.message || 'Unknown error occurred',
        type: 'client_error',
        code: error.code,
      },
    };
  }

  // OpenAI-compatible endpoints
  async createChatCompletion(request: OpenAIChatRequest): Promise<OpenAIChatResponse> {
    const response: AxiosResponse<OpenAIChatResponse> = await this.client.post(
      '/v1/chat/completions',
      request
    );
    return response.data;
  }

  async listModels(): Promise<ModelInfo[]> {
    const response = await this.client.get('/v1/models');
    return response.data.data || [];
  }

  async getModel(modelId: string): Promise<ModelInfo> {
    const response = await this.client.get(`/v1/models/${modelId}`);
    return response.data;
  }

  // GRID-specific agent endpoints
  async listAgents(): Promise<Agent[]> {
    const response = await this.client.get('/v1/agents');
    return response.data;
  }

  async getAgent(agentType: string): Promise<Agent> {
    const response = await this.client.get(`/v1/agents/${agentType}`);
    return response.data;
  }

  async executeAgent(
    agentType: string, 
    request: AgentExecuteRequest
  ): Promise<AgentExecution> {
    const response = await this.client.post(
      `/v1/agents/${agentType}/execute`,
      request
    );
    return response.data;
  }

  async getAgentCapabilities(agentType: string): Promise<AgentCapabilities> {
    const response = await this.client.get(`/v1/agents/${agentType}/capabilities`);
    return response.data;
  }

  async validateAgentRequest(
    agentType: string,
    request: AgentExecuteRequest
  ): Promise<ValidationResult> {
    const response = await this.client.post(
      `/v1/agents/${agentType}/validate`,
      request
    );
    return response.data;
  }

  // System endpoints
  async getSystemHealth(): Promise<{ status: string; timestamp: number }> {
    const response = await this.client.get('/v1/system/health');
    return response.data;
  }

  async getSystemStats(): Promise<SystemStats> {
    const response = await this.client.get('/v1/system/stats');
    return response.data;
  }

  // Session management (future implementation)
  async createSession(agentType?: string): Promise<{ session_id: string }> {
    const response = await this.client.post('/v1/sessions', { agent_type: agentType });
    return response.data;
  }

  async getSession(sessionId: string): Promise<any> {
    const response = await this.client.get(`/v1/sessions/${sessionId}`);
    return response.data;
  }

  async clearSession(sessionId: string): Promise<void> {
    await this.client.post(`/v1/sessions/${sessionId}/clear`);
  }

  async deleteSession(sessionId: string): Promise<void> {
    await this.client.delete(`/v1/sessions/${sessionId}`);
  }

  // Streaming support (for non-WebSocket scenarios)
  async *streamChatCompletion(request: OpenAIChatRequest): AsyncGenerator<string, void, unknown> {
    const response = await fetch(`${this.baseURL}/v1/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem('auth_token') || ''}`,
      },
      body: JSON.stringify({ ...request, stream: true }),
    });

    if (!response.body) {
      throw new Error('No response body');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value);
        const lines = chunk.split('\n').filter(line => line.trim());

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6);
            if (data === '[DONE]') {
              return;
            }
            yield data;
          }
        }
      }
    } finally {
      reader.releaseLock();
    }
  }

  // File operations (if needed)
  async uploadFile(file: File, path?: string): Promise<{ filename: string; path: string }> {
    const formData = new FormData();
    formData.append('file', file);
    if (path) formData.append('path', path);

    const response = await this.client.post('/v1/files/upload', formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
    });
    return response.data;
  }

  async downloadFile(path: string): Promise<Blob> {
    const response = await this.client.get(`/v1/files/download`, {
      params: { path },
      responseType: 'blob',
    });
    return response.data;
  }

  // Authentication
  async login(username: string, password: string): Promise<{ token: string; user: any }> {
    const response = await this.client.post('/v1/auth/login', {
      username,
      password,
    });
    
    if (response.data.token) {
      localStorage.setItem('auth_token', response.data.token);
    }
    
    return response.data;
  }

  async logout(): Promise<void> {
    try {
      await this.client.post('/v1/auth/logout');
    } finally {
      localStorage.removeItem('auth_token');
    }
  }

  async getCurrentUser(): Promise<any> {
    const response = await this.client.get('/v1/auth/me');
    return response.data;
  }

  // Health check without auth
  async ping(): Promise<boolean> {
    try {
      await this.client.get('/health');
      return true;
    } catch {
      return false;
    }
  }
}

// Create singleton instance
export const apiService = new APIService();
export default apiService;
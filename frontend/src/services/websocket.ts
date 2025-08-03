/**
 * WebSocket service for real-time communication with GRID agents
 */

import { WebSocketMessage, ConnectionStatus } from '@/types/api';

export type MessageHandler = (message: WebSocketMessage) => void;
export type StatusHandler = (status: ConnectionStatus) => void;

class WebSocketService {
  private agentSocket: WebSocket | null = null;
  private messageHandlers: Set<MessageHandler> = new Set();
  private statusHandlers: Set<StatusHandler> = new Set();
  private connectionStatus: ConnectionStatus = { connected: false };
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private reconnectDelay = 1000;
  private pingInterval: number | null = null;

  constructor() {
    this.setupEventListeners();
  }

  private setupEventListeners() {
    // Handle page visibility for connection management
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        this.pauseConnection();
      } else {
        this.resumeConnection();
      }
    });

    // Handle network status
    window.addEventListener('online', () => {
      this.handleNetworkOnline();
    });

    window.addEventListener('offline', () => {
      this.handleNetworkOffline();
    });
  }

  // Connect to agent WebSocket
  async connectToAgent(agentType: string): Promise<void> {
    return new Promise((resolve, reject) => {
      try {
        const wsUrl = this.getWebSocketUrl(`/ws/agents/${agentType}`);
        this.agentSocket = new WebSocket(wsUrl);

        this.agentSocket.onopen = () => {
          console.log(`Connected to agent: ${agentType}`);
          this.connectionStatus = {
            connected: true,
            agent_type: agentType,
            last_ping: Date.now(),
            reconnect_attempts: 0,
          };
          this.reconnectAttempts = 0;
          this.notifyStatusChange();
          this.startPinging();
          resolve();
        };

        this.agentSocket.onmessage = (event) => {
          try {
            const message: WebSocketMessage = JSON.parse(event.data);
            this.handleMessage(message);
          } catch (error) {
            console.error('Failed to parse WebSocket message:', error);
          }
        };

        this.agentSocket.onclose = (event) => {
          console.log('Agent WebSocket closed:', event.code, event.reason);
          this.connectionStatus.connected = false;
          this.notifyStatusChange();
          this.stopPinging();

          // Attempt reconnection if not intentional close
          if (event.code !== 1000 && this.reconnectAttempts < this.maxReconnectAttempts) {
            this.scheduleReconnect(agentType);
          }
        };

        this.agentSocket.onerror = (error) => {
          console.error('Agent WebSocket error:', error);
          this.connectionStatus.connected = false;
          this.notifyStatusChange();
          reject(error);
        };

      } catch (error) {
        reject(error);
      }
    });
  }

  // Disconnect from agent
  disconnect(): void {
    this.stopPinging();
    
    if (this.agentSocket) {
      this.agentSocket.close(1000, 'Client disconnect');
      this.agentSocket = null;
    }


    this.connectionStatus = { connected: false };
    this.notifyStatusChange();
  }

  // Send message to agent
  sendMessage(content: string, sessionId?: string, context?: Record<string, any>): void {
    if (!this.agentSocket || this.agentSocket.readyState !== WebSocket.OPEN) {
      throw new Error('WebSocket not connected');
    }

    const message: WebSocketMessage = {
      type: 'message',
      content,
      session_id: sessionId,
      timestamp: Date.now(),
      ...(context && { context }),
    };

    this.agentSocket.send(JSON.stringify(message));
  }

  // Add message handler
  onMessage(handler: MessageHandler): () => void {
    this.messageHandlers.add(handler);
    return () => {
      this.messageHandlers.delete(handler);
    };
  }

  // Add status handler
  onStatusChange(handler: StatusHandler): () => void {
    this.statusHandlers.add(handler);
    return () => {
      this.statusHandlers.delete(handler);
    };
  }

  // Get current connection status
  getConnectionStatus(): ConnectionStatus {
    return { ...this.connectionStatus };
  }

  // Private methods
  private getWebSocketUrl(path: string): string {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = import.meta.env.VITE_WS_URL || window.location.host;
    return `${protocol}//${host}${path}`;
  }

  private handleMessage(message: WebSocketMessage): void {
    // Update connection status for certain message types
    if (message.type === 'status' && message.connection_id) {
      this.connectionStatus.connection_id = message.connection_id;
    }

    // Handle pong responses
    if (message.type === 'pong') {
      this.connectionStatus.last_ping = Date.now();
      return;
    }

    // Notify all handlers
    this.messageHandlers.forEach(handler => {
      try {
        handler(message);
      } catch (error) {
        console.error('Error in message handler:', error);
      }
    });
  }

  private notifyStatusChange(): void {
    this.statusHandlers.forEach(handler => {
      try {
        handler({ ...this.connectionStatus });
      } catch (error) {
        console.error('Error in status handler:', error);
      }
    });
  }

  private scheduleReconnect(agentType: string): void {
    this.reconnectAttempts++;
    const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1);
    
    console.log(`Scheduling reconnect attempt ${this.reconnectAttempts} in ${delay}ms`);
    
    setTimeout(() => {
      if (this.reconnectAttempts <= this.maxReconnectAttempts) {
        console.log(`Attempting to reconnect to ${agentType}...`);
        this.connectToAgent(agentType).catch(error => {
          console.error('Reconnection failed:', error);
        });
      }
    }, delay);
  }

  private startPinging(): void {
    this.stopPinging();
    this.pingInterval = setInterval(() => {
      if (this.agentSocket?.readyState === WebSocket.OPEN) {
        const pingMessage: WebSocketMessage = {
          type: 'ping',
          timestamp: Date.now(),
        };
        this.agentSocket.send(JSON.stringify(pingMessage));
      }
    }, 30000); // Ping every 30 seconds
  }

  private stopPinging(): void {
    if (this.pingInterval) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }

  private pauseConnection(): void {
    this.stopPinging();
  }

  private resumeConnection(): void {
    if (this.connectionStatus.connected) {
      this.startPinging();
    }
  }

  private handleNetworkOnline(): void {
    if (this.connectionStatus.agent_type && !this.connectionStatus.connected) {
      // Attempt to reconnect
      this.connectToAgent(this.connectionStatus.agent_type).catch(error => {
        console.error('Failed to reconnect after network came online:', error);
      });
    }
  }

  private handleNetworkOffline(): void {
    this.connectionStatus.connected = false;
    this.notifyStatusChange();
  }
}

// Create singleton instance
export const webSocketService = new WebSocketService();
export default webSocketService;
/**
 * Global application state management using Zustand
 */

import { create } from 'zustand';
import { devtools, persist } from 'zustand/middleware';
import { Agent, ChatMessage, ConnectionStatus } from '@/types/api';

interface AppState {
  // UI State
  theme: 'light' | 'dark';
  sidebarOpen: boolean;
  isLoading: boolean;
  error: string | null;

  // Agent State
  agents: Agent[];
  selectedAgent: string | null;
  agentMetrics: Record<string, any>;

  // Chat State
  activeSession: string | null;
  messages: ChatMessage[];
  connectionStatus: ConnectionStatus;

  // User State
  user: any | null;
  isAuthenticated: boolean;

  // Actions
  setTheme: (theme: 'light' | 'dark') => void;
  toggleSidebar: () => void;
  setLoading: (loading: boolean) => void;
  setError: (error: string | null) => void;
  
  setAgents: (agents: Agent[]) => void;
  setSelectedAgent: (agentType: string | null) => void;
  updateAgentMetrics: (agentType: string, metrics: any) => void;

  setActiveSession: (sessionId: string | null) => void;
  addMessage: (message: ChatMessage) => void;
  clearMessages: () => void;
  setConnectionStatus: (status: ConnectionStatus) => void;

  setUser: (user: any) => void;
  logout: () => void;
}

export const useAppStore = create<AppState>()(
  devtools(
    persist(
      (set) => ({
        // Initial state
        theme: 'light',
        sidebarOpen: true,
        isLoading: false,
        error: null,

        agents: [],
        selectedAgent: null,
        agentMetrics: {},

        activeSession: null,
        messages: [],
        connectionStatus: { connected: false },

        user: null,
        isAuthenticated: false,

        // UI Actions
        setTheme: (theme) =>
          set((state) => ({ ...state, theme }), false, 'setTheme'),

        toggleSidebar: () =>
          set((state) => ({ ...state, sidebarOpen: !state.sidebarOpen }), false, 'toggleSidebar'),

        setLoading: (isLoading) =>
          set((state) => ({ ...state, isLoading }), false, 'setLoading'),

        setError: (error) =>
          set((state) => ({ ...state, error }), false, 'setError'),

        // Agent Actions
        setAgents: (agents) =>
          set((state) => ({ ...state, agents }), false, 'setAgents'),

        setSelectedAgent: (selectedAgent) =>
          set(
            (state) => ({ 
              ...state, 
              selectedAgent,
              // Clear messages when changing agents
              messages: selectedAgent !== state.selectedAgent ? [] : state.messages 
            }),
            false,
            'setSelectedAgent'
          ),

        updateAgentMetrics: (agentType, metrics) =>
          set(
            (state) => ({
              ...state,
              agentMetrics: {
                ...state.agentMetrics,
                [agentType]: metrics,
              },
            }),
            false,
            'updateAgentMetrics'
          ),

        // Chat Actions
        setActiveSession: (activeSession) =>
          set((state) => ({ ...state, activeSession }), false, 'setActiveSession'),

        addMessage: (message) =>
          set(
            (state) => ({
              ...state,
              messages: [...state.messages, message],
            }),
            false,
            'addMessage'
          ),

        clearMessages: () =>
          set((state) => ({ ...state, messages: [] }), false, 'clearMessages'),

        setConnectionStatus: (connectionStatus) =>
          set((state) => ({ ...state, connectionStatus }), false, 'setConnectionStatus'),

        // User Actions
        setUser: (user) =>
          set(
            (state) => ({ 
              ...state, 
              user, 
              isAuthenticated: !!user 
            }),
            false,
            'setUser'
          ),

        logout: () =>
          set(
            (state) => ({
              ...state,
              user: null,
              isAuthenticated: false,
              activeSession: null,
              messages: [],
              connectionStatus: { connected: false },
            }),
            false,
            'logout'
          ),
      }),
      {
        name: 'grid-app-storage',
        partialize: (state) => ({
          theme: state.theme,
          sidebarOpen: state.sidebarOpen,
          selectedAgent: state.selectedAgent,
          // Don't persist sensitive data like user info or messages
        }),
      }
    ),
    {
      name: 'grid-app-store',
    }
  )
);

// Selectors for better performance
export const useTheme = () => useAppStore((state) => state.theme);
export const useSidebar = () => useAppStore((state) => ({
  isOpen: state.sidebarOpen,
  toggle: state.toggleSidebar,
}));

export const useAgents = () => useAppStore((state) => ({
  agents: state.agents,
  selectedAgent: state.selectedAgent,
  setAgents: state.setAgents,
  setSelectedAgent: state.setSelectedAgent,
}));

export const useChat = () => useAppStore((state) => ({
  messages: state.messages,
  activeSession: state.activeSession,
  connectionStatus: state.connectionStatus,
  addMessage: state.addMessage,
  clearMessages: state.clearMessages,
  setActiveSession: state.setActiveSession,
  setConnectionStatus: state.setConnectionStatus,
}));

export const useAuth = () => useAppStore((state) => ({
  user: state.user,
  isAuthenticated: state.isAuthenticated,
  setUser: state.setUser,
  logout: state.logout,
}));

export const useUI = () => useAppStore((state) => ({
  isLoading: state.isLoading,
  error: state.error,
  setLoading: state.setLoading,
  setError: state.setError,
}));
import React, { useState, useEffect, useRef } from 'react'
import {
  Box,
  Card,
  TextField,
  IconButton,
  Typography,
  Avatar,
  Chip,
  Divider,
  MenuItem,
  Select,
  FormControl,
  InputLabel,
  Paper,
  Tooltip,
  CircularProgress,
} from '@mui/material'
import {
  Send,
  SmartToy,
  Person,
  Clear,
  ContentCopy,
} from '@mui/icons-material'
import { useParams } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism'
import toast from 'react-hot-toast'

import { useChat, useAgents, useAppStore } from '@/store/appStore'
import { webSocketService } from '@/services/websocket'
import { WebSocketMessage } from '@/types/api'

export default function ChatPage() {
  const { agentType } = useParams<{ agentType: string }>()
  const { messages, addMessage, clearMessages, connectionStatus, setConnectionStatus } = useChat()
  const { agents, selectedAgent, setSelectedAgent } = useAgents()
  const { theme } = useAppStore()
  
  const [inputMessage, setInputMessage] = useState('')
  const [isLoading, setIsLoading] = useState(false)
  const [streamingMessage, setStreamingMessage] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)

  // Set selected agent from URL
  useEffect(() => {
    if (agentType && agentType !== selectedAgent) {
      setSelectedAgent(agentType)
    }
  }, [agentType, selectedAgent, setSelectedAgent])

  // Connect to WebSocket when agent changes
  useEffect(() => {
    if (selectedAgent) {
      connectToAgent(selectedAgent)
    }
    return () => {
      webSocketService.disconnect()
    }
  }, [selectedAgent])

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingMessage])

  // WebSocket connection and message handling
  useEffect(() => {
    const unsubscribeStatus = webSocketService.onStatusChange((status) => {
      setConnectionStatus(status)
    })

    const unsubscribeMessage = webSocketService.onMessage((message: WebSocketMessage) => {
      handleWebSocketMessage(message)
    })

    return () => {
      unsubscribeStatus()
      unsubscribeMessage()
    }
  }, [setConnectionStatus])

  const connectToAgent = async (agentType: string) => {
    try {
      setIsLoading(true)
      await webSocketService.connectToAgent(agentType)
      toast.success(`Connected to ${agentType}`)
    } catch (error) {
      toast.error(`Failed to connect to ${agentType}`)
      console.error('Connection error:', error)
    } finally {
      setIsLoading(false)
    }
  }

  const handleWebSocketMessage = (message: WebSocketMessage) => {
    switch (message.type) {
      case 'chunk':
        if (message.content) {
          setStreamingMessage(prev => prev + message.content)
        }
        if (message.is_final) {
          // Finalize streaming message
          addMessage({
            role: 'assistant',
            content: streamingMessage + (message.content || ''),
            timestamp: new Date().toISOString(),
          })
          setStreamingMessage('')
          setIsLoading(false)
        }
        break

      case 'response':
        if (message.content) {
          addMessage({
            role: 'assistant',
            content: message.content,
            timestamp: new Date().toISOString(),
          })
        }
        setIsLoading(false)
        break

      case 'error':
        toast.error(message.content || 'An error occurred')
        setIsLoading(false)
        setStreamingMessage('')
        break

      case 'status':
        if (message.content === 'Processing...') {
          setIsLoading(true)
        }
        break
    }
  }

  const handleSendMessage = async () => {
    if (!inputMessage.trim() || !selectedAgent || !connectionStatus.connected) {
      return
    }

    const userMessage = {
      role: 'user' as const,
      content: inputMessage.trim(),
      timestamp: new Date().toISOString(),
    }

    // Add user message to chat
    addMessage(userMessage)
    setInputMessage('')
    setIsLoading(true)
    setStreamingMessage('')

    try {
      // Send message via WebSocket
      webSocketService.sendMessage(userMessage.content)
    } catch (error) {
      toast.error('Failed to send message')
      setIsLoading(false)
      console.error('Send message error:', error)
    }
  }

  const handleKeyPress = (event: React.KeyboardEvent) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      handleSendMessage()
    }
  }

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text)
    toast.success('Copied to clipboard')
  }

  const currentAgent = agents.find(a => a.agent_type === selectedAgent)

  return (
    <Box sx={{ height: '100%', display: 'flex' }}>
      {/* Main Chat Area */}
      <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
        {/* Chat Header */}
        <Paper
          sx={{
            p: 2,
            borderRadius: 0,
            borderBottom: 1,
            borderColor: 'divider',
            display: 'flex',
            alignItems: 'center',
            gap: 2,
          }}
        >
          <Avatar sx={{ bgcolor: 'primary.main' }}>
            <SmartToy />
          </Avatar>
          
          <Box sx={{ flex: 1 }}>
            <FormControl size="small" sx={{ minWidth: 200 }}>
              <InputLabel>Select Agent</InputLabel>
              <Select
                value={selectedAgent || ''}
                onChange={(e) => setSelectedAgent(e.target.value)}
                label="Select Agent"
              >
                {agents.map((agent) => (
                  <MenuItem key={agent.agent_type} value={agent.agent_type}>
                    {agent.name}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </Box>

          {connectionStatus.connected && (
            <Chip
              label="Connected"
              color="success"
              size="small"
              icon={<SmartToy />}
            />
          )}

          <Tooltip title="Clear Chat">
            <IconButton onClick={clearMessages}>
              <Clear />
            </IconButton>
          </Tooltip>
        </Paper>

        {/* Messages Area */}
        <Box
          sx={{
            flex: 1,
            overflow: 'auto',
            p: 2,
            backgroundColor: theme === 'dark' ? '#1a1a1a' : '#f8f9fa',
          }}
        >
          <AnimatePresence>
            {messages.map((message, index) => (
              <motion.div
                key={index}
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -20 }}
                transition={{ duration: 0.3 }}
              >
                <MessageBubble
                  message={{
                    role: message.role,
                    content: message.content,
                    timestamp: message.timestamp || new Date().toISOString()
                  }}
                  onCopy={copyToClipboard}
                />
              </motion.div>
            ))}
          </AnimatePresence>

          {/* Streaming Message */}
          {streamingMessage && (
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
            >
              <MessageBubble
                message={{
                  role: 'assistant',
                  content: streamingMessage,
                  timestamp: new Date().toISOString(),
                }}
                isStreaming
                onCopy={copyToClipboard}
              />
            </motion.div>
          )}

          {/* Loading Indicator */}
          {isLoading && !streamingMessage && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
            >
              <Box
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 2,
                  mb: 2,
                  p: 2,
                }}
              >
                <Avatar sx={{ bgcolor: 'primary.main' }}>
                  <SmartToy />
                </Avatar>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <CircularProgress size={16} />
                  <Typography variant="body2" color="text.secondary">
                    {currentAgent?.name || 'Agent'} is thinking...
                  </Typography>
                </Box>
              </Box>
            </motion.div>
          )}

          <div ref={messagesEndRef} />
        </Box>

        {/* Input Area */}
        <Paper
          sx={{
            p: 2,
            borderRadius: 0,
            borderTop: 1,
            borderColor: 'divider',
          }}
        >
          <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-end' }}>
            <TextField
              fullWidth
              multiline
              maxRows={4}
              placeholder={
                connectionStatus.connected
                  ? `Message ${currentAgent?.name || 'Agent'}...`
                  : 'Select an agent to start chatting'
              }
              value={inputMessage}
              onChange={(e) => setInputMessage(e.target.value)}
              onKeyPress={handleKeyPress}
              disabled={!connectionStatus.connected || isLoading}
              variant="outlined"
              size="small"
            />
            <IconButton
              color="primary"
              onClick={handleSendMessage}
              disabled={!inputMessage.trim() || !connectionStatus.connected || isLoading}
              sx={{ mb: 0.5 }}
            >
              <Send />
            </IconButton>
          </Box>
        </Paper>
      </Box>

      {/* Sidebar (Agent Info) */}
      {currentAgent && (
        <Paper
          sx={{
            width: 300,
            borderLeft: 1,
            borderColor: 'divider',
            borderRadius: 0,
            p: 2,
            overflow: 'auto',
          }}
        >
          <Typography variant="h6" sx={{ mb: 2, fontWeight: 600 }}>
            Agent Information
          </Typography>

          <Box sx={{ mb: 3 }}>
            <Typography variant="subtitle2" sx={{ mb: 1 }}>
              {currentAgent.name}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              {currentAgent.description}
            </Typography>
            <Chip
              label={currentAgent.status}
              color={currentAgent.status === 'available' ? 'success' : 'error'}
              size="small"
            />
          </Box>

          <Divider sx={{ my: 2 }} />

          <Box sx={{ mb: 3 }}>
            <Typography variant="subtitle2" sx={{ mb: 1 }}>
              Capabilities
            </Typography>
            <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
              {currentAgent.capabilities.map((capability) => (
                <Chip
                  key={capability}
                  label={capability}
                  size="small"
                  variant="outlined"
                />
              ))}
            </Box>
          </Box>

          <Divider sx={{ my: 2 }} />

          <Box>
            <Typography variant="subtitle2" sx={{ mb: 1 }}>
              Available Tools
            </Typography>
            <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
              {currentAgent.tools.map((tool) => (
                <Chip
                  key={tool}
                  label={tool}
                  size="small"
                  variant="outlined"
                  color="primary"
                />
              ))}
            </Box>
          </Box>
        </Paper>
      )}
    </Box>
  )
}

// Message Bubble Component
interface MessageBubbleProps {
  message: {
    role: 'user' | 'assistant' | 'system'
    content: string
    timestamp: string
  }
  isStreaming?: boolean
  onCopy: (text: string) => void
}

function MessageBubble({ message, isStreaming, onCopy }: MessageBubbleProps) {
  const isUser = message.role === 'user'

  return (
    <Box
      sx={{
        display: 'flex',
        justifyContent: isUser ? 'flex-end' : 'flex-start',
        mb: 2,
      }}
    >
      <Box
        sx={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: 1,
          maxWidth: '80%',
          flexDirection: isUser ? 'row-reverse' : 'row',
        }}
      >
        <Avatar
          sx={{
            bgcolor: isUser ? 'secondary.main' : 'primary.main',
            width: 32,
            height: 32,
          }}
        >
          {isUser ? <Person /> : <SmartToy />}
        </Avatar>

        <Card
          sx={{
            p: 2,
            backgroundColor: isUser ? 'primary.main' : 'background.paper',
            color: isUser ? 'primary.contrastText' : 'text.primary',
          }}
        >
          <Box sx={{ position: 'relative' }}>
            {message.role === 'assistant' ? (
              <Box sx={{ '& pre': { fontSize: '0.875rem' } }}>
                <ReactMarkdown
                  components={{
                    code: ({ className, children, ...props }: any) => {
                      const inline = props.inline
                      const match = /language-(\w+)/.exec(className || '')
                      return !inline && match ? (
                        <SyntaxHighlighter
                          style={vscDarkPlus as any}
                          language={match[1]}
                          PreTag="div"
                          {...props}
                        >
                          {String(children).replace(/\n$/, '')}
                        </SyntaxHighlighter>
                      ) : (
                        <code className={className} {...props}>
                          {children}
                        </code>
                      )
                    },
                  }}
                >
                  {message.content}
                </ReactMarkdown>
              </Box>
            ) : (
              <Typography variant="body1" sx={{ whiteSpace: 'pre-wrap' }}>
                {message.content}
              </Typography>
            )}

            {isStreaming && (
              <Box
                component="span"
                sx={{
                  display: 'inline-block',
                  width: 8,
                  height: 16,
                  backgroundColor: 'currentColor',
                  ml: 0.5,
                  animation: 'blink 1s infinite',
                  '@keyframes blink': {
                    '0%, 50%': { opacity: 1 },
                    '51%, 100%': { opacity: 0 },
                  },
                }}
              />
            )}

            <Box
              sx={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                mt: 1,
                pt: 1,
                borderTop: 1,
                borderColor: isUser ? 'primary.light' : 'divider',
                opacity: 0.7,
              }}
            >
              <Typography variant="caption">
                {new Date(message.timestamp).toLocaleTimeString()}
              </Typography>
              <IconButton
                size="small"
                onClick={() => onCopy(message.content)}
                sx={{ color: 'inherit', opacity: 0.7 }}
              >
                <ContentCopy fontSize="small" />
              </IconButton>
            </Box>
          </Box>
        </Card>
      </Box>
    </Box>
  )
}
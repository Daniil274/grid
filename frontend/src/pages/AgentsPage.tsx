import React, { useState } from 'react'
import {
  Box,
  Grid,
  Card,
  CardContent,
  Typography,
  Chip,
  Button,
  Divider,
  TextField,
  InputAdornment,
  Tabs,
  Tab,
  Avatar,
  LinearProgress,
  Accordion,
  AccordionSummary,
  AccordionDetails,
} from '@mui/material'
import {
  Search,
  SmartToy,
  Launch,
  ExpandMore,
  Speed,
  CheckCircle,
  Error as ErrorIcon,
  Info,
} from '@mui/icons-material'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { useNavigate, useParams } from 'react-router-dom'

import { apiService } from '@/services/api'
import { useAgents } from '@/store/appStore'
import { Agent } from '@/types/api'

interface TabPanelProps {
  children?: React.ReactNode
  index: number
  value: number
}

function TabPanel({ children, value, index }: TabPanelProps) {
  return (
    <div
      role="tabpanel"
      hidden={value !== index}
      id={`agents-tabpanel-${index}`}
    >
      {value === index && <Box sx={{ py: 3 }}>{children}</Box>}
    </div>
  )
}

export default function AgentsPage() {
  const navigate = useNavigate()
  const { agentType } = useParams<{ agentType: string }>()
  const { agents, setSelectedAgent } = useAgents()
  
  const [searchTerm, setSearchTerm] = useState('')
  const [selectedTab, setSelectedTab] = useState(0)
  const [selectedAgentDetails, setSelectedAgentDetails] = useState<string | null>(
    agentType || null
  )

  // Fetch agents
  const { isLoading: agentsLoading } = useQuery({
    queryKey: ['agents'],
    queryFn: apiService.listAgents,
  })

  // Fetch agent capabilities for selected agent
  const { data: agentCapabilities } = useQuery({
    queryKey: ['agent-capabilities', selectedAgentDetails],
    queryFn: () => selectedAgentDetails 
      ? apiService.getAgentCapabilities(selectedAgentDetails)
      : null,
    enabled: !!selectedAgentDetails,
  })

  // Filter agents based on search and tab
  const filteredAgents = agents.filter((agent) => {
    const matchesSearch = 
      agent.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
      agent.description.toLowerCase().includes(searchTerm.toLowerCase()) ||
      agent.capabilities.some(cap => 
        cap.toLowerCase().includes(searchTerm.toLowerCase())
      )

    const matchesTab = 
      selectedTab === 0 || // All
      (selectedTab === 1 && agent.status === 'available') ||
      (selectedTab === 2 && agent.status === 'busy') ||
      (selectedTab === 3 && agent.status === 'offline')

    return matchesSearch && matchesTab
  })

  const handleStartChat = (agentType: string) => {
    setSelectedAgent(agentType)
    navigate(`/chat/${agentType}`)
  }

  const handleViewDetails = (agentType: string) => {
    setSelectedAgentDetails(agentType)
    navigate(`/agents/${agentType}`)
  }

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'available':
        return 'success'
      case 'busy':
        return 'warning'
      case 'error':
        return 'error'
      default:
        return 'default'
    }
  }

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'available':
        return <CheckCircle />
      case 'busy':
        return <Speed />
      case 'error':
        return <ErrorIcon />
      default:
        return <Info />
    }
  }

  const selectedAgent = agents.find(a => a.agent_type === selectedAgentDetails)

  return (
    <Box sx={{ height: '100%', display: 'flex' }}>
      {/* Main Content */}
      <Box sx={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
        {/* Header */}
        <Box sx={{ p: 3, pb: 0 }}>
          <Typography variant="h4" sx={{ fontWeight: 600, mb: 1 }}>
            Agents
          </Typography>
          <Typography variant="body1" color="text.secondary" sx={{ mb: 3 }}>
            Manage and interact with GRID AI agents
          </Typography>

          {/* Search and Filters */}
          <Box sx={{ display: 'flex', gap: 2, alignItems: 'center', mb: 2 }}>
            <TextField
              placeholder="Search agents..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              size="small"
              sx={{ width: 300 }}
              InputProps={{
                startAdornment: (
                  <InputAdornment position="start">
                    <Search />
                  </InputAdornment>
                ),
              }}
            />
          </Box>

          {/* Status Tabs */}
          <Tabs
            value={selectedTab}
            onChange={(_, newValue) => setSelectedTab(newValue)}
            sx={{ borderBottom: 1, borderColor: 'divider' }}
          >
            <Tab label={`All (${agents.length})`} />
            <Tab 
              label={`Available (${agents.filter(a => a.status === 'available').length})`} 
            />
            <Tab 
              label={`Busy (${agents.filter(a => a.status === 'busy').length})`} 
            />
            <Tab 
              label={`Offline (${agents.filter(a => a.status === 'offline').length})`} 
            />
          </Tabs>
        </Box>

        {/* Agents Grid */}
        <Box sx={{ flex: 1, overflow: 'auto', p: 3 }}>
          <TabPanel value={selectedTab} index={selectedTab}>
            {agentsLoading ? (
              <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
                <LinearProgress sx={{ width: '100%' }} />
              </Box>
            ) : (
              <Grid container spacing={3}>
                {filteredAgents.map((agent) => (
                  <Grid item xs={12} md={6} lg={4} key={agent.agent_type}>
                    <AgentCard
                      agent={agent}
                      onStartChat={handleStartChat}
                      onViewDetails={handleViewDetails}
                      isSelected={selectedAgentDetails === agent.agent_type}
                    />
                  </Grid>
                ))}
              </Grid>
            )}

            {filteredAgents.length === 0 && !agentsLoading && (
              <Box sx={{ textAlign: 'center', py: 8 }}>
                <SmartToy sx={{ fontSize: 64, color: 'text.secondary', mb: 2 }} />
                <Typography variant="h6" color="text.secondary">
                  No agents found
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  Try adjusting your search or filters
                </Typography>
              </Box>
            )}
          </TabPanel>
        </Box>
      </Box>

      {/* Agent Details Sidebar */}
      {selectedAgent && (
        <Box
          sx={{
            width: 400,
            borderLeft: 1,
            borderColor: 'divider',
            overflow: 'auto',
            p: 3,
          }}
        >
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 3 }}>
            <Avatar sx={{ bgcolor: 'primary.main', width: 48, height: 48 }}>
              <SmartToy />
            </Avatar>
            <Box>
              <Typography variant="h6" sx={{ fontWeight: 600 }}>
                {selectedAgent.name}
              </Typography>
              <Chip
                icon={getStatusIcon(selectedAgent.status)}
                label={selectedAgent.status}
                color={getStatusColor(selectedAgent.status) as any}
                size="small"
              />
            </Box>
          </Box>

          <Typography variant="body1" sx={{ mb: 3 }}>
            {selectedAgent.description}
          </Typography>

          <Divider sx={{ my: 3 }} />

          {/* Quick Actions */}
          <Box sx={{ mb: 3 }}>
            <Typography variant="h6" sx={{ mb: 2, fontWeight: 600 }}>
              Quick Actions
            </Typography>
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
              <Button
                variant="contained"
                startIcon={<Launch />}
                onClick={() => handleStartChat(selectedAgent.agent_type)}
                disabled={selectedAgent.status !== 'available'}
                fullWidth
              >
                Start Chat
              </Button>
            </Box>
          </Box>

          <Divider sx={{ my: 3 }} />

          {/* Agent Information */}
          <Box sx={{ mb: 3 }}>
            <Typography variant="h6" sx={{ mb: 2, fontWeight: 600 }}>
              Information
            </Typography>
            
            <Box sx={{ mb: 2 }}>
              <Typography variant="subtitle2" sx={{ mb: 1 }}>
                Model
              </Typography>
              <Chip label={selectedAgent.model} variant="outlined" size="small" />
            </Box>

            <Box sx={{ mb: 2 }}>
              <Typography variant="subtitle2" sx={{ mb: 1 }}>
                Capabilities
              </Typography>
              <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
                {selectedAgent.capabilities.map((capability) => (
                  <Chip
                    key={capability}
                    label={capability}
                    size="small"
                    variant="outlined"
                  />
                ))}
              </Box>
            </Box>

            <Box>
              <Typography variant="subtitle2" sx={{ mb: 1 }}>
                Tools ({selectedAgent.tools.length})
              </Typography>
              <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
                {selectedAgent.tools.slice(0, 5).map((tool) => (
                  <Chip
                    key={tool}
                    label={tool}
                    size="small"
                    variant="outlined"
                    color="primary"
                  />
                ))}
                {selectedAgent.tools.length > 5 && (
                  <Chip
                    label={`+${selectedAgent.tools.length - 5} more`}
                    size="small"
                    variant="outlined"
                  />
                )}
              </Box>
            </Box>
          </Box>

          {/* Detailed Capabilities */}
          {agentCapabilities && (
            <>
              <Divider sx={{ my: 3 }} />
              <Box>
                <Typography variant="h6" sx={{ mb: 2, fontWeight: 600 }}>
                  Detailed Capabilities
                </Typography>

                <Accordion>
                  <AccordionSummary expandIcon={<ExpandMore />}>
                    <Typography variant="subtitle2">
                      Supported Formats
                    </Typography>
                  </AccordionSummary>
                  <AccordionDetails>
                    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
                      {agentCapabilities.supported_formats.map((format) => (
                        <Chip
                          key={format}
                          label={format}
                          size="small"
                          variant="outlined"
                        />
                      ))}
                    </Box>
                  </AccordionDetails>
                </Accordion>

                <Accordion>
                  <AccordionSummary expandIcon={<ExpandMore />}>
                    <Typography variant="subtitle2">
                      Limitations
                    </Typography>
                  </AccordionSummary>
                  <AccordionDetails>
                    {agentCapabilities.limitations.map((limitation, index) => (
                      <Typography
                        key={index}
                        variant="body2"
                        sx={{ mb: 1, display: 'flex', alignItems: 'center', gap: 1 }}
                      >
                        • {limitation}
                      </Typography>
                    ))}
                  </AccordionDetails>
                </Accordion>
              </Box>
            </>
          )}
        </Box>
      )}
    </Box>
  )
}

// Agent Card Component
interface AgentCardProps {
  agent: Agent
  onStartChat: (agentType: string) => void
  onViewDetails: (agentType: string) => void
  isSelected: boolean
}

function AgentCard({ agent, onStartChat, onViewDetails, isSelected }: AgentCardProps) {
  const getStatusColor = (status: string) => {
    switch (status) {
      case 'available':
        return 'success'
      case 'busy':
        return 'warning'
      case 'error':
        return 'error'
      default:
        return 'default'
    }
  }

  return (
    <motion.div
      whileHover={{ y: -4 }}
      transition={{ duration: 0.2 }}
    >
      <Card
        sx={{
          height: '100%',
          display: 'flex',
          flexDirection: 'column',
          cursor: 'pointer',
          border: isSelected ? 2 : 1,
          borderColor: isSelected ? 'primary.main' : 'divider',
          '&:hover': {
            boxShadow: 4,
          },
        }}
        onClick={() => onViewDetails(agent.agent_type)}
      >
        <CardContent sx={{ flex: 1 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 2 }}>
            <Avatar sx={{ bgcolor: 'primary.main' }}>
              <SmartToy />
            </Avatar>
            <Box sx={{ flex: 1 }}>
              <Typography variant="h6" sx={{ fontWeight: 600 }}>
                {agent.name}
              </Typography>
              <Chip
                label={agent.status}
                color={getStatusColor(agent.status) as any}
                size="small"
              />
            </Box>
          </Box>

          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            {agent.description}
          </Typography>

          <Box sx={{ mb: 2 }}>
            <Typography variant="subtitle2" sx={{ mb: 1 }}>
              Capabilities
            </Typography>
            <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
              {agent.capabilities.slice(0, 3).map((capability) => (
                <Chip
                  key={capability}
                  label={capability}
                  size="small"
                  variant="outlined"
                />
              ))}
              {agent.capabilities.length > 3 && (
                <Chip
                  label={`+${agent.capabilities.length - 3}`}
                  size="small"
                  variant="outlined"
                />
              )}
            </Box>
          </Box>

          <Box>
            <Typography variant="subtitle2" sx={{ mb: 1 }}>
              Tools: {agent.tools.length}
            </Typography>
          </Box>
        </CardContent>

        <Box sx={{ p: 2, pt: 0 }}>
          <Button
            variant="contained"
            fullWidth
            startIcon={<Launch />}
            onClick={(e) => {
              e.stopPropagation()
              onStartChat(agent.agent_type)
            }}
            disabled={agent.status !== 'available'}
          >
            Start Chat
          </Button>
        </Box>
      </Card>
    </motion.div>
  )
}
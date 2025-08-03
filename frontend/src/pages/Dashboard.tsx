import {
  Box,
  Grid,
  Card,
  CardContent,
  Typography,
  Chip,
  Avatar,
  LinearProgress,
  Divider,
  IconButton,
  Tooltip,
  Button,
} from '@mui/material'
import {
  SmartToy,
  Computer,
  Memory,
  Chat as ChatIcon,
  Refresh,
  Launch,
} from '@mui/icons-material'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import { useNavigate } from 'react-router-dom'

import { apiService } from '@/services/api'
import { useAgents } from '@/store/appStore'

const containerVariants = {
  hidden: { opacity: 0 },
  visible: {
    opacity: 1,
    transition: {
      staggerChildren: 0.1,
    },
  },
}

const itemVariants = {
  hidden: { opacity: 0, y: 20 },
  visible: { opacity: 1, y: 0 },
}

export default function Dashboard() {
  const navigate = useNavigate()
  const { agents, setSelectedAgent } = useAgents()

  // Fetch agents
  const { isLoading: agentsLoading, refetch: refetchAgents } = useQuery({
    queryKey: ['agents'],
    queryFn: apiService.listAgents,
  })

  // Fetch system stats (mock for now)
  const { data: systemStats, refetch: refetchStats } = useQuery({
    queryKey: ['system-stats'],
    queryFn: async () => {
      // Mock data - replace with actual API call
      return {
        total_agents: agents.length,
        active_connections: 3,
        total_sessions: 15,
        system_uptime: 86400000, // 24 hours in ms
        memory_usage: 65,
        cpu_usage: 35,
        agent_metrics: agents.map(agent => ({
          agent_type: agent.agent_type,
          total_executions: Math.floor(Math.random() * 100),
          avg_execution_time: Math.random() * 5,
          success_rate: 85 + Math.random() * 15,
          last_execution: new Date().toISOString(),
          error_count: Math.floor(Math.random() * 5),
          tools_usage: {},
        })),
      }
    },
    enabled: agents.length > 0,
  })

  const handleStartChat = (agentType: string) => {
    setSelectedAgent(agentType)
    navigate(`/chat/${agentType}`)
  }

  const handleRefresh = () => {
    refetchAgents()
    refetchStats()
  }

  const formatUptime = (ms: number) => {
    const hours = Math.floor(ms / (1000 * 60 * 60))
    const minutes = Math.floor((ms % (1000 * 60 * 60)) / (1000 * 60))
    return `${hours}h ${minutes}m`
  }

  return (
    <Box sx={{ p: 3, height: '100%', overflow: 'auto' }}>
      <motion.div
        variants={containerVariants}
        initial="hidden"
        animate="visible"
      >
        {/* Header */}
        <motion.div variants={itemVariants}>
          <Box sx={{ mb: 4, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <Box>
              <Typography variant="h4" sx={{ fontWeight: 600, mb: 1 }}>
                Dashboard
              </Typography>
              <Typography variant="body1" color="text.secondary">
                Monitor your GRID Agent System performance and status
              </Typography>
            </Box>
            <Button
              variant="outlined"
              startIcon={<Refresh />}
              onClick={handleRefresh}
              disabled={agentsLoading}
            >
              Refresh
            </Button>
          </Box>
        </motion.div>

        {/* System Stats Cards */}
        <motion.div variants={itemVariants}>
          <Grid container spacing={3} sx={{ mb: 4 }}>
            <Grid item xs={12} sm={6} md={3}>
              <Card>
                <CardContent>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                    <Avatar sx={{ bgcolor: 'primary.main' }}>
                      <SmartToy />
                    </Avatar>
                    <Box>
                      <Typography variant="h4" sx={{ fontWeight: 600 }}>
                        {systemStats?.total_agents || 0}
                      </Typography>
                      <Typography variant="body2" color="text.secondary">
                        Active Agents
                      </Typography>
                    </Box>
                  </Box>
                </CardContent>
              </Card>
            </Grid>

            <Grid item xs={12} sm={6} md={3}>
              <Card>
                <CardContent>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                    <Avatar sx={{ bgcolor: 'success.main' }}>
                      <ChatIcon />
                    </Avatar>
                    <Box>
                      <Typography variant="h4" sx={{ fontWeight: 600 }}>
                        {systemStats?.active_connections || 0}
                      </Typography>
                      <Typography variant="body2" color="text.secondary">
                        Active Connections
                      </Typography>
                    </Box>
                  </Box>
                </CardContent>
              </Card>
            </Grid>

            <Grid item xs={12} sm={6} md={3}>
              <Card>
                <CardContent>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                    <Avatar sx={{ bgcolor: 'info.main' }}>
                      <Computer />
                    </Avatar>
                    <Box>
                      <Typography variant="h4" sx={{ fontWeight: 600 }}>
                        {systemStats?.cpu_usage || 0}%
                      </Typography>
                      <Typography variant="body2" color="text.secondary">
                        CPU Usage
                      </Typography>
                    </Box>
                  </Box>
                  <LinearProgress
                    variant="determinate"
                    value={systemStats?.cpu_usage || 0}
                    sx={{ mt: 1 }}
                  />
                </CardContent>
              </Card>
            </Grid>

            <Grid item xs={12} sm={6} md={3}>
              <Card>
                <CardContent>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
                    <Avatar sx={{ bgcolor: 'warning.main' }}>
                      <Memory />
                    </Avatar>
                    <Box>
                      <Typography variant="h4" sx={{ fontWeight: 600 }}>
                        {systemStats?.memory_usage || 0}%
                      </Typography>
                      <Typography variant="body2" color="text.secondary">
                        Memory Usage
                      </Typography>
                    </Box>
                  </Box>
                  <LinearProgress
                    variant="determinate"
                    value={systemStats?.memory_usage || 0}
                    color="warning"
                    sx={{ mt: 1 }}
                  />
                </CardContent>
              </Card>
            </Grid>
          </Grid>
        </motion.div>

        {/* Agents Grid */}
        <motion.div variants={itemVariants}>
          <Typography variant="h5" sx={{ fontWeight: 600, mb: 3 }}>
            Available Agents
          </Typography>

          {agentsLoading ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
              <LinearProgress sx={{ width: '100%' }} />
            </Box>
          ) : (
            <Grid container spacing={3}>
              {agents.map((agent) => {
                const metrics = systemStats?.agent_metrics?.find(
                  m => m.agent_type === agent.agent_type
                )

                return (
                  <Grid item xs={12} md={6} lg={4} key={agent.agent_type}>
                    <Card
                      sx={{
                        height: '100%',
                        display: 'flex',
                        flexDirection: 'column',
                        transition: 'all 0.3s ease',
                        '&:hover': {
                          transform: 'translateY(-4px)',
                          boxShadow: 4,
                        },
                      }}
                    >
                      <CardContent sx={{ flex: 1 }}>
                        <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', mb: 2 }}>
                          <Box>
                            <Typography variant="h6" sx={{ fontWeight: 600, mb: 1 }}>
                              {agent.name}
                            </Typography>
                            <Chip
                              label={agent.status}
                              color={agent.status === 'available' ? 'success' : 'error'}
                              size="small"
                            />
                          </Box>
                          <Tooltip title="Start Chat">
                            <IconButton
                              color="primary"
                              onClick={() => handleStartChat(agent.agent_type)}
                            >
                              <Launch />
                            </IconButton>
                          </Tooltip>
                        </Box>

                        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                          {agent.description}
                        </Typography>

                        <Divider sx={{ my: 2 }} />

                        {/* Agent Metrics */}
                        {metrics && (
                          <Box sx={{ mb: 2 }}>
                            <Typography variant="subtitle2" sx={{ mb: 1 }}>
                              Performance
                            </Typography>
                            <Box sx={{ display: 'flex', gap: 2, mb: 1 }}>
                              <Box sx={{ flex: 1 }}>
                                <Typography variant="caption" color="text.secondary">
                                  Executions
                                </Typography>
                                <Typography variant="body2" sx={{ fontWeight: 500 }}>
                                  {metrics.total_executions}
                                </Typography>
                              </Box>
                              <Box sx={{ flex: 1 }}>
                                <Typography variant="caption" color="text.secondary">
                                  Avg Time
                                </Typography>
                                <Typography variant="body2" sx={{ fontWeight: 500 }}>
                                  {metrics.avg_execution_time.toFixed(1)}s
                                </Typography>
                              </Box>
                              <Box sx={{ flex: 1 }}>
                                <Typography variant="caption" color="text.secondary">
                                  Success Rate
                                </Typography>
                                <Typography variant="body2" sx={{ fontWeight: 500 }}>
                                  {metrics.success_rate.toFixed(0)}%
                                </Typography>
                              </Box>
                            </Box>
                          </Box>
                        )}

                        {/* Capabilities */}
                        <Box>
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
                                sx={{ fontSize: '0.7rem' }}
                              />
                            ))}
                            {agent.capabilities.length > 3 && (
                              <Chip
                                label={`+${agent.capabilities.length - 3} more`}
                                size="small"
                                variant="outlined"
                                sx={{ fontSize: '0.7rem' }}
                              />
                            )}
                          </Box>
                        </Box>
                      </CardContent>

                      <Box sx={{ p: 2, pt: 0 }}>
                        <Button
                          variant="contained"
                          fullWidth
                          startIcon={<ChatIcon />}
                          onClick={() => handleStartChat(agent.agent_type)}
                        >
                          Start Chat
                        </Button>
                      </Box>
                    </Card>
                  </Grid>
                )
              })}
            </Grid>
          )}
        </motion.div>

        {/* System Info */}
        {systemStats && (
          <motion.div variants={itemVariants}>
            <Card sx={{ mt: 4 }}>
              <CardContent>
                <Typography variant="h6" sx={{ fontWeight: 600, mb: 2 }}>
                  System Information
                </Typography>
                <Grid container spacing={2}>
                  <Grid item xs={12} sm={6} md={3}>
                    <Typography variant="caption" color="text.secondary">
                      Uptime
                    </Typography>
                    <Typography variant="body1" sx={{ fontWeight: 500 }}>
                      {formatUptime(systemStats.system_uptime)}
                    </Typography>
                  </Grid>
                  <Grid item xs={12} sm={6} md={3}>
                    <Typography variant="caption" color="text.secondary">
                      Total Sessions
                    </Typography>
                    <Typography variant="body1" sx={{ fontWeight: 500 }}>
                      {systemStats.total_sessions}
                    </Typography>
                  </Grid>
                  <Grid item xs={12} sm={6} md={3}>
                    <Typography variant="caption" color="text.secondary">
                      Active Agents
                    </Typography>
                    <Typography variant="body1" sx={{ fontWeight: 500 }}>
                      {systemStats.total_agents}
                    </Typography>
                  </Grid>
                  <Grid item xs={12} sm={6} md={3}>
                    <Typography variant="caption" color="text.secondary">
                      Status
                    </Typography>
                    <Chip label="Online" color="success" size="small" />
                  </Grid>
                </Grid>
              </CardContent>
            </Card>
          </motion.div>
        )}
      </motion.div>
    </Box>
  )
}
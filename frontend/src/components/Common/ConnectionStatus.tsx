import {
  Box,
  Typography,
  Chip,
  Tooltip,
  useTheme,
} from '@mui/material'
import {
  Circle as CircleIcon,
  WifiOff,
  Wifi,
} from '@mui/icons-material'
import { motion } from 'framer-motion'

import { useChat } from '@/store/appStore'

export default function ConnectionStatus() {
  const theme = useTheme()
  const { connectionStatus } = useChat()

  const getStatusColor = () => {
    if (!connectionStatus.connected) return theme.palette.error.main
    return theme.palette.success.main
  }

  const getStatusText = () => {
    if (!connectionStatus.connected) return 'Disconnected'
    if (connectionStatus.agent_type) return `Connected to ${connectionStatus.agent_type}`
    return 'Connected'
  }

  const getStatusIcon = () => {
    if (!connectionStatus.connected) return <WifiOff />
    return <Wifi />
  }

  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
      <Tooltip title={`Last ping: ${connectionStatus.last_ping ? new Date(connectionStatus.last_ping).toLocaleTimeString() : 'Never'}`}>
        <motion.div
          animate={{
            scale: connectionStatus.connected ? [1, 1.1, 1] : 1,
          }}
          transition={{
            duration: 2,
            repeat: connectionStatus.connected ? Infinity : 0,
            ease: 'easeInOut',
          }}
        >
          <CircleIcon
            sx={{
              fontSize: 12,
              color: getStatusColor(),
            }}
          />
        </motion.div>
      </Tooltip>

      <Box sx={{ flex: 1 }}>
        <Typography variant="caption" sx={{ fontWeight: 500 }}>
          Connection Status
        </Typography>
        <Typography
          variant="body2"
          sx={{
            color: getStatusColor(),
            fontSize: '0.75rem',
          }}
        >
          {getStatusText()}
        </Typography>
      </Box>

      <Chip
        icon={getStatusIcon()}
        label={connectionStatus.connected ? 'Online' : 'Offline'}
        size="small"
        color={connectionStatus.connected ? 'success' : 'error'}
        variant="outlined"
        sx={{ fontSize: '0.7rem', height: 24 }}
      />
    </Box>
  )
}
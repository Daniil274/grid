import { Routes, Route, Navigate } from 'react-router-dom'
import { Box } from '@mui/material'

import Layout from '@/components/Layout/Layout'
import Dashboard from '@/pages/Dashboard'
import ChatPage from '@/pages/ChatPage'
import AgentsPage from '@/pages/AgentsPage'
import SettingsPage from '@/pages/SettingsPage'

function App() {
  // For now, bypass authentication - in production, implement proper auth flow
  const isAuth = true

  if (!isAuth) {
    return (
      <Routes>
        <Route path="/login" element={<div>Login Page (To be implemented)</div>} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    )
  }

  return (
    <Layout>
      <Box sx={{ height: '100%', overflow: 'hidden' }}>
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:agentType" element={<ChatPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/agents/:agentType" element={<AgentsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </Box>
    </Layout>
  )
}

export default App
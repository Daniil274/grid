import React, { useState } from 'react'
import {
  Box,
  Card,
  CardContent,
  Typography,
  Switch,
  Divider,
  Button,
  TextField,
  Select,
  MenuItem,
  FormControl,
  InputLabel,
  Chip,
  Alert,
  Tabs,
  Tab,
  List,
  ListItem,
  ListItemText,
  ListItemSecondaryAction,
  IconButton,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
} from '@mui/material'
import {
  Save,
  Refresh,
  Delete,
  Edit,
  Add,
  Download,
  Upload,
} from '@mui/icons-material'
import { motion } from 'framer-motion'
import toast from 'react-hot-toast'

import { useAppStore } from '@/store/appStore'

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
      id={`settings-tabpanel-${index}`}
    >
      {value === index && <Box sx={{ py: 3 }}>{children}</Box>}
    </div>
  )
}

export default function SettingsPage() {
  const { theme, setTheme } = useAppStore()
  const [selectedTab, setSelectedTab] = useState(0)
  const [settings, setSettings] = useState({
    autoConnect: true,
    showNotifications: true,
    streamingMode: true,
    darkMode: theme === 'dark',
    defaultAgent: 'coordinator',
    messageHistory: 50,
    autoSave: true,
    debugMode: false,
  })

  const [apiSettings, setApiSettings] = useState({
    baseUrl: 'http://localhost:8000',
    timeout: 30000,
    maxRetries: 3,
    enableWebSocket: true,
  })

  const [securitySettings, setSecuritySettings] = useState({
    requireAuth: false,
    sessionTimeout: 3600,
    enableEncryption: true,
    auditLogging: false,
  })

  const [newConnectionDialog, setNewConnectionDialog] = useState(false)

  const handleSettingChange = (key: string, value: any) => {
    setSettings(prev => ({ ...prev, [key]: value }))
    
    if (key === 'darkMode') {
      setTheme(value ? 'dark' : 'light')
    }
  }

  const handleApiSettingChange = (key: string, value: any) => {
    setApiSettings(prev => ({ ...prev, [key]: value }))
  }

  const handleSecuritySettingChange = (key: string, value: any) => {
    setSecuritySettings(prev => ({ ...prev, [key]: value }))
  }

  const handleSaveSettings = () => {
    // Save settings to localStorage or API
    localStorage.setItem('grid-settings', JSON.stringify({
      ...settings,
      apiSettings,
      securitySettings,
    }))
    toast.success('Settings saved successfully')
  }

  const handleResetSettings = () => {
    const defaultSettings = {
      autoConnect: true,
      showNotifications: true,
      streamingMode: true,
      darkMode: false,
      defaultAgent: 'coordinator',
      messageHistory: 50,
      autoSave: true,
      debugMode: false,
    }
    setSettings(defaultSettings)
    setTheme('light')
    toast.success('Settings reset to defaults')
  }

  const handleExportSettings = () => {
    const data = {
      settings,
      apiSettings,
      securitySettings,
      exportedAt: new Date().toISOString(),
    }
    
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: 'application/json',
    })
    
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `grid-settings-${new Date().toISOString().split('T')[0]}.json`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    
    toast.success('Settings exported successfully')
  }

  const mockConnections = [
    { id: '1', name: 'Local Development', url: 'http://localhost:8000', status: 'connected' },
    { id: '2', name: 'Staging Server', url: 'https://staging.grid.ai', status: 'disconnected' },
    { id: '3', name: 'Production Server', url: 'https://api.grid.ai', status: 'error' },
  ]

  return (
    <Box sx={{ height: '100%', overflow: 'auto', p: 3 }}>
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3 }}
      >
        {/* Header */}
        <Box sx={{ mb: 4 }}>
          <Typography variant="h4" sx={{ fontWeight: 600, mb: 1 }}>
            Settings
          </Typography>
          <Typography variant="body1" color="text.secondary">
            Configure your GRID Agent System preferences and connections
          </Typography>
        </Box>

        {/* Settings Tabs */}
        <Card sx={{ mb: 3 }}>
          <Tabs
            value={selectedTab}
            onChange={(_, newValue) => setSelectedTab(newValue)}
            sx={{ borderBottom: 1, borderColor: 'divider' }}
          >
            <Tab label="General" />
            <Tab label="API & Connections" />
            <Tab label="Security" />
            <Tab label="Advanced" />
          </Tabs>

          {/* General Settings */}
          <TabPanel value={selectedTab} index={0}>
            <CardContent>
              <Typography variant="h6" sx={{ mb: 3, fontWeight: 600 }}>
                General Preferences
              </Typography>

              <Box sx={{ mb: 3 }}>
                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                  <Box>
                    <Typography variant="subtitle1">Dark Mode</Typography>
                    <Typography variant="body2" color="text.secondary">
                      Switch between light and dark themes
                    </Typography>
                  </Box>
                  <Switch
                    checked={settings.darkMode}
                    onChange={(e) => handleSettingChange('darkMode', e.target.checked)}
                  />
                </Box>

                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                  <Box>
                    <Typography variant="subtitle1">Auto Connect</Typography>
                    <Typography variant="body2" color="text.secondary">
                      Automatically connect to agents when available
                    </Typography>
                  </Box>
                  <Switch
                    checked={settings.autoConnect}
                    onChange={(e) => handleSettingChange('autoConnect', e.target.checked)}
                  />
                </Box>

                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                  <Box>
                    <Typography variant="subtitle1">Show Notifications</Typography>
                    <Typography variant="body2" color="text.secondary">
                      Display system notifications and alerts
                    </Typography>
                  </Box>
                  <Switch
                    checked={settings.showNotifications}
                    onChange={(e) => handleSettingChange('showNotifications', e.target.checked)}
                  />
                </Box>

                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                  <Box>
                    <Typography variant="subtitle1">Streaming Mode</Typography>
                    <Typography variant="body2" color="text.secondary">
                      Enable real-time message streaming
                    </Typography>
                  </Box>
                  <Switch
                    checked={settings.streamingMode}
                    onChange={(e) => handleSettingChange('streamingMode', e.target.checked)}
                  />
                </Box>
              </Box>

              <Divider sx={{ my: 3 }} />

              <Box sx={{ mb: 3 }}>
                <FormControl fullWidth sx={{ mb: 2 }}>
                  <InputLabel>Default Agent</InputLabel>
                  <Select
                    value={settings.defaultAgent}
                    onChange={(e) => handleSettingChange('defaultAgent', e.target.value)}
                  >
                    <MenuItem value="coordinator">Coordinator</MenuItem>
                    <MenuItem value="code_agent">Code Agent</MenuItem>
                    <MenuItem value="file_agent">File Agent</MenuItem>
                    <MenuItem value="git_agent">Git Agent</MenuItem>
                  </Select>
                </FormControl>

                <TextField
                  fullWidth
                  label="Message History Limit"
                  type="number"
                  value={settings.messageHistory}
                  onChange={(e) => handleSettingChange('messageHistory', parseInt(e.target.value))}
                  helperText="Maximum number of messages to keep in memory"
                  sx={{ mb: 2 }}
                />
              </Box>
            </CardContent>
          </TabPanel>

          {/* API & Connections */}
          <TabPanel value={selectedTab} index={1}>
            <CardContent>
              <Typography variant="h6" sx={{ mb: 3, fontWeight: 600 }}>
                API Configuration
              </Typography>

              <Box sx={{ mb: 3 }}>
                <TextField
                  fullWidth
                  label="Base URL"
                  value={apiSettings.baseUrl}
                  onChange={(e) => handleApiSettingChange('baseUrl', e.target.value)}
                  sx={{ mb: 2 }}
                />

                <TextField
                  fullWidth
                  label="Timeout (ms)"
                  type="number"
                  value={apiSettings.timeout}
                  onChange={(e) => handleApiSettingChange('timeout', parseInt(e.target.value))}
                  sx={{ mb: 2 }}
                />

                <TextField
                  fullWidth
                  label="Max Retries"
                  type="number"
                  value={apiSettings.maxRetries}
                  onChange={(e) => handleApiSettingChange('maxRetries', parseInt(e.target.value))}
                  sx={{ mb: 2 }}
                />

                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                  <Box>
                    <Typography variant="subtitle1">Enable WebSocket</Typography>
                    <Typography variant="body2" color="text.secondary">
                      Use WebSocket for real-time communication
                    </Typography>
                  </Box>
                  <Switch
                    checked={apiSettings.enableWebSocket}
                    onChange={(e) => handleApiSettingChange('enableWebSocket', e.target.checked)}
                  />
                </Box>
              </Box>

              <Divider sx={{ my: 3 }} />

              <Typography variant="h6" sx={{ mb: 2, fontWeight: 600 }}>
                Saved Connections
              </Typography>

              <List>
                {mockConnections.map((connection) => (
                  <ListItem key={connection.id}>
                    <ListItemText
                      primary={connection.name}
                      secondary={
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 0.5 }}>
                          <Typography variant="body2" color="text.secondary">
                            {connection.url}
                          </Typography>
                          <Chip
                            label={connection.status}
                            size="small"
                            color={
                              connection.status === 'connected' ? 'success' :
                              connection.status === 'error' ? 'error' : 'default'
                            }
                          />
                        </Box>
                      }
                    />
                    <ListItemSecondaryAction>
                      <IconButton
                        edge="end"
                        onClick={() => console.log('Edit connection:', connection.id)}
                      >
                        <Edit />
                      </IconButton>
                      <IconButton edge="end">
                        <Delete />
                      </IconButton>
                    </ListItemSecondaryAction>
                  </ListItem>
                ))}
              </List>

              <Button
                startIcon={<Add />}
                onClick={() => setNewConnectionDialog(true)}
                sx={{ mt: 2 }}
              >
                Add Connection
              </Button>
            </CardContent>
          </TabPanel>

          {/* Security Settings */}
          <TabPanel value={selectedTab} index={2}>
            <CardContent>
              <Typography variant="h6" sx={{ mb: 3, fontWeight: 600 }}>
                Security & Privacy
              </Typography>

              <Alert severity="info" sx={{ mb: 3 }}>
                Security settings help protect your data and control access to the system.
              </Alert>

              <Box sx={{ mb: 3 }}>
                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                  <Box>
                    <Typography variant="subtitle1">Require Authentication</Typography>
                    <Typography variant="body2" color="text.secondary">
                      Force users to authenticate before accessing agents
                    </Typography>
                  </Box>
                  <Switch
                    checked={securitySettings.requireAuth}
                    onChange={(e) => handleSecuritySettingChange('requireAuth', e.target.checked)}
                  />
                </Box>

                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                  <Box>
                    <Typography variant="subtitle1">Enable Encryption</Typography>
                    <Typography variant="body2" color="text.secondary">
                      Encrypt data in transit and at rest
                    </Typography>
                  </Box>
                  <Switch
                    checked={securitySettings.enableEncryption}
                    onChange={(e) => handleSecuritySettingChange('enableEncryption', e.target.checked)}
                  />
                </Box>

                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                  <Box>
                    <Typography variant="subtitle1">Audit Logging</Typography>
                    <Typography variant="body2" color="text.secondary">
                      Log all user actions for security auditing
                    </Typography>
                  </Box>
                  <Switch
                    checked={securitySettings.auditLogging}
                    onChange={(e) => handleSecuritySettingChange('auditLogging', e.target.checked)}
                  />
                </Box>

                <TextField
                  fullWidth
                  label="Session Timeout (seconds)"
                  type="number"
                  value={securitySettings.sessionTimeout}
                  onChange={(e) => handleSecuritySettingChange('sessionTimeout', parseInt(e.target.value))}
                  helperText="Automatically log out inactive users"
                  sx={{ mt: 2 }}
                />
              </Box>
            </CardContent>
          </TabPanel>

          {/* Advanced Settings */}
          <TabPanel value={selectedTab} index={3}>
            <CardContent>
              <Typography variant="h6" sx={{ mb: 3, fontWeight: 600 }}>
                Advanced Configuration
              </Typography>

              <Box sx={{ mb: 3 }}>
                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                  <Box>
                    <Typography variant="subtitle1">Debug Mode</Typography>
                    <Typography variant="body2" color="text.secondary">
                      Enable detailed logging and debugging information
                    </Typography>
                  </Box>
                  <Switch
                    checked={settings.debugMode}
                    onChange={(e) => handleSettingChange('debugMode', e.target.checked)}
                  />
                </Box>

                <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
                  <Box>
                    <Typography variant="subtitle1">Auto Save</Typography>
                    <Typography variant="body2" color="text.secondary">
                      Automatically save settings changes
                    </Typography>
                  </Box>
                  <Switch
                    checked={settings.autoSave}
                    onChange={(e) => handleSettingChange('autoSave', e.target.checked)}
                  />
                </Box>
              </Box>

              <Divider sx={{ my: 3 }} />

              <Typography variant="h6" sx={{ mb: 2, fontWeight: 600 }}>
                Data Management
              </Typography>

              <Box sx={{ display: 'flex', gap: 2, mb: 2 }}>
                <Button
                  variant="outlined"
                  startIcon={<Download />}
                  onClick={handleExportSettings}
                >
                  Export Settings
                </Button>
                <Button
                  variant="outlined"
                  startIcon={<Upload />}
                  component="label"
                >
                  Import Settings
                  <input type="file" hidden accept=".json" />
                </Button>
              </Box>

              <Alert severity="warning" sx={{ mb: 2 }}>
                Importing settings will overwrite your current configuration.
              </Alert>
            </CardContent>
          </TabPanel>

          {/* Action Buttons */}
          <Box sx={{ p: 3, display: 'flex', gap: 2, justifyContent: 'flex-end' }}>
            <Button
              variant="outlined"
              startIcon={<Refresh />}
              onClick={handleResetSettings}
            >
              Reset to Defaults
            </Button>
            <Button
              variant="contained"
              startIcon={<Save />}
              onClick={handleSaveSettings}
            >
              Save Settings
            </Button>
          </Box>
        </Card>

        {/* Add Connection Dialog */}
        <Dialog
          open={newConnectionDialog}
          onClose={() => setNewConnectionDialog(false)}
          maxWidth="sm"
          fullWidth
        >
          <DialogTitle>Add New Connection</DialogTitle>
          <DialogContent>
            <TextField
              fullWidth
              label="Connection Name"
              margin="dense"
              variant="outlined"
            />
            <TextField
              fullWidth
              label="URL"
              margin="dense"
              variant="outlined"
              placeholder="https://api.example.com"
            />
          </DialogContent>
          <DialogActions>
            <Button onClick={() => setNewConnectionDialog(false)}>
              Cancel
            </Button>
            <Button variant="contained" onClick={() => setNewConnectionDialog(false)}>
              Add Connection
            </Button>
          </DialogActions>
        </Dialog>
      </motion.div>
    </Box>
  )
}
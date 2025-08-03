import React, { useState } from 'react'
import {
  Box,
  AppBar,
  Toolbar,
  Typography,
  IconButton,
  Drawer,
  List,
  ListItem,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Divider,
  useTheme,
  Tooltip,
  Badge,
} from '@mui/material'
import {
  Menu as MenuIcon,
  Dashboard as DashboardIcon,
  Chat as ChatIcon,
  SmartToy as AgentsIcon,
  Settings as SettingsIcon,
  Brightness4,
  Brightness7,
  Notifications,
  AccountCircle,
} from '@mui/icons-material'
import { useNavigate, useLocation } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'

import { useSidebar, useAppStore } from '@/store/appStore'
import ConnectionStatus from '@/components/Common/ConnectionStatus'

const DRAWER_WIDTH = 280

interface LayoutProps {
  children: React.ReactNode
}

const menuItems = [
  { text: 'Dashboard', icon: <DashboardIcon />, path: '/dashboard' },
  { text: 'Chat', icon: <ChatIcon />, path: '/chat' },
  { text: 'Agents', icon: <AgentsIcon />, path: '/agents' },
  { text: 'Settings', icon: <SettingsIcon />, path: '/settings' },
]

export default function Layout({ children }: LayoutProps) {
  const theme = useTheme()
  const navigate = useNavigate()
  const location = useLocation()
  const { isOpen: sidebarOpen, toggle: toggleSidebar } = useSidebar()
  const { theme: appTheme, setTheme } = useAppStore()
  
  const [mobileOpen, setMobileOpen] = useState(false)

  const handleDrawerToggle = () => {
    setMobileOpen(!mobileOpen)
  }

  const handleThemeToggle = () => {
    setTheme(appTheme === 'light' ? 'dark' : 'light')
  }

  const isActivePath = (path: string) => {
    return location.pathname === path || location.pathname.startsWith(path + '/')
  }

  const drawerContent = (
    <Box sx={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      {/* Logo and Title */}
      <Box sx={{ p: 2, display: 'flex', alignItems: 'center', gap: 2 }}>
        <Box
          sx={{
            width: 40,
            height: 40,
            borderRadius: '50%',
            background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'white',
            fontWeight: 'bold',
            fontSize: '1.2rem',
          }}
        >
          G
        </Box>
        <Typography variant="h6" sx={{ fontWeight: 600, color: 'primary.main' }}>
          GRID Agent System
        </Typography>
      </Box>

      <Divider />

      {/* Connection Status */}
      <Box sx={{ p: 2 }}>
        <ConnectionStatus />
      </Box>

      <Divider />

      {/* Navigation */}
      <List sx={{ flex: 1, px: 1 }}>
        {menuItems.map((item) => {
          const isActive = isActivePath(item.path)
          return (
            <ListItem key={item.text} disablePadding sx={{ mb: 0.5 }}>
              <ListItemButton
                onClick={() => navigate(item.path)}
                selected={isActive}
                sx={{
                  borderRadius: 2,
                  '&.Mui-selected': {
                    backgroundColor: theme.palette.primary.main + '15',
                    '&:hover': {
                      backgroundColor: theme.palette.primary.main + '20',
                    },
                  },
                }}
              >
                <ListItemIcon
                  sx={{
                    color: isActive ? 'primary.main' : 'text.secondary',
                    minWidth: 40,
                  }}
                >
                  {item.icon}
                </ListItemIcon>
                <ListItemText
                  primary={item.text}
                  sx={{
                    '& .MuiListItemText-primary': {
                      fontWeight: isActive ? 600 : 400,
                      color: isActive ? 'primary.main' : 'text.primary',
                    },
                  }}
                />
              </ListItemButton>
            </ListItem>
          )
        })}
      </List>

      {/* Theme Toggle */}
      <Box sx={{ p: 2 }}>
        <ListItemButton
          onClick={handleThemeToggle}
          sx={{ borderRadius: 2 }}
        >
          <ListItemIcon sx={{ minWidth: 40 }}>
            {appTheme === 'light' ? <Brightness4 /> : <Brightness7 />}
          </ListItemIcon>
          <ListItemText
            primary={`${appTheme === 'light' ? 'Dark' : 'Light'} Mode`}
          />
        </ListItemButton>
      </Box>
    </Box>
  )

  return (
    <Box sx={{ display: 'flex', height: '100vh' }}>
      {/* App Bar */}
      <AppBar
        position="fixed"
        sx={{
          width: { sm: sidebarOpen ? `calc(100% - ${DRAWER_WIDTH}px)` : '100%' },
          ml: { sm: sidebarOpen ? `${DRAWER_WIDTH}px` : 0 },
          transition: theme.transitions.create(['width', 'margin'], {
            easing: theme.transitions.easing.sharp,
            duration: theme.transitions.duration.leavingScreen,
          }),
          zIndex: theme.zIndex.drawer + 1,
        }}
      >
        <Toolbar>
          <IconButton
            color="inherit"
            aria-label="toggle drawer"
            edge="start"
            onClick={toggleSidebar}
            sx={{ mr: 2, display: { sm: 'block' } }}
          >
            <MenuIcon />
          </IconButton>

          <IconButton
            color="inherit"
            aria-label="toggle mobile drawer"
            edge="start"
            onClick={handleDrawerToggle}
            sx={{ mr: 2, display: { sm: 'none' } }}
          >
            <MenuIcon />
          </IconButton>

          <Typography variant="h6" noWrap component="div" sx={{ flexGrow: 1 }}>
            {menuItems.find(item => isActivePath(item.path))?.text || 'GRID Agent System'}
          </Typography>

          {/* Header Actions */}
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <Tooltip title="Notifications">
              <IconButton color="inherit">
                <Badge badgeContent={0} color="error">
                  <Notifications />
                </Badge>
              </IconButton>
            </Tooltip>

            <Tooltip title="Profile">
              <IconButton color="inherit">
                <AccountCircle />
              </IconButton>
            </Tooltip>
          </Box>
        </Toolbar>
      </AppBar>

      {/* Sidebar */}
      <Box
        component="nav"
        sx={{ width: { sm: sidebarOpen ? DRAWER_WIDTH : 0 }, flexShrink: { sm: 0 } }}
      >
        {/* Mobile drawer */}
        <Drawer
          variant="temporary"
          open={mobileOpen}
          onClose={handleDrawerToggle}
          ModalProps={{
            keepMounted: true, // Better open performance on mobile
          }}
          sx={{
            display: { xs: 'block', sm: 'none' },
            '& .MuiDrawer-paper': {
              boxSizing: 'border-box',
              width: DRAWER_WIDTH,
            },
          }}
        >
          {drawerContent}
        </Drawer>

        {/* Desktop drawer */}
        <AnimatePresence>
          {sidebarOpen && (
            <Drawer
              variant="permanent"
              sx={{
                display: { xs: 'none', sm: 'block' },
                '& .MuiDrawer-paper': {
                  boxSizing: 'border-box',
                  width: DRAWER_WIDTH,
                  border: 'none',
                  boxShadow: theme.shadows[2],
                },
              }}
              open
            >
              <motion.div
                initial={{ x: -DRAWER_WIDTH }}
                animate={{ x: 0 }}
                exit={{ x: -DRAWER_WIDTH }}
                transition={{ duration: 0.3, ease: 'easeInOut' }}
                style={{ height: '100%' }}
              >
                {drawerContent}
              </motion.div>
            </Drawer>
          )}
        </AnimatePresence>
      </Box>

      {/* Main content */}
      <Box
        component="main"
        sx={{
          flexGrow: 1,
          width: { sm: sidebarOpen ? `calc(100% - ${DRAWER_WIDTH}px)` : '100%' },
          height: '100vh',
          overflow: 'hidden',
          transition: theme.transitions.create(['width'], {
            easing: theme.transitions.easing.sharp,
            duration: theme.transitions.duration.leavingScreen,
          }),
        }}
      >
        <Toolbar />
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.3 }}
          style={{ height: 'calc(100% - 64px)', overflow: 'hidden' }}
        >
          {children}
        </motion.div>
      </Box>
    </Box>
  )
}
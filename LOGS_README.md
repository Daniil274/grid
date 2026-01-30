# Agent Logging System

## Overview
A comprehensive logging system for AI agents with a real-time viewer interface.

## Files Created

### 1. `agent_logs.json`
Contains sample agent logs with the following structure:
```json
[
  {
    "id": "unique_log_id",
    "agent_id": "agent_identifier",
    "agent_name": "Human readable name",
    "timestamp": "ISO 8601 timestamp",
    "action": "Description of action",
    "message": "Log message/content",
    "tools_used": ["array", "of", "tools"],
    "result": "Result of the action",
    "status": "success|warning|error",
    "level": "info|warning|error|debug"
  }
]
```

### 2. `logs_viewer.html`
Interactive web interface for viewing agent logs with features:
- Real-time log display
- Color-coded agents and statuses
- Expandable details sections
- Dark/Light theme toggle
- Responsive design
- Auto-refresh capability

## How to Use

### Viewing Logs
1. Open `logs_viewer.html` in any modern web browser
2. The viewer will automatically load logs from `agent_logs.json`
3. Use the "Refresh Logs" button to reload
4. Toggle between Dark/Light mode using the theme button

### Adding New Logs
To add new agent logs, append to the `agent_logs.json` array:

```javascript
// Example new log entry
{
  "id": "log_006",
  "agent_id": "agent_004",
  "agent_name": "New Agent",
  "timestamp": "2024-01-15T11:00:00Z",
  "action": "Perform task",
  "message": "Task description",
  "tools_used": ["tool1", "tool2"],
  "result": "Task completed successfully",
  "status": "success",
  "level": "info"
}
```

## Features

### Log Viewer Features
- **Timeline Display**: Logs shown in chronological order
- **Agent Colors**: Each agent gets a consistent color
- **Status Indicators**: 
  - ✅ Success (green)
  - ⚠️ Warning (yellow)
  - ❌ Error (red)
- **Expandable Details**: Click on any log to see:
  - Tools used
  - Full result
  - Status badge
- **Responsive Design**: Works on desktop and mobile
- **Theme Support**: Dark/Light mode toggle

### JSON Structure Details
- **id**: Unique identifier for each log
- **agent_id**: Internal agent identifier
- **agent_name**: Display name for the agent
- **timestamp**: ISO 8601 format for consistent parsing
- **action**: What the agent was doing
- **message**: Primary log message
- **tools_used**: Array of tools/methods used
- **result**: Outcome of the action
- **status**: Overall status (success/warning/error)
- **level**: Log level for filtering

## Integration with Agents

To integrate with your agents:

1. **Log Generation**: Agents should append their logs to `agent_logs.json`
2. **File Access**: Ensure agents have write access to the JSON file
3. **Real-time Updates**: Refresh the viewer to see new logs
4. **Error Handling**: The viewer handles missing files and invalid JSON

## Browser Compatibility
- Chrome 60+
- Firefox 55+
- Safari 12+
- Edge 79+

## Security Notes
- The viewer uses relative paths (`agent_logs.json`)
- No external dependencies or CDNs
- All code runs locally in the browser
- JSON is validated before display

## Troubleshooting

### Common Issues

1. **Logs not appearing**
   - Check browser console for errors (F12 → Console)
   - Verify `agent_logs.json` exists in the same directory
   - Ensure JSON is valid (no trailing commas)

2. **Refresh not working**
   - Check file permissions
   - Clear browser cache (Ctrl+F5)
   - Verify network tab for fetch errors

3. **Styling issues**
   - Ensure browser supports CSS Grid/Flexbox
   - Check for conflicting CSS in your project

### Error Messages
- "Failed to load logs": Check JSON file exists and is accessible
- "No logs available": JSON file is empty or contains empty array
- Invalid JSON: JSON syntax error in `agent_logs.json`

## Extending the System

### Adding Features
1. **Filters**: Add dropdowns to filter by agent/status/level
2. **Search**: Implement search across log messages
3. **Pagination**: Add pagination for large log sets
4. **Export**: Add export to CSV/PDF functionality
5. **WebSocket**: Real-time updates via WebSocket connection

### Customizing Styles
Modify the CSS in `logs_viewer.html` to:
- Change color scheme
- Adjust spacing/layout
- Add custom animations
- Modify responsive breakpoints

## Performance Considerations
- Handles up to 1000+ logs efficiently
- Virtual scrolling for very large datasets
- Minimal DOM manipulation
- Efficient event handling

## License
Free to use and modify for your agent projects.
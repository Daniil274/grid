# GRID Agent System - Examples and Use Cases

This document provides comprehensive examples of using GRID Agent System for various tasks and scenarios.

## 🚀 Quick Start Examples

### 1. Basic Chat Interaction

**Web Interface:**
1. Open http://localhost:3000
2. Navigate to Chat
3. Select an agent (e.g., "Coordinator")
4. Type: "Hello, what can you help me with?"

**API Call:**
```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grid-coordinator",
    "messages": [
      {"role": "user", "content": "Hello, what can you help me with?"}
    ]
  }'
```

**Expected Response:**
```json
{
  "id": "chatcmpl-grid-123",
  "object": "chat.completion",
  "created": 1703123456,
  "model": "grid-coordinator",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "Hello! I'm the GRID Coordinator agent. I can help you with various tasks by delegating to specialized agents like file operations, code analysis, Git management, and more. What would you like to work on today?"
    },
    "finish_reason": "stop"
  }],
  "grid_metadata": {
    "agent_used": "coordinator",
    "execution_time": 1.23,
    "tools_called": []
  }
}
```

## 📁 File Operations Examples

### Reading and Analyzing Files

**Task:** Read and analyze a Python file
```bash
curl -X POST "http://localhost:8000/v1/agents/file_agent/execute" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Read and analyze the structure of main.py",
    "context": {
      "working_directory": "/workspace"
    }
  }'
```

**WebSocket Example:**
```javascript
const ws = new WebSocket('ws://localhost:8000/ws/agents/file_agent');

ws.onopen = () => {
  ws.send(JSON.stringify({
    type: 'message',
    content: 'List all Python files in the current directory and show their sizes',
    session_id: 'file-session-123'
  }));
};

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  console.log('File Agent:', message.content);
};
```

### File Creation and Editing

**Create a new file:**
```json
{
  "model": "grid-file-agent",
  "messages": [
    {
      "role": "user", 
      "content": "Create a new Python file called 'hello.py' with a simple Hello World function"
    }
  ],
  "grid_context": {
    "working_directory": "/workspace",
    "tools_enabled": true
  }
}
```

**Edit existing file:**
```json
{
  "model": "grid-file-agent",
  "messages": [
    {
      "role": "user",
      "content": "Add error handling to the hello.py file we just created"
    }
  ],
  "grid_context": {
    "session_id": "file-session-123"
  }
}
```

## 💻 Code Development Examples

### Code Analysis and Review

**Analyze code quality:**
```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grid-code-agent",
    "messages": [
      {
        "role": "user",
        "content": "Review the Python files in /workspace and suggest improvements for code quality, security, and performance"
      }
    ],
    "grid_context": {
      "working_directory": "/workspace",
      "tools_enabled": true
    }
  }'
```

**Bug Detection:**
```json
{
  "model": "grid-code-agent",
  "messages": [
    {
      "role": "user",
      "content": "Scan the codebase for potential bugs, security vulnerabilities, and anti-patterns"
    }
  ],
  "stream": true,
  "grid_context": {
    "working_directory": "/workspace",
    "security_level": "high"
  }
}
```

### Code Generation

**Generate API endpoint:**
```json
{
  "model": "grid-code-agent",
  "messages": [
    {
      "role": "user",
      "content": "Create a FastAPI endpoint for user authentication with JWT tokens, including login and logout functions"
    }
  ],
  "grid_context": {
    "working_directory": "/workspace/api",
    "tools_enabled": true
  }
}
```

**Generate tests:**
```json
{
  "model": "grid-code-agent",
  "messages": [
    {
      "role": "user",
      "content": "Generate comprehensive unit tests for the authentication endpoint we just created"
    }
  ],
  "grid_context": {
    "session_id": "coding-session-456"
  }
}
```

## 🔧 Git Operations Examples

### Repository Management

**Check repository status:**
```bash
curl -X POST "http://localhost:8000/v1/agents/git_agent/execute" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Show me the current Git status and recent commits",
    "context": {
      "working_directory": "/workspace"
    }
  }'
```

**Create commit:**
```json
{
  "model": "grid-git-agent",
  "messages": [
    {
      "role": "user",
      "content": "Add all changes to Git and create a commit with message 'Add authentication API endpoint with tests'"
    }
  ],
  "grid_context": {
    "working_directory": "/workspace"
  }
}
```

### Branch Management

**Create and switch branch:**
```json
{
  "model": "grid-git-agent",
  "messages": [
    {
      "role": "user",
      "content": "Create a new branch called 'feature/user-dashboard' and switch to it"
    }
  ]
}
```

**Merge branches:**
```json
{
  "model": "grid-git-agent",
  "messages": [
    {
      "role": "user",
      "content": "Merge the feature/user-dashboard branch into main branch"
    }
  ]
}
```

## 🔒 Security Analysis Examples

### Security Scanning

**Security audit:**
```bash
curl -X POST "http://localhost:8000/v1/agents/security_guardian/execute" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Perform a comprehensive security audit of the codebase, checking for vulnerabilities, exposed secrets, and security best practices",
    "context": {
      "working_directory": "/workspace",
      "security_level": "strict"
    }
  }'
```

**Threat analysis:**
```json
{
  "model": "grid-security-guardian",
  "messages": [
    {
      "role": "user",
      "content": "Analyze this API endpoint for potential security threats: POST /api/users/login"
    }
  ],
  "grid_context": {
    "security_level": "high",
    "tools_enabled": true
  }
}
```

## 🔍 Research and Analysis Examples

### Document Analysis

**Research task:**
```bash
curl -X POST "http://localhost:8000/v1/agents/researcher/execute" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Research and summarize the contents of all markdown files in the docs/ directory",
    "context": {
      "working_directory": "/workspace"
    }
  }'
```

**Data extraction:**
```json
{
  "model": "grid-researcher",
  "messages": [
    {
      "role": "user",
      "content": "Extract key information from the README.md files and create a summary of the project structure and main features"
    }
  ],
  "grid_context": {
    "working_directory": "/workspace"
  }
}
```

## 🧠 Complex Task Examples

### Multi-Agent Workflows

**Project setup workflow:**
```json
{
  "model": "grid-coordinator",
  "messages": [
    {
      "role": "user",
      "content": "Set up a new Python web API project with FastAPI, including: 1) Create project structure, 2) Add basic authentication, 3) Create initial Git repository, 4) Add basic tests, 5) Create documentation"
    }
  ],
  "grid_context": {
    "working_directory": "/workspace/new-project",
    "tools_enabled": true,
    "timeout": 600
  }
}
```

**Code review and improvement workflow:**
```json
{
  "model": "grid-coordinator",
  "messages": [
    {
      "role": "user",
      "content": "Please review the entire codebase and: 1) Identify code quality issues, 2) Suggest security improvements, 3) Optimize performance bottlenecks, 4) Update documentation, 5) Create or update tests where needed"
    }
  ],
  "stream": true,
  "grid_context": {
    "working_directory": "/workspace",
    "security_level": "high",
    "timeout": 900
  }
}
```

## 📊 Monitoring and Analytics Examples

### System Metrics

**Get system status:**
```bash
curl -X GET "http://localhost:8000/v1/system/health"
```

**Agent metrics:**
```bash
curl -X GET "http://localhost:8000/v1/system/stats"
```

**Response:**
```json
{
  "total_agents": 8,
  "active_connections": 3,
  "total_sessions": 15,
  "system_uptime": 86400000,
  "memory_usage": 65.2,
  "cpu_usage": 23.8,
  "agent_metrics": [
    {
      "agent_type": "coordinator",
      "total_executions": 45,
      "avg_execution_time": 2.3,
      "success_rate": 98.5,
      "last_execution": "2023-12-01T10:30:00Z"
    }
  ]
}
```

## 🎯 Advanced Use Cases

### Custom Agent Integration

**Using agents as tools:**
```json
{
  "model": "grid-coordinator",
  "messages": [
    {
      "role": "user",
      "content": "I need to: 1) Analyze the database schema in schema.sql, 2) Generate API endpoints for each table, 3) Create corresponding tests, 4) Update the documentation. Please coordinate this across the appropriate agents."
    }
  ],
  "grid_context": {
    "working_directory": "/workspace",
    "tools_enabled": true,
    "context_strategy": "full"
  }
}
```

### Session Management

**Create persistent session:**
```bash
curl -X POST "http://localhost:8000/v1/sessions" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_type": "coordinator"
  }'
```

**Continue session:**
```json
{
  "model": "grid-coordinator",
  "messages": [
    {
      "role": "user",
      "content": "Continue from where we left off with the API development"
    }
  ],
  "grid_context": {
    "session_id": "session-789",
    "context_strategy": "smart"
  }
}
```

### Streaming Responses

**JavaScript client for streaming:**
```javascript
async function streamChatCompletion(message) {
  const response = await fetch('/api/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model: 'grid-coordinator',
      messages: [{ role: 'user', content: message }],
      stream: true
    })
  });

  const reader = response.body.getReader();
  const decoder = new TextDecoder();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const chunk = decoder.decode(value);
    const lines = chunk.split('\n').filter(line => line.trim());

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const data = line.slice(6);
        if (data === '[DONE]') return;
        
        try {
          const parsed = JSON.parse(data);
          const content = parsed.choices[0]?.delta?.content;
          if (content) {
            console.log('Streaming:', content);
            // Update UI with streaming content
          }
        } catch (error) {
          console.error('Parse error:', error);
        }
      }
    }
  }
}
```

### Error Handling

**Handling API errors:**
```javascript
async function handleApiCall() {
  try {
    const response = await fetch('/api/v1/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'grid-coordinator',
        messages: [{ role: 'user', content: 'Test message' }]
      })
    });

    if (!response.ok) {
      const error = await response.json();
      console.error('API Error:', error.error.message);
      return;
    }

    const result = await response.json();
    console.log('Success:', result);
    
  } catch (error) {
    console.error('Network Error:', error);
  }
}
```

## 🛠 Development Examples

### Custom Tool Development

**Adding a custom tool:**
```python
# In tools/custom_tools.py
from agents import function_tool

@function_tool
def analyze_logs(log_file: str, pattern: str = None) -> str:
    """Analyze log files for patterns and anomalies."""
    # Implementation here
    return "Log analysis results"

# In config.yaml
tools:
  log_analyzer:
    type: "function"
    name: "analyze_logs"
    description: "Analyze log files for patterns"
    prompt_addition: "Use analyze_logs(log_file, pattern) to analyze logs."
```

### Custom Agent Configuration

**Creating a specialized agent:**
```yaml
# In config.yaml
agents:
  devops_agent:
    name: "DevOps Specialist"
    model: "gpt-4"
    tools: ["file_read", "file_write", "git_status", "analyze_logs"]
    base_prompt: "with_devops"
    custom_prompt: |
      You are a DevOps specialist. Focus on infrastructure, monitoring,
      deployment, and operational concerns. Use available tools to
      analyze systems and provide operational insights.
    description: "DevOps and infrastructure specialist"
```

### Testing Examples

**Backend testing:**
```python
# tests/test_api.py
import pytest
from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)

def test_chat_completion():
    response = client.post("/v1/chat/completions", json={
        "model": "grid-coordinator",
        "messages": [{"role": "user", "content": "Hello"}]
    })
    assert response.status_code == 200
    data = response.json()
    assert "choices" in data
    assert len(data["choices"]) > 0
```

**Frontend testing:**
```javascript
// tests/ChatPage.test.tsx
import { render, screen, fireEvent } from '@testing-library/react';
import ChatPage from '../pages/ChatPage';

test('sends message on button click', async () => {
  render(<ChatPage />);
  
  const input = screen.getByPlaceholderText('Message Agent...');
  const button = screen.getByRole('button', { name: /send/i });
  
  fireEvent.change(input, { target: { value: 'Test message' } });
  fireEvent.click(button);
  
  expect(await screen.findByText('Test message')).toBeInTheDocument();
});
```

## 📈 Performance Optimization Examples

### Caching Strategies

**API response caching:**
```python
from fastapi_cache import FastAPICache
from fastapi_cache.decorator import cache

@app.get("/v1/agents")
@cache(expire=300)  # Cache for 5 minutes
async def list_agents():
    return await get_agents_from_database()
```

**Frontend caching:**
```javascript
// Using React Query for caching
const { data: agents, isLoading } = useQuery({
  queryKey: ['agents'],
  queryFn: () => apiService.listAgents(),
  staleTime: 5 * 60 * 1000, // 5 minutes
  cacheTime: 10 * 60 * 1000, // 10 minutes
});
```

### Load Testing

**API load testing:**
```bash
# Using Apache Bench
ab -n 1000 -c 10 http://localhost:8000/v1/chat/completions

# Using wrk
wrk -t12 -c400 -d30s --script=post.lua http://localhost:8000/v1/chat/completions
```

---

These examples demonstrate the versatility and power of GRID Agent System. For more specific use cases or custom implementations, please refer to the API documentation at `/docs` when running the system.
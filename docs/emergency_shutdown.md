# Emergency Shutdown for Dynamic Agents

## Overview

Emergency Shutdown is a mechanism for emergency stopping of the entire pipeline upon a critical error in one of the agents. It allows agents to stop the entire pipeline, clear the task queue, and provide the orchestrator with information for restart.

## Architecture

### Components

1. **PipelineRegistry** (`core/pipeline_registry.py`)
   - Singleton for tracking all active pipelines
   - Lifecycle management of tasks
   - Emergency shutdown with graceful completion (5 sec timeout)

2. **Emergency Tools** (`tools/emergency_tools.py`)
   - `emergency_shutdown(reason, severity)` - stop pipeline
   - `get_pipeline_status()` - pipeline status

3. **Orchestrator Integration** (`tools/orchestrator_tools.py`)
   - Automatic pipeline registration
   - CancelledError handling
   - Passing shutdown information

4. **Agent Factory** (`core/agent_factory.py`)
   - Automatic addition of emergency_shutdown to agents

## Usage

### Automatic Usage

The emergency shutdown tool is **automatically added** to all agents within an active pipeline. Agents do not need to explicitly request this tool.

### Agent Invocation

```python
# When an agent detects a critical problem:
emergency_shutdown(
    reason="Database connection failed: Connection refused. All tasks require database access.",
    severity="critical"
)
```

### Parameters

- **reason** (required): Detailed description of the stop reason
  - Should be as specific as possible
  - Helps the orchestrator make restart decisions

- **severity** (optional): Criticality level
  - `"critical"` (default) - critical error, system cannot continue
  - `"error"` - serious error, but system can continue
  - `"warning"` - problem requires attention

### Return Value

JSON string with shutdown information:

```json
{
  "success": true,
  "pipeline_id": "pipeline-a1b2c3d4",
  "reason": "Database unavailable",
  "severity": "critical",
  "cancelled_tasks": 3,
  "completed_tasks": 2,
  "failed_tasks": 1,
  "total_tasks": 6,
  "message": "Emergency shutdown executed..."
}
```

## Usage Examples

### Example 1: Missing Dependency

```python
# Agent checks for critical dependency
try:
    import required_library
except ImportError:
    emergency_shutdown(
        reason="Required library 'required_library' not found. Cannot proceed with task execution.",
        severity="critical"
    )
```

### Example 2: Unavailable Service

```python
# Agent tries to connect to the database
try:
    connection = connect_to_database()
except ConnectionError as e:
    emergency_shutdown(
        reason=f"Database connection failed after 3 retries: {e}. All subsequent tasks require database access.",
        severity="critical"
    )
```

### Example 3: Infinite Recursion Detected

```python
# Agent checks the number of running tasks
status = get_pipeline_status()
status_data = json.loads(status)

if len(status_data["running_tasks"]) > 20:
    emergency_shutdown(
        reason="Pipeline spawned >20 concurrent tasks. Possible infinite recursion detected.",
        severity="error"
    )
```

## Handling in the Orchestrator

When an emergency shutdown occurs, the orchestrator receives a result with the flag `emergency_stopped=True`:

```python
result = orchestrate(task="Complex task")
result_data = json.loads(result)

if result_data.get("emergency_stopped"):
    print(f"Pipeline stopped: {result_data['emergency_reason']}")
    print(f"Severity: {result_data['emergency_severity']}")

    # Situation analysis and possible restart
    if result_data['emergency_severity'] == 'critical':
        # Critical error - requires human intervention
        notify_admin(result_data['emergency_reason'])
    else:
        # Non-critical error - can try restarting
        retry_pipeline(result_data)
```

## Pipeline Lifecycle

### 1. Normal Execution

```
orchestrate() called
    ↓
Pipeline registered (pipeline-xxx)
    ↓
Agent created with emergency_shutdown
    ↓
Task registered and tracked
    ↓
Task executed successfully
    ↓
Task marked as completed
    ↓
Pipeline completed successfully
```

### 2. Emergency Shutdown

```
orchestrate() called
    ↓
Pipeline registered
    ↓
Multiple agents started
    ↓
Agent #2 detects critical problem
    ↓
emergency_shutdown() called
    ↓
PipelineRegistry:
  - Finds all running tasks
  - Calls task.cancel() for each
  - Waits for graceful completion (5 sec)
  - Status → EMERGENCY_STOPPED
    ↓
Orchestrator catches CancelledError
    ↓
Checks pipeline status
    ↓
Returns emergency_stopped=True
    ↓
High-level code analyzes and decides
```

## Technical Details

### Pipeline Statuses

- `RUNNING` - actively executing
- `COMPLETED` - successfully completed
- `FAILED` - failed with error
- `EMERGENCY_STOPPED` - stopped via emergency_shutdown
- `CANCELLING` - in the process of stopping

### Graceful Shutdown

Emergency shutdown uses a graceful approach:

1. All running tasks receive `task.cancel()`
2. The system waits 5 seconds for completion
3. After timeout, the process terminates forcibly

### Persistence

Critical emergency shutdown events are saved to MemoryStore for subsequent analysis.

## Testing

Run the test to verify functionality:

```bash
python test_emergency_shutdown.py
```

The test checks:
- Pipeline registration
- Task registration
- Emergency shutdown with multiple tasks
- Graceful vs timeout shutdown
- Correctness of statuses

## Best Practices

1. **Detailed reasons**: Always provide the most specific reason for stopping
2. **Correct severity**: Use `critical` only for truly critical errors
3. **Check before stopping**: Ensure the problem truly blocks the entire pipeline
4. **Logging**: Log the context before calling emergency_shutdown
5. **Alternatives**: Consider handling the error without stopping the pipeline

## Limitations

1. Emergency shutdown stops the ENTIRE pipeline - no partial stopping
2. Graceful shutdown is limited to 5 seconds
3. The mechanism only works inside orchestrate() pipeline
4. No automatic restart - the orchestrator decides

## Troubleshooting

### Problem: emergency_shutdown is not available

**Solution**: Ensure the agent is created inside an active pipeline via orchestrate()

### Problem: Tasks are not stopping

**Solution**: Check logs - tasks might not be handling CancelledError

### Problem: Pipeline status is not updating

**Solution**: Check that PipelineRegistry is properly initialized in AgentFactory

## See Also

- [Implementation Plan](../.claude/plans/concurrent-knitting-pumpkin.md)
- [PipelineRegistry API](../core/pipeline_registry.py)
- [Emergency Tools API](../tools/emergency_tools.py)

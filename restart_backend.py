#!/usr/bin/env python3
"""
Script to restart the GRID backend API
"""

import subprocess
import sys
import time
import signal
import os

def kill_existing_processes():
    """Kill existing uvicorn processes"""
    try:
        # Find uvicorn processes
        result = subprocess.run(['pgrep', '-f', 'uvicorn.*api.main'], 
                              capture_output=True, text=True)
        if result.stdout:
            pids = result.stdout.strip().split('\n')
            for pid in pids:
                if pid:
                    print(f"Killing process {pid}")
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                        time.sleep(2)
                        os.kill(int(pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
    except Exception as e:
        print(f"Error killing processes: {e}")

def start_backend():
    """Start the backend server"""
    print("Starting GRID backend API...")
    
    # Kill existing processes first
    kill_existing_processes()
    time.sleep(3)
    
    # Start new process
    try:
        cmd = [
            sys.executable, '-m', 'uvicorn', 
            'api.main:app', 
            '--host', '0.0.0.0', 
            '--port', '8000', 
            '--reload',
            '--log-level', 'debug'
        ]
        
        subprocess.Popen(cmd, cwd='/workspaces/grid')
        print("Backend started on http://localhost:8000")
        
    except Exception as e:
        print(f"Failed to start backend: {e}")

if __name__ == "__main__":
    start_backend()
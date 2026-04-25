#!/usr/bin/env python3
"""
Test script for verifying Claude Tools.

Run:
    cd examples/claude-tools
    python test_tools.py
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tools import (
    bash_tool,
    file_read, file_write, file_edit, file_append,
    glob_tool, grep_tool,
    web_fetch,
    notebook_create, notebook_read, notebook_edit,
    todo_write, todo_list, todo_clear
)


def test_bash():
    print("=" * 60)
    print("Test: bash_tool")
    print("=" * 60)
    
    # Simple command
    result = bash_tool("echo 'Hello, World!'")
    print(result)
    print()
    
    # Security check
    result = bash_tool("rm -rf /")
    print(result)
    print()
    
    # With timeout
    result = bash_tool("pwd", description="Current directory")
    print(result)
    print()


def test_file():
    print("=" * 60)
    print("Test: file_tools")
    print("=" * 60)
    
    test_file_path = "/tmp/test_claude_tools.txt"
    
    # Write
    result = file_write(test_file_path, "Line 1\nLine 2\nLine 3\n")
    print(result)
    print()
    
    # Read
    result = file_read(test_file_path)
    print(result)
    print()
    
    # Append
    result = file_append(test_file_path, "Line 4\n")
    print(result)
    print()
    
    # Read with offset
    result = file_read(test_file_path, offset=1, limit_lines=2)
    print(result)
    print()
    
    # Edit
    patch = """--- a/test_claude_tools.txt
+++ b/test_claude_tools.txt
@@ -1,4 +1,4 @@
 Line 1
-Line 2
+Line 2 modified
 Line 3
 Line 4
"""
    result = file_edit(test_file_path, patch)
    print(result)
    print()
    
    # Check the result
    result = file_read(test_file_path)
    print(result)
    print()
    
    # Cleanup
    import os
    os.remove(test_file_path)
    print("✅ Test file deleted")
    print()


def test_search():
    print("=" * 60)
    print("Test: search_tools")
    print("=" * 60)
    
    # Glob
    result = glob_tool("*.py", directory="tools")
    print(result)
    print()
    
    # Grep
    result = grep_tool("function_tool", directory="tools", file_extensions="py")
    print(result)
    print()


def test_todo():
    print("=" * 60)
    print("Test: todo_tools")
    print("=" * 60)
    
    # Clear before test
    todo_clear()
    
    # Create tasks
    result = todo_write("First task", priority=3)
    print(result)
    
    result = todo_write("Second task", priority=5)
    print(result)
    
    result = todo_write("Third task", status="in_progress")
    print(result)
    print()
    
    # List
    result = todo_list()
    print(result)
    print()
    
    # Update
    result = todo_write("First task (updated)", todo_id="todo_1", status="done")
    print(result)
    print()
    
    # List with filter
    result = todo_list(status_filter="pending")
    print(result)
    print()
    
    # Clear
    result = todo_clear()
    print(result)
    print()


def test_notebook():
    print("=" * 60)
    print("Test: notebook_tools")
    print("=" * 60)
    
    test_nb_path = "/tmp/test_claude_tools.ipynb"
    
    # Create
    result = notebook_create(test_nb_path)
    print(result)
    print()
    
    # Read
    result = notebook_read(test_nb_path)
    print(result)
    print()
    
    # Edit
    result = notebook_edit(
        test_nb_path,
        cell_index=1,
        new_source="# New header\n\nThis is a markdown cell"
    )
    print(result)
    print()
    
    # Verify
    result = notebook_read(test_nb_path)
    print(result)
    print()
    
    # Cleanup
    import os
    os.remove(test_nb_path)
    print("✅ Test notebook deleted")
    print()


def main():
    print("\n" + "=" * 60)
    print("Testing Claude Tools")
    print("=" * 60 + "\n")
    
    try:
        test_bash()
    except Exception as e:
        print(f"❌ Error in test_bash: {e}")
    
    try:
        test_file()
    except Exception as e:
        print(f"❌ Error in test_file: {e}")
    
    try:
        test_search()
    except Exception as e:
        print(f"❌ Error in test_search: {e}")
    
    try:
        test_todo()
    except Exception as e:
        print(f"❌ Error in test_todo: {e}")
    
    try:
        test_notebook()
    except Exception as e:
        print(f"❌ Error in test_notebook: {e}")
    
    print("\n" + "=" * 60)
    print("Testing complete")
    print("=" * 60)


if __name__ == "__main__":
    main()

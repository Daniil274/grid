"""
Unit tests for tools/git_tools.py module.
"""

import pytest
import subprocess
from unittest.mock import patch, Mock

from tools.git_tools import _run_git_command





class TestGitCommandRunner:
    """Test the Git command runner functionality."""
    
    def test_run_git_command_success(self):
        """Test successful git command execution."""
        with patch('subprocess.run') as mock_run:
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = "success output"
            mock_result.stderr = ""
            mock_run.return_value = mock_result
            
            result = _run_git_command(["git", "status"])
            
            assert result["success"] is True
            assert result["output"] == "success output"
            assert result["error"] == ""
    
    def test_run_git_command_failure(self):
        """Test git command execution with failure."""
        with patch('subprocess.run') as mock_run:
            mock_result = Mock()
            mock_result.returncode = 1
            mock_result.stdout = ""
            mock_result.stderr = "error message"
            mock_run.return_value = mock_result
            
            result = _run_git_command(["git", "status"])
            
            assert result["success"] is False
            assert result["output"] == ""
            assert result["error"] == "error message"
    
    def test_run_git_command_non_git_command(self):
        """Test validation of non-git commands."""
        result = _run_git_command(["ls", "-la"])
        
        assert result["success"] is False
        assert "Command must start with 'git'" in result["error"]
    
    def test_run_git_command_empty_command(self):
        """Test validation of empty commands."""
        result = _run_git_command([])
        
        assert result["success"] is False
        assert "Command must start with 'git'" in result["error"]
    
    def test_run_git_command_dangerous_commands(self):
        """Test blocking of dangerous git commands."""
        dangerous_commands = [
            ["git", "rm", "file.txt"],
            ["git", "clean", "-fd"],
            ["git", "reset", "--hard"],
            ["git", "push", "--force"],
            ["git", "rebase", "-i"]
        ]
        
        for cmd in dangerous_commands:
            result = _run_git_command(cmd)
            assert result["success"] is False
            assert "Dangerous command blocked" in result["error"]
    
    def test_run_git_command_timeout(self):
        """Test git command timeout handling."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("git", 30)
            
            result = _run_git_command(["git", "status"])
            
            assert result["success"] is False
            assert "Command exceeded time limit" in result["error"]
    
    def test_run_git_command_exception(self):
        """Test git command exception handling."""
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = OSError("Command not found")
            
            result = _run_git_command(["git", "status"])
            
            assert result["success"] is False
            assert "Command execution error" in result["error"]
    
    def test_run_git_command_with_cwd(self):
        """Test git command execution with custom working directory."""
        with patch('subprocess.run') as mock_run:
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = "output"
            mock_result.stderr = ""
            mock_run.return_value = mock_result
            
            result = _run_git_command(["git", "status"], cwd="/tmp")
            
            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            assert kwargs['cwd'] == "/tmp"
    
    @patch('utils.logger.log_custom')
    def test_run_git_command_logging(self, mock_log_custom):
        """Test that git commands are properly logged."""
        with patch('subprocess.run') as mock_run:
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = "output"
            mock_result.stderr = ""
            mock_run.return_value = mock_result
            
            _run_git_command(["git", "status"])
            
            # Should log the command execution and success
            assert mock_log_custom.call_count >= 2


class TestGitToolsIntegration:
    """Integration tests for Git tools with real Git operations."""
    
    
    def test_dangerous_command_protection(self):
        """Test that dangerous commands are properly blocked."""
        dangerous_cases = [
            "git rm important_file.txt",
            "git clean -fd",
            "git reset --hard HEAD~1",
            "git push --force origin main",
            "git rebase -i HEAD~3"
        ]
        
        for cmd_str in dangerous_cases:
            cmd = cmd_str.split()
            result = _run_git_command(cmd)
            assert result["success"] is False
            assert "Dangerous command blocked" in result["error"]
    
    def test_safe_command_execution(self):
        """Test that safe commands are allowed."""
        safe_commands = [
            ["git", "status"],
            ["git", "log", "--oneline"],
            ["git", "branch"],
            ["git", "diff"],
            ["git", "show"]
        ]
        
        for cmd in safe_commands:
            with patch('subprocess.run') as mock_run:
                mock_result = Mock()
                mock_result.returncode = 0
                mock_result.stdout = "safe output"
                mock_result.stderr = ""
                mock_run.return_value = mock_result
                
                result = _run_git_command(cmd)
                assert result["success"] is True


class TestGitToolsEdgeCases:
    """Test edge cases and error conditions for Git tools."""
    
    def test_git_command_with_unicode_output(self):
        """Test git command handling unicode output."""
        unicode_output = "On branch main\nChanges not staged"
        
        with patch('subprocess.run') as mock_run:
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = unicode_output
            mock_result.stderr = ""
            mock_run.return_value = mock_result
            
            result = _run_git_command(["git", "status"])
            
            assert result["success"] is True
            assert result["output"] == unicode_output
    
    def test_git_command_with_large_output(self):
        """Test git command with large output."""
        large_output = "line\n" * 10000  # Large output
        
        with patch('subprocess.run') as mock_run:
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = large_output
            mock_result.stderr = ""
            mock_run.return_value = mock_result
            
            result = _run_git_command(["git", "log"])
            
            assert result["success"] is True
            assert result["output"] == large_output.strip()  # nothing is cut off
    
    def test_git_command_partial_dangerous_match(self):
        """Test that partial matches don't trigger dangerous command block."""
        # These should NOT be blocked
        safe_commands = [
            ["git", "status", "--rm"],  # Contains 'rm' but not dangerous
            ["git", "log", "--clean"],  # Contains 'clean' but not dangerous
            ["git", "branch", "--force-delete"]  # Contains 'force' but not dangerous
        ]
        
        for cmd in safe_commands:
            with patch('subprocess.run') as mock_run:
                mock_result = Mock()
                mock_result.returncode = 0
                mock_result.stdout = "output"
                mock_result.stderr = ""
                mock_run.return_value = mock_result
                
                result = _run_git_command(cmd)
                assert result["success"] is True
    
    
    def test_concurrent_git_operations(self, mock_git_repo):
        """Test concurrent git operations."""
        import threading
        
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=mock_git_repo, capture_output=True, timeout=10)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=mock_git_repo, capture_output=True, timeout=10)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=mock_git_repo, capture_output=True, timeout=10)
        
        results = []
        
        def git_operation(thread_id):
            try:
                result = _run_git_command(["git", "status"], cwd=str(mock_git_repo))
                assert result["success"], result
                results.append(("success", result))
            except Exception as e:
                results.append(("error", str(e)))
        
        threads = []
        for i in range(5):
            thread = threading.Thread(target=git_operation, args=(i,))
            threads.append(thread)
            thread.start()
        
        for thread in threads:
            thread.join(timeout=10)  # 10 sec timeout for each thread
            if thread.is_alive():
                pytest.fail(f"Thread {thread.name} did not finish within timeout")
        
        # All operations should succeed
        assert len(results) == 5
        assert all(status == "success" for status, _ in results)
    
    @patch('utils.logger.log_custom')
    def test_logging_with_different_log_levels(self, mock_log_custom):
        """Test that git operations log at appropriate levels."""
        with patch('subprocess.run') as mock_run:
            # Test successful operation logging
            mock_result = Mock()
            mock_result.returncode = 0
            mock_result.stdout = "success"
            mock_result.stderr = ""
            mock_run.return_value = mock_result
            
            _run_git_command(["git", "status"])
            
            # Should log debug messages
            debug_calls = [call for call in mock_log_custom.call_args_list 
                          if call[0][0] == 'debug']
            assert len(debug_calls) >= 2
        
        mock_log_custom.reset_mock()
        
        with patch('subprocess.run') as mock_run:
            # Test failed operation logging
            mock_result = Mock()
            mock_result.returncode = 1
            mock_result.stdout = ""
            mock_result.stderr = "error"
            mock_run.return_value = mock_result
            
            _run_git_command(["git", "status"])
            
            # Should log debug message for error
            debug_calls = [call for call in mock_log_custom.call_args_list 
                          if call[0][0] == 'debug']
            assert len(debug_calls) >= 1
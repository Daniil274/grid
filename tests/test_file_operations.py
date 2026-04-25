"""
Unit tests for file operations logic (without agents integration).
"""

import pytest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

# Import the actual implementation logic by recreating the functions


def read_file_impl(filepath: str) -> str:
    """Test implementation of read_file logic."""
    try:
        path = Path(filepath)
        if not path.exists():
            return f"❌ File {filepath} not found"
        
        if not path.is_file():
            return f"❌ {filepath} is not a file"
        
        content = path.read_text(encoding='utf-8')
        lines_count = len(content.splitlines())
        
        return f"📄 File contents {filepath}:\n\n{content}"
        
    except Exception as e:
        return f"❌ Error reading {filepath}: {str(e)}"


def write_file_impl(filepath: str, content: str) -> str:
    """Test implementation of write_file logic."""
    try:
        path = Path(filepath)
        
        # Create parent directories if needed
        path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write file
        path.write_text(content, encoding='utf-8')
        
        size = path.stat().st_size
        lines_count = len(content.splitlines())
        
        return f"✅ File {filepath} successfully written ({size} bytes)"
        
    except Exception as e:
        return f"❌ Error writing file {filepath}: {str(e)}"


def get_file_info_impl(filepath: str) -> str:
    """Test implementation of get_file_info logic."""
    try:
        path = Path(filepath)
        if not path.exists():
            return f"❌ File {filepath} not found"
        
        if not path.is_file():
            return f"❌ {filepath} is not a file"
        
        stat = path.stat()
        content = path.read_text(encoding='utf-8')
        lines_count = len(content.splitlines())
        extension = path.suffix.lower()
        
        result = f"""📄 File info {filepath}:
• Name: {path.name}
• Size: {stat.st_size} bytes
• Lines: {lines_count}
• Extension: {extension if extension else 'no extension'}"""
        
        return result
        
    except Exception as e:
        return f"❌ Error getting file info for {filepath}: {str(e)}"


def list_files_impl(directory: str = ".") -> str:
    """Test implementation of list_files logic."""
    try:
        path = Path(directory)
        if not path.exists():
            return f"❌ Directory {directory} not found"
        
        if not path.is_dir():
            return f"❌ {directory} is not a directory"
        
        files = []
        dirs = []
        for item in sorted(path.iterdir()):
            if item.is_file():
                size = item.stat().st_size
                files.append(f"📄 {item.name} ({size} bytes)")
            elif item.is_dir():
                dirs.append(f"📁 {item.name}/")
        
        total_items = len(files) + len(dirs)
        
        if total_items == 0:
            return f"📂 Directory {directory} is empty"
        
        all_items = dirs + files  # Directories first
        result = f"📂 Directory contents {directory} ({total_items} items):\n\n" + "\n".join(all_items)
        return result
        
    except Exception as e:
        return f"❌ Error reading directory {directory}: {str(e)}"


class TestFileOperations:
    """Test file operations logic."""
    
    def test_read_file_success(self, temp_dir):
        """Test successful file reading."""
        # Create test file
        test_file = temp_dir / "test_file.txt"
        test_file.write_text("Hello, World!")
        
        result = read_file_impl(str(test_file))
        
        assert "📄 File contents" in result
        assert "Hello, World!" in result
        assert str(test_file) in result
    
    def test_read_file_not_found(self, temp_dir):
        """Test reading non-existent file."""
        non_existent_file = temp_dir / "non_existent.txt"
        result = read_file_impl(str(non_existent_file))
        
        assert "❌ File" in result
        assert "not found" in result
    
    def test_read_file_not_a_file(self, temp_dir):
        """Test reading a directory instead of file."""
        result = read_file_impl(str(temp_dir))
        
        assert "❌" in result
        assert "is not a file" in result
    
    def test_read_file_encoding_error(self, temp_dir):
        """Test reading file with encoding issues."""
        # Create a file with binary content
        binary_file = temp_dir / "binary.bin"
        binary_file.write_bytes(b"\x80\x81\x82\x83")
        
        result = read_file_impl(str(binary_file))
        
        # Should handle the error gracefully
        assert "❌ Error reading" in result
    
    def test_write_file_success(self, temp_dir):
        """Test successful file writing."""
        test_file = temp_dir / "test_write.txt"
        content = "Test content\nLine 2"
        
        result = write_file_impl(str(test_file), content)
        
        assert "✅ File" in result
        assert "successfully written" in result
        assert test_file.exists()
        assert test_file.read_text() == content
    
    def test_write_file_creates_directories(self, temp_dir):
        """Test that write_file creates parent directories."""
        nested_file = temp_dir / "new_dir" / "nested_dir" / "test.txt"
        content = "Test content"
        
        result = write_file_impl(str(nested_file), content)
        
        assert "✅ File" in result
        assert nested_file.exists()
        assert nested_file.read_text() == content
    
    def test_get_file_info_success(self, temp_dir):
        """Test getting file information."""
        test_file = temp_dir / "test_file.txt"
        test_file.write_text("Hello, World!")
        
        result = get_file_info_impl(str(test_file))
        
        assert "📄 File info" in result
        assert "Name:" in result
        assert "Size:" in result
        assert "Lines:" in result
        assert "Extension:" in result
        assert test_file.name in result
    
    def test_get_file_info_not_found(self, temp_dir):
        """Test getting info for non-existent file."""
        non_existent_file = temp_dir / "non_existent.txt"
        result = get_file_info_impl(str(non_existent_file))
        
        assert "❌ File" in result
        assert "not found" in result
    
    def test_list_files_success(self, temp_dir):
        """Test listing files in directory."""
        # Create test files and directories
        (temp_dir / "test1.txt").write_text("content1")
        (temp_dir / "test2.py").write_text("content2")
        (temp_dir / "subdir").mkdir()
        
        result = list_files_impl(str(temp_dir))
        
        assert "📂 Directory contents" in result
        assert "📁 subdir/" in result
        assert "📄 test1.txt" in result
        assert "📄 test2.py" in result
        assert "3 items" in result
    
    def test_list_files_empty_directory(self, temp_dir):
        """Test listing empty directory."""
        empty_dir = temp_dir / "empty"
        empty_dir.mkdir()
        
        result = list_files_impl(str(empty_dir))
        
        assert "📂 Directory" in result
        assert "is empty" in result
    
    def test_list_files_not_found(self, temp_dir):
        """Test listing non-existent directory."""
        non_existent_dir = temp_dir / "non_existent"
        result = list_files_impl(str(non_existent_dir))
        
        assert "❌ Directory" in result
        assert "not found" in result
    
    def test_unicode_handling(self, temp_dir):
        """Test handling of unicode content."""
        test_file = temp_dir / "unicode_test.txt"
        unicode_content = "Hello world! 🌍 测试 テスト"
        
        # Write unicode content
        write_result = write_file_impl(str(test_file), unicode_content)
        assert "✅" in write_result
        
        # Read unicode content
        read_result = read_file_impl(str(test_file))
        assert unicode_content in read_result
        
        # Get info for unicode file
        info_result = get_file_info_impl(str(test_file))
        assert "📄 File info" in info_result
    
    def test_large_file_handling(self, temp_dir):
        """Test handling of large files."""
        test_file = temp_dir / "large_file.txt"
        large_content = "x" * 10000  # 10KB content
        
        write_result = write_file_impl(str(test_file), large_content)
        assert "✅ File" in write_result
        assert test_file.exists()
        assert len(test_file.read_text()) == 10000
        
        read_result = read_file_impl(str(test_file))
        assert large_content in read_result
    
    def test_special_characters_in_path(self, temp_dir):
        """Test handling of special characters in file paths."""
        # Create file with special characters in name
        special_file = temp_dir / "file with spaces & symbols.txt"
        content = "Test content"
        
        write_result = write_file_impl(str(special_file), content)
        assert "✅" in write_result
        assert special_file.exists()
        
        read_result = read_file_impl(str(special_file))
        assert content in read_result
    
    def test_nested_directory_creation(self, temp_dir):
        """Test creation of deeply nested directories."""
        deep_file = temp_dir / "level1" / "level2" / "level3" / "test.txt"
        content = "Deep content"
        
        write_result = write_file_impl(str(deep_file), content)
        assert "✅" in write_result
        assert deep_file.exists()
        assert deep_file.read_text() == content
    
    def test_file_overwrite(self, temp_dir):
        """Test overwriting existing files."""
        test_file = temp_dir / "overwrite_test.txt"
        
        # Write initial content
        initial_content = "Initial content"
        write_result1 = write_file_impl(str(test_file), initial_content)
        assert "✅" in write_result1
        assert test_file.read_text() == initial_content
        
        # Overwrite with new content
        new_content = "New content"
        write_result2 = write_file_impl(str(test_file), new_content)
        assert "✅" in write_result2
        assert test_file.read_text() == new_content
    
    def test_concurrent_file_operations(self, temp_dir):
        """Test concurrent file operations."""
        import threading
        
        results = []
        
        def write_file_thread(file_num):
            file_path = temp_dir / f"concurrent_{file_num}.txt"
            content = f"Content from thread {file_num}"
            result = write_file_impl(str(file_path), content)
            results.append(result)
        
        threads = []
        for i in range(5):
            thread = threading.Thread(target=write_file_thread, args=(i,))
            threads.append(thread)
            thread.start()
        
        for thread in threads:
            thread.join(timeout=10)  # Timeout 10 sec for each thread
            if thread.is_alive():
                pytest.fail(f"Thread {thread.name} did not finish within timeout")
        
        # All operations should succeed
        assert len(results) == 5
        assert all("✅" in result for result in results)
        
        # All files should exist
        for i in range(5):
            file_path = temp_dir / f"concurrent_{i}.txt"
            assert file_path.exists()
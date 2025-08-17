"""
Unit tests for tools/file_tools.py module.
"""

import pytest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock

from tools.file_tools import (
    read_file as _read_file, write_file as _write_file, get_file_info as _get_file_info, 
    list_files as _list_files, search_files as _search_files, edit_file_patch as _edit_file_patch, 
    get_file_tools, get_file_tools_by_names
)

# Helper functions to call the actual functions
def read_file(filepath):
    return _read_file.on_invoke_tool({"filepath": filepath})

def write_file(filepath, content):
    return _write_file.on_invoke_tool({"filepath": filepath, "content": content})

def get_file_info(filepath):
    return _get_file_info.on_invoke_tool({"filepath": filepath})

def list_files(directory="."):
    return _list_files.on_invoke_tool({"directory": directory})

def search_files(search_pattern, directory=".", use_regex=False, search_in_content=False, file_extensions="", max_results=50):
    return _search_files.on_invoke_tool({
        "search_pattern": search_pattern,
        "directory": directory, 
        "use_regex": use_regex,
        "search_in_content": search_in_content,
        "file_extensions": file_extensions,
        "max_results": max_results
    })

def edit_file_patch(filepath, patch_content):
    return _edit_file_patch.on_invoke_tool({"filepath": filepath, "patch_content": patch_content})


class TestFileTools:
    """Test file tools functionality."""
    
    def test_read_file_success(self, sample_test_file):
        """Test successful file reading."""
        result = read_file(str(sample_test_file))
        
        assert "📄 Содержимое файла" in result
        assert "Hello, World!" in result
        assert str(sample_test_file) in result
    
    def test_read_file_not_found(self, temp_dir):
        """Test reading non-existent file."""
        non_existent_file = temp_dir / "non_existent.txt"
        result = read_file(str(non_existent_file))
        
        assert "❌ Файл" in result
        assert "не найден" in result
    
    def test_read_file_not_a_file(self, temp_dir):
        """Test reading a directory instead of file."""
        result = read_file(str(temp_dir))
        
        assert "❌" in result
        assert "не является файлом" in result
    
    def test_read_file_encoding_error(self, temp_dir):
        """Test reading file with encoding issues."""
        # Create a file with binary content
        binary_file = temp_dir / "binary.bin"
        binary_file.write_bytes(b"\x80\x81\x82\x83")
        
        result = read_file(str(binary_file))
        
        # Should handle the error gracefully
        assert "❌ Ошибка при чтении" in result
    
    def test_write_file_success(self, temp_dir):
        """Test successful file writing."""
        test_file = temp_dir / "test_write.txt"
        content = "Test content\nLine 2"
        
        result = write_file(str(test_file), content)
        
        assert "✅ Файл" in result
        assert "успешно записан" in result
        assert test_file.exists()
        assert test_file.read_text() == content
    
    def test_write_file_creates_directories(self, temp_dir):
        """Test that write_file creates parent directories."""
        nested_file = temp_dir / "new_dir" / "nested_dir" / "test.txt"
        content = "Test content"
        
        result = write_file(str(nested_file), content)
        
        assert "✅ Файл" in result
        assert nested_file.exists()
        assert nested_file.read_text() == content
    
    def test_write_file_permission_error(self, temp_dir):
        """Test write_file with permission error."""
        # Create read-only directory
        readonly_dir = temp_dir / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o444)
        
        readonly_file = readonly_dir / "test.txt"
        
        result = write_file(str(readonly_file), "content")
        
        assert "❌ Ошибка при записи файла" in result
        
        # Restore permissions for cleanup
        readonly_dir.chmod(0o755)
    
    def test_get_file_info_success(self, sample_test_file):
        """Test getting file information."""
        result = get_file_info(str(sample_test_file))
        
        assert "📄 Информация о файле" in result
        assert "Имя:" in result
        assert "Размер:" in result
        assert "Строк:" in result
        assert "Расширение:" in result
        assert sample_test_file.name in result
    
    def test_get_file_info_not_found(self, temp_dir):
        """Test getting info for non-existent file."""
        non_existent_file = temp_dir / "non_existent.txt"
        result = get_file_info(str(non_existent_file))
        
        assert "❌ Файл" in result
        assert "не найден" in result
    
    def test_get_file_info_not_a_file(self, temp_dir):
        """Test getting info for directory."""
        result = get_file_info(str(temp_dir))
        
        assert "❌" in result
        assert "не является файлом" in result
    
    def test_list_files_success(self, temp_dir):
        """Test listing files in directory."""
        # Create test files and directories
        (temp_dir / "test1.txt").write_text("content1")
        (temp_dir / "test2.py").write_text("content2")
        (temp_dir / "subdir").mkdir()
        
        result = list_files(str(temp_dir))
        
        assert "📂 Содержимое директории" in result
        assert "📁 subdir/" in result
        assert "📄 test1.txt" in result
        assert "📄 test2.py" in result
        assert "3 элементов" in result
    
    def test_list_files_empty_directory(self, temp_dir):
        """Test listing empty directory."""
        empty_dir = temp_dir / "empty"
        empty_dir.mkdir()
        
        result = list_files(str(empty_dir))
        
        assert "📂 Директория" in result
        assert "пуста" in result
    
    def test_list_files_not_found(self, temp_dir):
        """Test listing non-existent directory."""
        non_existent_dir = temp_dir / "non_existent"
        result = list_files(str(non_existent_dir))
        
        assert "❌ Директория" in result
        assert "не найдена" in result
    
    def test_list_files_not_a_directory(self, sample_test_file):
        """Test listing a file instead of directory."""
        result = list_files(str(sample_test_file))
        
        assert "❌" in result
        assert "не является директорией" in result
    
    def test_search_files_by_name(self, temp_dir):
        """Test searching files by name."""
        # Create test files
        (temp_dir / "test_file.py").write_text("content")
        (temp_dir / "another.txt").write_text("content")
        (temp_dir / "test_dir").mkdir()
        
        result = search_files("test", str(temp_dir))
        
        assert "Результаты поиска" in result
        assert "📄 test_file.py" in result
        assert "📁 test_dir/" in result
        assert "another.txt" not in result
    
    def test_search_files_with_regex(self, temp_dir):
        """Test searching files with regex."""
        # Create test files
        (temp_dir / "file1.py").write_text("content")
        (temp_dir / "file2.js").write_text("content")
        (temp_dir / "document.txt").write_text("content")
        
        result = search_files(r"file\d+", str(temp_dir), use_regex=True)
        
        assert "Результаты поиска" in result
        assert "file1.py" in result
        assert "file2.js" in result
        assert "document.txt" not in result
    
    def test_search_files_in_content(self, temp_dir):
        """Test searching in file content."""
        # Create test files with different content
        (temp_dir / "file1.py").write_text("def test_function():")
        (temp_dir / "file2.py").write_text("class MyClass:")
        (temp_dir / "file3.txt").write_text("some test data")
        
        result = search_files("test", str(temp_dir), search_in_content=True)
        
        assert "Результаты поиска" in result
        assert "file1.py" in result
        assert "file3.txt" in result
        assert "содержимое файла" in result
    
    def test_search_files_with_extensions(self, temp_dir):
        """Test searching with file extension filter."""
        # Create test files
        (temp_dir / "test.py").write_text("content")
        (temp_dir / "test.js").write_text("content")
        (temp_dir / "test.txt").write_text("content")
        
        result = search_files("test", str(temp_dir), file_extensions="py,js")
        
        assert "test.py" in result
        assert "test.js" in result
        assert "test.txt" not in result
    
    def test_search_files_max_results(self, temp_dir):
        """Test search with max results limit."""
        # Create many test files
        for i in range(10):
            (temp_dir / f"test_file_{i}.txt").write_text("content")
        
        result = search_files("test", str(temp_dir), max_results=3)
        
        assert "Результаты поиска" in result
        assert "показаны первые 3" in result
    
    def test_search_files_no_results(self, temp_dir):
        """Test search with no results."""
        (temp_dir / "file.txt").write_text("content")
        
        result = search_files("nonexistent", str(temp_dir))
        
        assert "не дал результатов" in result
    
    def test_search_files_invalid_regex(self, temp_dir):
        """Test search with invalid regex."""
        result = search_files("[invalid", str(temp_dir), use_regex=True)
        
        assert "ОШИБКА: Некорректное регулярное выражение" in result
    
    def test_search_files_invalid_directory(self, temp_dir):
        """Test search in non-existent directory."""
        non_existent_dir = temp_dir / "non_existent"
        result = search_files("test", str(non_existent_dir))
        
        assert "ОШИБКА: Директория" in result
        assert "не найдена" in result
    
    def test_edit_file_patch_simple(self, temp_dir):
        """Test simple file patch editing."""
        # Create test file
        test_file = temp_dir / "test.txt"
        original_content = """Line 1
Line 2
Line 3
Line 4"""
        test_file.write_text(original_content)
        
        # Simple patch to replace Line 2
        patch_content = """--- test.txt
+++ test.txt
@@ -1,4 +1,4 @@
 Line 1
-Line 2
+Modified Line 2
 Line 3
 Line 4"""
        
        result = edit_file_patch(str(test_file), patch_content)
        
        assert "✅ Файл" in result
        assert "успешно обновлен патчем" in result
        
        # Check that file was modified
        new_content = test_file.read_text()
        assert "Modified Line 2" in new_content
        assert "Line 2" not in new_content
    
    def test_edit_file_patch_not_found(self, temp_dir):
        """Test patch editing on non-existent file."""
        non_existent_file = temp_dir / "non_existent.txt"
        patch = "--- file\n+++ file\n@@ -1,1 +1,1 @@\n-old\n+new"
        
        result = edit_file_patch(str(non_existent_file), patch)
        
        assert "ОШИБКА: Файл" in result
        assert "не найден" in result
    
    def test_edit_file_patch_invalid_format(self, sample_test_file):
        """Test patch editing with invalid patch format."""
        invalid_patch = "invalid patch content"
        
        result = edit_file_patch(str(sample_test_file), invalid_patch)
        
        # Should handle gracefully - may not find valid patch blocks
        assert "✅" in result or "ОШИБКА" in result
    
    def test_get_file_tools(self):
        """Test getting all file tools."""
        tools = get_file_tools()
        
        assert len(tools) > 0
        # Check that we get function objects
        assert callable(tools[0])
    
    def test_get_file_tools_by_names_valid(self):
        """Test getting file tools by valid names."""
        tool_names = ["file_read", "file_write", "file_list"]
        tools = get_file_tools_by_names(tool_names)
        
        assert len(tools) == 3
        assert all(callable(tool) for tool in tools)
    
    def test_get_file_tools_by_names_invalid(self):
        """Test getting file tools with some invalid names."""
        tool_names = ["file_read", "invalid_tool", "file_write"]
        
        with patch('tools.file_tools.Logger') as mock_logger_class:
            mock_logger = Mock()
            mock_logger_class.return_value = mock_logger
            
            tools = get_file_tools_by_names(tool_names)
            
            assert len(tools) == 2  # Only valid tools returned
            mock_logger.warning.assert_called_once()
    
    @patch('tools.file_tools.log_tool_call')
    @patch('tools.file_tools.log_tool_result')
    def test_logging_integration(self, mock_log_result, mock_log_call, sample_test_file):
        """Test that file tools integrate with logging."""
        read_file(str(sample_test_file))
        
        # Verify logging calls were made
        mock_log_call.assert_called()
        mock_log_result.assert_called()
        
        # Check call arguments
        call_args = mock_log_call.call_args
        assert call_args[0][0] == "read_file"
        assert "filepath" in call_args[0][1]
    
    def test_write_file_large_content(self, temp_dir):
        """Test writing large file content."""
        test_file = temp_dir / "large_file.txt"
        large_content = "x" * 10000  # 10KB content
        
        result = write_file(str(test_file), large_content)
        
        assert "✅ Файл" in result
        assert test_file.exists()
        assert len(test_file.read_text()) == 10000
    
    def test_search_files_deep_directory_structure(self, temp_dir):
        """Test searching in deep directory structure."""
        # Create nested directories with files
        deep_dir = temp_dir / "level1" / "level2" / "level3"
        deep_dir.mkdir(parents=True)
        (deep_dir / "deep_test_file.txt").write_text("content")
        (temp_dir / "shallow_test_file.txt").write_text("content")
        
        result = search_files("test_file", str(temp_dir))
        
        assert "deep_test_file.txt" in result
        assert "shallow_test_file.txt" in result
    
    def test_file_operations_with_unicode(self, temp_dir):
        """Test file operations with unicode content."""
        test_file = temp_dir / "unicode_test.txt"
        unicode_content = "Привет мир! 🌍 测试 テスト"
        
        # Write unicode content
        write_result = write_file(str(test_file), unicode_content)
        assert "✅" in write_result
        
        # Read unicode content
        read_result = read_file(str(test_file))
        assert unicode_content in read_result
        
        # Get info for unicode file
        info_result = get_file_info(str(test_file))
        assert "📄 Информация о файле" in info_result
    
    def test_search_files_with_unicode_pattern(self, temp_dir):
        """Test searching with unicode patterns."""
        # Create files with unicode names and content
        (temp_dir / "тест_файл.txt").write_text("содержимое")
        (temp_dir / "test_file.txt").write_text("content")
        
        result = search_files("тест", str(temp_dir))
        
        assert "тест_файл.txt" in result
    
    def test_concurrent_file_operations(self, temp_dir):
        """Test concurrent file operations."""
        import threading
        
        results = []
        
        def write_file_thread(file_num):
            file_path = temp_dir / f"concurrent_{file_num}.txt"
            content = f"Content from thread {file_num}"
            result = write_file(str(file_path), content)
            results.append(result)
        
        threads = []
        for i in range(5):
            thread = threading.Thread(target=write_file_thread, args=(i,))
            threads.append(thread)
            thread.start()
        
        for thread in threads:
            thread.join()
        
        # All operations should succeed
        assert len(results) == 5
        assert all("✅" in result for result in results)
        
        # All files should exist
        for i in range(5):
            file_path = temp_dir / f"concurrent_{i}.txt"
            assert file_path.exists()
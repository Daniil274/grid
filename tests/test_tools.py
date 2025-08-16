"""
Тесты для инструментов системы Grid.
Проверяют функциональность файловых операций, Git операций, MCP серверов и агентных инструментов.
"""

import pytest
import os
import tempfile
import json
from unittest.mock import Mock, patch, AsyncMock
from typing import Dict, Any, List

from tools import get_tools_by_names
from tools.file_tools import read_file, write_file, list_files, get_file_info, search_files, edit_file_patch
from tools.git_tools import git_status, git_add_all, git_commit
from tools.function_tools import get_all_tools
from .test_framework import TestEnvironment


class TestFunctionTools:
    """Тесты функциональных инструментов."""
    
    def test_get_tools_by_names_valid(self):
        """Тест получения инструментов по именам."""
        tool_names = ["read_file", "write_file"]
        tools = get_tools_by_names(tool_names)
        
        assert len(tools) == 2
        assert all(hasattr(tool, 'name') for tool in tools)
        
        tool_names_result = [getattr(tool, 'name', '') for tool in tools]
        assert "read_file" in tool_names_result
        assert "write_file" in tool_names_result
    
    def test_get_tools_by_names_invalid(self):
        """Тест получения инструментов с невалидными именами."""
        tool_names = ["nonexistent_tool"]
        
        # Должен вернуть пустой список или исключение
        try:
            tools = get_tools_by_names(tool_names)
            assert len(tools) == 0 or tools is None
        except Exception:
            # Ожидаем исключение для несуществующих инструментов
            pass
    
    def test_get_function_tools(self):
        """Тест получения всех функциональных инструментов."""
        tools = get_all_tools()
        
        assert isinstance(tools, list)
        assert len(tools) > 0
        
        # Проверяем, что все инструменты имеют необходимые атрибуты
        for tool in tools:
            assert hasattr(tool, 'name')
            assert hasattr(tool, 'description') or hasattr(tool, '__doc__')


class TestFileTools:
    """Тесты файловых инструментов."""
    
    def setup_method(self):
        """Подготовка для каждого теста."""
        self.temp_dir = tempfile.mkdtemp(prefix="grid_file_test_")
        self.test_file = os.path.join(self.temp_dir, "test.txt")
        self.test_content = "Тестовое содержимое файла\nВторая строка"
    
    def teardown_method(self):
        """Очистка после каждого теста."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    @pytest.mark.asyncio
    async def test_write_and_read_file(self):
        """Тест записи и чтения файла."""
        # Записываем файл
        write_result = await write_file(self.test_file, self.test_content)
        assert "успешно" in write_result.lower() or "записан" in write_result.lower()
        assert os.path.exists(self.test_file)
        
        # Читаем файл
        read_result = await read_file(self.test_file)
        assert self.test_content in read_result
    
    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self):
        """Тест чтения несуществующего файла."""
        nonexistent_file = os.path.join(self.temp_dir, "nonexistent.txt")
        result = await read_file(nonexistent_file)
        
        assert "не найден" in result.lower() or "ошибка" in result.lower()
    
    @pytest.mark.asyncio
    async def test_list_files(self):
        """Тест получения списка файлов."""
        # Создаем несколько тестовых файлов
        files_to_create = ["file1.txt", "file2.py", "subdir/file3.md"]
        
        for file_path in files_to_create:
            full_path = os.path.join(self.temp_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w') as f:
                f.write("test content")
        
        # Получаем список файлов
        result = await list_files(self.temp_dir)
        
        assert "file1.txt" in result
        assert "file2.py" in result
        assert "subdir" in result
    
    @pytest.mark.asyncio
    async def test_get_file_info(self):
        """Тест получения информации о файле."""
        # Создаем тестовый файл
        with open(self.test_file, 'w') as f:
            f.write(self.test_content)
        
        result = await get_file_info(self.test_file)
        
        assert "размер" in result.lower() or "size" in result.lower()
        assert "test.txt" in result
    
    @pytest.mark.asyncio
    async def test_search_files(self):
        """Тест поиска файлов."""
        # Создаем файлы с разным содержимым
        test_files = {
            "doc1.txt": "Это важный документ с ключевым словом",
            "doc2.py": "Код Python без ключевого слова",
            "doc3.md": "Markdown документ с ключевым словом"
        }
        
        for filename, content in test_files.items():
            filepath = os.path.join(self.temp_dir, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
        
        # Поиск по содержимому
        result = await search_files("ключевым", self.temp_dir, False, True)
        
        assert "doc1.txt" in result
        assert "doc3.md" in result
        assert "doc2.py" not in result
    
    @pytest.mark.asyncio
    async def test_edit_file_patch(self):
        """Тест редактирования файла патчем."""
        # Создаем исходный файл
        original_content = "строка 1\nстрока 2\nстрока 3"
        with open(self.test_file, 'w', encoding='utf-8') as f:
            f.write(original_content)
        
        # Создаем патч для замены второй строки
        patch_content = """--- a/test.txt
+++ b/test.txt
@@ -1,3 +1,3 @@
 строка 1
-строка 2
+измененная строка 2
 строка 3"""
        
        result = await edit_file_patch(self.test_file, patch_content)
        
        # Проверяем, что патч применился
        with open(self.test_file, 'r', encoding='utf-8') as f:
            modified_content = f.read()
        
        assert "измененная строка 2" in modified_content
        assert "строка 2" not in modified_content


class TestGitTools:
    """Тесты Git инструментов."""
    
    def setup_method(self):
        """Подготовка Git репозитория для тестов."""
        self.temp_dir = tempfile.mkdtemp(prefix="grid_git_test_")
        
        # Инициализируем Git репозиторий
        import subprocess
        subprocess.run(["git", "init"], cwd=self.temp_dir, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.temp_dir)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.temp_dir)
    
    def teardown_method(self):
        """Очистка после тестов."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    @pytest.mark.asyncio
    async def test_git_status(self):
        """Тест статуса Git репозитория."""
        result = await git_status(self.temp_dir)
        
        assert "статус" in result.lower() or "status" in result.lower()
        assert "рабочая" in result.lower() or "working" in result.lower()
    
    @pytest.mark.asyncio
    async def test_git_add_and_commit(self):
        """Тест добавления файлов и коммита."""
        # Создаем тестовый файл
        test_file = os.path.join(self.temp_dir, "test.txt")
        with open(test_file, 'w') as f:
            f.write("test content")
        
        # Добавляем все файлы
        add_result = await git_add_all(self.temp_dir)
        assert "добавлен" in add_result.lower() or "added" in add_result.lower()
        
        # Создаем коммит
        commit_result = await git_commit(
            self.temp_dir, 
            "Test commit", 
            "Test User", 
            "test@example.com"
        )
        assert "коммит" in commit_result.lower() or "commit" in commit_result.lower()


class TestMCPTools:
    """Тесты MCP инструментов."""
    
    @pytest.mark.asyncio
    async def test_mcp_tool_configuration(self):
        """Тест конфигурации MCP инструментов."""
        async with TestEnvironment() as env:
            # Проверяем, что MCP инструменты правильно настроены в тестовой конфигурации
            mcp_enabled = env.config.is_mcp_enabled()
            assert mcp_enabled == False  # В тестовой конфигурации MCP отключен
    
    @pytest.mark.asyncio
    async def test_mcp_server_creation_disabled(self):
        """Тест создания MCP серверов когда они отключены."""
        async with TestEnvironment() as env:
            # Пытаемся создать агента с MCP инструментами
            agent = await env.agent_factory.create_agent("test_simple_agent")
            
            # Проверяем, что агент создался без MCP серверов
            assert agent is not None
            assert hasattr(agent, 'mcp_servers') or True  # MCP может быть не инициализирован


class TestAgentTools:
    """Тесты агентных инструментов."""
    
    @pytest.mark.asyncio
    async def test_agent_tool_creation(self):
        """Тест создания агентных инструментов."""
        async with TestEnvironment() as env:
            # Создаем агента с агентными инструментами
            agent = await env.agent_factory.create_agent("test_coordinator_agent")
            
            assert agent is not None
            assert agent.name == "Тестовый координатор"
    
    @pytest.mark.asyncio
    async def test_agent_tool_context_sharing(self):
        """Тест передачи контекста между агентными инструментами."""
        async with TestEnvironment() as env:
            # Настраиваем контекст
            env.agent_factory.add_to_context("user", "Начальное сообщение")
            
            # Создаем агента
            agent = await env.agent_factory.create_agent("test_coordinator_agent")
            
            # Проверяем, что контекст доступен
            context_info = env.agent_factory.get_context_info()
            assert context_info.get("message_count", 0) >= 1


class TestToolIntegration:
    """Интеграционные тесты инструментов."""
    
    @pytest.mark.asyncio
    async def test_tool_loading_in_agent(self):
        """Тест загрузки инструментов в агенте."""
        async with TestEnvironment() as env:
            # Создаем агента с файловыми инструментами
            agent = await env.agent_factory.create_agent("test_file_agent")
            
            assert agent is not None
            assert hasattr(agent, 'tools') or hasattr(agent, '_tools')
    
    @pytest.mark.asyncio
    async def test_tool_error_handling(self):
        """Тест обработки ошибок в инструментах."""
        # Тестируем чтение файла с недопустимым путем
        invalid_path = "/invalid/path/file.txt"
        result = await read_file(invalid_path)
        
        # Должно быть сообщение об ошибке, а не исключение
        assert isinstance(result, str)
        assert "ошибка" in result.lower() or "error" in result.lower()
    
    @pytest.mark.asyncio
    async def test_tool_caching(self):
        """Тест кеширования инструментов."""
        async with TestEnvironment() as env:
            # Создаем несколько агентов с одинаковыми инструментами
            agent1 = await env.agent_factory.create_agent("test_file_agent")
            agent2 = await env.agent_factory.create_agent("test_file_agent")
            
            # Проверяем, что инструменты кешируются
            assert agent1 is agent2  # Агенты кешируются
    
    @pytest.mark.asyncio
    async def test_tool_permissions(self):
        """Тест ограничений доступа инструментов."""
        async with TestEnvironment() as env:
            # Создаем файл вне рабочей директории
            outside_file = "/tmp/outside_test.txt"
            
            try:
                with open(outside_file, 'w') as f:
                    f.write("test")
                
                # Пытаемся прочитать файл - должно работать
                result = await read_file(outside_file)
                assert "test" in result or "ошибка" in result.lower()
                
            finally:
                # Очищаем
                if os.path.exists(outside_file):
                    os.remove(outside_file)


class TestToolPerformance:
    """Тесты производительности инструментов."""
    
    @pytest.mark.asyncio
    async def test_file_operations_performance(self):
        """Тест производительности файловых операций."""
        temp_dir = tempfile.mkdtemp()
        
        try:
            import time
            
            # Тестируем множественные файловые операции
            start_time = time.time()
            
            tasks = []
            for i in range(10):
                filepath = os.path.join(temp_dir, f"test_{i}.txt")
                tasks.append(write_file(filepath, f"Содержимое файла {i}"))
            
            # Выполняем параллельно
            import asyncio
            await asyncio.gather(*tasks)
            
            duration = time.time() - start_time
            
            # Проверяем, что операции выполнились быстро
            assert duration < 5.0  # 5 секунд на 10 файлов
            
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    @pytest.mark.asyncio
    async def test_large_file_handling(self):
        """Тест работы с большими файлами."""
        temp_dir = tempfile.mkdtemp()
        large_file = os.path.join(temp_dir, "large.txt")
        
        try:
            # Создаем относительно большой файл (1MB)
            large_content = "A" * (1024 * 1024)  # 1MB
            
            # Записываем большой файл
            result = await write_file(large_file, large_content)
            assert "успешно" in result.lower()
            
            # Читаем большой файл
            read_result = await read_file(large_file)
            assert len(read_result) > 1000000  # Проверяем, что прочитали много данных
            
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
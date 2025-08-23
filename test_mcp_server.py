#!/usr/bin/env python3
"""
Тест MCP сервера Grid системы.
Проверяет основную функциональность без запуска полного сервера.
"""

import asyncio
import sys
import json
from pathlib import Path

# Добавляем путь к grid пакету
sys.path.insert(0, str(Path(__file__).parent))

# Импорты для тестирования
from mcp_server import initialize_grid_system, list_tools, list_resources, call_tool, read_resource

async def test_initialization():
    """Тест инициализации системы."""
    print("🧪 Testing Grid system initialization...")
    
    result = await initialize_grid_system("config.yaml")
    if result:
        print("✅ Grid system initialized successfully")
        return True
    else:
        print("❌ Failed to initialize Grid system")
        return False

async def test_resources():
    """Тест ресурсов."""
    print("\n🧪 Testing MCP resources...")
    
    try:
        resources = await list_resources()
        print(f"✅ Found {len(resources)} resources:")
        for resource in resources[:5]:  # Показываем первые 5
            print(f"  - {resource.name} ({resource.uri})")
        if len(resources) > 5:
            print(f"  ... and {len(resources) - 5} more")
        
        # Тестируем чтение ресурса
        if resources:
            test_uri = resources[0].uri
            content = await read_resource(test_uri)
            print(f"✅ Successfully read resource '{test_uri}' ({len(content)} chars)")
        
        return True
    except Exception as e:
        print(f"❌ Resource test failed: {e}")
        return False

async def test_tools():
    """Тест инструментов."""
    print("\n🧪 Testing MCP tools...")
    
    try:
        tools = await list_tools()
        print(f"✅ Found {len(tools)} tools:")
        
        # Группируем инструменты
        agent_tools = [t for t in tools if t.name.startswith("run_agent_")]
        system_tools = [t for t in tools if not t.name.startswith("run_agent_")]
        
        print(f"  - Agent tools: {len(agent_tools)}")
        for tool in agent_tools[:3]:  # Показываем первые 3
            print(f"    • {tool.name}")
        if len(agent_tools) > 3:
            print(f"    • ... and {len(agent_tools) - 3} more")
            
        print(f"  - System tools: {len(system_tools)}")
        for tool in system_tools:
            print(f"    • {tool.name}")
        
        return True
    except Exception as e:
        print(f"❌ Tools test failed: {e}")
        return False

async def test_system_tools():
    """Тест системных инструментов."""
    print("\n🧪 Testing system tools...")
    
    try:
        # Тест списка агентов
        result = await call_tool("list_grid_agents", {})
        if result and result[0].text:
            agents_data = json.loads(result[0].text)
            print(f"✅ list_grid_agents: Found {len(agents_data)} agents")
        else:
            print("❌ list_grid_agents returned empty result")
            return False
        
        # Тест статуса системы
        result = await call_tool("get_grid_status", {})
        if result and result[0].text:
            status_data = json.loads(result[0].text)
            print(f"✅ get_grid_status: System status '{status_data.get('status', 'unknown')}'")
        else:
            print("❌ get_grid_status returned empty result")
            return False
        
        # Тест очистки контекста
        result = await call_tool("clear_grid_context", {})
        if result and result[0].text and "успешно" in result[0].text.lower():
            print("✅ clear_grid_context: Context cleared")
        else:
            print("❌ clear_grid_context failed")
            return False
        
        return True
    except Exception as e:
        print(f"❌ System tools test failed: {e}")
        return False

async def test_agent_tool():
    """Тест инструмента агента."""
    print("\n🧪 Testing agent tool...")
    
    try:
        # Получаем список инструментов
        tools = await list_tools()
        agent_tools = [t for t in tools if t.name.startswith("run_agent_")]
        
        if not agent_tools:
            print("❌ No agent tools found")
            return False
        
        # Ищем простой агент без MCP инструментов
        test_tool = None
        for tool in agent_tools:
            if "simple" in tool.name:
                test_tool = tool
                break
        if not test_tool:
            test_tool = agent_tools[0]
        test_message = "Привет! Это тестовое сообщение. Просто ответь 'Получил сообщение'."
        
        print(f"Testing {test_tool.name} with test message...")
        
        result = await call_tool(test_tool.name, {
            "message": test_message,
            "stream": False
        })
        
        if result and result[0].text:
            response = result[0].text
            print(f"✅ {test_tool.name}: Got response ({len(response)} chars)")
            if len(response) > 100:
                print(f"    Response preview: {response[:97]}...")
            else:
                print(f"    Response: {response}")
        else:
            print(f"❌ {test_tool.name}: No response received")
            return False
        
        return True
    except Exception as e:
        print(f"❌ Agent tool test failed: {e}")
        return False

async def main():
    """Главная функция тестирования."""
    print("🚀 Grid MCP Server Test Suite")
    print("=" * 50)
    
    # Список тестов
    tests = [
        ("Initialization", test_initialization),
        ("Resources", test_resources), 
        ("Tools", test_tools),
        ("System Tools", test_system_tools),
        ("Agent Tool", test_agent_tool)
    ]
    
    results = []
    
    for test_name, test_func in tests:
        try:
            result = await test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"❌ {test_name} test crashed: {e}")
            results.append((test_name, False))
    
    # Результаты
    print("\n" + "=" * 50)
    print("📊 Test Results:")
    print("-" * 30)
    
    passed = 0
    total = len(results)
    
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} {test_name}")
        if result:
            passed += 1
    
    print("-" * 30)
    print(f"Total: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed! MCP server is working correctly.")
        return 0
    else:
        print("⚠️  Some tests failed. Check the output above.")
        return 1

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n👋 Tests interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"💥 Fatal error: {e}")
        sys.exit(1)
#!/usr/bin/env python3
"""
Test script for GRID Agent System API.
Verifies basic functionality of OpenAI-compatible endpoints.
"""

import sys
import requests
import json
import time
from pathlib import Path

def test_api_endpoint(base_url="http://localhost:8000"):
    """Test all major API endpoints."""
    
    print("🧪 Testing GRID Agent System API")
    print("=" * 50)
    
    # Test 1: Health Check
    print("1. Testing health endpoint...")
    try:
        response = requests.get(f"{base_url}/health")
        if response.status_code == 200:
            health = response.json()
            print(f"   ✅ Health: {health['status']} (mode: {health['mode']})")
        else:
            print(f"   ❌ Health check failed: {response.status_code}")
            return False
    except Exception as e:
        print(f"   ❌ Health check error: {e}")
        return False
    
    # Test 2: Models List
    print("2. Testing models endpoint...")
    try:
        response = requests.get(f"{base_url}/v1/models")
        if response.status_code == 200:
            models = response.json()
            print(f"   ✅ Found {len(models['data'])} models")
            for model in models['data'][:3]:  # Show first 3
                print(f"      - {model['id']}")
        else:
            print(f"   ❌ Models list failed: {response.status_code}")
            return False
    except Exception as e:
        print(f"   ❌ Models list error: {e}")
        return False
    
    # Test 3: Chat Completion
    print("3. Testing chat completions...")
    try:
        payload = {
            "model": "grid-coordinator",
            "messages": [
                {"role": "user", "content": "Hello! Can you help me create a simple Python script?"}
            ],
            "max_tokens": 500,
            "temperature": 0.7
        }
        
        response = requests.post(
            f"{base_url}/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            json=payload
        )
        
        if response.status_code == 200:
            result = response.json()
            content = result['choices'][0]['message']['content']
            print(f"   ✅ Chat completion successful")
            print(f"      Response length: {len(content)} chars")
            print(f"      Usage: {result['usage']}")
            print(f"      Preview: {content[:100]}...")
        else:
            print(f"   ❌ Chat completion failed: {response.status_code}")
            print(f"      Error: {response.text}")
            return False
    except Exception as e:
        print(f"   ❌ Chat completion error: {e}")
        return False
    
    # Test 4: Different Agent Types
    print("4. Testing different agent types...")
    agent_tests = [
        ("grid-code-agent", "Analyze this Python code: print('hello')"),
        ("grid-security-guardian", "Check this command: ls -la"),
        ("grid-file-agent", "List files in current directory"),
    ]
    
    for model, message in agent_tests:
        try:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": message}],
                "max_tokens": 200
            }
            
            response = requests.post(
                f"{base_url}/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                json=payload
            )
            
            if response.status_code == 200:
                result = response.json()
                content = result['choices'][0]['message']['content']
                print(f"   ✅ {model}: {content[:60]}...")
            else:
                print(f"   ❌ {model} failed: {response.status_code}")
        except Exception as e:
            print(f"   ❌ {model} error: {e}")
    
    # Test 5: Streaming
    print("5. Testing streaming...")
    try:
        payload = {
            "model": "grid-coordinator",
            "messages": [{"role": "user", "content": "Count from 1 to 5"}],
            "stream": True
        }
        
        response = requests.post(
            f"{base_url}/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            json=payload,
            stream=True
        )
        
        if response.status_code == 200:
            chunks = 0
            for line in response.iter_lines():
                if line:
                    line = line.decode('utf-8')
                    if line.startswith('data: ') and not line.endswith('[DONE]'):
                        chunks += 1
                        if chunks >= 5:  # Stop after 5 chunks
                            break
            print(f"   ✅ Streaming successful, received {chunks} chunks")
        else:
            print(f"   ❌ Streaming failed: {response.status_code}")
    except Exception as e:
        print(f"   ❌ Streaming error: {e}")
    
    print("\n🎉 All tests completed!")
    return True

def test_openai_compatibility():
    """Test OpenAI SDK compatibility (if available)."""
    try:
        import openai
        print("\n🔌 Testing OpenAI SDK compatibility...")
        
        # This would work if user has OpenAI SDK and configures it for local endpoint
        print("   💡 To test with OpenAI SDK, configure:")
        print("   openai.api_base = 'http://localhost:8000/v1'")
        print("   openai.api_key = 'any-key'")
        
    except ImportError:
        print("\n💡 OpenAI SDK not installed. Install with: pip install openai")

if __name__ == "__main__":
    # Check if API is running
    base_url = "http://localhost:8000"
    
    print("🚀 GRID Agent System API Test")
    print(f"Testing endpoint: {base_url}")
    print()
    
    try:
        # Quick connectivity test
        response = requests.get(f"{base_url}/", timeout=5)
        if response.status_code != 200:
            print("❌ API not responding correctly")
            print("💡 Start the API with: python api/standalone.py")
            sys.exit(1)
    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to API")
        print("💡 Start the API with: python api/standalone.py")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Connection error: {e}")
        sys.exit(1)
    
    # Run all tests
    success = test_api_endpoint(base_url)
    test_openai_compatibility()
    
    if success:
        print("\n✅ All tests passed! API is working correctly.")
        sys.exit(0)
    else:
        print("\n❌ Some tests failed. Check the API logs.")
        sys.exit(1)
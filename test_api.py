#!/usr/bin/env python3
"""
Simple test script to verify GRID API functionality.
"""

import asyncio
import httpx
import json
import sys
from pathlib import Path

# Add grid to path
sys.path.insert(0, str(Path(__file__).parent))

API_BASE = "http://localhost:8000"

async def test_api():
    """Test basic API functionality."""
    
    print("🧪 Testing GRID Agent System API")
    print("=" * 50)
    
    async with httpx.AsyncClient() as client:
        
        # Test 1: Health check
        print("\n1. Testing health endpoint...")
        try:
            response = await client.get(f"{API_BASE}/health")
            if response.status_code == 200:
                print("✅ Health check passed")
                data = response.json()
                print(f"   Status: {data.get('status')}")
                print(f"   Uptime: {data.get('uptime', 0):.2f}s")
            else:
                print(f"❌ Health check failed: {response.status_code}")
        except Exception as e:
            print(f"❌ Health check error: {e}")
        
        # Test 2: List models
        print("\n2. Testing models endpoint...")
        try:
            response = await client.get(f"{API_BASE}/v1/models")
            if response.status_code == 200:
                print("✅ Models endpoint working")
                data = response.json()
                models = data.get('data', [])
                print(f"   Available models: {len(models)}")
                for model in models[:3]:  # Show first 3
                    print(f"   - {model.get('id')}: {model.get('description', 'No description')[:50]}...")
            else:
                print(f"❌ Models endpoint failed: {response.status_code}")
        except Exception as e:
            print(f"❌ Models endpoint error: {e}")
        
        # Test 3: Authentication
        print("\n3. Testing authentication...")
        try:
            auth_data = {
                "username": "testuser",
                "password": "password123"
            }
            response = await client.post(f"{API_BASE}/v1/auth/login", json=auth_data)
            if response.status_code == 200:
                print("✅ Authentication working")
                data = response.json()
                token = data.get('access_token')
                print(f"   Token received: {token[:20]}...")
                
                # Test authenticated request
                headers = {"Authorization": f"Bearer {token}"}
                profile_response = await client.get(f"{API_BASE}/v1/auth/profile", headers=headers)
                if profile_response.status_code == 200:
                    print("✅ Authenticated request working")
                    profile = profile_response.json()
                    print(f"   User: {profile.get('username')}")
                else:
                    print(f"❌ Authenticated request failed: {profile_response.status_code}")
                
                # Test 4: Chat completion (simple)
                print("\n4. Testing chat completion...")
                try:
                    chat_data = {
                        "model": "grid-coordinator",
                        "messages": [
                            {"role": "user", "content": "Hello, can you help me?"}
                        ],
                        "max_tokens": 100
                    }
                    
                    # Note: This might fail if agents aren't fully initialized
                    response = await client.post(
                        f"{API_BASE}/v1/chat/completions", 
                        json=chat_data,
                        headers=headers,
                        timeout=30.0
                    )
                    
                    if response.status_code == 200:
                        print("✅ Chat completion working")
                        data = response.json()
                        message = data.get('choices', [{}])[0].get('message', {})
                        content = message.get('content', '')
                        print(f"   Response: {content[:100]}...")
                        
                        # Show metadata
                        metadata = data.get('grid_metadata', {})
                        if metadata:
                            print(f"   Agent used: {metadata.get('agent_used')}")
                            print(f"   Execution time: {metadata.get('execution_time', 0):.2f}s")
                    else:
                        print(f"❌ Chat completion failed: {response.status_code}")
                        print(f"   Error: {response.text}")
                        
                except Exception as e:
                    print(f"❌ Chat completion error: {e}")
                
            else:
                print(f"❌ Authentication failed: {response.status_code}")
                print(f"   Error: {response.text}")
        except Exception as e:
            print(f"❌ Authentication error: {e}")
        
        # Test 5: Agent list
        print("\n5. Testing agents endpoint...")
        try:
            response = await client.get(f"{API_BASE}/v1/agents/")
            if response.status_code == 200:
                print("✅ Agents endpoint working")
                agents = response.json()
                print(f"   Available agents: {len(agents)}")
                for agent in agents[:3]:
                    print(f"   - {agent.get('agent_type')}: {agent.get('name')}")
            else:
                print(f"❌ Agents endpoint failed: {response.status_code}")
        except Exception as e:
            print(f"❌ Agents endpoint error: {e}")

    print("\n" + "=" * 50)
    print("🎉 API test completed!")
    print("\nTo start the API server:")
    print("   cd /workspaces/grid")
    print("   python -m api.main")
    print("\nThen run this test again to see full functionality.")

if __name__ == "__main__":
    asyncio.run(test_api())
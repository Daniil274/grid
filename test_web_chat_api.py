#!/usr/bin/env python3
"""
Тестовый скрипт для проверки веб-чата через API
"""
import asyncio
import json
import sys
from datetime import datetime

import aiohttp
import websockets


BASE_URL = "http://127.0.0.1:8000"
WS_URL = "ws://127.0.0.1:8000"


async def test_bootstrap():
    """Проверка загрузки bootstrap данных"""
    print("1. Проверка bootstrap...")
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/api/chat/bootstrap") as resp:
            if resp.status != 200:
                print(f"   ❌ Bootstrap failed: {resp.status}")
                return None
            data = await resp.json()
            print(f"   ✓ Bootstrap OK: {data.get('workspace_path', 'N/A')}")
            return data


async def test_create_conversation(agent_key):
    """Создание нового разговора"""
    print("2. Создание нового разговора...")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{BASE_URL}/api/chat/conversations",
            json={"agent_key": agent_key}
        ) as resp:
            if resp.status != 200:
                print(f"   ❌ Create conversation failed: {resp.status}")
                return None
            data = await resp.json()
            context_id = data.get("id")
            print(f"   ✓ Conversation created: {context_id}")
            return context_id


async def test_send_message(context_id, agent_key, message):
    """Отправка сообщения и получение ответа через WebSocket"""
    print(f"3. Отправка сообщения: '{message}'")
    
    ws_url = f"{WS_URL}/api/chat/ws/{context_id}"
    trace_events = []
    tokens = []
    has_thinking = False
    has_empty_blocks = False
    
    try:
        async with websockets.connect(ws_url) as websocket:
            # Отправляем сообщение
            await websocket.send(json.dumps({
                "message": message,
                "agent_key": agent_key
            }))
            print("   ✓ Сообщение отправлено")
            
            # Получаем ответы
            while True:
                try:
                    raw_message = await asyncio.wait_for(websocket.recv(), timeout=60.0)
                    payload = json.loads(raw_message)
                    
                    msg_type = payload.get("type")
                    
                    if msg_type == "token":
                        tokens.append(payload.get("content", ""))
                    
                    elif msg_type == "trace":
                        kind = payload.get("kind")
                        title = payload.get("title", "")
                        details = payload.get("details", "")
                        
                        trace_events.append({
                            "kind": kind,
                            "title": title,
                            "details": details,
                            "subtitle": payload.get("subtitle", ""),
                        })
                        
                        # Проверка на "Размышления"
                        if kind == "thinking" and title == "Размышления":
                            has_thinking = True
                            print(f"   ✓ Найдено событие 'Размышления'")
                        
                        # Проверка на пустые блоки (нет title и нет details)
                        if not title.strip() and not details.strip():
                            has_empty_blocks = True
                            print(f"   ⚠ Обнаружен пустой блок trace: {payload}")
                    
                    elif msg_type == "final_output":
                        print(f"   ✓ Получен final_output")
                    
                    elif msg_type == "error":
                        print(f"   ❌ Ошибка: {payload.get('content')}")
                    
                    elif msg_type == "done":
                        print("   ✓ Генерация завершена")
                        break
                        
                except asyncio.TimeoutError:
                    print("   ❌ Timeout при получении ответа")
                    break
    
    except Exception as e:
        print(f"   ❌ WebSocket ошибка: {e}")
        return None
    
    return {
        "trace_events": trace_events,
        "tokens": tokens,
        "has_thinking": has_thinking,
        "has_empty_blocks": has_empty_blocks,
    }


async def test_reload_conversation(context_id):
    """Проверка сохранения trace после перезагрузки"""
    print("4. Проверка сохранения trace после перезагрузки...")
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/api/chat/conversations/{context_id}") as resp:
            if resp.status != 200:
                print(f"   ❌ Reload failed: {resp.status}")
                return None
            data = await resp.json()
            
            messages = data.get("messages", [])
            if not messages:
                print("   ❌ Нет сообщений в разговоре")
                return None
            
            # Проверяем последнее сообщение ассистента
            assistant_messages = [m for m in messages if m.get("role") == "assistant"]
            if not assistant_messages:
                print("   ❌ Нет сообщений от ассистента")
                return None
            
            last_assistant = assistant_messages[-1]
            metadata = last_assistant.get("metadata", {})
            trace_events = metadata.get("trace_events", [])
            
            print(f"   ✓ Загружено {len(trace_events)} trace событий")
            
            # Проверяем наличие "Размышления"
            has_thinking = any(
                e.get("kind") == "thinking" and e.get("title") == "Размышления"
                for e in trace_events
            )
            
            if has_thinking:
                print("   ✓ 'Размышления' сохранились после перезагрузки")
            else:
                print("   ⚠ 'Размышления' не найдены в сохраненных trace")
            
            return {
                "trace_events": trace_events,
                "has_thinking": has_thinking,
            }


async def main():
    print("=" * 60)
    print("Тестирование веб-чата")
    print("=" * 60)
    print()
    
    # 1. Bootstrap
    bootstrap = await test_bootstrap()
    if not bootstrap:
        print("\n❌ Не удалось загрузить bootstrap")
        return 1
    
    agent_key = bootstrap.get("default_agent")
    if not agent_key:
        print("\n❌ Нет default_agent в bootstrap")
        return 1
    
    print()
    
    # 2. Создание разговора
    context_id = await test_create_conversation(agent_key)
    if not context_id:
        print("\n❌ Не удалось создать разговор")
        return 1
    
    print()
    
    # 3. Отправка сообщения
    result = await test_send_message(context_id, agent_key, "Привет, сделай ls")
    if not result:
        print("\n❌ Не удалось отправить сообщение")
        return 1
    
    print()
    
    # 4. Перезагрузка разговора
    reload_result = await test_reload_conversation(context_id)
    if not reload_result:
        print("\n❌ Не удалось перезагрузить разговор")
        return 1
    
    print()
    print("=" * 60)
    print("ИТОГОВЫЙ ОТЧЕТ")
    print("=" * 60)
    
    # Проверка 1: Пустые серые блоки
    if result["has_empty_blocks"]:
        print("❌ 1. Пустые серые блоки ПОЯВЛЯЮТСЯ")
    else:
        print("✓ 1. Пустые серые блоки НЕ появляются")
    
    # Проверка 2: Размышления перед вызовом инструмента
    if result["has_thinking"]:
        print("✓ 2. Блок 'Размышления' ПОЯВЛЯЕТСЯ перед вызовом инструмента")
    else:
        print("❌ 2. Блок 'Размышления' НЕ появляется")
    
    # Проверка 3: Сохранение trace после перезагрузки
    if reload_result["has_thinking"]:
        print("✓ 3. Ход выполнения СОХРАНЯЕТСЯ после перезагрузки")
    else:
        print("⚠ 3. Ход выполнения не полностью сохраняется (нет 'Размышления')")
    
    print()
    print(f"Всего trace событий: {len(result['trace_events'])}")
    print(f"Токенов получено: {len(result['tokens'])}")
    print()
    
    return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\nПрервано пользователем")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

import os
import socket
import pytest
from fastapi.testclient import TestClient
from api.main import app

LM_HOST = os.getenv('GRID_LMSTUDIO_HOST', '192.168.3.2')
LM_PORT = int(os.getenv('GRID_LMSTUDIO_PORT', '1234'))
RUN_IT = os.getenv('RUN_LMSTUDIO_TESTS', '0') == '1'

pytestmark = pytest.mark.integration


def _is_reachable(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def test_chat_completions_real_lmstudio():
    if not RUN_IT or not _is_reachable(LM_HOST, LM_PORT):
        pytest.skip('LM Studio недоступен или не включены интеграционные тесты (RUN_LMSTUDIO_TESTS=1)')

    with TestClient(app) as client:
        payload = {
            'model': 'qwen3-4b-instruct-2507',
            'messages': [
                {'role': 'user', 'content': 'Скажи два плюс два'}
            ]
        }
        r = client.post('/v1/chat/completions', json=payload)
        assert r.status_code == 200
        data = r.json()
        assert 'choices' in data and len(data['choices']) >= 1
        msg = data['choices'][0]['message']
        assert msg['role'] == 'assistant'
        assert isinstance(msg.get('content', ''), str) 
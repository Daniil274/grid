import json
import pytest
from fastapi.testclient import TestClient
from api.main_simple import app
from api.utils.openai_converter import OpenAIConverter


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get('/health')
    assert r.status_code == 200
    data = r.json()
    assert data['status'] == 'healthy'
    assert data['mode'] == 'mock'


def test_models_list(client):
    r = client.get('/v1/models')
    assert r.status_code == 200
    data = r.json()
    assert data['object'] == 'list'
    assert isinstance(data['data'], list)


def test_chat_completions_mock(client):
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
    assert data['choices'][0]['message']['role'] == 'assistant'


def test_converter_accepts_any_model_name():
    assert OpenAIConverter.validate_model_name('qwen3-4b-instruct-2507') is True
    assert OpenAIConverter.validate_model_name('ollama:llama3.1') is True 
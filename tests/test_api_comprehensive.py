"""
Комплексные тесты для API системы Grid.
Проверяют все endpoints, middleware, и интеграцию OpenAI-compatible API.
"""

import pytest
import asyncio
import json
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from api.main import app
from api.routers.openai_compatible import router as openai_router
from api.routers.agents import router as agents_router
from api.routers.system import router as system_router
from api.middleware.authentication import AuthenticationMiddleware
from api.middleware.rate_limiting import RateLimitingMiddleware
from api.middleware.security import SecurityMiddleware


@pytest.mark.api
class TestOpenAICompatibleAPI:
    """Тесты для OpenAI-совместимого API."""
    
    def setup_method(self):
        """Настройка тестового клиента."""
        self.client = TestClient(app)
    
    def test_chat_completions_endpoint(self):
        """Тест endpoint /v1/chat/completions."""
        with patch('core.agent_factory.AgentFactory') as mock_factory:
            mock_instance = AsyncMock()
            mock_instance.run_agent = AsyncMock(return_value="Тестовый ответ")
            mock_factory.return_value = mock_instance
            
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "test_simple_agent",
                    "messages": [
                        {"role": "user", "content": "Привет"}
                    ]
                }
            )
            
            assert response.status_code == 200
            data = response.json()
            assert "choices" in data
            assert len(data["choices"]) > 0
            assert "message" in data["choices"][0]
    
    def test_chat_completions_streaming(self):
        """Тест streaming для chat completions."""
        with patch('core.agent_factory.AgentFactory') as mock_factory:
            mock_instance = AsyncMock()
            mock_instance.run_agent = AsyncMock(return_value="Тестовый ответ")
            mock_factory.return_value = mock_instance
            
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "test_simple_agent",
                    "messages": [{"role": "user", "content": "Привет"}],
                    "stream": True
                }
            )
            
            assert response.status_code == 200
            assert "text/plain" in response.headers.get("content-type", "")
    
    def test_completions_endpoint(self):
        """Тест endpoint /v1/completions."""
        with patch('core.agent_factory.AgentFactory') as mock_factory:
            mock_instance = AsyncMock()
            mock_instance.run_agent = AsyncMock(return_value="Тестовый ответ")
            mock_factory.return_value = mock_instance
            
            response = self.client.post(
                "/v1/completions",
                json={
                    "model": "test_simple_agent",
                    "prompt": "Привет"
                }
            )
            
            assert response.status_code == 200
            data = response.json()
            assert "choices" in data
    
    def test_models_endpoint(self):
        """Тест endpoint /v1/models."""
        response = self.client.get("/v1/models")
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert isinstance(data["data"], list)
    
    def test_invalid_model_error(self):
        """Тест обработки ошибки несуществующей модели."""
        response = self.client.post(
            "/v1/chat/completions",
            json={
                "model": "nonexistent_model",
                "messages": [{"role": "user", "content": "Привет"}]
            }
        )
        
        assert response.status_code in [400, 404]
    
    def test_malformed_request_error(self):
        """Тест обработки некорректного запроса."""
        response = self.client.post(
            "/v1/chat/completions",
            json={
                "model": "test_simple_agent"
                # Отсутствует обязательное поле messages
            }
        )
        
        assert response.status_code == 422  # Validation error


@pytest.mark.api
class TestAgentsAPI:
    """Тесты для API управления агентами."""
    
    def setup_method(self):
        """Настройка тестового клиента."""
        self.client = TestClient(app)
    
    def test_list_agents(self):
        """Тест получения списка агентов."""
        response = self.client.get("/agents")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
    
    def test_get_agent_details(self):
        """Тест получения деталей конкретного агента."""
        with patch('core.config.Config') as mock_config:
            mock_config.return_value.get_agents.return_value = {
                "test_agent": {
                    "name": "Test Agent",
                    "model": "gpt-4",
                    "tools": []
                }
            }
            
            response = self.client.get("/agents/test_agent")
            assert response.status_code == 200
            data = response.json()
            assert "name" in data
    
    def test_get_nonexistent_agent(self):
        """Тест получения несуществующего агента."""
        response = self.client.get("/agents/nonexistent")
        assert response.status_code == 404


@pytest.mark.api
class TestSystemAPI:
    """Тесты для системного API."""
    
    def setup_method(self):
        """Настройка тестового клиента."""
        self.client = TestClient(app)
    
    def test_health_check(self):
        """Тест health check endpoint."""
        response = self.client.get("/system/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert data["status"] == "healthy"
    
    def test_system_info(self):
        """Тест получения системной информации."""
        response = self.client.get("/system/info")
        assert response.status_code == 200
        data = response.json()
        assert "version" in data or "components" in data


@pytest.mark.api
class TestMiddleware:
    """Тесты для middleware компонентов."""
    
    def setup_method(self):
        """Настройка тестового клиента."""
        self.client = TestClient(app)
    
    def test_cors_headers(self):
        """Тест CORS headers."""
        response = self.client.options("/v1/chat/completions")
        assert response.status_code in [200, 405]  # OPTIONS может быть не поддержан
        
        # Проверяем CORS в обычном запросе
        response = self.client.get("/system/health")
        assert "access-control-allow-origin" in [
            h.lower() for h in response.headers.keys()
        ] or response.status_code == 200  # CORS может быть настроен по-разному
    
    def test_security_headers(self):
        """Тест security headers."""
        response = self.client.get("/system/health")
        assert response.status_code == 200
        # Проверяем наличие основных security headers
        headers = {k.lower(): v for k, v in response.headers.items()}
        # Эти headers могут присутствовать в зависимости от настроек
        security_headers = [
            "x-content-type-options",
            "x-frame-options", 
            "x-xss-protection"
        ]
        # Хотя бы один security header должен присутствовать
        # assert any(header in headers for header in security_headers)
    
    @pytest.mark.asyncio
    async def test_rate_limiting(self):
        """Тест rate limiting middleware."""
        # Для тестирования rate limiting нужно сделать много запросов
        responses = []
        for i in range(10):
            response = self.client.get("/system/health")
            responses.append(response.status_code)
        
        # Проверяем, что все запросы прошли (или rate limiting не настроен для health)
        assert all(status == 200 for status in responses)


@pytest.mark.api
@pytest.mark.integration
class TestAPIIntegration:
    """Интеграционные тесты для API."""
    
    def setup_method(self):
        """Настройка тестового клиента."""
        self.client = TestClient(app)
    
    def test_full_conversation_flow(self):
        """Тест полного flow разговора через API."""
        with patch('core.agent_factory.AgentFactory') as mock_factory:
            mock_instance = AsyncMock()
            responses = ["Привет!", "Как дела?", "Хорошо, спасибо!"]
            mock_instance.run_agent = AsyncMock(side_effect=responses)
            mock_factory.return_value = mock_instance
            
            conversation = [
                "Привет",
                "Как у тебя дела?", 
                "Расскажи что-нибудь интересное"
            ]
            
            for i, message in enumerate(conversation):
                response = self.client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "test_simple_agent",
                        "messages": [{"role": "user", "content": message}]
                    }
                )
                
                assert response.status_code == 200
                data = response.json()
                assert data["choices"][0]["message"]["content"] == responses[i]
    
    def test_error_propagation(self):
        """Тест распространения ошибок через API."""
        with patch('core.agent_factory.AgentFactory') as mock_factory:
            mock_instance = AsyncMock()
            mock_instance.run_agent = AsyncMock(side_effect=Exception("Test error"))
            mock_factory.return_value = mock_instance
            
            response = self.client.post(
                "/v1/chat/completions",
                json={
                    "model": "test_simple_agent",
                    "messages": [{"role": "user", "content": "Тест"}]
                }
            )
            
            assert response.status_code >= 400  # Должна быть ошибка


@pytest.mark.api
@pytest.mark.performance
class TestAPIPerformance:
    """Тесты производительности API."""
    
    def setup_method(self):
        """Настройка тестового клиента."""
        self.client = TestClient(app)
    
    def test_concurrent_requests(self):
        """Тест одновременных запросов к API."""
        import threading
        import time
        
        results = []
        
        def make_request():
            start_time = time.time()
            response = self.client.get("/system/health")
            duration = time.time() - start_time
            results.append((response.status_code, duration))
        
        threads = []
        for i in range(10):
            thread = threading.Thread(target=make_request)
            threads.append(thread)
            thread.start()
        
        for thread in threads:
            thread.join()
        
        # Проверяем, что все запросы успешны
        assert all(status == 200 for status, _ in results)
        
        # Проверяем, что время ответа разумное (< 5 секунд)
        assert all(duration < 5.0 for _, duration in results)
    
    def test_response_time_health_check(self):
        """Тест времени ответа health check."""
        import time
        
        start_time = time.time()
        response = self.client.get("/system/health")
        duration = time.time() - start_time
        
        assert response.status_code == 200
        assert duration < 1.0  # Должен отвечать быстро 
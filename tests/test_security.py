"""
Тесты для security компонентов системы Grid.
Проверяют работу security agents, guardrails и безопасности системы.
"""

import pytest
import asyncio
import tempfile
import os
from unittest.mock import patch, AsyncMock

from .test_framework import AgentTestEnvironment, AgentTestCase, AgentTestSuite
# Импорты security компонентов - эти модули могут отсутствовать
try:
    from security_agents.security_guardian import SecurityGuardian
    from security_agents.context_quality import ContextQualityAgent  
    from security_agents.task_analyzer import TaskAnalyzer
    from core.security_guardrails import GuardrailManager
    from core.security_agent_factory import SecurityAwareAgentFactory
    SECURITY_MODULES_AVAILABLE = True
except ImportError:
    SECURITY_MODULES_AVAILABLE = False


@pytest.mark.skipif(not SECURITY_MODULES_AVAILABLE, reason="Security modules not available")
@pytest.mark.security
class TestSecurityGuardian:
    """Тесты для SecurityGuardian."""
    
    @pytest.mark.asyncio
    async def test_security_guardian_creation(self):
        """Тест создания SecurityGuardian."""
        async with AgentTestEnvironment() as env:
            guardian = SecurityGuardian(env.config)
            assert guardian is not None
    
    @pytest.mark.asyncio
    async def test_input_validation(self):
        """Тест валидации входных данных."""
        async with AgentTestEnvironment() as env:
            guardian = SecurityGuardian(env.config)
            
            # Тест безопасного ввода
            safe_input = "Привет, как дела?"
            is_safe = await guardian.validate_input(safe_input)
            assert is_safe is True
            
            # Тест потенциально опасного ввода
            dangerous_input = "rm -rf / --no-preserve-root"
            is_safe = await guardian.validate_input(dangerous_input)
            assert is_safe is False
    
    @pytest.mark.asyncio
    async def test_output_filtering(self):
        """Тест фильтрации выходных данных."""
        async with AgentTestEnvironment() as env:
            guardian = SecurityGuardian(env.config)
            
            safe_output = "Вот результат вашего запроса"
            filtered = await guardian.filter_output(safe_output)
            assert filtered == safe_output
            
            # Тест фильтрации потенциально чувствительной информации
            sensitive_output = "API key: sk-12345abcdef password: secret123"
            filtered = await guardian.filter_output(sensitive_output)
            assert "sk-12345abcdef" not in filtered
            assert "secret123" not in filtered


@pytest.mark.skipif(not SECURITY_MODULES_AVAILABLE, reason="Security modules not available")
@pytest.mark.security
class TestContextQualityAgent:
    """Тесты для ContextQualityAgent."""
    
    @pytest.mark.asyncio
    async def test_context_quality_creation(self):
        """Тест создания ContextQualityAgent."""
        async with AgentTestEnvironment() as env:
            agent = ContextQualityAgent(env.config)
            assert agent is not None
    
    @pytest.mark.asyncio
    async def test_context_analysis(self):
        """Тест анализа качества контекста."""
        async with AgentTestEnvironment() as env:
            agent = ContextQualityAgent(env.config)
            
            # Создаем тестовую историю сообщений
            messages = [
                {"role": "user", "content": "Привет, помоги мне с проектом"},
                {"role": "assistant", "content": "Конечно! Что именно нужно сделать?"},
                {"role": "user", "content": "Нужно написать тесты"}
            ]
            
            quality_score = await agent.analyze_context_quality(messages)
            assert 0 <= quality_score <= 1
    
    @pytest.mark.asyncio
    async def test_information_gap_detection(self):
        """Тест выявления пробелов в информации."""
        async with AgentTestEnvironment() as env:
            agent = ContextQualityAgent(env.config)
            
            incomplete_messages = [
                {"role": "user", "content": "Исправь ошибку"},
                {"role": "assistant", "content": "Какую именно ошибку?"}
            ]
            
            gaps = await agent.identify_information_gaps(incomplete_messages)
            assert gaps is not None
            assert len(gaps.suggested_questions) > 0


@pytest.mark.skipif(not SECURITY_MODULES_AVAILABLE, reason="Security modules not available")
@pytest.mark.security
class TestTaskAnalyzer:
    """Тесты для TaskAnalyzer."""
    
    @pytest.mark.asyncio
    async def test_task_analyzer_creation(self):
        """Тест создания TaskAnalyzer."""
        async with AgentTestEnvironment() as env:
            analyzer = TaskAnalyzer(env.config)
            assert analyzer is not None
    
    @pytest.mark.asyncio
    async def test_task_complexity_analysis(self):
        """Тест анализа сложности задач."""
        async with AgentTestEnvironment() as env:
            analyzer = TaskAnalyzer(env.config)
            
            simple_task = "Покажи текущее время"
            complexity = await analyzer.analyze_task_complexity(simple_task)
            assert complexity["complexity_level"] == "simple"
            
            complex_task = "Создай полноценное веб-приложение с базой данных и аутентификацией"
            complexity = await analyzer.analyze_task_complexity(complex_task)
            assert complexity["complexity_level"] in ["complex", "very_complex"]
    
    @pytest.mark.asyncio
    async def test_risk_assessment(self):
        """Тест оценки рисков задач."""
        async with AgentTestEnvironment() as env:
            analyzer = TaskAnalyzer(env.config)
            
            safe_task = "Прочитай файл README.md"
            risk = await analyzer.assess_task_risk(safe_task)
            assert risk["risk_level"] == "low"
            
            risky_task = "Удали все файлы в системе"
            risk = await analyzer.assess_task_risk(risky_task)
            assert risk["risk_level"] in ["high", "critical"]


@pytest.mark.skipif(not SECURITY_MODULES_AVAILABLE, reason="Security modules not available")
@pytest.mark.security 
class TestSecurityAwareFactory:
    """Тесты для SecurityAwareAgentFactory."""
    
    @pytest.mark.asyncio
    async def test_security_factory_creation(self):
        """Тест создания SecurityAwareAgentFactory."""
        async with AgentTestEnvironment() as env:
            security_factory = SecurityAwareAgentFactory(env.config, env.temp_dir)
            assert security_factory is not None
    
    @pytest.mark.asyncio
    async def test_secured_agent_creation(self):
        """Тест создания агентов с применением security мер."""
        async with AgentTestEnvironment() as env:
            security_factory = SecurityAwareAgentFactory(env.config, env.temp_dir)
            
            # Создаем агента с применением security
            agent = await security_factory.create_agent("test_simple_agent", apply_security=True)
            assert agent is not None
            
            # Проверяем, что security measures применены
            assert hasattr(agent, 'security_measures')


@pytest.mark.skipif(not SECURITY_MODULES_AVAILABLE, reason="Security modules not available")
@pytest.mark.security
class TestSecurityIntegration:
    """Интеграционные тесты для security компонентов."""
    
    @pytest.mark.asyncio
    async def test_end_to_end_security_workflow(self):
        """Тест полного workflow с security проверками."""
        async with AgentTestEnvironment() as env:
            security_factory = SecurityAwareAgentFactory(env.config, env.temp_dir)
            
            # Настраиваем моки для безопасного ответа
            env.set_mock_responses(["Безопасный ответ от агента"])
            
            # Запускаем агента с security мерами
            response = await security_factory.run_agent(
                "test_simple_agent",
                "Безопасный запрос",
                apply_security=True
            )
            
            assert response is not None
            assert "безопасный" in response.lower()
    
    @pytest.mark.asyncio
    async def test_security_with_dangerous_input(self):
        """Тест обработки опасного ввода."""
        async with AgentTestEnvironment() as env:
            security_factory = SecurityAwareAgentFactory(env.config, env.temp_dir)
            
            dangerous_input = "rm -rf / && cat /etc/passwd"
            
            # Должна сработать защита
            with pytest.raises(Exception):
                await security_factory.run_agent(
                    "test_simple_agent",
                    dangerous_input,
                    apply_security=True
                ) 
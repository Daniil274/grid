"""
Health Monitor - мониторинг здоровья системы

Отслеживает состояние всех компонентов и предоставляет информацию о здоровье системы.
"""

import asyncio
import logging
import psutil
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from enum import Enum


logger = logging.getLogger(__name__)


class HealthStatus(Enum):
    """Статус здоровья компонента"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ComponentHealth:
    """Информация о здоровье компонента"""
    name: str
    status: HealthStatus
    last_check: datetime
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    error_count: int = 0


@dataclass
class SystemHealth:
    """Общее здоровье системы"""
    status: HealthStatus
    uptime: timedelta
    components: Dict[str, ComponentHealth]
    system_metrics: Dict[str, Any]
    timestamp: datetime


class HealthMonitor:
    """
    Монитор здоровья системы

    Отслеживает:
    - Telegram соединение
    - LiveTransparencyBroadcaster
    - UnifiedMemory
    - SkillsIntegration
    - AgentFactory
    - Системные ресурсы (CPU, память, диск)
    """

    def __init__(
        self,
        check_interval: int = 60,  # секунды
        enable_system_metrics: bool = True
    ):
        """
        Инициализация Health Monitor

        Args:
            check_interval: Интервал проверки здоровья (секунды)
            enable_system_metrics: Собирать системные метрики (CPU, память)
        """
        self.check_interval = check_interval
        self.enable_system_metrics = enable_system_metrics

        self.start_time = datetime.now()
        self.components: Dict[str, ComponentHealth] = {}
        self._monitoring_task: Optional[asyncio.Task] = None
        self._is_running = False

        # Компоненты для мониторинга (будут установлены через register_component)
        self.telegram_bridge = None
        self.broadcaster = None
        self.memory = None
        self.skills = None
        self.agent_factory = None

        logger.info("HealthMonitor инициализирован")

    def register_component(
        self,
        component_name: str,
        component: Any,
        initial_status: HealthStatus = HealthStatus.UNKNOWN
    ):
        """
        Регистрация компонента для мониторинга

        Args:
            component_name: Имя компонента
            component: Экземпляр компонента
            initial_status: Начальный статус
        """
        # Сохранить ссылку на компонент
        if component_name == "telegram_bridge":
            self.telegram_bridge = component
        elif component_name == "broadcaster":
            self.broadcaster = component
        elif component_name == "memory":
            self.memory = component
        elif component_name == "skills":
            self.skills = component
        elif component_name == "agent_factory":
            self.agent_factory = component

        # Создать запись о здоровье
        self.components[component_name] = ComponentHealth(
            name=component_name,
            status=initial_status,
            last_check=datetime.now(),
            message="Компонент зарегистрирован"
        )

        logger.info(f"Компонент '{component_name}' зарегистрирован для мониторинга")

    async def start(self):
        """Запуск мониторинга"""
        if self._is_running:
            logger.warning("HealthMonitor уже запущен")
            return

        self._is_running = True
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        logger.info("HealthMonitor запущен")

    async def stop(self):
        """Остановка мониторинга"""
        self._is_running = False

        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass

        logger.info("HealthMonitor остановлен")

    async def _monitoring_loop(self):
        """Основной цикл мониторинга"""
        try:
            while self._is_running:
                await self._perform_health_checks()
                await asyncio.sleep(self.check_interval)

        except asyncio.CancelledError:
            logger.info("Monitoring loop cancelled")
        except Exception as e:
            logger.error(f"Ошибка в monitoring loop: {e}")

    async def _perform_health_checks(self):
        """Выполнить проверки здоровья всех компонентов"""
        try:
            # Проверить каждый зарегистрированный компонент
            for component_name in list(self.components.keys()):
                await self._check_component_health(component_name)

        except Exception as e:
            logger.error(f"Ошибка при проверке здоровья: {e}")

    async def _check_component_health(self, component_name: str):
        """
        Проверить здоровье конкретного компонента

        Args:
            component_name: Имя компонента
        """
        try:
            component_health = self.components.get(component_name)
            if not component_health:
                return

            # Проверить в зависимости от типа компонента
            if component_name == "telegram_bridge":
                await self._check_telegram_bridge()
            elif component_name == "broadcaster":
                await self._check_broadcaster()
            elif component_name == "memory":
                await self._check_memory()
            elif component_name == "skills":
                await self._check_skills()
            elif component_name == "agent_factory":
                await self._check_agent_factory()

        except Exception as e:
            logger.error(f"Ошибка при проверке '{component_name}': {e}")
            self._update_component_health(
                component_name,
                HealthStatus.UNHEALTHY,
                f"Ошибка проверки: {str(e)}",
                error_count_increment=1
            )

    async def _check_telegram_bridge(self):
        """Проверить TelegramBridge"""
        if not self.telegram_bridge:
            self._update_component_health(
                "telegram_bridge",
                HealthStatus.UNKNOWN,
                "Компонент не зарегистрирован"
            )
            return

        try:
            # Проверить что app существует и инициализирован
            if not hasattr(self.telegram_bridge, 'app') or not self.telegram_bridge.app:
                self._update_component_health(
                    "telegram_bridge",
                    HealthStatus.UNHEALTHY,
                    "Telegram Application не инициализирован"
                )
                return

            # Проверить активные задачи
            active_tasks = len(self.telegram_bridge.active_tasks)

            self._update_component_health(
                "telegram_bridge",
                HealthStatus.HEALTHY,
                f"Работает нормально. Активных задач: {active_tasks}",
                details={"active_tasks": active_tasks}
            )

        except Exception as e:
            self._update_component_health(
                "telegram_bridge",
                HealthStatus.UNHEALTHY,
                f"Ошибка: {str(e)}",
                error_count_increment=1
            )

    async def _check_broadcaster(self):
        """Проверить LiveTransparencyBroadcaster"""
        if not self.broadcaster:
            self._update_component_health(
                "broadcaster",
                HealthStatus.UNKNOWN,
                "Компонент не зарегистрирован"
            )
            return

        try:
            # Проверить размер очереди событий
            queue_size = self.broadcaster.progress_queue.qsize()

            # Предупреждение если очередь слишком большая
            if queue_size > 100:
                status = HealthStatus.DEGRADED
                message = f"Большая очередь событий: {queue_size}"
            else:
                status = HealthStatus.HEALTHY
                message = f"Работает нормально. Очередь: {queue_size}"

            self._update_component_health(
                "broadcaster",
                status,
                message,
                details={"queue_size": queue_size}
            )

        except Exception as e:
            self._update_component_health(
                "broadcaster",
                HealthStatus.UNHEALTHY,
                f"Ошибка: {str(e)}",
                error_count_increment=1
            )

    async def _check_memory(self):
        """Проверить UnifiedMemory"""
        if not self.memory:
            self._update_component_health(
                "memory",
                HealthStatus.UNKNOWN,
                "Компонент не зарегистрирован"
            )
            return

        try:
            # Проверить context_manager
            if not hasattr(self.memory, 'context_manager'):
                self._update_component_health(
                    "memory",
                    HealthStatus.UNHEALTHY,
                    "ContextManager отсутствует"
                )
                return

            # Получить количество сообщений в истории
            history_size = len(self.memory.context_manager.conversation_history)

            self._update_component_health(
                "memory",
                HealthStatus.HEALTHY,
                f"Работает нормально. Сообщений в истории: {history_size}",
                details={"history_size": history_size}
            )

        except Exception as e:
            self._update_component_health(
                "memory",
                HealthStatus.UNHEALTHY,
                f"Ошибка: {str(e)}",
                error_count_increment=1
            )

    async def _check_skills(self):
        """Проверить SkillsIntegration"""
        if not self.skills:
            self._update_component_health(
                "skills",
                HealthStatus.UNKNOWN,
                "Компонент не зарегистрирован"
            )
            return

        try:
            # Получить количество зарегистрированных навыков
            registered_skills = len(self.skills._registered_skills)

            self._update_component_health(
                "skills",
                HealthStatus.HEALTHY,
                f"Работает нормально. Навыков зарегистрировано: {registered_skills}",
                details={"registered_skills": registered_skills}
            )

        except Exception as e:
            self._update_component_health(
                "skills",
                HealthStatus.UNHEALTHY,
                f"Ошибка: {str(e)}",
                error_count_increment=1
            )

    async def _check_agent_factory(self):
        """Проверить AgentFactory"""
        if not self.agent_factory:
            self._update_component_health(
                "agent_factory",
                HealthStatus.UNKNOWN,
                "Компонент не зарегистрирован"
            )
            return

        try:
            # Проверить кэш агентов
            cached_agents = len(self.agent_factory._agent_cache)

            self._update_component_health(
                "agent_factory",
                HealthStatus.HEALTHY,
                f"Работает нормально. Агентов в кэше: {cached_agents}",
                details={"cached_agents": cached_agents}
            )

        except Exception as e:
            self._update_component_health(
                "agent_factory",
                HealthStatus.UNHEALTHY,
                f"Ошибка: {str(e)}",
                error_count_increment=1
            )

    def _update_component_health(
        self,
        component_name: str,
        status: HealthStatus,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        error_count_increment: int = 0
    ):
        """
        Обновить информацию о здоровье компонента

        Args:
            component_name: Имя компонента
            status: Новый статус
            message: Сообщение о статусе
            details: Дополнительные детали
            error_count_increment: Увеличить счетчик ошибок
        """
        if component_name not in self.components:
            self.components[component_name] = ComponentHealth(
                name=component_name,
                status=status,
                last_check=datetime.now(),
                message=message,
                details=details or {}
            )
        else:
            health = self.components[component_name]
            health.status = status
            health.last_check = datetime.now()
            health.message = message
            health.details = details or {}
            health.error_count += error_count_increment

    def get_system_health(self) -> SystemHealth:
        """
        Получить общее здоровье системы

        Returns:
            SystemHealth с информацией о всех компонентах
        """
        # Определить общий статус
        overall_status = HealthStatus.HEALTHY

        for component_health in self.components.values():
            if component_health.status == HealthStatus.UNHEALTHY:
                overall_status = HealthStatus.UNHEALTHY
                break
            elif component_health.status == HealthStatus.DEGRADED:
                overall_status = HealthStatus.DEGRADED

        # Системные метрики
        system_metrics = {}
        if self.enable_system_metrics:
            try:
                system_metrics = {
                    "cpu_percent": psutil.cpu_percent(interval=0.1),
                    "memory_percent": psutil.virtual_memory().percent,
                    "disk_percent": psutil.disk_usage('/').percent,
                }
            except Exception as e:
                logger.warning(f"Не удалось получить системные метрики: {e}")

        return SystemHealth(
            status=overall_status,
            uptime=datetime.now() - self.start_time,
            components=self.components.copy(),
            system_metrics=system_metrics,
            timestamp=datetime.now()
        )

    def get_health_report(self) -> str:
        """
        Получить текстовый отчет о здоровье системы

        Returns:
            Форматированный текстовый отчет
        """
        health = self.get_system_health()

        # Эмодзи для статусов
        status_emoji = {
            HealthStatus.HEALTHY: "✅",
            HealthStatus.DEGRADED: "⚠️",
            HealthStatus.UNHEALTHY: "❌",
            HealthStatus.UNKNOWN: "❓",
        }

        report_lines = [
            f"🏥 <b>Health Monitor Report</b>",
            f"",
            f"<b>Общий статус:</b> {status_emoji.get(health.status, '❓')} {health.status.value.upper()}",
            f"<b>Uptime:</b> {health.uptime}",
            f"<b>Timestamp:</b> {health.timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
            f"",
            f"<b>Компоненты:</b>",
        ]

        for component_name, component_health in health.components.items():
            emoji = status_emoji.get(component_health.status, '❓')
            report_lines.append(
                f"{emoji} <b>{component_name}</b>: {component_health.message}"
            )

        # Системные метрики
        if health.system_metrics:
            report_lines.append("")
            report_lines.append("<b>Системные метрики:</b>")
            for metric_name, metric_value in health.system_metrics.items():
                report_lines.append(f"  • {metric_name}: {metric_value}")

        return "\n".join(report_lines)

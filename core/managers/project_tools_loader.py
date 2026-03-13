"""
Project Tools Loader - динамическая загрузка инструментов из проектных директорий.

Позволяет проектам (например ISKOR-autotest) иметь собственные инструменты
без изменения корневой директории системы.
"""

import os
import sys
import importlib
import importlib.util
import inspect
import logging
from typing import Dict, List, Any, Optional
from pathlib import Path

logger = logging.getLogger("grid.project_tools_loader")


class ProjectToolsLoader:
    """
    Загрузчик инструментов из проектных директорий.

    Поддерживает динамический импорт модулей и извлечение функций
    с декоратором @function_tool.
    """

    def __init__(self, config_dir: str, tools_directory: str):
        """
        Initialize loader.

        Args:
            config_dir: Путь к директории с config.yaml проекта
            tools_directory: Относительный путь к директории с инструментами
        """
        self.config_dir = Path(config_dir).resolve()
        self.tools_dir = (self.config_dir / tools_directory).resolve()
        self._loaded_tools: Dict[str, Any] = {}
        self._module_cache: Dict[str, Any] = {}

        logger.info(f"ProjectToolsLoader initialized: config_dir={self.config_dir}, tools_dir={self.tools_dir}")

    def load_project_tools(self) -> Dict[str, Any]:
        """
        Загружает все инструменты из директории проекта.

        Returns:
            Dict[str, Any]: Словарь {имя_инструмента: функция}
        """
        if not self.tools_dir.exists():
            logger.warning(f"Tools directory does not exist: {self.tools_dir}")
            return {}

        if not self.tools_dir.is_dir():
            logger.error(f"Tools path is not a directory: {self.tools_dir}")
            return {}

        logger.info(f"Loading project tools from: {self.tools_dir}")

        # Добавляем config_dir и саму директорию инструментов в sys.path для импортов
        parent_dir = str(self.config_dir)
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
            logger.debug(f"Added to sys.path: {parent_dir}")
        tools_dir_str = str(self.tools_dir)
        if tools_dir_str not in sys.path:
            sys.path.insert(0, tools_dir_str)
            logger.debug(f"Added to sys.path: {tools_dir_str}")

        # Сканируем .py файлы
        for file_path in self.tools_dir.glob("*.py"):
            if file_path.name.startswith("_"):
                continue  # Пропускаем __init__.py и приватные модули

            module_name = file_path.stem
            self._load_module(module_name, file_path)

        logger.info(f"Loaded {len(self._loaded_tools)} project tools: {list(self._loaded_tools.keys())}")
        return self._loaded_tools

    def _load_module(self, module_name: str, file_path: Path) -> None:
        """
        Загружает модуль и извлекает инструменты.

        Args:
            module_name: Имя модуля
            file_path: Путь к файлу модуля
        """
        try:
            # Создаем полное имя модуля для импорта
            # Используем относительный путь от config_dir
            relative_path = file_path.relative_to(self.config_dir)
            parts = list(relative_path.parts[:-1]) + [relative_path.stem]
            full_module_name = ".".join(parts)

            logger.debug(f"Loading module: {full_module_name} from {file_path}")

            # Импортируем модуль
            spec = importlib.util.spec_from_file_location(full_module_name, file_path)
            if spec is None or spec.loader is None:
                logger.error(f"Failed to create spec for {full_module_name}")
                return

            module = importlib.util.module_from_spec(spec)
            sys.modules[full_module_name] = module
            spec.loader.exec_module(module)

            self._module_cache[module_name] = module

            # Извлекаем функции-инструменты
            tools_found = 0
            for name, obj in inspect.getmembers(module):
                if name.startswith("_"):
                    continue

                # Проверяем типы, которые являются инструментами
                is_tool = False

                # 1. FunctionTool from agents SDK
                if type(obj).__name__ == 'FunctionTool':
                    is_tool = True

                # 2. Обычные callable функции с атрибутами agents SDK
                elif callable(obj) and (hasattr(obj, '__wrapped__') or hasattr(obj, 'metadata')):
                    is_tool = True

                # 3. Функции из этого модуля (для fallback декоратора)
                elif callable(obj) and getattr(obj, '__module__', None) == full_module_name:
                    is_tool = True

                if is_tool:
                    self._loaded_tools[name] = obj
                    tools_found += 1
                    logger.debug(f"  Found tool: {name}")

            logger.info(f"Module {module_name}: loaded {tools_found} tools")

        except Exception as exc:
            logger.error(f"Error loading module {module_name}: {exc}", exc_info=True)

    def get_tool(self, tool_name: str) -> Optional[Any]:
        """
        Получает инструмент по имени.

        Args:
            tool_name: Имя инструмента

        Returns:
            Функция инструмента или None
        """
        return self._loaded_tools.get(tool_name)

    def get_all_tools(self) -> Dict[str, Any]:
        """
        Возвращает все загруженные инструменты.

        Returns:
            Dict[str, Any]: Словарь инструментов
        """
        return self._loaded_tools.copy()

    def has_tool(self, tool_name: str) -> bool:
        """
        Проверяет наличие инструмента.

        Args:
            tool_name: Имя инструмента

        Returns:
            bool: True если инструмент загружен
        """
        return tool_name in self._loaded_tools


# ============================================================================
# Global instance management
# ============================================================================

_global_loader: Optional[ProjectToolsLoader] = None


def initialize_project_tools(config_dir: str, tools_directory: str) -> ProjectToolsLoader:
    """
    Инициализирует глобальный загрузчик проектных инструментов.

    Args:
        config_dir: Путь к директории с config.yaml
        tools_directory: Относительный путь к директории с инструментами

    Returns:
        ProjectToolsLoader: Экземпляр загрузчика
    """
    global _global_loader

    _global_loader = ProjectToolsLoader(config_dir, tools_directory)
    _global_loader.load_project_tools()

    return _global_loader


def get_project_loader() -> Optional[ProjectToolsLoader]:
    """
    Возвращает глобальный загрузчик проектных инструментов.

    Returns:
        Optional[ProjectToolsLoader]: Загрузчик или None если не инициализирован
    """
    return _global_loader


def clear_project_tools() -> None:
    """Очищает глобальный загрузчик."""
    global _global_loader
    _global_loader = None

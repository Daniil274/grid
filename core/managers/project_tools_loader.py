"""
Project Tools Loader - dynamic loading of tools from project directories.

Allows projects (e.g., ISKOR-autotest) to have their own tools
without modifying the system root directory.
"""

import os
import sys
import importlib
import importlib.util
import inspect
import logging
import hashlib
from typing import Dict, List, Any, Optional
from pathlib import Path

logger = logging.getLogger("grid.project_tools_loader")


class ProjectToolsLoader:
    """
    Loader for tools from project directories.

    Supports dynamic import of modules and extraction of functions
    decorated with @function_tool.
    """

    def __init__(self, config_dir: str, tools_directory: str):
        """
        Initialize loader.

        Args:
            config_dir: Path to the project's config.yaml directory
            tools_directory: Relative path to the tools directory
        """
        self.config_dir = Path(config_dir).resolve()
        self.tools_dir = (self.config_dir / tools_directory).resolve()
        self._loaded_tools: Dict[str, Any] = {}
        self._module_cache: Dict[str, Any] = {}

        logger.info(f"ProjectToolsLoader initialized: config_dir={self.config_dir}, tools_dir={self.tools_dir}")

    def load_project_tools(self) -> Dict[str, Any]:
        """
        Loads all tools from the project directory.

        Returns:
            Dict[str, Any]: Dictionary {tool_name: function}
        """
        if not self.tools_dir.exists():
            logger.warning(f"Tools directory does not exist: {self.tools_dir}")
            return {}

        if not self.tools_dir.is_dir():
            logger.error(f"Tools path is not a directory: {self.tools_dir}")
            return {}

        logger.info(f"Loading project tools from: {self.tools_dir}")

        # Add config_dir and the tools directory itself to sys.path for imports.
        # Keep them behind installed packages so tool files like click.py do not
        # shadow dependencies imported while project tools are loading.
        parent_dir = str(self.config_dir)
        if parent_dir not in sys.path:
            sys.path.append(parent_dir)
            logger.debug(f"Added to sys.path: {parent_dir}")
        tools_dir_str = str(self.tools_dir)
        if tools_dir_str not in sys.path:
            sys.path.append(tools_dir_str)
            logger.debug(f"Added to sys.path: {tools_dir_str}")

        # Scan .py files
        for file_path in self.tools_dir.glob("*.py"):
            if file_path.name.startswith("_"):
                continue  # Skip __init__.py and private modules

            module_name = file_path.stem
            self._load_module(module_name, file_path)

        logger.info(f"Loaded {len(self._loaded_tools)} project tools: {list(self._loaded_tools.keys())}")
        return self._loaded_tools

    def _load_module(self, module_name: str, file_path: Path) -> None:
        """
        Loads a module and extracts tools.

        Args:
            module_name: Module name
            file_path: Path to module file
        """
        try:
            # Create the full module name for import.
            # Project tools may live outside config_dir, for example shared
            # tools used by multiple example configs.
            try:
                relative_path = file_path.relative_to(self.config_dir)
                parts = list(relative_path.parts[:-1]) + [relative_path.stem]
                full_module_name = ".".join(parts)
            except ValueError:
                digest = hashlib.sha1(str(file_path.resolve()).encode("utf-8")).hexdigest()[:12]
                full_module_name = f"_project_tools_{digest}_{file_path.stem}"

            logger.debug(f"Loading module: {full_module_name} from {file_path}")

            # Import the module (reuse from sys.modules to avoid executing
            # module-level code twice — this breaks stderr/stdout wrappers on Windows).
            # BUT: if sys.modules already has another module with the same name
            # (e.g., Grid's own tools/file_tools.py vs example's tools/file_tools.py),
            # use a unique name to avoid collision.
            cached = sys.modules.get(full_module_name)
            if cached is not None:
                cached_file = getattr(cached, '__file__', None)
                if cached_file and Path(cached_file).resolve() == file_path.resolve():
                    module = cached  # Same file — safe to reuse
                else:
                    # Collision with another module — use a unique name
                    digest = hashlib.sha1(str(file_path.resolve()).encode("utf-8")).hexdigest()[:12]
                    full_module_name = f"_project_tools_{digest}_{file_path.stem}"
                    cached = sys.modules.get(full_module_name)
                    if cached is not None:
                        module = cached
                    else:
                        spec = importlib.util.spec_from_file_location(full_module_name, file_path)
                        if spec is None or spec.loader is None:
                            logger.error(f"Failed to create spec for {full_module_name}")
                            return
                        module = importlib.util.module_from_spec(spec)
                        sys.modules[full_module_name] = module
                        spec.loader.exec_module(module)
            else:
                spec = importlib.util.spec_from_file_location(full_module_name, file_path)
                if spec is None or spec.loader is None:
                    logger.error(f"Failed to create spec for {full_module_name}")
                    return

                module = importlib.util.module_from_spec(spec)
                sys.modules[full_module_name] = module
                spec.loader.exec_module(module)

            self._module_cache[module_name] = module

            # Extract tool functions
            tools_found = 0
            for name, obj in inspect.getmembers(module):
                if name.startswith("_"):
                    continue

                # Check if the type is a tool
                is_tool = False

                # 1. FunctionTool from agents SDK
                if type(obj).__name__ == 'FunctionTool':
                    is_tool = True

                # 2. Regular callable functions with agents SDK attributes
                elif callable(obj) and (hasattr(obj, '__wrapped__') or hasattr(obj, 'metadata')):
                    is_tool = True

                # 3. Functions from this module (for fallback decorator)
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
        Get a tool by name.

        Args:
            tool_name: Tool name

        Returns:
            Tool function or None
        """
        return self._loaded_tools.get(tool_name)

    def get_all_tools(self) -> Dict[str, Any]:
        """
        Returns all loaded tools.

        Returns:
            Dict[str, Any]: Dictionary of tools
        """
        return self._loaded_tools.copy()

    def has_tool(self, tool_name: str) -> bool:
        """
        Checks if a tool is available.

        Args:
            tool_name: Tool name

        Returns:
            bool: True if tool is loaded
        """
        return tool_name in self._loaded_tools


# ============================================================================
# Global instance management
# ============================================================================

_global_loader: Optional[ProjectToolsLoader] = None


def initialize_project_tools(config_dir: str, tools_directory: str) -> ProjectToolsLoader:
    """
    Initialize the global project tools loader.

    Args:
        config_dir: Path to directory with config.yaml
        tools_directory: Relative path to tools directory

    Returns:
        ProjectToolsLoader: Loader instance
    """
    global _global_loader

    _global_loader = ProjectToolsLoader(config_dir, tools_directory)
    _global_loader.load_project_tools()

    return _global_loader


def get_project_loader() -> Optional[ProjectToolsLoader]:
    """
    Returns the global project tools loader.

    Returns:
        Optional[ProjectToolsLoader]: Loader or None if not initialized
    """
    return _global_loader


def clear_project_tools() -> None:
    """Clears the global loader."""
    global _global_loader
    _global_loader = None

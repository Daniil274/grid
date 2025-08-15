"""
Tool Registry for simplified tool management and agent integration.
Provides centralized tool creation, caching, and orchestration capabilities.
"""

from typing import Dict, List, Any, Optional, Callable, Union, Tuple
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import inspect

from agents import function_tool, Agent, RunContextWrapper


class ToolType(Enum):
    """Types of tools available in the system."""
    FUNCTION = "function"
    AGENT = "agent"
    MCP = "mcp"
    COMPOSITE = "composite"


@dataclass
class ToolMetadata:
    """Metadata for a registered tool."""
    
    name: str
    tool_type: ToolType
    description: str
    category: str = "general"
    tags: List[str] = None
    parameters: Dict[str, Any] = None
    dependencies: List[str] = None
    
    def __post_init__(self):
        if self.tags is None:
            self.tags = []
        if self.parameters is None:
            self.parameters = {}
        if self.dependencies is None:
            self.dependencies = []


class ToolWrapper(ABC):
    """Abstract wrapper for different tool types."""
    
    def __init__(self, metadata: ToolMetadata):
        self.metadata = metadata
    
    @abstractmethod
    async def invoke(self, context: RunContextWrapper, **kwargs) -> Any:
        """Invoke the tool with given parameters."""
        pass
    
    @abstractmethod
    def get_function_tool(self) -> Any:
        """Get the agents SDK function tool representation."""
        pass


class FunctionToolWrapper(ToolWrapper):
    """Wrapper for function-based tools."""
    
    def __init__(self, metadata: ToolMetadata, func: Callable):
        super().__init__(metadata)
        self.func = func
        self._function_tool = None
    
    async def invoke(self, context: RunContextWrapper, **kwargs) -> Any:
        """Invoke the underlying function."""
        if inspect.iscoroutinefunction(self.func):
            return await self.func(**kwargs)
        else:
            return self.func(**kwargs)
    
    def get_function_tool(self) -> Any:
        """Get the function tool, creating it if necessary."""
        if self._function_tool is None:
            @function_tool(
                name_override=self.metadata.name,
                description_override=self.metadata.description
            )
            async def wrapped_function(context: RunContextWrapper, **kwargs):
                return await self.invoke(context, **kwargs)
            
            self._function_tool = wrapped_function
        
        return self._function_tool


class AgentToolWrapper(ToolWrapper):
    """Wrapper for agent-based tools."""
    
    def __init__(self, metadata: ToolMetadata, agent: Agent, agent_factory=None):
        super().__init__(metadata)
        self.agent = agent
        self.agent_factory = agent_factory
        self._function_tool = None
    
    async def invoke(self, context: RunContextWrapper, **kwargs) -> Any:
        """Invoke the agent tool."""
        from agents import Runner
        
        # Extract input from kwargs
        input_text = kwargs.get("input", kwargs.get("message", kwargs.get("task", "")))
        if not input_text:
            return "No input provided for agent tool"
        
        # Get session from agent if available
        session = getattr(self.agent, '_session', None)
        
        # Run the agent
        result = await Runner.run(
            starting_agent=self.agent,
            input=input_text,
            context=context.context,
            session=session
        )
        
        return result
    
    def get_function_tool(self) -> Any:
        """Get the function tool representation."""
        if self._function_tool is None:
            @function_tool(
                name_override=self.metadata.name,
                description_override=self.metadata.description
            )
            async def agent_function_tool(context: RunContextWrapper, input: str) -> str:
                return await self.invoke(context, input=input)
            
            self._function_tool = agent_function_tool
        
        return self._function_tool


class CompositeToolWrapper(ToolWrapper):
    """Wrapper for composite tools that combine multiple tools."""
    
    def __init__(self, metadata: ToolMetadata, tool_chain: List[str], registry=None):
        super().__init__(metadata)
        self.tool_chain = tool_chain
        self.registry = registry
        self._function_tool = None
    
    async def invoke(self, context: RunContextWrapper, **kwargs) -> Any:
        """Invoke the tool chain sequentially."""
        if not self.registry:
            return "Registry not available for composite tool"
        
        current_input = kwargs.get("input", "")
        results = []
        
        for tool_name in self.tool_chain:
            tool_wrapper = self.registry.get_tool(tool_name)
            if not tool_wrapper:
                results.append(f"Tool '{tool_name}' not found")
                continue
            
            try:
                result = await tool_wrapper.invoke(context, input=current_input)
                results.append(result)
                current_input = str(result)  # Use result as input for next tool
            except Exception as e:
                results.append(f"Error in '{tool_name}': {str(e)}")
                break
        
        return "\n".join(f"Step {i+1} ({self.tool_chain[i]}): {result}" 
                        for i, result in enumerate(results))
    
    def get_function_tool(self) -> Any:
        """Get the function tool representation."""
        if self._function_tool is None:
            @function_tool(
                name_override=self.metadata.name,
                description_override=self.metadata.description
            )
            async def composite_function_tool(context: RunContextWrapper, input: str) -> str:
                return await self.invoke(context, input=input)
            
            self._function_tool = composite_function_tool
        
        return self._function_tool


class ToolRegistry:
    """Central registry for managing all types of tools."""
    
    def __init__(self):
        self._tools: Dict[str, ToolWrapper] = {}
        self._categories: Dict[str, List[str]] = {}
        self._tool_cache: Dict[str, Any] = {}
    
    def register_function_tool(
        self, 
        name: str, 
        func: Callable, 
        description: str = "",
        category: str = "general",
        tags: List[str] = None
    ):
        """Register a function-based tool."""
        metadata = ToolMetadata(
            name=name,
            tool_type=ToolType.FUNCTION,
            description=description or f"Function tool: {name}",
            category=category,
            tags=tags or []
        )
        
        wrapper = FunctionToolWrapper(metadata, func)
        self._tools[name] = wrapper
        
        # Update category index
        if category not in self._categories:
            self._categories[category] = []
        self._categories[category].append(name)
    
    def register_agent_tool(
        self, 
        name: str, 
        agent: Agent, 
        description: str = "",
        category: str = "agents",
        tags: List[str] = None,
        agent_factory=None
    ):
        """Register an agent-based tool."""
        metadata = ToolMetadata(
            name=name,
            tool_type=ToolType.AGENT,
            description=description or f"Agent tool: {agent.name}",
            category=category,
            tags=tags or []
        )
        
        wrapper = AgentToolWrapper(metadata, agent, agent_factory)
        self._tools[name] = wrapper
        
        # Update category index
        if category not in self._categories:
            self._categories[category] = []
        self._categories[category].append(name)
    
    def register_composite_tool(
        self,
        name: str,
        tool_chain: List[str],
        description: str = "",
        category: str = "composite",
        tags: List[str] = None
    ):
        """Register a composite tool that chains multiple tools."""
        metadata = ToolMetadata(
            name=name,
            tool_type=ToolType.COMPOSITE,
            description=description or f"Composite tool: {' -> '.join(tool_chain)}",
            category=category,
            tags=tags or [],
            dependencies=tool_chain
        )
        
        wrapper = CompositeToolWrapper(metadata, tool_chain, self)
        self._tools[name] = wrapper
        
        # Update category index
        if category not in self._categories:
            self._categories[category] = []
        self._categories[category].append(name)
    
    def get_tool(self, name: str) -> Optional[ToolWrapper]:
        """Get a tool wrapper by name."""
        return self._tools.get(name)
    
    def get_tool_function(self, name: str) -> Optional[Any]:
        """Get the function tool representation for agents SDK."""
        if name in self._tool_cache:
            return self._tool_cache[name]
        
        wrapper = self.get_tool(name)
        if wrapper:
            function_tool = wrapper.get_function_tool()
            self._tool_cache[name] = function_tool
            return function_tool
        
        return None
    
    def get_tools_by_names(self, names: List[str]) -> List[Any]:
        """Get multiple function tools by names."""
        tools = []
        for name in names:
            tool = self.get_tool_function(name)
            if tool:
                tools.append(tool)
        return tools
    
    def get_tools_by_category(self, category: str) -> List[str]:
        """Get all tool names in a category."""
        return self._categories.get(category, [])
    
    def get_tools_by_tags(self, tags: List[str]) -> List[str]:
        """Get tool names that have any of the specified tags."""
        matching_tools = []
        for name, wrapper in self._tools.items():
            if any(tag in wrapper.metadata.tags for tag in tags):
                matching_tools.append(name)
        return matching_tools
    
    def list_tools(self) -> Dict[str, ToolMetadata]:
        """List all registered tools with metadata."""
        return {name: wrapper.metadata for name, wrapper in self._tools.items()}
    
    def get_tool_dependencies(self, name: str) -> List[str]:
        """Get dependencies for a tool."""
        wrapper = self.get_tool(name)
        if wrapper:
            return wrapper.metadata.dependencies
        return []
    
    def validate_tool_chain(self, tool_names: List[str]) -> Tuple[bool, List[str]]:
        """Validate that a chain of tools can be executed."""
        missing_tools = []
        for name in tool_names:
            if name not in self._tools:
                missing_tools.append(name)
        
        return len(missing_tools) == 0, missing_tools
    
    def create_agent_with_tools(
        self, 
        agent: Agent, 
        tool_names: List[str],
        include_categories: List[str] = None
    ) -> List[Any]:
        """Create a list of function tools for an agent."""
        tools = []
        
        # Add explicitly named tools
        tools.extend(self.get_tools_by_names(tool_names))
        
        # Add tools from categories
        if include_categories:
            for category in include_categories:
                category_tool_names = self.get_tools_by_category(category)
                category_tools = self.get_tools_by_names(category_tool_names)
                tools.extend(category_tools)
        
        return tools
    
    def get_tool_statistics(self) -> Dict[str, Any]:
        """Get statistics about registered tools."""
        tool_counts_by_type = {}
        tool_counts_by_category = {}
        
        for wrapper in self._tools.values():
            tool_type = wrapper.metadata.tool_type.value
            category = wrapper.metadata.category
            
            tool_counts_by_type[tool_type] = tool_counts_by_type.get(tool_type, 0) + 1
            tool_counts_by_category[category] = tool_counts_by_category.get(category, 0) + 1
        
        return {
            "total_tools": len(self._tools),
            "by_type": tool_counts_by_type,
            "by_category": tool_counts_by_category,
            "categories": list(self._categories.keys()),
            "cache_size": len(self._tool_cache)
        }
    
    def clear_cache(self):
        """Clear the tool function cache."""
        self._tool_cache.clear()


class ToolBuilder:
    """Builder class for creating tools with fluent interface."""
    
    def __init__(self, registry: ToolRegistry):
        self.registry = registry
        self.reset()
    
    def reset(self):
        """Reset builder state."""
        self._name = ""
        self._description = ""
        self._category = "general"
        self._tags = []
        self._func = None
        self._agent = None
        self._tool_chain = []
        return self
    
    def name(self, name: str):
        """Set tool name."""
        self._name = name
        return self
    
    def description(self, description: str):
        """Set tool description."""
        self._description = description
        return self
    
    def category(self, category: str):
        """Set tool category."""
        self._category = category
        return self
    
    def tags(self, *tags: str):
        """Set tool tags."""
        self._tags = list(tags)
        return self
    
    def function(self, func: Callable):
        """Set function for function tool."""
        self._func = func
        return self
    
    def agent(self, agent: Agent):
        """Set agent for agent tool."""
        self._agent = agent
        return self
    
    def chain(self, *tool_names: str):
        """Set tool chain for composite tool."""
        self._tool_chain = list(tool_names)
        return self
    
    def build(self):
        """Build and register the tool."""
        if not self._name:
            raise ValueError("Tool name is required")
        
        if self._func:
            self.registry.register_function_tool(
                self._name, self._func, self._description, self._category, self._tags
            )
        elif self._agent:
            self.registry.register_agent_tool(
                self._name, self._agent, self._description, self._category, self._tags
            )
        elif self._tool_chain:
            self.registry.register_composite_tool(
                self._name, self._tool_chain, self._description, self._category, self._tags
            )
        else:
            raise ValueError("Tool must have either function, agent, or tool chain")
        
        tool_name = self._name
        self.reset()
        return self.registry.get_tool(tool_name)
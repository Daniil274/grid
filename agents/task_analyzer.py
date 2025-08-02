"""
Task Analysis Agent - Task feasibility and decomposition analysis.
Analyzes task complexity, resource requirements, and execution feasibility.
"""

from typing import List, Dict, Any, Optional, Tuple
from pydantic import BaseModel, Field
from datetime import datetime, timedelta
from enum import Enum

from agents import Agent, OpenAIChatCompletionsModel, function_tool, RunContextWrapper
from core.security_context import SecurityAnalysisContext
from tools.security_tools import FeasibilityAnalysisResult


class TaskComplexity(Enum):
    """Task complexity levels."""
    TRIVIAL = "trivial"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"
    EXTREME = "extreme"


class TaskCategory(Enum):
    """Task categories for classification."""
    FILE_OPERATION = "file_operation"
    CODE_DEVELOPMENT = "code_development"
    SYSTEM_ADMINISTRATION = "system_administration"
    DATA_PROCESSING = "data_processing"
    NETWORK_OPERATION = "network_operation"
    GIT_OPERATION = "git_operation"
    RESEARCH = "research"
    ANALYSIS = "analysis"
    INTEGRATION = "integration"
    DEPLOYMENT = "deployment"
    UNKNOWN = "unknown"


class ResourceRequirement(BaseModel):
    """Resource requirement specification."""
    resource_type: str = Field(description="Type of resource needed")
    resource_name: str = Field(description="Specific resource name")
    required: bool = Field(default=True, description="Whether resource is mandatory")
    alternatives: List[str] = Field(default_factory=list, description="Alternative resources")
    justification: str = Field(description="Why this resource is needed")


class TaskStep(BaseModel):
    """Individual task step specification."""
    step_id: str = Field(description="Unique step identifier")
    description: str = Field(description="Step description")
    dependencies: List[str] = Field(default_factory=list, description="Dependencies on other steps")
    required_tools: List[str] = Field(default_factory=list, description="Tools needed for this step")
    estimated_duration: int = Field(description="Estimated duration in minutes")
    complexity: TaskComplexity = Field(description="Step complexity level")
    risk_level: str = Field(description="Risk level for this step")


class TaskDecomposition(BaseModel):
    """Complete task decomposition result."""
    task_id: str = Field(description="Unique task identifier")
    original_description: str = Field(description="Original task description")
    category: TaskCategory = Field(description="Task category")
    overall_complexity: TaskComplexity = Field(description="Overall task complexity")
    
    # Decomposition results
    steps: List[TaskStep] = Field(description="Ordered list of task steps")
    total_estimated_duration: int = Field(description="Total estimated duration in minutes")
    critical_path: List[str] = Field(description="Critical path step IDs")
    
    # Resource analysis
    required_resources: List[ResourceRequirement] = Field(description="Required resources")
    missing_resources: List[str] = Field(description="Missing resources")
    resource_availability_score: float = Field(ge=0.0, le=1.0, description="Resource availability score")
    
    # Feasibility assessment
    feasible: bool = Field(description="Whether task is feasible")
    feasibility_score: float = Field(ge=0.0, le=1.0, description="Overall feasibility score")
    blocking_issues: List[str] = Field(description="Issues that block execution")
    warnings: List[str] = Field(description="Potential issues or warnings")
    
    # Alternative approaches
    alternative_approaches: List[str] = Field(description="Alternative ways to accomplish the task")
    recommended_approach: Optional[str] = Field(description="Recommended approach if not original")
    
    # Analysis metadata
    analysis_confidence: float = Field(ge=0.0, le=1.0, description="Confidence in analysis")
    analysis_timestamp: datetime = Field(default_factory=datetime.now)


class TaskAnalyzer:
    """
    Task Analysis Agent for comprehensive task decomposition and feasibility analysis.
    Operates on raw context to provide unbiased task analysis.
    """
    
    def __init__(self, model: OpenAIChatCompletionsModel):
        self.model = model
        self.agent = Agent(
            name="Task Analyzer",
            model=model,
            instructions=self._get_task_analysis_instructions(),
            tools=[
                self.analyze_task_feasibility,
                self.decompose_task,
                self.assess_resource_requirements,
                self.estimate_task_complexity,
                self.generate_alternative_approaches
            ]
        )
    
    def _get_task_analysis_instructions(self) -> str:
        """Get task analysis focused instructions."""
        return """You are a Task Analyzer AI specializing in comprehensive task decomposition and feasibility assessment.

Your core responsibilities:
1. Decompose complex tasks into manageable steps
2. Assess task feasibility with available resources
3. Identify resource requirements and dependencies
4. Estimate complexity and execution time
5. Generate alternative approaches when needed
6. Identify potential risks and blocking issues

Analysis approach:
- Break down tasks systematically using proven methodologies
- Consider resource constraints and availability
- Assess technical feasibility and complexity
- Identify dependencies and critical paths
- Provide realistic time estimates
- Consider security and safety implications

Key analysis domains:
- File system operations and data processing
- Code development and software engineering
- System administration and configuration
- Git operations and version control
- Research and information gathering
- Integration and deployment tasks

Always provide:
- Step-by-step task decomposition
- Realistic feasibility assessment
- Clear resource requirements
- Time and complexity estimates
- Risk assessment for each step
- Alternative approaches when applicable
- Confidence levels in your analysis

You analyze based on raw conversation context and available tools,
ensuring realistic assessments based on actual capabilities."""
    
    @function_tool
    async def analyze_task_feasibility(
        self,
        ctx: RunContextWrapper[SecurityAnalysisContext],
        task_description: str,
        available_tools: List[str],
        constraints: Optional[Dict[str, Any]] = None
    ) -> FeasibilityAnalysisResult:
        """
        Analyze overall task feasibility.
        
        Args:
            task_description: Description of task to analyze
            available_tools: List of available tools
            constraints: Optional constraints (time, resources, etc.)
        """
        # Import security tools
        from tools.security_tools import task_feasibility_analysis
        
        # Perform basic feasibility analysis
        basic_result = await task_feasibility_analysis(ctx, task_description, available_tools)
        
        # Enhance with Analyzer-specific analysis
        enhanced_analysis = await self._enhance_feasibility_analysis(
            ctx.context, task_description, available_tools, basic_result, constraints
        )
        
        return enhanced_analysis
    
    @function_tool
    async def decompose_task(
        self,
        ctx: RunContextWrapper[SecurityAnalysisContext],
        task_description: str,
        available_tools: List[str],
        max_steps: int = 20
    ) -> TaskDecomposition:
        """
        Decompose task into detailed steps.
        
        Args:
            task_description: Task to decompose
            available_tools: Available tools for execution
            max_steps: Maximum number of steps to create
        """
        # Categorize the task
        category = self._categorize_task(task_description)
        
        # Decompose into steps
        steps = await self._decompose_into_steps(
            task_description, category, available_tools, max_steps
        )
        
        # Analyze dependencies and critical path
        dependencies_graph = self._build_dependency_graph(steps)
        critical_path = self._calculate_critical_path(steps, dependencies_graph)
        
        # Calculate total duration
        total_duration = self._calculate_total_duration(steps, dependencies_graph)
        
        # Assess overall complexity
        overall_complexity = self._assess_overall_complexity(steps)
        
        # Analyze resource requirements
        resource_analysis = await self._analyze_resource_requirements(
            steps, available_tools, ctx.context
        )
        
        # Assess feasibility
        feasibility_assessment = self._assess_decomposition_feasibility(
            steps, resource_analysis, ctx.context
        )
        
        # Generate alternatives
        alternatives = await self._generate_task_alternatives(
            task_description, category, available_tools, steps
        )
        
        # Calculate confidence
        confidence = self._calculate_decomposition_confidence(
            task_description, steps, resource_analysis, ctx.context
        )
        
        return TaskDecomposition(
            task_id=f"task_{datetime.now().timestamp()}",
            original_description=task_description,
            category=category,
            overall_complexity=overall_complexity,
            steps=steps,
            total_estimated_duration=total_duration,
            critical_path=critical_path,
            required_resources=resource_analysis["required"],
            missing_resources=resource_analysis["missing"],
            resource_availability_score=resource_analysis["availability_score"],
            feasible=feasibility_assessment["feasible"],
            feasibility_score=feasibility_assessment["score"],
            blocking_issues=feasibility_assessment["blocking_issues"],
            warnings=feasibility_assessment["warnings"],
            alternative_approaches=alternatives,
            recommended_approach=alternatives[0] if alternatives else None,
            analysis_confidence=confidence
        )
    
    @function_tool
    async def assess_resource_requirements(
        self,
        ctx: RunContextWrapper[SecurityAnalysisContext],
        task_steps: List[Dict[str, Any]],
        available_resources: List[str]
    ) -> Dict[str, Any]:
        """
        Assess resource requirements for task steps.
        
        Args:
            task_steps: List of task steps to analyze
            available_resources: Currently available resources
        """
        requirements = []
        missing = []
        
        for step_data in task_steps:
            step_requirements = self._analyze_step_resources(step_data)
            requirements.extend(step_requirements)
            
            # Check availability
            for req in step_requirements:
                if req.required and req.resource_name not in available_resources:
                    if not any(alt in available_resources for alt in req.alternatives):
                        missing.append(req.resource_name)
        
        # Remove duplicates
        unique_requirements = self._deduplicate_requirements(requirements)
        unique_missing = list(set(missing))
        
        # Calculate availability score
        total_required = len([r for r in unique_requirements if r.required])
        available_count = total_required - len(unique_missing)
        availability_score = available_count / total_required if total_required > 0 else 1.0
        
        return {
            "requirements": unique_requirements,
            "missing": unique_missing,
            "availability_score": availability_score,
            "analysis_timestamp": datetime.now()
        }
    
    @function_tool
    async def estimate_task_complexity(
        self,
        ctx: RunContextWrapper[SecurityAnalysisContext],
        task_description: str,
        context_factors: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Estimate task complexity based on multiple factors.
        
        Args:
            task_description: Task to analyze
            context_factors: Additional context factors
        """
        # Base complexity from description
        base_complexity = self._estimate_base_complexity(task_description)
        
        # Context-based complexity adjustments
        context_adjustments = self._calculate_context_complexity_adjustments(
            ctx.context, context_factors
        )
        
        # Domain-specific complexity factors
        domain_factors = self._calculate_domain_complexity_factors(task_description)
        
        # Calculate final complexity
        complexity_score = self._combine_complexity_factors(
            base_complexity, context_adjustments, domain_factors
        )
        
        complexity_level = self._score_to_complexity_level(complexity_score)
        
        return {
            "complexity_level": complexity_level,
            "complexity_score": complexity_score,
            "base_complexity": base_complexity,
            "context_adjustments": context_adjustments,
            "domain_factors": domain_factors,
            "factors_explanation": self._explain_complexity_factors(
                base_complexity, context_adjustments, domain_factors
            ),
            "analysis_timestamp": datetime.now()
        }
    
    @function_tool
    async def generate_alternative_approaches(
        self,
        ctx: RunContextWrapper[SecurityAnalysisContext],
        task_description: str,
        available_tools: List[str],
        constraints: Optional[Dict[str, Any]] = None
    ) -> List[str]:
        """
        Generate alternative approaches for task completion.
        
        Args:
            task_description: Original task description
            available_tools: Available tools
            constraints: Optional constraints
        """
        alternatives = []
        
        # Tool-based alternatives
        tool_alternatives = self._generate_tool_based_alternatives(
            task_description, available_tools
        )
        alternatives.extend(tool_alternatives)
        
        # Methodology alternatives
        method_alternatives = self._generate_methodology_alternatives(task_description)
        alternatives.extend(method_alternatives)
        
        # Constraint-aware alternatives
        if constraints:
            constraint_alternatives = self._generate_constraint_aware_alternatives(
                task_description, constraints
            )
            alternatives.extend(constraint_alternatives)
        
        # Context-specific alternatives
        context_alternatives = self._generate_context_specific_alternatives(
            task_description, ctx.context
        )
        alternatives.extend(context_alternatives)
        
        # Remove duplicates and sort by viability
        unique_alternatives = list(set(alternatives))
        sorted_alternatives = self._sort_alternatives_by_viability(
            unique_alternatives, available_tools, ctx.context
        )
        
        return sorted_alternatives[:10]  # Return top 10 alternatives
    
    # Helper methods for task analysis
    
    def _categorize_task(self, task_description: str) -> TaskCategory:
        """Categorize task based on description."""
        task_lower = task_description.lower()
        
        # File operations
        if any(word in task_lower for word in ["file", "read", "write", "copy", "move", "delete"]):
            return TaskCategory.FILE_OPERATION
        
        # Code development
        if any(word in task_lower for word in ["code", "program", "develop", "implement", "function", "class"]):
            return TaskCategory.CODE_DEVELOPMENT
        
        # System administration
        if any(word in task_lower for word in ["install", "configure", "setup", "deploy", "system", "server"]):
            return TaskCategory.SYSTEM_ADMINISTRATION
        
        # Git operations
        if any(word in task_lower for word in ["git", "commit", "branch", "merge", "repository", "repo"]):
            return TaskCategory.GIT_OPERATION
        
        # Research
        if any(word in task_lower for word in ["research", "find", "search", "investigate", "analyze", "study"]):
            return TaskCategory.RESEARCH
        
        # Data processing
        if any(word in task_lower for word in ["data", "process", "parse", "transform", "convert"]):
            return TaskCategory.DATA_PROCESSING
        
        # Network operations
        if any(word in task_lower for word in ["network", "download", "upload", "api", "request", "fetch"]):
            return TaskCategory.NETWORK_OPERATION
        
        # Integration
        if any(word in task_lower for word in ["integrate", "connect", "combine", "merge", "link"]):
            return TaskCategory.INTEGRATION
        
        # Deployment
        if any(word in task_lower for word in ["deploy", "release", "publish", "launch"]):
            return TaskCategory.DEPLOYMENT
        
        return TaskCategory.UNKNOWN
    
    async def _decompose_into_steps(
        self,
        task_description: str,
        category: TaskCategory,
        available_tools: List[str],
        max_steps: int
    ) -> List[TaskStep]:
        """Decompose task into detailed steps."""
        steps = []
        
        # Category-specific decomposition strategies
        if category == TaskCategory.FILE_OPERATION:
            steps = self._decompose_file_operation(task_description, available_tools)
        elif category == TaskCategory.CODE_DEVELOPMENT:
            steps = self._decompose_code_development(task_description, available_tools)
        elif category == TaskCategory.GIT_OPERATION:
            steps = self._decompose_git_operation(task_description, available_tools)
        elif category == TaskCategory.RESEARCH:
            steps = self._decompose_research_task(task_description, available_tools)
        else:
            steps = self._decompose_generic_task(task_description, available_tools)
        
        # Limit steps and ensure they have proper IDs
        steps = steps[:max_steps]
        for i, step in enumerate(steps):
            if not step.step_id:
                step.step_id = f"step_{i+1}"
        
        return steps
    
    def _decompose_file_operation(self, task_description: str, tools: List[str]) -> List[TaskStep]:
        """Decompose file operation tasks."""
        steps = []
        task_lower = task_description.lower()
        
        # Always start with understanding the requirement
        steps.append(TaskStep(
            step_id="analyze_requirement",
            description="Analyze file operation requirements",
            dependencies=[],
            required_tools=[],
            estimated_duration=2,
            complexity=TaskComplexity.LOW,
            risk_level="low"
        ))
        
        # Check file existence/permissions
        if any(word in task_lower for word in ["read", "write", "modify", "edit"]):
            steps.append(TaskStep(
                step_id="check_file_access",
                description="Check file existence and permissions",
                dependencies=["analyze_requirement"],
                required_tools=["file_info"] if "file_info" in tools else ["file_list"],
                estimated_duration=1,
                complexity=TaskComplexity.TRIVIAL,
                risk_level="low"
            ))
        
        # Perform the actual operation
        if "read" in task_lower:
            steps.append(TaskStep(
                step_id="read_file",
                description="Read file content",
                dependencies=["check_file_access"],
                required_tools=["file_read"],
                estimated_duration=2,
                complexity=TaskComplexity.LOW,
                risk_level="low"
            ))
        
        if any(word in task_lower for word in ["write", "create", "modify"]):
            steps.append(TaskStep(
                step_id="write_file",
                description="Write or modify file content",
                dependencies=["check_file_access"],
                required_tools=["file_write", "file_edit_patch"],
                estimated_duration=5,
                complexity=TaskComplexity.MEDIUM,
                risk_level="medium"
            ))
        
        # Verification step
        steps.append(TaskStep(
            step_id="verify_operation",
            description="Verify file operation completed successfully",
            dependencies=[step.step_id for step in steps if step.step_id != "analyze_requirement"],
            required_tools=["file_read", "file_info"],
            estimated_duration=2,
            complexity=TaskComplexity.LOW,
            risk_level="low"
        ))
        
        return steps
    
    def _decompose_code_development(self, task_description: str, tools: List[str]) -> List[TaskStep]:
        """Decompose code development tasks."""
        steps = []
        
        # Planning phase
        steps.append(TaskStep(
            step_id="plan_implementation",
            description="Plan code implementation approach",
            dependencies=[],
            required_tools=[],
            estimated_duration=10,
            complexity=TaskComplexity.MEDIUM,
            risk_level="low"
        ))
        
        # Design phase
        steps.append(TaskStep(
            step_id="design_architecture",
            description="Design code architecture and interfaces",
            dependencies=["plan_implementation"],
            required_tools=[],
            estimated_duration=15,
            complexity=TaskComplexity.MEDIUM,
            risk_level="medium"
        ))
        
        # Implementation phase
        steps.append(TaskStep(
            step_id="implement_code",
            description="Implement the code solution",
            dependencies=["design_architecture"],
            required_tools=["file_write", "file_edit_patch"],
            estimated_duration=30,
            complexity=TaskComplexity.HIGH,
            risk_level="medium"
        ))
        
        # Testing phase
        steps.append(TaskStep(
            step_id="test_implementation",
            description="Test the implemented solution",
            dependencies=["implement_code"],
            required_tools=["file_read"],
            estimated_duration=15,
            complexity=TaskComplexity.MEDIUM,
            risk_level="medium"
        ))
        
        # Documentation
        steps.append(TaskStep(
            step_id="document_solution",
            description="Document the code solution",
            dependencies=["test_implementation"],
            required_tools=["file_write"],
            estimated_duration=10,
            complexity=TaskComplexity.LOW,
            risk_level="low"
        ))
        
        return steps
    
    def _decompose_git_operation(self, task_description: str, tools: List[str]) -> List[TaskStep]:
        """Decompose Git operation tasks."""
        steps = []
        task_lower = task_description.lower()
        
        # Check repository status
        steps.append(TaskStep(
            step_id="check_git_status",
            description="Check current Git repository status",
            dependencies=[],
            required_tools=["git_status"],
            estimated_duration=1,
            complexity=TaskComplexity.TRIVIAL,
            risk_level="low"
        ))
        
        if "commit" in task_lower:
            # Stage changes
            steps.append(TaskStep(
                step_id="stage_changes",
                description="Stage changes for commit",
                dependencies=["check_git_status"],
                required_tools=["git_add_file"],
                estimated_duration=2,
                complexity=TaskComplexity.LOW,
                risk_level="low"
            ))
            
            # Commit changes
            steps.append(TaskStep(
                step_id="commit_changes",
                description="Commit staged changes",
                dependencies=["stage_changes"],
                required_tools=["git_commit"],
                estimated_duration=3,
                complexity=TaskComplexity.LOW,
                risk_level="medium"
            ))
        
        if any(word in task_lower for word in ["branch", "checkout"]):
            steps.append(TaskStep(
                step_id="manage_branches",
                description="Manage Git branches",
                dependencies=["check_git_status"],
                required_tools=["git_checkout_branch", "git_branch_list"],
                estimated_duration=2,
                complexity=TaskComplexity.LOW,
                risk_level="medium"
            ))
        
        return steps
    
    def _decompose_research_task(self, task_description: str, tools: List[str]) -> List[TaskStep]:
        """Decompose research tasks."""
        steps = []
        
        # Define research scope
        steps.append(TaskStep(
            step_id="define_research_scope",
            description="Define research scope and objectives",
            dependencies=[],
            required_tools=[],
            estimated_duration=5,
            complexity=TaskComplexity.LOW,
            risk_level="low"
        ))
        
        # Gather information
        steps.append(TaskStep(
            step_id="gather_information",
            description="Gather information from available sources",
            dependencies=["define_research_scope"],
            required_tools=["web_search", "file_search"],
            estimated_duration=20,
            complexity=TaskComplexity.MEDIUM,
            risk_level="low"
        ))
        
        # Analyze findings
        steps.append(TaskStep(
            step_id="analyze_findings",
            description="Analyze and synthesize research findings",
            dependencies=["gather_information"],
            required_tools=[],
            estimated_duration=15,
            complexity=TaskComplexity.MEDIUM,
            risk_level="low"
        ))
        
        # Document results
        steps.append(TaskStep(
            step_id="document_results",
            description="Document research results and conclusions",
            dependencies=["analyze_findings"],
            required_tools=["file_write"],
            estimated_duration=10,
            complexity=TaskComplexity.LOW,
            risk_level="low"
        ))
        
        return steps
    
    def _decompose_generic_task(self, task_description: str, tools: List[str]) -> List[TaskStep]:
        """Decompose generic tasks."""
        steps = []
        
        # Analysis step
        steps.append(TaskStep(
            step_id="analyze_task",
            description="Analyze task requirements and constraints",
            dependencies=[],
            required_tools=[],
            estimated_duration=5,
            complexity=TaskComplexity.LOW,
            risk_level="low"
        ))
        
        # Planning step
        steps.append(TaskStep(
            step_id="plan_approach",
            description="Plan execution approach",
            dependencies=["analyze_task"],
            required_tools=[],
            estimated_duration=8,
            complexity=TaskComplexity.MEDIUM,
            risk_level="low"
        ))
        
        # Execution step
        steps.append(TaskStep(
            step_id="execute_task",
            description="Execute the planned approach",
            dependencies=["plan_approach"],
            required_tools=tools[:3] if tools else [],  # Use first few available tools
            estimated_duration=20,
            complexity=TaskComplexity.MEDIUM,
            risk_level="medium"
        ))
        
        # Verification step
        steps.append(TaskStep(
            step_id="verify_completion",
            description="Verify task completion and quality",
            dependencies=["execute_task"],
            required_tools=[],
            estimated_duration=5,
            complexity=TaskComplexity.LOW,
            risk_level="low"
        ))
        
        return steps
    
    def _build_dependency_graph(self, steps: List[TaskStep]) -> Dict[str, List[str]]:
        """Build dependency graph for steps."""
        graph = {}
        for step in steps:
            graph[step.step_id] = step.dependencies
        return graph
    
    def _calculate_critical_path(self, steps: List[TaskStep], dependencies: Dict[str, List[str]]) -> List[str]:
        """Calculate critical path through task steps."""
        # Simple critical path calculation based on dependencies and duration
        step_durations = {step.step_id: step.estimated_duration for step in steps}
        
        # For now, return the longest path through dependencies
        # In a full implementation, this would use proper critical path method
        max_path = []
        max_duration = 0
        
        for step in steps:
            if not step.dependencies:  # Starting step
                path = self._find_longest_path(step.step_id, dependencies, step_durations)
                path_duration = sum(step_durations.get(step_id, 0) for step_id in path)
                if path_duration > max_duration:
                    max_duration = path_duration
                    max_path = path
        
        return max_path
    
    def _find_longest_path(
        self, 
        start_step: str, 
        dependencies: Dict[str, List[str]], 
        durations: Dict[str, int]
    ) -> List[str]:
        """Find longest path from start step."""
        # Simple DFS to find longest path
        visited = set()
        path = []
        
        def dfs(step_id: str, current_path: List[str]) -> List[str]:
            if step_id in visited:
                return current_path
            
            visited.add(step_id)
            current_path.append(step_id)
            
            # Find steps that depend on this step
            dependent_steps = [
                s for s, deps in dependencies.items() 
                if step_id in deps
            ]
            
            if not dependent_steps:
                return current_path.copy()
            
            longest_path = current_path.copy()
            for dep_step in dependent_steps:
                path = dfs(dep_step, current_path.copy())
                if len(path) > len(longest_path):
                    longest_path = path
            
            return longest_path
        
        return dfs(start_step, [])
    
    def _calculate_total_duration(self, steps: List[TaskStep], dependencies: Dict[str, List[str]]) -> int:
        """Calculate total estimated duration considering parallelization."""
        # For simplicity, return the duration of the critical path
        # In practice, this would consider parallel execution possibilities
        critical_path = self._calculate_critical_path(steps, dependencies)
        step_durations = {step.step_id: step.estimated_duration for step in steps}
        
        return sum(step_durations.get(step_id, 0) for step_id in critical_path)
    
    def _assess_overall_complexity(self, steps: List[TaskStep]) -> TaskComplexity:
        """Assess overall task complexity based on steps."""
        complexity_scores = {
            TaskComplexity.TRIVIAL: 1,
            TaskComplexity.LOW: 2,
            TaskComplexity.MEDIUM: 3,
            TaskComplexity.HIGH: 4,
            TaskComplexity.VERY_HIGH: 5,
            TaskComplexity.EXTREME: 6
        }
        
        total_score = sum(complexity_scores[step.complexity] for step in steps)
        avg_score = total_score / len(steps) if steps else 1
        
        # Consider number of steps as complexity factor
        step_complexity_factor = min(len(steps) / 10, 1.0)  # More steps = more complex
        adjusted_score = avg_score * (1 + step_complexity_factor)
        
        if adjusted_score >= 5.5:
            return TaskComplexity.EXTREME
        elif adjusted_score >= 4.5:
            return TaskComplexity.VERY_HIGH
        elif adjusted_score >= 3.5:
            return TaskComplexity.HIGH
        elif adjusted_score >= 2.5:
            return TaskComplexity.MEDIUM
        elif adjusted_score >= 1.5:
            return TaskComplexity.LOW
        else:
            return TaskComplexity.TRIVIAL
    
    async def _analyze_resource_requirements(
        self,
        steps: List[TaskStep],
        available_tools: List[str],
        context: SecurityAnalysisContext
    ) -> Dict[str, Any]:
        """Analyze resource requirements for all steps."""
        all_requirements = []
        missing_resources = []
        
        for step in steps:
            for tool in step.required_tools:
                if tool not in [req.resource_name for req in all_requirements]:
                    requirement = ResourceRequirement(
                        resource_type="tool",
                        resource_name=tool,
                        required=True,
                        alternatives=[],
                        justification=f"Required for step: {step.description}"
                    )
                    all_requirements.append(requirement)
                    
                    if tool not in available_tools:
                        missing_resources.append(tool)
        
        # Calculate availability score
        total_required = len(all_requirements)
        available_count = total_required - len(missing_resources)
        availability_score = available_count / total_required if total_required > 0 else 1.0
        
        return {
            "required": all_requirements,
            "missing": missing_resources,
            "availability_score": availability_score
        }
    
    def _assess_decomposition_feasibility(
        self,
        steps: List[TaskStep],
        resource_analysis: Dict[str, Any],
        context: SecurityAnalysisContext
    ) -> Dict[str, Any]:
        """Assess feasibility of decomposed task."""
        blocking_issues = []
        warnings = []
        
        # Check for missing critical resources
        missing_resources = resource_analysis["missing"]
        if missing_resources:
            blocking_issues.append(f"Missing critical tools: {', '.join(missing_resources)}")
        
        # Check for high-risk steps
        high_risk_steps = [step for step in steps if step.risk_level in ["high", "critical"]]
        if high_risk_steps:
            warnings.append(f"Contains {len(high_risk_steps)} high-risk steps")
        
        # Check for extremely complex steps
        extreme_steps = [step for step in steps if step.complexity == TaskComplexity.EXTREME]
        if extreme_steps:
            blocking_issues.append(f"Contains {len(extreme_steps)} extremely complex steps")
        
        # Check total duration
        total_duration = sum(step.estimated_duration for step in steps)
        if total_duration > 480:  # 8 hours
            warnings.append(f"Estimated duration is very long: {total_duration} minutes")
        
        # Calculate feasibility score
        score = 1.0
        score -= len(blocking_issues) * 0.4
        score -= len(warnings) * 0.1
        score *= resource_analysis["availability_score"]
        
        feasible = len(blocking_issues) == 0 and score >= 0.6
        
        return {
            "feasible": feasible,
            "score": max(0.0, score),
            "blocking_issues": blocking_issues,
            "warnings": warnings
        }
    
    async def _generate_task_alternatives(
        self,
        task_description: str,
        category: TaskCategory,
        available_tools: List[str],
        original_steps: List[TaskStep]
    ) -> List[str]:
        """Generate alternative approaches for the task."""
        alternatives = []
        
        # Tool-based alternatives
        if category == TaskCategory.FILE_OPERATION:
            if "file_edit_patch" not in available_tools and "file_write" in available_tools:
                alternatives.append("Use file_write with full content replacement instead of patching")
        
        # Methodology alternatives
        if len(original_steps) > 10:
            alternatives.append("Break into smaller sub-tasks and execute incrementally")
        
        # Risk reduction alternatives
        high_risk_steps = [step for step in original_steps if step.risk_level in ["high", "critical"]]
        if high_risk_steps:
            alternatives.append("Implement additional safety checks and rollback mechanisms")
        
        # Complexity reduction alternatives
        complex_steps = [step for step in original_steps if step.complexity in [TaskComplexity.HIGH, TaskComplexity.VERY_HIGH]]
        if complex_steps:
            alternatives.append("Simplify complex steps by using manual intervention where needed")
        
        return alternatives
    
    def _calculate_decomposition_confidence(
        self,
        task_description: str,
        steps: List[TaskStep],
        resource_analysis: Dict[str, Any],
        context: SecurityAnalysisContext
    ) -> float:
        """Calculate confidence in task decomposition."""
        confidence_factors = []
        
        # Task clarity factor
        task_clarity = min(len(task_description.split()) / 20, 1.0)
        confidence_factors.append(task_clarity)
        
        # Step quality factor
        step_quality = 1.0 if len(steps) > 0 else 0.0
        if steps:
            avg_step_complexity = sum(
                {"trivial": 1, "low": 2, "medium": 3, "high": 4, "very_high": 5, "extreme": 6}[step.complexity.value]
                for step in steps
            ) / len(steps)
            # Higher complexity reduces confidence slightly
            step_quality = max(0.5, 1.0 - (avg_step_complexity - 3) * 0.1)
        confidence_factors.append(step_quality)
        
        # Resource availability factor
        confidence_factors.append(resource_analysis["availability_score"])
        
        # Context quality factor
        if context.raw_conversation:
            context_quality = min(len(context.raw_conversation) / 5, 1.0)
        else:
            context_quality = 0.5
        confidence_factors.append(context_quality)
        
        return sum(confidence_factors) / len(confidence_factors)
    
    # Additional helper methods for enhanced feasibility analysis
    
    async def _enhance_feasibility_analysis(
        self,
        context: SecurityAnalysisContext,
        task_description: str,
        available_tools: List[str],
        basic_result: FeasibilityAnalysisResult,
        constraints: Optional[Dict[str, Any]]
    ) -> FeasibilityAnalysisResult:
        """Enhance basic feasibility analysis with Analyzer-specific insights."""
        
        # Additional complexity assessment
        enhanced_complexity = await self._enhanced_complexity_assessment(
            task_description, context, constraints
        )
        
        # Context-aware feasibility adjustments
        context_adjustments = self._calculate_context_feasibility_adjustments(
            context, basic_result.feasibility_score
        )
        
        # Constraint-based feasibility adjustments
        constraint_adjustments = self._calculate_constraint_adjustments(
            constraints, basic_result.feasibility_score
        )
        
        # Combine adjustments
        final_score = basic_result.feasibility_score * context_adjustments * constraint_adjustments
        final_feasible = final_score >= 0.6 and len(basic_result.missing_dependencies) == 0
        
        # Enhanced alternative approaches
        enhanced_alternatives = basic_result.alternative_approaches.copy()
        enhanced_alternatives.extend(
            self._generate_context_aware_alternatives(task_description, context)
        )
        
        return FeasibilityAnalysisResult(
            feasible=final_feasible,
            feasibility_score=final_score,
            required_tools=basic_result.required_tools,
            missing_dependencies=basic_result.missing_dependencies,
            estimated_complexity=enhanced_complexity,
            alternative_approaches=enhanced_alternatives[:10]  # Limit to 10
        )
    
    async def _enhanced_complexity_assessment(
        self,
        task_description: str,
        context: SecurityAnalysisContext,
        constraints: Optional[Dict[str, Any]]
    ) -> str:
        """Enhanced complexity assessment considering context and constraints."""
        base_complexity = self._estimate_base_complexity(task_description)
        
        # Context-based adjustments
        if context.tool_execution_history:
            # Previous tool usage suggests higher capability, reduce perceived complexity
            if len(context.tool_execution_history) > 10:
                base_complexity = max(base_complexity - 0.5, 1.0)
        
        # Constraint-based adjustments
        if constraints:
            if constraints.get("time_limit"):
                base_complexity += 0.5  # Time pressure increases complexity
            if constraints.get("safety_critical"):
                base_complexity += 1.0  # Safety requirements increase complexity
        
        return self._score_to_complexity_level(base_complexity).value
    
    def _calculate_context_feasibility_adjustments(
        self,
        context: SecurityAnalysisContext,
        base_score: float
    ) -> float:
        """Calculate feasibility adjustments based on context quality."""
        adjustment = 1.0
        
        # Conversation quality adjustment
        if context.raw_conversation:
            conv_quality = min(len(context.raw_conversation) / 5, 1.0)
            adjustment *= (0.8 + 0.2 * conv_quality)
        else:
            adjustment *= 0.8  # Reduce score without conversation context
        
        # Tool execution history adjustment
        if context.tool_execution_history:
            tool_experience = min(len(context.tool_execution_history) / 10, 1.0)
            adjustment *= (0.9 + 0.1 * tool_experience)
        
        return adjustment
    
    def _calculate_constraint_adjustments(
        self,
        constraints: Optional[Dict[str, Any]],
        base_score: float
    ) -> float:
        """Calculate feasibility adjustments based on constraints."""
        if not constraints:
            return 1.0
        
        adjustment = 1.0
        
        # Time constraints
        if constraints.get("time_limit"):
            time_limit = constraints["time_limit"]
            if time_limit < 30:  # Less than 30 minutes
                adjustment *= 0.7
            elif time_limit < 120:  # Less than 2 hours
                adjustment *= 0.9
        
        # Resource constraints
        if constraints.get("limited_resources"):
            adjustment *= 0.8
        
        # Safety constraints
        if constraints.get("safety_critical"):
            adjustment *= 0.9  # Slightly reduce feasibility for safety-critical tasks
        
        return adjustment
    
    def _generate_context_aware_alternatives(
        self,
        task_description: str,
        context: SecurityAnalysisContext
    ) -> List[str]:
        """Generate alternatives based on context."""
        alternatives = []
        
        # If we have tool execution history, suggest leveraging experience
        if context.tool_execution_history:
            successful_tools = [
                exec.tool_name for exec in context.tool_execution_history 
                if exec.success
            ]
            if successful_tools:
                alternatives.append(f"Leverage successful experience with tools: {', '.join(set(successful_tools))}")
        
        # If conversation suggests user preferences, incorporate them
        if context.raw_conversation:
            user_messages = [msg for msg in context.raw_conversation if msg.role == "user"]
            if len(user_messages) > 3:
                alternatives.append("Break task into smaller interactive steps based on conversation pattern")
        
        return alternatives
    
    # Complexity estimation helpers
    
    def _estimate_base_complexity(self, task_description: str) -> float:
        """Estimate base complexity score from task description."""
        task_lower = task_description.lower()
        complexity_score = 2.0  # Base medium complexity
        
        # Keywords that increase complexity
        complex_keywords = {
            "integrate": 1.5, "deploy": 1.2, "optimize": 1.0, "analyze": 0.8,
            "configure": 1.0, "implement": 1.2, "design": 1.0, "architect": 1.5,
            "migrate": 1.5, "transform": 1.0, "automate": 1.2, "scale": 1.3
        }
        
        for keyword, weight in complex_keywords.items():
            if keyword in task_lower:
                complexity_score += weight
        
        # Keywords that suggest simplicity
        simple_keywords = {
            "read": -0.5, "list": -0.8, "show": -0.8, "display": -0.6,
            "copy": -0.3, "move": -0.3, "simple": -0.5
        }
        
        for keyword, weight in simple_keywords.items():
            if keyword in task_lower:
                complexity_score += weight
        
        # Length-based complexity
        word_count = len(task_description.split())
        if word_count > 50:
            complexity_score += 1.0
        elif word_count > 20:
            complexity_score += 0.5
        elif word_count < 5:
            complexity_score -= 0.5
        
        return max(1.0, min(complexity_score, 6.0))
    
    def _score_to_complexity_level(self, score: float) -> TaskComplexity:
        """Convert complexity score to TaskComplexity level."""
        if score >= 5.5:
            return TaskComplexity.EXTREME
        elif score >= 4.5:
            return TaskComplexity.VERY_HIGH
        elif score >= 3.5:
            return TaskComplexity.HIGH
        elif score >= 2.5:
            return TaskComplexity.MEDIUM
        elif score >= 1.5:
            return TaskComplexity.LOW
        else:
            return TaskComplexity.TRIVIAL
    
    # Other helper methods for various analysis functions...
    
    def _calculate_context_complexity_adjustments(
        self,
        context: SecurityAnalysisContext,
        context_factors: Optional[Dict[str, Any]]
    ) -> Dict[str, float]:
        """Calculate complexity adjustments based on context."""
        adjustments = {}
        
        # Conversation complexity
        if context.raw_conversation:
            conv_complexity = len(context.raw_conversation) / 10.0
            adjustments["conversation"] = min(conv_complexity, 1.0)
        else:
            adjustments["conversation"] = 0.0
        
        # Tool usage complexity
        if context.tool_execution_history:
            unique_tools = len(set(exec.tool_name for exec in context.tool_execution_history))
            tool_complexity = unique_tools / 5.0
            adjustments["tools"] = min(tool_complexity, 1.0)
        else:
            adjustments["tools"] = 0.0
        
        # Additional context factors
        if context_factors:
            if context_factors.get("urgent"):
                adjustments["urgency"] = 0.5
            if context_factors.get("safety_critical"):
                adjustments["safety"] = 1.0
        
        return adjustments
    
    def _calculate_domain_complexity_factors(self, task_description: str) -> Dict[str, float]:
        """Calculate domain-specific complexity factors."""
        factors = {}
        task_lower = task_description.lower()
        
        # Technical domains
        if any(word in task_lower for word in ["database", "sql", "query"]):
            factors["database"] = 0.8
        
        if any(word in task_lower for word in ["network", "api", "http", "rest"]):
            factors["network"] = 0.6
        
        if any(word in task_lower for word in ["security", "encrypt", "auth", "permission"]):
            factors["security"] = 1.0
        
        if any(word in task_lower for word in ["machine learning", "ai", "model", "train"]):
            factors["ai_ml"] = 1.5
        
        return factors
    
    def _combine_complexity_factors(
        self,
        base_complexity: float,
        context_adjustments: Dict[str, float],
        domain_factors: Dict[str, float]
    ) -> float:
        """Combine all complexity factors into final score."""
        total_adjustment = sum(context_adjustments.values()) / len(context_adjustments) if context_adjustments else 0
        total_domain = sum(domain_factors.values()) / len(domain_factors) if domain_factors else 0
        
        # Combine with weights
        final_score = base_complexity + (total_adjustment * 0.3) + (total_domain * 0.4)
        
        return max(1.0, min(final_score, 6.0))
    
    def _explain_complexity_factors(
        self,
        base_complexity: float,
        context_adjustments: Dict[str, float],
        domain_factors: Dict[str, float]
    ) -> str:
        """Explain complexity assessment reasoning."""
        explanations = []
        
        explanations.append(f"Base complexity: {base_complexity:.1f}")
        
        if context_adjustments:
            for factor, value in context_adjustments.items():
                explanations.append(f"{factor} adjustment: +{value:.1f}")
        
        if domain_factors:
            for factor, value in domain_factors.items():
                explanations.append(f"{factor} domain complexity: +{value:.1f}")
        
        return "; ".join(explanations)
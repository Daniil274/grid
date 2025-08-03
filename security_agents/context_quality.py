"""
Context Quality Agent - Context validation and dependency checking.
Analyzes quality and completeness of context for task execution.
"""
from typing import List, Dict, Any, Optional, Set, Tuple
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum
from agents import Agent, OpenAIChatCompletionsModel, function_tool, RunContextWrapper

from core.security_context import SecurityAnalysisContext, RawMessage, ToolExecution
from tools.security_tools import ContextValidationResult, DependencyCheckResult, QualityMetricsResult


class ContextQualityLevel(Enum):
    """Context quality assessment levels"""
    EXCELLENT = "excellent"
    GOOD = "good"
    ADEQUATE = "adequate"
    POOR = "poor"
    INSUFFICIENT = "insufficient"


class ContextCompleteness(BaseModel):
    """Context completeness assessment"""
    has_clear_objective: bool = Field(description="Task objective is clear")
    has_sufficient_detail: bool = Field(description="Sufficient detail provided")
    has_relevant_context: bool = Field(description="Relevant context available")
    has_necessary_resources: bool = Field(description="Required resources identified")
    missing_elements: List[str] = Field(default_factory=list, description="Missing context elements")
    
    
class InformationGaps(BaseModel):
    """Identified information gaps"""
    critical_gaps: List[str] = Field(default_factory=list, description="Critical missing information")
    important_gaps: List[str] = Field(default_factory=list, description="Important missing information")
    minor_gaps: List[str] = Field(default_factory=list, description="Minor missing information")
    suggested_questions: List[str] = Field(default_factory=list, description="Questions to fill gaps")


class DependencyAnalysis(BaseModel):
    """Dependency analysis results"""
    identified_dependencies: List[str] = Field(default_factory=list, description="Identified dependencies")
    missing_dependencies: List[str] = Field(default_factory=list, description="Missing dependencies")
    circular_dependencies: List[str] = Field(default_factory=list, description="Circular dependencies found")
    external_dependencies: List[str] = Field(default_factory=list, description="External dependencies")
    resolution_order: List[str] = Field(default_factory=list, description="Suggested resolution order")


class ContextQualityReport(BaseModel):
    """Comprehensive context quality assessment report"""
    overall_quality: ContextQualityLevel = Field(description="Overall quality assessment")
    completeness: ContextCompleteness = Field(description="Context completeness analysis")
    information_gaps: InformationGaps = Field(description="Information gaps analysis")
    dependency_analysis: DependencyAnalysis = Field(description="Dependency analysis")
    quality_score: float = Field(ge=0.0, le=1.0, description="Quality score 0-1")
    recommendations: List[str] = Field(default_factory=list, description="Improvement recommendations")
    risks: List[str] = Field(default_factory=list, description="Identified risks")
    timestamp: datetime = Field(default_factory=datetime.now)


class ContextQualityAgent:
    """
    Context Quality Agent for analyzing context quality and completeness.
    Provides comprehensive analysis without requiring agent prompts.
    """
    
    def __init__(self):
        self.agent = Agent(
            model=OpenAIChatCompletionsModel("qwen3"),
            system_prompt=self._get_system_prompt(),
            tools=[
                self._context_validation_tool,
                self._dependency_check_tool,
                self._quality_metrics_tool
            ]
        )
    
    def _get_system_prompt(self) -> str:
        """Get the system prompt for context quality analysis"""
        return """
        You are a Context Quality Specialist responsible for analyzing the quality and completeness 
        of conversational context for task execution.

        Your primary responsibilities:
        1. Assess context completeness and clarity
        2. Identify information gaps and missing dependencies
        3. Analyze context quality for effective task execution
        4. Provide actionable recommendations for improvement

        Analysis Framework:
        - Context Completeness: Clear objectives, sufficient detail, relevant context
        - Information Quality: Accuracy, relevance, timeliness
        - Dependency Analysis: Required resources, external dependencies, resolution order
        - Risk Assessment: Potential issues from incomplete context

        Always provide specific, actionable recommendations for improving context quality.
        Focus on what's needed for successful task completion.
        """
    
    async def analyze_context_quality(
        self, 
        context: SecurityAnalysisContext,
        task_description: Optional[str] = None
    ) -> ContextQualityReport:
        """
        Analyze the quality and completeness of context for task execution.
        
        Args:
            context: Security analysis context with conversation history
            task_description: Optional specific task description
            
        Returns:
            Comprehensive context quality report
        """
        try:
            # Analyze conversation for context quality
            conversation_analysis = await self._analyze_conversation_quality(context.raw_conversation)
            
            # Check for information gaps
            gaps_analysis = await self._identify_information_gaps(
                context.raw_conversation, 
                task_description
            )
            
            # Analyze dependencies
            dependency_analysis = await self._analyze_dependencies(
                context.tool_execution_history,
                context.raw_conversation
            )
            
            # Calculate overall quality score
            quality_score = self._calculate_quality_score(
                conversation_analysis,
                gaps_analysis,
                dependency_analysis
            )
            
            # Generate recommendations
            recommendations = await self._generate_recommendations(
                conversation_analysis,
                gaps_analysis,
                dependency_analysis
            )
            
            # Assess risks
            risks = self._assess_context_risks(gaps_analysis, dependency_analysis)
            
            return ContextQualityReport(
                overall_quality=self._determine_quality_level(quality_score),
                completeness=conversation_analysis,
                information_gaps=gaps_analysis,
                dependency_analysis=dependency_analysis,
                quality_score=quality_score,
                recommendations=recommendations,
                risks=risks
            )
            
        except Exception as e:
            # Return minimal quality report for errors
            return ContextQualityReport(
                overall_quality=ContextQualityLevel.INSUFFICIENT,
                completeness=ContextCompleteness(
                    has_clear_objective=False,
                    has_sufficient_detail=False,
                    has_relevant_context=False,
                    has_necessary_resources=False,
                    missing_elements=[f"Analysis error: {str(e)}"]
                ),
                information_gaps=InformationGaps(
                    critical_gaps=[f"Unable to analyze context: {str(e)}"]
                ),
                dependency_analysis=DependencyAnalysis(),
                quality_score=0.0,
                recommendations=[f"Fix context analysis error: {str(e)}"],
                risks=[f"Context analysis failed: {str(e)}"]
            )
    
    async def _analyze_conversation_quality(self, conversation: List[RawMessage]) -> ContextCompleteness:
        """Analyze conversation for completeness"""
        if not conversation:
            return ContextCompleteness(
                has_clear_objective=False,
                has_sufficient_detail=False,
                has_relevant_context=False,
                has_necessary_resources=False,
                missing_elements=["No conversation history available"]
            )
        
        # Extract key elements from conversation
        user_messages = [msg for msg in conversation if msg.role == "user"]
        
        # Check for clear objective
        has_clear_objective = any(
            len(msg.content.strip()) > 10 and any(
                keyword in msg.content.lower() 
                for keyword in ["help", "create", "implement", "fix", "analyze", "build"]
            )
            for msg in user_messages
        )
        
        # Check for sufficient detail
        total_user_content = " ".join(msg.content for msg in user_messages)
        has_sufficient_detail = len(total_user_content.strip()) > 50
        
        # Check for relevant context
        has_relevant_context = len(conversation) > 1
        
        # Check for necessary resources (file paths, tools mentioned)
        has_necessary_resources = any(
            "/" in msg.content or "tool" in msg.content.lower()
            for msg in user_messages
        )
        
        # Identify missing elements
        missing_elements = []
        if not has_clear_objective:
            missing_elements.append("Clear task objective")
        if not has_sufficient_detail:
            missing_elements.append("Sufficient task details")
        if not has_relevant_context:
            missing_elements.append("Relevant context information")
        if not has_necessary_resources:
            missing_elements.append("Resource specifications")
        
        return ContextCompleteness(
            has_clear_objective=has_clear_objective,
            has_sufficient_detail=has_sufficient_detail,
            has_relevant_context=has_relevant_context,
            has_necessary_resources=has_necessary_resources,
            missing_elements=missing_elements
        )
    
    async def _identify_information_gaps(
        self, 
        conversation: List[RawMessage],
        task_description: Optional[str] = None
    ) -> InformationGaps:
        """Identify information gaps in the context"""
        critical_gaps = []
        important_gaps = []
        minor_gaps = []
        suggested_questions = []
        
        if not conversation:
            critical_gaps.append("No conversation history")
            suggested_questions.append("What task would you like to accomplish?")
            return InformationGaps(
                critical_gaps=critical_gaps,
                suggested_questions=suggested_questions
            )
        
        user_messages = [msg for msg in conversation if msg.role == "user"]
        combined_content = " ".join(msg.content for msg in user_messages).lower()
        
        # Check for common gaps
        if "file" in combined_content and not any("/" in msg.content for msg in user_messages):
            important_gaps.append("Specific file paths not provided")
            suggested_questions.append("Which specific files should be modified?")
        
        if "config" in combined_content and "setting" not in combined_content:
            important_gaps.append("Configuration details not specified")
            suggested_questions.append("What configuration settings need to be changed?")
        
        if "error" in combined_content and "message" not in combined_content:
            important_gaps.append("Error details not provided")
            suggested_questions.append("What is the exact error message?")
        
        if "test" in combined_content and "run" not in combined_content:
            minor_gaps.append("Test execution method not specified")
            suggested_questions.append("How should tests be executed?")
        
        # Check for task-specific gaps
        if task_description:
            task_lower = task_description.lower()
            if "implement" in task_lower and "requirement" not in combined_content:
                important_gaps.append("Implementation requirements not detailed")
                suggested_questions.append("What are the specific requirements for this implementation?")
        
        return InformationGaps(
            critical_gaps=critical_gaps,
            important_gaps=important_gaps,
            minor_gaps=minor_gaps,
            suggested_questions=suggested_questions
        )
    
    async def _analyze_dependencies(
        self,
        tool_history: List[ToolExecution],
        conversation: List[RawMessage]
    ) -> DependencyAnalysis:
        """Analyze dependencies from context"""
        identified_dependencies = []
        missing_dependencies = []
        external_dependencies = []
        
        # Analyze tool usage patterns
        tool_names = [tool.tool_name for tool in tool_history]
        
        # Common dependency patterns
        if "file_write" in tool_names and "file_read" not in tool_names:
            missing_dependencies.append("File content analysis before modification")
        
        if "git_commit" in tool_names and "git_status" not in tool_names:
            missing_dependencies.append("Git status check before commit")
        
        if "test_run" in tool_names and "build" not in tool_names:
            missing_dependencies.append("Build verification before testing")
        
        # Extract mentions of external systems
        combined_content = " ".join(msg.content for msg in conversation).lower()
        
        if "database" in combined_content:
            external_dependencies.append("Database connection")
        if "api" in combined_content:
            external_dependencies.append("External API")
        if "network" in combined_content:
            external_dependencies.append("Network connectivity")
        
        # Suggested resolution order
        resolution_order = []
        if "read" in " ".join(tool_names):
            resolution_order.append("Read existing files")
        if "analyze" in " ".join(tool_names):
            resolution_order.append("Analyze current state")
        if "implement" in combined_content:
            resolution_order.append("Implement changes")
        if "test" in " ".join(tool_names):
            resolution_order.append("Run tests")
        
        return DependencyAnalysis(
            identified_dependencies=identified_dependencies,
            missing_dependencies=missing_dependencies,
            external_dependencies=external_dependencies,
            resolution_order=resolution_order
        )
    
    def _calculate_quality_score(
        self,
        completeness: ContextCompleteness,
        gaps: InformationGaps,
        dependencies: DependencyAnalysis
    ) -> float:
        """Calculate overall quality score"""
        score = 0.0
        
        # Completeness score (40% weight)
        completeness_score = sum([
            completeness.has_clear_objective,
            completeness.has_sufficient_detail,
            completeness.has_relevant_context,
            completeness.has_necessary_resources
        ]) / 4.0
        score += completeness_score * 0.4
        
        # Information gaps score (30% weight)
        total_gaps = len(gaps.critical_gaps) + len(gaps.important_gaps) + len(gaps.minor_gaps)
        gaps_score = max(0.0, 1.0 - (total_gaps * 0.1))
        score += gaps_score * 0.3
        
        # Dependencies score (30% weight)
        missing_deps = len(dependencies.missing_dependencies)
        deps_score = max(0.0, 1.0 - (missing_deps * 0.15))
        score += deps_score * 0.3
        
        return min(1.0, max(0.0, score))
    
    def _determine_quality_level(self, score: float) -> ContextQualityLevel:
        """Determine quality level from score"""
        if score >= 0.9:
            return ContextQualityLevel.EXCELLENT
        elif score >= 0.7:
            return ContextQualityLevel.GOOD
        elif score >= 0.5:
            return ContextQualityLevel.ADEQUATE
        elif score >= 0.3:
            return ContextQualityLevel.POOR
        else:
            return ContextQualityLevel.INSUFFICIENT
    
    async def _generate_recommendations(
        self,
        completeness: ContextCompleteness,
        gaps: InformationGaps,
        dependencies: DependencyAnalysis
    ) -> List[str]:
        """Generate improvement recommendations"""
        recommendations = []
        
        # Completeness recommendations
        for missing in completeness.missing_elements:
            recommendations.append(f"Provide {missing.lower()}")
        
        # Gap recommendations
        if gaps.critical_gaps:
            recommendations.append("Address critical information gaps immediately")
        if gaps.important_gaps:
            recommendations.append("Clarify important missing information")
        
        # Dependency recommendations
        for missing_dep in dependencies.missing_dependencies:
            recommendations.append(f"Ensure {missing_dep} is available")
        
        # General recommendations
        if not recommendations:
            recommendations.append("Context quality is adequate for task execution")
        
        return recommendations[:5]  # Limit to top 5 recommendations
    
    def _assess_context_risks(
        self,
        gaps: InformationGaps,
        dependencies: DependencyAnalysis
    ) -> List[str]:
        """Assess risks from context quality issues"""
        risks = []
        
        if gaps.critical_gaps:
            risks.append("High risk of task failure due to critical information gaps")
        
        if len(gaps.important_gaps) > 2:
            risks.append("Medium risk of incomplete task execution")
        
        if len(dependencies.missing_dependencies) > 1:
            risks.append("Risk of dependency-related failures")
        
        if dependencies.external_dependencies:
            risks.append("Risk from external dependency availability")
        
        return risks
    
    @function_tool
    async def _context_validation_tool(
        self,
        ctx: RunContextWrapper[SecurityAnalysisContext],
        context_elements: List[str],
        validation_criteria: List[str]
    ) -> ContextValidationResult:
        """Validate context elements against criteria"""
        validation_results = {}
        
        for element in context_elements:
            for criterion in validation_criteria:
                key = f"{element}_{criterion}"
                # Simple validation logic
                validation_results[key] = len(element) > 0
        
        is_valid = all(validation_results.values())
        
        return ContextValidationResult(
            is_valid=is_valid,
            validation_details=validation_results,
            missing_elements=[k for k, v in validation_results.items() if not v],
            recommendations=["Provide missing context elements"] if not is_valid else []
        )
    
    @function_tool
    async def _dependency_check_tool(
        self,
        ctx: RunContextWrapper[SecurityAnalysisContext],
        dependencies: List[str]
    ) -> DependencyCheckResult:
        """Check dependency availability and requirements"""
        available_dependencies = []
        missing_dependencies = []
        
        # Check each dependency (simplified logic)
        for dep in dependencies:
            if len(dep) > 0:  # Simple check
                available_dependencies.append(dep)
            else:
                missing_dependencies.append(dep)
        
        return DependencyCheckResult(
            all_dependencies_available=len(missing_dependencies) == 0,
            available_dependencies=available_dependencies,
            missing_dependencies=missing_dependencies,
            resolution_suggestions=[f"Install {dep}" for dep in missing_dependencies]
        )
    
    @function_tool
    async def _quality_metrics_tool(
        self,
        ctx: RunContextWrapper[SecurityAnalysisContext],
        content: str
    ) -> QualityMetricsResult:
        """Calculate quality metrics for content"""
        content_length = len(content)
        word_count = len(content.split())
        
        # Simple quality metrics
        completeness_score = min(1.0, content_length / 1000.0)
        clarity_score = min(1.0, word_count / 100.0)
        
        return QualityMetricsResult(
            completeness_score=completeness_score,
            clarity_score=clarity_score,
            overall_quality_score=(completeness_score + clarity_score) / 2.0,
            improvement_areas=["Add more detail"] if completeness_score < 0.5 else []
        )


# Factory function for creating the agent
def create_context_quality_agent() -> ContextQualityAgent:
    """Create and return a Context Quality Agent instance"""
    return ContextQualityAgent()


# Export main components
__all__ = [
    "ContextQualityAgent",
    "ContextQualityReport",
    "ContextQualityLevel",
    "ContextCompleteness",
    "InformationGaps",
    "DependencyAnalysis",
    "create_context_quality_agent"
]
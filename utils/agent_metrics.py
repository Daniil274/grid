"""
Agent performance metrics collection and analysis system.
Provides comprehensive monitoring, reporting, and optimization insights.
"""

import time
import statistics
import threading
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict, deque
from enum import Enum


class MetricType(Enum):
    """Types of metrics collected."""
    PERFORMANCE = "performance"
    QUALITY = "quality"
    USAGE = "usage"
    ERROR = "error"
    RESOURCE = "resource"


@dataclass
class ExecutionMetric:
    """Single execution metric record."""
    
    agent_key: str
    timestamp: float
    duration: float
    success: bool
    input_length: int
    output_length: int
    tools_used: List[str] = field(default_factory=list)
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    quality_score: Optional[float] = None
    memory_usage: Optional[float] = None
    
    @property
    def datetime(self) -> datetime:
        """Get datetime from timestamp."""
        return datetime.fromtimestamp(self.timestamp)


@dataclass
class AgentHealth:
    """Agent health status."""
    
    agent_key: str
    status: str  # "healthy", "warning", "critical"
    success_rate: float
    avg_response_time: float
    error_rate: float
    last_execution: Optional[datetime] = None
    issues: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


class MetricAggregator:
    """Aggregates metrics for analysis."""
    
    def __init__(self, window_size: int = 100):
        self.window_size = window_size
        self.metrics = deque(maxlen=window_size)
        self.agent_metrics = defaultdict(lambda: deque(maxlen=window_size))
    
    def add_metric(self, metric: ExecutionMetric):
        """Add a new metric."""
        self.metrics.append(metric)
        self.agent_metrics[metric.agent_key].append(metric)
    
    def get_agent_metrics(self, agent_key: str, time_window: Optional[timedelta] = None) -> List[ExecutionMetric]:
        """Get metrics for a specific agent within time window."""
        agent_metrics = list(self.agent_metrics[agent_key])
        
        if time_window:
            cutoff_time = time.time() - time_window.total_seconds()
            agent_metrics = [m for m in agent_metrics if m.timestamp >= cutoff_time]
        
        return agent_metrics
    
    def get_all_metrics(self, time_window: Optional[timedelta] = None) -> List[ExecutionMetric]:
        """Get all metrics within time window."""
        metrics = list(self.metrics)
        
        if time_window:
            cutoff_time = time.time() - time_window.total_seconds()
            metrics = [m for m in metrics if m.timestamp >= cutoff_time]
        
        return metrics


class PerformanceAnalyzer:
    """Analyzes performance metrics and identifies patterns."""
    
    def __init__(self):
        self.alert_thresholds = {
            "max_response_time": 30.0,
            "min_success_rate": 0.8,
            "max_error_rate": 0.2,
            "min_quality_score": 0.7
        }
    
    def analyze_agent_performance(self, metrics: List[ExecutionMetric]) -> Dict[str, Any]:
        """Analyze performance for an agent."""
        if not metrics:
            return {"status": "no_data"}
        
        # Basic statistics
        durations = [m.duration for m in metrics]
        successes = [m.success for m in metrics]
        quality_scores = [m.quality_score for m in metrics if m.quality_score is not None]
        
        success_rate = sum(successes) / len(successes)
        error_rate = 1 - success_rate
        
        analysis = {
            "total_executions": len(metrics),
            "success_rate": success_rate,
            "error_rate": error_rate,
            "avg_response_time": statistics.mean(durations),
            "median_response_time": statistics.median(durations),
            "min_response_time": min(durations),
            "max_response_time": max(durations),
            "response_time_std": statistics.stdev(durations) if len(durations) > 1 else 0,
        }
        
        if quality_scores:
            analysis.update({
                "avg_quality_score": statistics.mean(quality_scores),
                "median_quality_score": statistics.median(quality_scores),
                "min_quality_score": min(quality_scores),
                "max_quality_score": max(quality_scores)
            })
        
        # Performance trends
        if len(metrics) >= 10:
            analysis["trends"] = self._analyze_trends(metrics)
        
        # Tool usage analysis
        all_tools = [tool for m in metrics for tool in m.tools_used]
        tool_usage = defaultdict(int)
        for tool in all_tools:
            tool_usage[tool] += 1
        
        analysis["tool_usage"] = dict(tool_usage)
        analysis["most_used_tools"] = sorted(tool_usage.items(), key=lambda x: x[1], reverse=True)[:5]
        
        # Error analysis
        errors = [m for m in metrics if not m.success]
        if errors:
            error_types = defaultdict(int)
            for error in errors:
                error_types[error.error_type or "unknown"] += 1
            analysis["error_breakdown"] = dict(error_types)
        
        return analysis
    
    def _analyze_trends(self, metrics: List[ExecutionMetric]) -> Dict[str, Any]:
        """Analyze performance trends."""
        # Sort by timestamp
        sorted_metrics = sorted(metrics, key=lambda m: m.timestamp)
        
        # Split into two halves for trend analysis
        mid = len(sorted_metrics) // 2
        first_half = sorted_metrics[:mid]
        second_half = sorted_metrics[mid:]
        
        # Calculate averages for each half
        first_avg_duration = statistics.mean([m.duration for m in first_half])
        second_avg_duration = statistics.mean([m.duration for m in second_half])
        
        first_success_rate = sum(m.success for m in first_half) / len(first_half)
        second_success_rate = sum(m.success for m in second_half) / len(second_half)
        
        duration_trend = "improving" if second_avg_duration < first_avg_duration else "degrading"
        success_trend = "improving" if second_success_rate > first_success_rate else "degrading"
        
        return {
            "duration_trend": duration_trend,
            "duration_change": second_avg_duration - first_avg_duration,
            "success_trend": success_trend,
            "success_change": second_success_rate - first_success_rate
        }
    
    def generate_health_status(self, agent_key: str, analysis: Dict[str, Any]) -> AgentHealth:
        """Generate health status based on analysis."""
        issues = []
        recommendations = []
        status = "healthy"
        
        # Check response time
        if analysis.get("avg_response_time", 0) > self.alert_thresholds["max_response_time"]:
            issues.append(f"Slow response time: {analysis['avg_response_time']:.2f}s")
            recommendations.append("Optimize agent configuration or model selection")
            status = "warning"
        
        # Check success rate
        if analysis.get("success_rate", 1) < self.alert_thresholds["min_success_rate"]:
            issues.append(f"Low success rate: {analysis['success_rate']:.1%}")
            recommendations.append("Review error logs and improve error handling")
            status = "critical"
        
        # Check error rate
        if analysis.get("error_rate", 0) > self.alert_thresholds["max_error_rate"]:
            issues.append(f"High error rate: {analysis['error_rate']:.1%}")
            recommendations.append("Investigate common error patterns")
            if status != "critical":
                status = "warning"
        
        # Check quality score
        if analysis.get("avg_quality_score", 1) < self.alert_thresholds["min_quality_score"]:
            issues.append(f"Low quality score: {analysis['avg_quality_score']:.2f}")
            recommendations.append("Review agent prompts and improve training data")
            if status == "healthy":
                status = "warning"
        
        # Check trends
        trends = analysis.get("trends", {})
        if trends.get("duration_trend") == "degrading":
            issues.append("Performance degrading over time")
            recommendations.append("Monitor for resource constraints or model drift")
        
        if trends.get("success_trend") == "degrading":
            issues.append("Success rate declining over time")
            recommendations.append("Review recent changes and rollback if necessary")
        
        return AgentHealth(
            agent_key=agent_key,
            status=status,
            success_rate=analysis.get("success_rate", 0),
            avg_response_time=analysis.get("avg_response_time", 0),
            error_rate=analysis.get("error_rate", 0),
            issues=issues,
            recommendations=recommendations
        )


class AgentMetrics:
    """Main metrics collection and analysis system."""
    
    def __init__(self, max_metrics: int = 10000):
        self.aggregator = MetricAggregator(max_metrics)
        self.analyzer = PerformanceAnalyzer()
        self._lock = threading.Lock()
        self.start_time = time.time()
    
    def record_execution(
        self,
        agent_key: str,
        duration: float,
        success: bool,
        input_text: str = "",
        output_text: str = "",
        tools_used: List[str] = None,
        error_type: str = None,
        error_message: str = None,
        quality_score: float = None,
        memory_usage: float = None
    ):
        """Record a single agent execution."""
        metric = ExecutionMetric(
            agent_key=agent_key,
            timestamp=time.time(),
            duration=duration,
            success=success,
            input_length=len(input_text),
            output_length=len(output_text),
            tools_used=tools_used or [],
            error_type=error_type,
            error_message=error_message,
            quality_score=quality_score,
            memory_usage=memory_usage
        )
        
        with self._lock:
            self.aggregator.add_metric(metric)
    
    def get_agent_health(self, agent_key: str) -> AgentHealth:
        """Get current health status for an agent."""
        with self._lock:
            metrics = self.aggregator.get_agent_metrics(agent_key, timedelta(hours=24))
        
        if not metrics:
            return AgentHealth(
                agent_key=agent_key,
                status="no_data",
                success_rate=0,
                avg_response_time=0,
                error_rate=0,
                issues=["No recent execution data"],
                recommendations=["Execute some tasks to generate metrics"]
            )
        
        analysis = self.analyzer.analyze_agent_performance(metrics)
        health = self.analyzer.generate_health_status(agent_key, analysis)
        health.last_execution = metrics[-1].datetime
        
        return health
    
    def get_system_overview(self) -> Dict[str, Any]:
        """Get overall system metrics overview."""
        with self._lock:
            all_metrics = self.aggregator.get_all_metrics(timedelta(hours=24))
        
        if not all_metrics:
            return {"status": "no_data", "message": "No metrics available"}
        
        # Agent breakdown
        agents = set(m.agent_key for m in all_metrics)
        agent_summaries = {}
        
        for agent_key in agents:
            agent_metrics = [m for m in all_metrics if m.agent_key == agent_key]
            analysis = self.analyzer.analyze_agent_performance(agent_metrics)
            health = self.analyzer.generate_health_status(agent_key, analysis)
            
            agent_summaries[agent_key] = {
                "executions": len(agent_metrics),
                "success_rate": analysis["success_rate"],
                "avg_response_time": analysis["avg_response_time"],
                "status": health.status,
                "last_execution": agent_metrics[-1].datetime.isoformat()
            }
        
        # Overall statistics
        total_executions = len(all_metrics)
        successful_executions = sum(1 for m in all_metrics if m.success)
        avg_response_time = statistics.mean([m.duration for m in all_metrics])
        
        # Tool usage across all agents
        all_tools = [tool for m in all_metrics for tool in m.tools_used]
        tool_usage = defaultdict(int)
        for tool in all_tools:
            tool_usage[tool] += 1
        
        return {
            "uptime_hours": (time.time() - self.start_time) / 3600,
            "total_executions": total_executions,
            "successful_executions": successful_executions,
            "overall_success_rate": successful_executions / total_executions,
            "average_response_time": avg_response_time,
            "active_agents": len(agents),
            "agents": agent_summaries,
            "top_tools": sorted(tool_usage.items(), key=lambda x: x[1], reverse=True)[:10],
            "health_summary": {
                "healthy": sum(1 for a in agent_summaries.values() if a["status"] == "healthy"),
                "warning": sum(1 for a in agent_summaries.values() if a["status"] == "warning"),
                "critical": sum(1 for a in agent_summaries.values() if a["status"] == "critical")
            }
        }
    
    def get_detailed_analysis(self, agent_key: str, hours: int = 24) -> Dict[str, Any]:
        """Get detailed analysis for a specific agent."""
        with self._lock:
            metrics = self.aggregator.get_agent_metrics(agent_key, timedelta(hours=hours))
        
        if not metrics:
            return {"error": "No metrics found for agent"}
        
        analysis = self.analyzer.analyze_agent_performance(metrics)
        health = self.analyzer.generate_health_status(agent_key, analysis)
        
        # Time series data for charts
        time_series = []
        for metric in sorted(metrics, key=lambda m: m.timestamp):
            time_series.append({
                "timestamp": metric.datetime.isoformat(),
                "duration": metric.duration,
                "success": metric.success,
                "quality_score": metric.quality_score,
                "tools_count": len(metric.tools_used)
            })
        
        return {
            "agent_key": agent_key,
            "analysis_period_hours": hours,
            "performance_analysis": analysis,
            "health_status": health.__dict__,
            "time_series": time_series,
            "recommendations": self._generate_optimization_recommendations(analysis)
        }
    
    def _generate_optimization_recommendations(self, analysis: Dict[str, Any]) -> List[str]:
        """Generate optimization recommendations based on analysis."""
        recommendations = []
        
        # Performance recommendations
        if analysis.get("avg_response_time", 0) > 15:
            recommendations.append("Consider using a faster model or optimizing prompts")
        
        if analysis.get("response_time_std", 0) > 10:
            recommendations.append("High response time variance - investigate inconsistent performance")
        
        # Quality recommendations
        avg_quality = analysis.get("avg_quality_score", 1)
        if avg_quality < 0.8:
            recommendations.append("Low quality scores - review and improve agent instructions")
        
        # Tool usage recommendations
        tool_usage = analysis.get("tool_usage", {})
        if len(tool_usage) < 2:
            recommendations.append("Limited tool usage - consider expanding agent capabilities")
        
        # Error recommendations
        error_rate = analysis.get("error_rate", 0)
        if error_rate > 0.1:
            recommendations.append("High error rate - implement better error handling and validation")
        
        # Trend recommendations
        trends = analysis.get("trends", {})
        if trends.get("duration_trend") == "degrading":
            recommendations.append("Performance degrading - monitor for resource constraints")
        
        return recommendations
    
    def export_metrics(self, agent_key: Optional[str] = None, hours: int = 24) -> List[Dict[str, Any]]:
        """Export metrics for external analysis."""
        with self._lock:
            if agent_key:
                metrics = self.aggregator.get_agent_metrics(agent_key, timedelta(hours=hours))
            else:
                metrics = self.aggregator.get_all_metrics(timedelta(hours=hours))
        
        return [
            {
                "agent_key": m.agent_key,
                "timestamp": m.datetime.isoformat(),
                "duration": m.duration,
                "success": m.success,
                "input_length": m.input_length,
                "output_length": m.output_length,
                "tools_used": m.tools_used,
                "error_type": m.error_type,
                "quality_score": m.quality_score,
                "memory_usage": m.memory_usage
            }
            for m in metrics
        ]
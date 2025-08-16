"""
Guardrails system for validating agent inputs and outputs.
Provides security, safety, and quality controls for agent interactions.
"""

import re
import os
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass


class ValidationError(Exception):
    """Raised when guardrail validation fails."""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}


@dataclass
class ValidationResult:
    """Result of guardrail validation."""
    
    is_valid: bool
    message: str = ""
    modified_content: Optional[str] = None
    details: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.details is None:
            self.details = {}


class Guardrail(ABC):
    """Abstract base class for all guardrails."""
    
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
    
    @abstractmethod
    async def validate_input(self, content: str, context: Dict[str, Any] = None) -> ValidationResult:
        """Validate input content before processing."""
        pass
    
    @abstractmethod
    async def validate_output(self, content: str, context: Dict[str, Any] = None) -> ValidationResult:
        """Validate output content after processing."""
        pass


class InputValidationGuardrail(Guardrail):
    """Validates basic input requirements."""
    
    def __init__(self, max_length: int = 50000, min_length: int = 1):
        super().__init__("input_validation", "Validates basic input requirements")
        self.max_length = max_length
        self.min_length = min_length
    
    async def validate_input(self, content: str, context: Dict[str, Any] = None) -> ValidationResult:
        content = content or ""
        
        if len(content.strip()) < self.min_length:
            return ValidationResult(
                is_valid=False,
                message="Input is too short or empty",
                details={"length": len(content), "min_required": self.min_length}
            )
        
        if len(content) > self.max_length:
            return ValidationResult(
                is_valid=False,
                message="Input exceeds maximum length",
                details={"length": len(content), "max_allowed": self.max_length}
            )
        
        return ValidationResult(is_valid=True, message="Input validation passed")
    
    async def validate_output(self, content: str, context: Dict[str, Any] = None) -> ValidationResult:
        return ValidationResult(is_valid=True, message="No output validation required")


class PathSafetyGuardrail(Guardrail):
    """Ensures file paths are safe and within allowed directories."""
    
    def __init__(self, allowed_paths: List[str] = None, blocked_patterns: List[str] = None):
        super().__init__("path_safety", "Validates file path safety")
        self.allowed_paths = allowed_paths or ["."]
        self.blocked_patterns = blocked_patterns or [
            r"\.\.\/",  # Directory traversal
            r"\/etc\/",  # System directories
            r"\/proc\/",
            r"\/sys\/",
            r"\/dev\/",
            r"C:\\Windows\\",  # Windows system directories
            r"C:\\Program Files\\",
        ]
    
    async def validate_input(self, content: str, context: Dict[str, Any] = None) -> ValidationResult:
        # Extract potential file paths from input
        paths = self._extract_paths(content)
        
        for path in paths:
            if not self._is_path_safe(path):
                return ValidationResult(
                    is_valid=False,
                    message=f"Unsafe file path detected: {path}",
                    details={"unsafe_path": path}
                )
        
        return ValidationResult(is_valid=True, message="Path safety validation passed")
    
    async def validate_output(self, content: str, context: Dict[str, Any] = None) -> ValidationResult:
        return await self.validate_input(content, context)
    
    def _extract_paths(self, content: str) -> List[str]:
        """Extract potential file paths from content."""
        # Simple regex to find file-like paths
        path_patterns = [
            r'(?:[./][\w./\\-]+)',  # Relative paths
            r'(?:[A-Za-z]:[\w./\\-]+)',  # Windows absolute paths
            r'(?:/[\w./\\-]+)',  # Unix absolute paths
        ]
        
        paths = []
        for pattern in path_patterns:
            paths.extend(re.findall(pattern, content))
        
        return paths
    
    def _is_path_safe(self, path: str) -> bool:
        """Check if a path is safe to access."""
        # Check blocked patterns
        for pattern in self.blocked_patterns:
            if re.search(pattern, path, re.IGNORECASE):
                return False
        
        # Check if path is within allowed directories
        try:
            abs_path = os.path.abspath(path)
            for allowed in self.allowed_paths:
                allowed_abs = os.path.abspath(allowed)
                if abs_path.startswith(allowed_abs):
                    return True
        except Exception:
            return False
        
        return False


class CodeSafetyGuardrail(Guardrail):
    """Validates code content for safety."""
    
    def __init__(self):
        super().__init__("code_safety", "Validates code for potentially dangerous operations")
        self.dangerous_patterns = [
            r'eval\s*\(',
            r'exec\s*\(',
            r'subprocess\.',
            r'os\.system',
            r'rm\s+-rf',
            r'del\s+/[fqsh]',
            r'format\s+[a-z]:',  # Windows format command
            r'__import__',
            r'open\s*\(.+["\']w["\']',  # Writing to files (might be too restrictive)
        ]
    
    async def validate_input(self, content: str, context: Dict[str, Any] = None) -> ValidationResult:
        return await self._check_code_safety(content)
    
    async def validate_output(self, content: str, context: Dict[str, Any] = None) -> ValidationResult:
        return await self._check_code_safety(content)
    
    async def _check_code_safety(self, content: str) -> ValidationResult:
        for pattern in self.dangerous_patterns:
            matches = re.findall(pattern, content, re.IGNORECASE)
            if matches:
                return ValidationResult(
                    is_valid=False,
                    message=f"Potentially dangerous code pattern detected",
                    details={"pattern": pattern, "matches": matches}
                )
        
        return ValidationResult(is_valid=True, message="Code safety validation passed")


class OutputSanitizationGuardrail(Guardrail):
    """Sanitizes output to remove sensitive information."""
    
    def __init__(self):
        super().__init__("output_sanitization", "Removes sensitive information from output")
        self.sensitive_patterns = [
            (r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[EMAIL_REDACTED]'),  # Email
            (r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b', '[CARD_REDACTED]'),  # Credit card
            (r'\b(?:\d{3}-\d{2}-\d{4}|\d{9})\b', '[SSN_REDACTED]'),  # SSN
            (r'(?:password|passwd|pwd)[\s:=]+[\w!@#$%^&*]+', '[PASSWORD_REDACTED]'),  # Passwords
            (r'(?:api_key|apikey|token)[\s:=]+[\w-]+', '[API_KEY_REDACTED]'),  # API keys
        ]
    
    async def validate_input(self, content: str, context: Dict[str, Any] = None) -> ValidationResult:
        return ValidationResult(is_valid=True, message="No input sanitization required")
    
    async def validate_output(self, content: str, context: Dict[str, Any] = None) -> ValidationResult:
        sanitized_content = content
        redactions_made = []
        
        for pattern, replacement in self.sensitive_patterns:
            matches = re.findall(pattern, sanitized_content, re.IGNORECASE)
            if matches:
                sanitized_content = re.sub(pattern, replacement, sanitized_content, flags=re.IGNORECASE)
                redactions_made.extend(matches)
        
        if redactions_made:
            return ValidationResult(
                is_valid=True,
                message="Output sanitized - sensitive information redacted",
                modified_content=sanitized_content,
                details={"redactions_count": len(redactions_made)}
            )
        
        return ValidationResult(is_valid=True, message="No sensitive information found")


class TaskValidationGuardrail(Guardrail):
    """Validates that tasks are appropriate and within scope."""
    
    def __init__(self, allowed_task_types: List[str] = None):
        super().__init__("task_validation", "Validates task appropriateness")
        self.allowed_task_types = allowed_task_types or [
            "file_operations", "code_analysis", "data_processing", 
            "documentation", "testing", "git_operations"
        ]
        self.blocked_keywords = [
            "hack", "crack", "exploit", "malware", "virus",
            "illegal", "piracy", "fraud", "spam"
        ]
    
    async def validate_input(self, content: str, context: Dict[str, Any] = None) -> ValidationResult:
        content_lower = content.lower()
        
        # Check for blocked keywords
        for keyword in self.blocked_keywords:
            if keyword in content_lower:
                return ValidationResult(
                    is_valid=False,
                    message=f"Task contains inappropriate content: {keyword}",
                    details={"blocked_keyword": keyword}
                )
        
        return ValidationResult(is_valid=True, message="Task validation passed")
    
    async def validate_output(self, content: str, context: Dict[str, Any] = None) -> ValidationResult:
        return ValidationResult(is_valid=True, message="No output task validation required")


class GuardrailManager:
    """Manages and executes guardrails for agents."""
    
    def __init__(self):
        self._guardrails: Dict[str, Guardrail] = {}
        self._initialize_default_guardrails()
    
    def _initialize_default_guardrails(self):
        """Initialize built-in guardrails."""
        self.register_guardrail(InputValidationGuardrail())
        self.register_guardrail(PathSafetyGuardrail())
        self.register_guardrail(CodeSafetyGuardrail())
        self.register_guardrail(OutputSanitizationGuardrail())
        self.register_guardrail(TaskValidationGuardrail())
    
    def register_guardrail(self, guardrail: Guardrail):
        """Register a new guardrail."""
        self._guardrails[guardrail.name] = guardrail
    
    def get_guardrail(self, name: str) -> Optional[Guardrail]:
        """Get guardrail by name."""
        return self._guardrails.get(name)
    
    async def validate_input(
        self, 
        content: str, 
        guardrail_names: List[str], 
        context: Dict[str, Any] = None
    ) -> Tuple[bool, List[ValidationResult]]:
        """Validate input through specified guardrails."""
        results = []
        all_valid = True
        
        for name in guardrail_names:
            guardrail = self.get_guardrail(name)
            if not guardrail:
                continue
            
            try:
                result = await guardrail.validate_input(content, context)
                results.append(result)
                if not result.is_valid:
                    all_valid = False
            except Exception as e:
                results.append(ValidationResult(
                    is_valid=False,
                    message=f"Guardrail '{name}' failed with error: {str(e)}",
                    details={"error": str(e)}
                ))
                all_valid = False
        
        return all_valid, results
    
    async def validate_output(
        self, 
        content: str, 
        guardrail_names: List[str], 
        context: Dict[str, Any] = None
    ) -> Tuple[str, List[ValidationResult]]:
        """Validate and potentially modify output through specified guardrails."""
        results = []
        modified_content = content
        
        for name in guardrail_names:
            guardrail = self.get_guardrail(name)
            if not guardrail:
                continue
            
            try:
                result = await guardrail.validate_output(modified_content, context)
                results.append(result)
                
                # Apply content modifications if any
                if result.modified_content is not None:
                    modified_content = result.modified_content
                    
            except Exception as e:
                results.append(ValidationResult(
                    is_valid=False,
                    message=f"Guardrail '{name}' failed with error: {str(e)}",
                    details={"error": str(e)}
                ))
        
        return modified_content, results
    
    def list_guardrails(self) -> Dict[str, str]:
        """List all available guardrails."""
        return {name: guardrail.description for name, guardrail in self._guardrails.items()}
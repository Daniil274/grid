"""Safe evaluator for declarative condition predicates."""

from __future__ import annotations

from typing import Any, Dict

from schemas.system_platform import ConditionOperator, ConditionPredicate, ValueRef


class ConditionEvaluationError(ValueError):
    """Raised when a condition cannot be safely evaluated."""


class ConditionEvaluator:
    """Evaluate allowlisted predicate ASTs against typed runtime context."""

    ALLOWED_ROOTS = ("input", "state", "node_output", "context")

    def evaluate(self, predicate: ConditionPredicate, runtime_context: Dict[str, Any]) -> bool:
        op = predicate.op
        if op == ConditionOperator.AND:
            return all(self.evaluate(arg, runtime_context) for arg in predicate.args)
        if op == ConditionOperator.OR:
            return any(self.evaluate(arg, runtime_context) for arg in predicate.args)
        if op == ConditionOperator.NOT:
            return not self.evaluate(predicate.args[0], runtime_context)
        if op == ConditionOperator.EXISTS:
            return self._resolve_value(predicate.left, runtime_context, allow_missing=True)[1]

        left = self._resolve_value(predicate.left, runtime_context)[0]
        right = self._resolve_value(predicate.right, runtime_context)[0]
        if op == ConditionOperator.EQ:
            return left == right
        if op == ConditionOperator.NEQ:
            return left != right
        if op == ConditionOperator.IN:
            return left in right
        if op == ConditionOperator.NOT_IN:
            return left not in right
        if op == ConditionOperator.GT:
            return left > right
        if op == ConditionOperator.GTE:
            return left >= right
        if op == ConditionOperator.LT:
            return left < right
        if op == ConditionOperator.LTE:
            return left <= right
        raise ConditionEvaluationError(f"Unsupported operator: {op}")

    def _resolve_value(
        self,
        value_ref: ValueRef | None,
        runtime_context: Dict[str, Any],
        *,
        allow_missing: bool = False,
    ) -> tuple[Any, bool]:
        if value_ref is None:
            raise ConditionEvaluationError("Condition operand is required")
        if value_ref.var:
            return self._resolve_var(value_ref.var, runtime_context, allow_missing=allow_missing)
        return value_ref.value, True

    def _resolve_var(
        self,
        path: str,
        runtime_context: Dict[str, Any],
        *,
        allow_missing: bool = False,
    ) -> tuple[Any, bool]:
        parts = path.split(".")
        if not parts or parts[0] not in self.ALLOWED_ROOTS:
            raise ConditionEvaluationError(f"Variable path '{path}' uses a disallowed root")

        current: Any = runtime_context
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
                continue
            if allow_missing:
                return None, False
            raise ConditionEvaluationError(f"Variable path '{path}' is missing at '{part}'")
        return current, True

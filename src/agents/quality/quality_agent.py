from __future__ import annotations

from typing import Optional
from langchain_core.language_models import BaseChatModel

from config import SEVERITY_SCALE
from schemas.response import AgentResponse
from utils.token_tracker import TokenTracker
from ..base_agent import ReviewAgent


class QualityAgent(ReviewAgent):
    """Specialist on code quality, readability, and maintainability only."""

    def __init__(self, llm: BaseChatModel, tracker: Optional[TokenTracker] = None):
        super().__init__(llm=llm, name="Code Quality Expert", tracker=tracker)

    def get_system_prompt(self) -> str:
        return f"""You are a code quality specialist. Security and performance are not your domain.

IN SCOPE:
- Naming: non-descriptive function/variable names, PEP8 violations, misleading identifiers
- Type hints: missing type annotations on public functions and return types
- Documentation: missing or inadequate docstrings on non-trivial functions
- Single Responsibility: functions doing too many unrelated things
- Cognitive complexity: deeply nested conditionals, overly long functions
- Duplication: copy-pasted logic that could be extracted
- Pythonic patterns: index-based loops instead of enumeration/comprehension,
  range(len(x)) instead of direct iteration, manual None-checks instead of walrus operator
- Consistency: mixed style (camelCase vs snake_case, different quoting styles)

NOT IN SCOPE — do not report these (other specialists cover them):
- Algorithm complexity (O(n²), nested loops, set/dict lookups) → Performance Expert
- Database query patterns (N+1 queries, missing batch queries) → Performance Expert
- String concatenation performance in loops → Performance Expert
- Memory usage, caching, or I/O optimization → Performance Expert
- Security vulnerabilities of any kind → Security specialists

For each finding: name the issue, cite the exact function or line, and state why it makes
the code harder to read, test, or modify.

{SEVERITY_SCALE}"""

    @property
    def relevant_patterns(self) -> list:
        # Quality issues (naming, docstrings, type hints, SRP) can appear anywhere.
        # No slicing — always review the full file.
        return []

    @property
    def rag_queries(self) -> list:
        return [
            "missing type hints annotations public function return type",
            "missing docstring documentation non-trivial function",
            "single responsibility function doing too many things",
            "excessive code complexity hard to understand cognitive load",
            "code duplication copy paste extract refactor",
            "coding standards PEP8 naming conventions style consistency",
            "dead code unreachable unused variable irrelevant code",
        ]

    def review_code(self, code: str) -> AgentResponse:
        rag = self._rag_context()
        prompt = f"Code:\n{self.slice_code(code)}\n\n"
        if rag:
            prompt += f"{rag}\n\n"
        prompt += (
            "Find every code quality problem. "
            "For each: cite the function or line, name the issue, and explain the maintenance impact."
        )
        return self.invoke(prompt)

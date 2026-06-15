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

IN SCOPE (only report if it genuinely impacts readability, testability, or maintainability):
- Naming: misleading or dangerously ambiguous names that could cause bugs — NOT minor style preferences
- Type hints: missing type annotations on public/exported functions with non-obvious signatures
- Documentation: missing docstrings on complex non-trivial functions (>10 lines, non-obvious behavior)
- Single Responsibility: functions doing 3+ clearly unrelated things
- Cognitive complexity: deeply nested conditionals (4+ levels) or functions exceeding ~60 lines
- Duplication: copy-pasted logic that is identical in 3+ places and would be a bug multiplier
- Pythonic patterns: only when the anti-pattern makes the code materially harder to understand

NOT IN SCOPE — do not report these:
- Algorithm complexity, N+1 queries, performance → Performance Expert
- Security vulnerabilities of any kind → Security specialists
- Missing docstrings on: simple getters/setters, private helpers under 10 lines, test functions
- Missing type hints on: private helpers, test functions, trivially obvious signatures
- Minor formatting preferences: quote style, blank lines, trailing whitespace
- Single-character variable names in short loops (e.g. `for i in range(n)`) — acceptable
- Anything that would score below 20 on any reasonable quality rubric

SEVERITY CALIBRATION FOR QUALITY (do not exceed these caps):
- Naming, style, minor Pythonic patterns: max 20
- Missing type hints or docstrings on non-trivial public functions: max 30
- Single Responsibility violation or high cognitive complexity: max 50
- Never exceed 55 for quality issues — security and performance agents handle higher severity

For each finding: cite the exact function or line, name the issue, and explain why it makes
the code harder to read, test, or maintain.

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

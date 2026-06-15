from __future__ import annotations

from typing import Optional
from langchain_core.language_models import BaseChatModel

from config import SEVERITY_SCALE
from schemas.response import AgentResponse
from utils.token_tracker import TokenTracker
from ..base_agent import ReviewAgent


class PerformanceAgent(ReviewAgent):
    """Specialist on performance and algorithmic complexity only."""

    def __init__(self, llm: BaseChatModel, tracker: Optional[TokenTracker] = None):
        super().__init__(llm=llm, name="Performance Expert", tracker=tracker)

    def get_system_prompt(self) -> str:
        return f"""You are a performance and algorithmic complexity specialist.

SCOPE:
- Algorithmic complexity: O(n²) or worse when O(n) or O(n log n) is achievable
- N+1 query problems: database or API calls inside loops
- Inefficient data structures: list membership check (x in list) instead of set/dict O(1) lookup
- Redundant computation: recalculating the same expensive value repeatedly in a loop
- Memory waste: loading entire large datasets into memory when streaming/pagination would suffice
- Missing caching for proven-expensive repeated operations
- Unnecessary iterations or passes over the same data

DO NOT FLAG these (they are fine in practice):
- Simple for/while loops over small or fixed-size collections — NOT a performance issue
- range(len(x)) — only flag if the loop body itself is O(n), making the whole thing O(n²)
- f-strings or string formatting — these are fast in modern Python, NOT performance issues
- list.append() in a loop that builds a result list — correct Python pattern, NOT an issue
- Sorting a list once — O(n log n), acceptable unless inside another loop
- Any issue where the realistic input size would never cause a measurable slowdown

Only report issues that would cause a real slowdown at scales the code is likely to encounter.
For each finding: state the current complexity, the achievable complexity, the realistic input
scale at which it becomes a problem (e.g. "slows noticeably at n>10,000 records"), and the fix.

Security, secrets, and quality are handled by other agents — ignore them entirely.

{SEVERITY_SCALE}"""

    @property
    def relevant_patterns(self) -> list:
        return [
            "for ", "while ",
            "range(len",           # classic O(n) anti-pattern: range(len(lst))
            "execute", "fetchone", "fetchall", "sqlite3", "cursor",
            "append(", " += ",     # string concat or list growth in loops
            ".join(", "sorted(",
        ]

    @property
    def rag_queries(self) -> list:
        return [
            "N+1 query database call inside loop inefficient",
            "O(n squared) nested loop quadratic complexity inefficient",
            "string concatenation loop performance list join",
            "list membership check O(n) should use set dict O(1)",
            "regular expression exponential backtracking user input performance",
            "unbounded resource allocation memory limits pagination streaming",
        ]

    def review_code(self, code: str) -> AgentResponse:
        rag = self._rag_context()
        prompt = f"Code:\n{self.slice_code(code)}\n\n"
        if rag:
            prompt += f"{rag}\n\n"
        prompt += (
            "Find every performance issue. "
            "For each: state current vs optimal complexity, scale at which it matters, "
            "and the fix approach."
        )
        return self.invoke(prompt)

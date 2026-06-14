from __future__ import annotations

import concurrent.futures
import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional, Type

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from config import LLM_TIMEOUT, MAX_RETRIES
from schemas.response import AgentResponse
from schemas.fix_response import FixResponse
from utils.token_tracker import TokenTracker
from utils.code_splitter import split_code, slice_for_agent
from utils.rag_store import RagStore

logger = logging.getLogger(__name__)

_RATE_LIMIT_BASE_DELAY = 2.0   # seconds — doubles each retry
_RATE_LIMIT_MAX_DELAY  = 60.0  # seconds — cap


def _call_with_timeout(fn: Callable, timeout_seconds: int) -> Any:
    """
    Run fn() in a background thread and return its result.
    Raises TimeoutError if the call has not completed within timeout_seconds.

    Note: Python threads are not interruptible — the background thread continues
    running after the timeout, but the caller proceeds immediately.  This is
    acceptable: the LLM provider will eventually close the connection and the
    thread will exit on its own.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"LLM call timed out after {timeout_seconds}s")


def _is_rate_limit(exc: Exception) -> bool:
    """Return True if the exception is an HTTP 429 rate-limit response."""
    msg = str(exc).lower()
    return (
        "429" in msg
        or "rate limit" in msg
        or "ratelimit" in msg
        or type(exc).__name__ == "RateLimitError"
    )


class BaseAgent(ABC):
    """
    Core foundation shared by all agents.
    Provides LLM invocation with structured output and retry logic.
    Subclasses must implement get_system_prompt().
    """

    def __init__(self, llm: BaseChatModel, name: str, tracker: Optional[TokenTracker] = None):
        self.llm = llm
        self.name = name
        self.tracker = tracker

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Return the agent's system prompt defining its exclusive focus area."""

    def invoke(
        self,
        user_prompt: str,
        max_retries: int = MAX_RETRIES,
        response_schema: Optional[Type[BaseModel]] = None,
        _llm: Optional[BaseChatModel] = None,
        _system_prompt: Optional[str] = None,
    ) -> Any:
        schema = response_schema or AgentResponse
        using_default_prompt = _system_prompt is None
        sys_prompt = self.get_system_prompt() if using_default_prompt else _system_prompt

        # Review agents receive a git diff. Tell them which lines to focus on.
        # Fix agents always supply _system_prompt, so this block is skipped for them.
        diff_note = (
            "\nDIFF REVIEW RULES: The code below may be a git diff. "
            "ONLY flag issues in lines starting with '+' (newly added code). "
            "Lines starting with '-' are removed — ignore them completely. "
            "Lines with no prefix are unchanged context — use for understanding only.\n"
            if using_default_prompt else ""
        )

        full_prompt = (
            f"{sys_prompt}{diff_note}\n\n"
            f"{user_prompt}\n\n"
            f"Output ONLY the structured fields — no XML tags, no markdown, no extra text.\n"
            f"Each finding MUST start with the function name and a colon, "
            f"e.g. 'login(): SQL injection via f-string'.\n"
            f"For `locations`, list every affected function name (bare name, no parentheses), "
            f"e.g. ['login', 'get_user']."
        )
        callbacks = [self.tracker] if self.tracker else []
        effective_llm = _llm or self.llm
        structured_llm = effective_llm.with_structured_output(schema, method="function_calling")
        last_error: Exception = Exception("max_retries must be >= 1")

        for attempt in range(1, max(max_retries, 1) + 1):
            try:
                return _call_with_timeout(
                    lambda: structured_llm.invoke(full_prompt, config={"callbacks": callbacks}),
                    LLM_TIMEOUT,
                )
            except Exception as exc:
                last_error = exc
                if attempt >= max_retries:
                    break
                if isinstance(exc, TimeoutError):
                    logger.warning(
                        "[%s] Attempt %d timed out after %ds — retrying...",
                        self.name, attempt, LLM_TIMEOUT,
                    )
                elif _is_rate_limit(exc):
                    delay = min(
                        _RATE_LIMIT_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 1),
                        _RATE_LIMIT_MAX_DELAY,
                    )
                    logger.warning(
                        "[%s] Attempt %d rate-limited (429) — waiting %.1fs before retry...",
                        self.name, attempt, delay,
                    )
                    time.sleep(delay)
                else:
                    logger.warning(
                        "[%s] Attempt %d failed: %s — retrying...", self.name, attempt, exc
                    )

        logger.error("[%s] All %d attempts failed: %s", self.name, max_retries, last_error)

        if schema is FixResponse:
            return FixResponse(
                fixed_code="",
                changes=[],
                unfixable=[f"Fix generation failed after {max_retries} attempts: {last_error}"],
            )
        return AgentResponse(
            reasoning=f"Agent unavailable after {max_retries} attempts: {last_error}",
            findings=[],
            severity=0,
            confidence=0,
        )

    def __str__(self) -> str:
        return f"{self.name} (using {self.llm.__class__.__name__})"


class ReviewAgent(BaseAgent, ABC):
    """
    Base class for all specialist review agents.
    Enforces the review_code() contract so every review agent is interchangeable.

    Subclasses declare relevant_patterns to enable code slicing: only sections
    whose source matches at least one pattern are sent to the LLM, reducing
    tokens and noise. The header is always included.
    """

    @property
    def relevant_patterns(self) -> list:
        """Keyword patterns identifying sections relevant to this agent's domain.

        Override in subclasses. Empty list = no slicing (full file sent).
        Patterns are matched case-insensitively against each section's source.
        """
        return []

    def slice_code(self, code: str) -> str:
        """Return only the source sections relevant to this agent.

        Falls back to the full file if nothing matches or patterns is empty.
        Logs which functions were kept vs. dropped at INFO level.
        """
        patterns = self.relevant_patterns
        if not patterns:
            return code

        sections = split_code(code)
        sliced = slice_for_agent(sections, patterns)

        all_funcs = [s.name for s in sections if s.section_type in ("function", "class")]
        kept_funcs = [s.name for s in sliced if s.section_type in ("function", "class")]

        if len(kept_funcs) < len(all_funcs):
            dropped = [f for f in all_funcs if f not in kept_funcs]
            logger.info(
                "[%s] Slice: %d → %d functions  kept=%s  dropped=%s",
                self.name,
                len(all_funcs),
                len(kept_funcs),
                kept_funcs,
                dropped,
            )

        return "".join(s.source for s in sliced)

    @property
    def rag_queries(self) -> list:
        """Semantic search phrases for pre-analysis knowledge injection.

        Override in subclasses with 3-6 short phrases that describe what this
        agent looks for.  The RAG store is queried with each phrase before the
        LLM sees the code, and the matching vulnerability patterns are injected
        into the review prompt so the agent knows exactly what to look for and
        how to describe it.

        Empty list = no RAG injection (default).
        """
        return []

    def _rag_context(self) -> str:
        """
        Query the knowledge store with this agent's rag_queries and return a
        formatted block of vulnerability patterns to inject into the prompt.

        Returns "" if rag_queries is empty, the store is unavailable, or no
        entries match within the similarity threshold.  Failures are logged at
        WARNING level and never propagate — RAG must never block a review.
        """
        queries = self.rag_queries
        if not queries:
            return ""
        try:
            store = RagStore.load()
            seen_ids: set = set()
            entries = []
            for q in queries:
                for entry in store.query(q, n_results=2):
                    if entry.id not in seen_ids:
                        seen_ids.add(entry.id)
                        entries.append(entry)
            if not entries:
                return ""
            lines = ["KNOWN VULNERABILITY PATTERNS FOR THIS DOMAIN (use these as a reference):"]
            for e in entries:
                lines.append(f"\n  [{e.label()}]")
                lines.append(f"  {e.description}")
                lines.append(f"  → Fix: {e.remediation}")
            return "\n".join(lines)
        except Exception as exc:
            logger.warning("[%s] RAG context unavailable: %s", self.name, exc)
            return ""

    @abstractmethod
    def review_code(self, code: str) -> AgentResponse:
        """Analyse code within this agent's exclusive domain and return findings."""

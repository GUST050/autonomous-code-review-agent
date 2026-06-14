"""
Tests for agent infrastructure — _rag_context() error handling
and completeness of all five review agents.
"""
import pytest
from unittest.mock import MagicMock, patch

from agents.base_agent import ReviewAgent
from agents.security.injection_agent import InjectionAgent
from agents.security.auth_agent import AuthAgent
from agents.security.secrets_agent import SecretsAgent
from agents.performance.performance_agent import PerformanceAgent
from agents.quality.quality_agent import QualityAgent
from utils.rag_store import KbEntry


# ── Minimal concrete agents for testing base behaviour ────────────────────────

class _NoQueryAgent(ReviewAgent):
    def get_system_prompt(self): return "test"
    def review_code(self, code): pass


class _WithQueryAgent(ReviewAgent):
    def get_system_prompt(self): return "test"
    def review_code(self, code): pass

    @property
    def rag_queries(self):
        return ["sql injection query", "second query"]


# ── _rag_context ──────────────────────────────────────────────────────────────

class TestRagContext:
    def test_returns_empty_string_when_rag_queries_is_empty(self):
        agent = _NoQueryAgent(llm=MagicMock(), name="test")
        assert agent._rag_context() == ""

    def test_returns_empty_string_on_store_load_failure(self):
        agent = _WithQueryAgent(llm=MagicMock(), name="test")
        with patch("agents.base_agent.RagStore.load",
                   side_effect=Exception("ChromaDB unavailable")):
            assert agent._rag_context() == ""

    def test_returns_empty_string_when_no_kb_matches(self):
        agent = _WithQueryAgent(llm=MagicMock(), name="test")
        mock_store = MagicMock()
        mock_store.query.return_value = []
        with patch("agents.base_agent.RagStore.load", return_value=mock_store):
            assert agent._rag_context() == ""

    def test_returns_formatted_block_when_matches_found(self):
        agent = _WithQueryAgent(llm=MagicMock(), name="test")
        entry = KbEntry(
            id="CWE-89", title="SQL Injection",
            description="SQL via string concatenation",
            owasp="A03:2021", cvss=9.8,
            remediation="Use parameterized queries",
        )
        mock_store = MagicMock()
        mock_store.query.return_value = [entry]
        with patch("agents.base_agent.RagStore.load", return_value=mock_store):
            result = agent._rag_context()
        assert "CWE-89" in result
        assert "SQL Injection" in result
        assert "parameterized queries" in result

    def test_deduplicates_same_entry_across_multiple_queries(self):
        agent = _WithQueryAgent(llm=MagicMock(), name="test")
        entry = KbEntry(
            id="CWE-89", title="SQL Injection",
            description="SQL via concat", owasp=None, cvss=9.8,
            remediation="Use params",
        )
        mock_store = MagicMock()
        mock_store.query.return_value = [entry]
        with patch("agents.base_agent.RagStore.load", return_value=mock_store):
            result = agent._rag_context()
        assert result.count("CWE-89") == 1


# ── Agent completeness ────────────────────────────────────────────────────────

ALL_AGENTS = [InjectionAgent, AuthAgent, SecretsAgent, PerformanceAgent, QualityAgent]


class TestAgentCompleteness:
    @pytest.fixture(params=ALL_AGENTS, ids=[a.__name__ for a in ALL_AGENTS])
    def agent(self, request):
        return request.param(llm=MagicMock())

    def test_has_non_empty_system_prompt(self, agent):
        prompt = agent.get_system_prompt()
        assert isinstance(prompt, str)
        assert len(prompt) > 50, f"{agent.__class__.__name__} system prompt too short"

    def test_has_rag_queries_defined(self, agent):
        queries = agent.rag_queries
        assert len(queries) >= 3, (
            f"{agent.__class__.__name__} has only {len(queries)} rag_queries — need at least 3"
        )

    def test_rag_queries_are_non_empty_strings(self, agent):
        for q in agent.rag_queries:
            assert isinstance(q, str) and q.strip(), (
                f"{agent.__class__.__name__} has blank rag query: {q!r}"
            )

    def test_system_prompt_contains_scope(self, agent):
        prompt = agent.get_system_prompt().upper()
        assert "SCOPE" in prompt or "IN SCOPE" in prompt or "SPECIALIST" in prompt, (
            f"{agent.__class__.__name__} system prompt has no SCOPE section"
        )

    def test_system_prompt_excludes_other_domains(self, agent):
        prompt_lower = agent.get_system_prompt().lower()
        exclusion_signals = [
            "other agents", "ignore", "not in scope",
            "handled by", "specialists", "do not report",
        ]
        assert any(s in prompt_lower for s in exclusion_signals), (
            f"{agent.__class__.__name__} doesn't tell the LLM to ignore other domains"
        )

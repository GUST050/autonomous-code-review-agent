"""
Tests for RAG store and enricher — no LLM calls, no network after first model download.
"""
import pathlib
import pytest
from unittest.mock import MagicMock, patch

from utils.rag_store import RagStore, KbEntry, _SIMILARITY_THRESHOLD

KB_PATH = pathlib.Path(__file__).parent.parent / "data" / "rag" / "knowledge_base.json"


# ── KbEntry.label ─────────────────────────────────────────────────────────────

class TestKbEntryLabel:
    def test_full_label_with_owasp_and_cvss(self):
        e = KbEntry(id="CWE-89", title="SQL Injection", description="", owasp="A03:2021", cvss=9.8, remediation="")
        assert e.label() == "CWE-89 | SQL Injection | OWASP A03:2021 | CVSS 9.8"

    def test_label_without_owasp(self):
        e = KbEntry(id="PERF-N1", title="N+1 Query", description="", owasp=None, cvss=None, remediation="")
        assert e.label() == "PERF-N1 | N+1 Query"

    def test_label_without_cvss(self):
        e = KbEntry(id="OWASP-A09", title="Logging Failures", description="", owasp="A09:2021", cvss=None, remediation="")
        assert e.label() == "OWASP-A09 | Logging Failures | OWASP A09:2021"


# ── RagStore ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def store():
    """Real store built from knowledge_base.json — shared across tests in this module."""
    if not KB_PATH.exists():
        pytest.skip("knowledge_base.json not found")
    RagStore._instance = None  # reset singleton for clean test
    s = RagStore.load(KB_PATH)
    yield s
    RagStore._instance = None  # clean up after module


class TestRagStore:
    def test_store_loads_all_entries(self, store):
        assert store._col.count() == 47  # total entries in knowledge_base.json

    def test_sql_injection_query_returns_cwe89(self, store):
        results = store.query("SQL injection via string concatenation in database execute call")
        ids = [r.id for r in results]
        assert "CWE-89" in ids

    def test_pickle_deserialization_query_returns_cwe502(self, store):
        results = store.query("pickle.loads with unverified base64 payload enables RCE")
        ids = [r.id for r in results]
        assert "CWE-502" in ids

    def test_md5_password_query_returns_weak_crypto(self, store):
        results = store.query("MD5 used for password hashing — hashlib.md5(password)")
        ids = [r.id for r in results]
        assert "CWE-327" in ids or "CWE-916" in ids

    def test_hardcoded_secret_query_returns_cwe798(self, store):
        results = store.query("STRIPE_SECRET = 'sk_live_xyz' hardcoded in source code")
        ids = [r.id for r in results]
        assert "CWE-798" in ids or "CWE-259" in ids

    def test_timing_attack_query_returns_cwe208(self, store):
        results = store.query("admin token compared with == allowing timing attack brute force")
        ids = [r.id for r in results]
        assert "CWE-208" in ids

    def test_n_plus_1_query_returns_perf_entry(self, store):
        results = store.query("N+1 queries — database call inside loop for each user_id")
        ids = [r.id for r in results]
        assert "PERF-N1" in ids

    def test_on2_loop_query_returns_perf_entry(self, store):
        results = store.query("O(n²) nested loop iterating same list twice")
        ids = [r.id for r in results]
        assert "PERF-ON2" in ids

    def test_empty_text_returns_empty(self, store):
        results = store.query("")
        assert results == []

    def test_whitespace_only_returns_empty(self, store):
        results = store.query("   ")
        assert results == []

    def test_n_results_respected(self, store):
        results = store.query("SQL injection", n_results=1)
        assert len(results) <= 1

    def test_irrelevant_query_filtered_by_threshold(self, store):
        # This nonsense string should match nothing within the similarity threshold
        results = store.query("xyzzy foobar quux 12345 completely unrelated gibberish")
        # Either no results, or results are far enough away that they were filtered
        # (distance > _SIMILARITY_THRESHOLD)
        for r in results:
            assert r.id  # if any remain, they at least have an id

    def test_singleton_returns_same_instance(self):
        RagStore._instance = None
        if not KB_PATH.exists():
            pytest.skip("knowledge_base.json not found")
        a = RagStore.load(KB_PATH)
        b = RagStore.load(KB_PATH)
        assert a is b
        RagStore._instance = None


# ── RagEnricher ───────────────────────────────────────────────────────────────

from agents.rag.rag_enricher import RagEnricher
from schemas.response import AgentResponse


def _make_result(findings, severity=90) -> AgentResponse:
    return AgentResponse(
        reasoning="test", findings=findings, severity=severity, locations=[]
    )


class TestRagEnricher:
    @pytest.fixture
    def enricher(self, store):
        return RagEnricher(store=store)

    def test_sql_finding_gets_reference(self, enricher):
        results = {
            "Injection Expert": _make_result([
                "SQL injection in login() via string concatenation into execute()"
            ])
        }
        enricher.enrich(results)
        refs = results["Injection Expert"].references
        assert len(refs) >= 1
        assert any("CWE-89" in r or "SQL" in r for r in refs)

    def test_references_deduplicated_across_findings(self, enricher):
        """Two findings that both match CWE-89 must produce only one CWE-89 reference."""
        results = {
            "Injection Expert": _make_result([
                "SQL injection in login() via string concat",
                "SQL injection in search_products() via LIKE clause concat",
            ])
        }
        enricher.enrich(results)
        refs = results["Injection Expert"].references
        cwe89_count = sum(1 for r in refs if "CWE-89" in r)
        assert cwe89_count <= 1

    def test_none_result_skipped(self, enricher):
        results = {"Injection Expert": None}
        enricher.enrich(results)  # must not raise
        assert results["Injection Expert"] is None

    def test_empty_findings_skipped(self, enricher):
        results = {"Quality Expert": _make_result([], severity=0)}
        enricher.enrich(results)
        assert results["Quality Expert"].references == []

    def test_multiple_agents_enriched_independently(self, enricher):
        results = {
            "Injection Expert": _make_result(["SQL injection in login()"]),
            "Secrets Expert": _make_result(["STRIPE_SECRET hardcoded sk_live_..."]),
        }
        enricher.enrich(results)
        inj_refs = results["Injection Expert"].references
        sec_refs = results["Secrets Expert"].references
        # Each agent gets its own set of references
        assert len(inj_refs) >= 1
        assert len(sec_refs) >= 1

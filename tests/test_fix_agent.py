"""
Tests for FixGeneratorAgent._build_findings_block — the pre-processing step
that groups findings by function before sending to the LLM.

This is pure logic — no LLM calls made.
"""
from unittest.mock import MagicMock

import pytest

from agents.fix.fix_agent import FixGeneratorAgent, _names_function
from schemas.response import AgentResponse
from utils.code_splitter import CodeSection


def _make_agent() -> FixGeneratorAgent:
    """Instantiate with a mock LLM — no API calls made."""
    return FixGeneratorAgent(llm=MagicMock())


def _make_result(findings, locations=None, severity=50) -> AgentResponse:
    return AgentResponse(
        reasoning="test",
        findings=findings,
        severity=severity,
        locations=locations or [],
    )


class TestBuildFindingsBlock:
    def test_empty_results_returns_no_findings_message(self):
        agent = _make_agent()
        block = agent._build_findings_block({})
        assert block == "No findings to fix."

    def test_all_none_results_returns_no_findings_message(self):
        agent = _make_agent()
        block = agent._build_findings_block({"Injection Expert": None})
        assert block == "No findings to fix."

    def test_findings_with_location_appear_under_function(self):
        agent = _make_agent()
        results = {
            "Injection Expert": _make_result(
                findings=["SQL injection via string concat"],
                locations=["login()"],
                severity=95,
            )
        }
        block = agent._build_findings_block(results)
        assert "login()" in block
        assert "SQL injection via string concat" in block
        assert "Injection Expert" in block

    def test_findings_without_location_go_to_global_section(self):
        agent = _make_agent()
        results = {
            "Secrets Expert": _make_result(
                findings=["Hardcoded DB_PASSWORD = 'admin123'"],
                locations=[],  # no specific function
                severity=90,
            )
        }
        block = agent._build_findings_block(results)
        assert "GLOBAL" in block
        assert "Hardcoded DB_PASSWORD" in block

    def test_multiple_agents_findings_grouped_under_same_function(self):
        """login() issues from two agents should appear together, not separately."""
        agent = _make_agent()
        results = {
            "Injection Expert": _make_result(
                findings=["SQL injection in login()"],
                locations=["login()"],
                severity=95,
            ),
            "Auth Expert": _make_result(
                findings=["No rate limiting on login()"],
                locations=["login()"],
                severity=92,
            ),
        }
        block = agent._build_findings_block(results)
        lines = block.split("\n")
        # login() should appear exactly once as a section header
        login_headers = [l for l in lines if l.startswith("login()")]
        assert len(login_headers) == 1

        # Both findings should be present
        assert "SQL injection" in block
        assert "No rate limiting" in block

    def test_functions_ordered_by_max_severity_descending(self):
        agent = _make_agent()
        results = {
            "Performance Expert": _make_result(
                findings=["O(n²) in find_admins()"],
                locations=["find_admins()"],
                severity=60,
            ),
            "Injection Expert": _make_result(
                findings=["SQL injection in login()"],
                locations=["login()"],
                severity=95,
            ),
        }
        block = agent._build_findings_block(results)
        login_pos = block.index("login()")
        find_admins_pos = block.index("find_admins()")
        # Higher severity function must appear first
        assert login_pos < find_admins_pos

    def test_severity_shown_in_block(self):
        agent = _make_agent()
        results = {
            "Injection Expert": _make_result(
                findings=["SQL injection"],
                locations=["login()"],
                severity=95,
            )
        }
        block = agent._build_findings_block(results)
        assert "95" in block

    def test_mixed_located_and_global_findings(self):
        agent = _make_agent()
        results = {
            "Injection Expert": _make_result(
                findings=["SQL injection in login()"],
                locations=["login()"],
                severity=95,
            ),
            "Secrets Expert": _make_result(
                findings=["Hardcoded API key at module level"],
                locations=[],
                severity=90,
            ),
        }
        block = agent._build_findings_block(results)
        assert "login()" in block
        assert "GLOBAL" in block
        assert "SQL injection" in block
        assert "Hardcoded API key" in block

    def test_result_with_empty_findings_skipped(self):
        agent = _make_agent()
        results = {
            "Performance Expert": _make_result(findings=[], severity=0),
            "Injection Expert": _make_result(
                findings=["SQL injection in login()"],
                locations=["login()"],
                severity=95,
            ),
        }
        block = agent._build_findings_block(results)
        assert "Performance Expert" not in block
        assert "SQL injection" in block


class TestNamesFunctionMatcher:
    """Tests for _names_function — the pattern-based function name matcher.

    Critical: single-letter names (e.g. 'd') must NOT match arbitrary English
    text. Only explicit structural patterns count.
    """

    def test_paren_pattern_matches(self):
        assert _names_function("login", "SQL injection in login() via concat")

    def test_single_quote_pattern_matches(self):
        assert _names_function("proc", "Function 'proc' is missing type hints")

    def test_double_quote_pattern_matches(self):
        assert _names_function("d", 'Function "d" has a non-descriptive name')

    def test_colon_pattern_matches(self):
        assert _names_function("find_admins", "find_admins: O(n²) nested iteration")

    def test_startswith_pattern_matches(self):
        assert _names_function("find_admins", "find_admins causes O(n²) complexity")

    def test_single_letter_no_false_positive_in_prose(self):
        # "d" appears in "reduced", "and", "hardcoded" — must NOT match
        assert not _names_function("d", "Hardcoded DB_PASSWORD exposes credentials")

    def test_single_letter_no_false_positive_in_finding(self):
        assert not _names_function("d", "Function 'proc' is missing type hints")

    def test_single_letter_matches_explicit_pattern(self):
        assert _names_function("d", "Function 'd' has a non-descriptive name")

    def test_slash_compound_pattern_matches(self):
        assert _names_function("build_order_summary", "build_order_summary/export_csv: quadratic string concat")

    def test_slash_compound_pattern_also_matches_second_name(self):
        assert _names_function("export_csv", "build_order_summary/export_csv: quadratic string concat")

    def test_slash_compound_pattern_matches(self):
        assert _names_function("build_order_summary", "build_order_summary/export_csv: quadratic string concat")

    def test_slash_compound_pattern_also_matches_second_name(self):
        assert _names_function("export_csv", "build_order_summary/export_csv: quadratic string concat")

    def test_unrelated_function_does_not_match(self):
        assert not _names_function("get_user", "SQL injection in login() via concat")


def _make_sections(*names: str) -> list:
    """Build minimal CodeSection list: one header + one function per name."""
    sections = [CodeSection(name="__header__", source="import os\n", section_type="header")]
    for name in names:
        sections.append(
            CodeSection(name=name, source=f"def {name}():\n    pass\n", section_type="function")
        )
    return sections


class TestMapFindings:
    """Tests for FixGeneratorAgent._map_findings — pure logic, no LLM calls."""

    def test_single_function_finding_routed_correctly(self):
        agent = _make_agent()
        sections = _make_sections("login", "get_user")
        results = {
            "Injection Expert": _make_result(
                findings=["SQL injection in login() via concat"],
                locations=["login()"],
                severity=95,
            )
        }
        mapping = agent._map_findings(results, sections)
        assert "login" in mapping
        assert "get_user" not in mapping

    def test_compound_slash_finding_goes_to_both_functions(self):
        """Performance finding 'foo/bar: issue' must reach both foo and bar."""
        agent = _make_agent()
        sections = _make_sections("build_order_summary", "export_csv", "find_top_customers")
        results = {
            "Performance Expert": _make_result(
                findings=["build_order_summary/export_csv: quadratic string concat vs list+join"],
                locations=["build_order_summary", "export_csv"],
                severity=60,
            )
        }
        mapping = agent._map_findings(results, sections)
        assert "build_order_summary" in mapping
        assert "export_csv" in mapping
        assert "find_top_customers" not in mapping

    def test_unmatched_finding_broadcast_to_all_locations(self):
        """Finding with no explicit function name in text goes to every location."""
        agent = _make_agent()
        sections = _make_sections("login", "reset_password")
        results = {
            "Auth Expert": _make_result(
                findings=["MD5 used for passwords"],
                locations=["login()", "reset_password()"],
                severity=80,
            )
        }
        mapping = agent._map_findings(results, sections)
        assert "login" in mapping
        assert "reset_password" in mapping

    def test_no_location_goes_to_header(self):
        agent = _make_agent()
        sections = _make_sections("login")
        results = {
            "Secrets Expert": _make_result(
                findings=["ADMIN_TOKEN hardcoded"],
                locations=[],
                severity=85,
            )
        }
        mapping = agent._map_findings(results, sections)
        assert "__header__" in mapping
        assert "login" not in mapping

    def test_finding_does_not_leak_to_unrelated_function(self):
        """A text-matched finding must not also broadcast to other locations."""
        agent = _make_agent()
        sections = _make_sections("login", "get_user", "find_admins")
        results = {
            "Injection Expert": _make_result(
                findings=["SQL injection in login() via concat"],
                locations=["login()", "get_user()", "find_admins()"],
                severity=95,
            )
        }
        mapping = agent._map_findings(results, sections)
        # Text-matched to login only — must not broadcast to get_user or find_admins
        assert "login" in mapping
        assert "get_user" not in mapping
        assert "find_admins" not in mapping

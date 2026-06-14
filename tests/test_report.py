"""
Tests for graph/report.py — the report-generation logic is pure Python
and must work correctly across all severity levels and fix states.
"""
import pytest

from graph.report import generate_final_report, _severity_label, _route_finding, _group_findings
from schemas.response import AgentResponse
from schemas.fix_response import FixResponse


def _make_result(severity, findings=None, locations=None) -> AgentResponse:
    return AgentResponse(
        reasoning="test reasoning",
        findings=findings or ["finding 1"],
        severity=severity,
        locations=locations or [],
    )


class TestSeverityLabel:
    def test_critical(self):
        assert "CRITICAL" in _severity_label(80)
        assert "CRITICAL" in _severity_label(100)

    def test_high(self):
        assert "HIGH" in _severity_label(60)
        assert "HIGH" in _severity_label(79)

    def test_low(self):
        assert "LOW" in _severity_label(1)
        assert "LOW" in _severity_label(59)

    def test_clean(self):
        assert "Clean" in _severity_label(0)


class TestRouteFinding:
    def test_routes_to_function_by_colon_prefix(self):
        assert _route_finding("login(): SQL injection", ["login", "signup"]) == "login"

    def test_routes_to_function_by_paren(self):
        assert _route_finding("call to login() is unsafe", ["login"]) == "login"

    def test_falls_back_to_module_level_when_no_match(self):
        from graph.report import _MODULE_LEVEL
        assert _route_finding("hardcoded secret in module", ["login", "logout"]) == _MODULE_LEVEL

    def test_single_location_used_as_fallback(self):
        assert _route_finding("unrelated finding", ["process"]) == "process"

    def test_empty_locations_returns_module_level(self):
        from graph.report import _MODULE_LEVEL
        assert _route_finding("some finding", []) == _MODULE_LEVEL


class TestGroupFindings:
    def test_groups_finding_to_correct_function(self):
        results = {
            "Injection Expert": _make_result(
                95,
                findings=["login(): SQL injection"],
                locations=["login"],
            )
        }
        grouped, _ = _group_findings(results)
        assert "login" in grouped
        assert grouped["login"][0] == ("Injection Expert", "login(): SQL injection")

    def test_skips_clean_agents(self):
        results = {"Performance Expert": _make_result(0)}
        grouped, _ = _group_findings(results)
        assert len(grouped) == 0

    def test_max_severity_tracked_per_function(self):
        results = {
            "Injection Expert": _make_result(95, findings=["login(): SQL injection"], locations=["login"]),
            "Auth Expert": _make_result(60, findings=["login(): weak session"], locations=["login"]),
        }
        _, func_severity = _group_findings(results)
        assert func_severity["login"] == 95

    def test_multiple_agents_findings_grouped_together(self):
        results = {
            "Injection Expert": _make_result(95, findings=["login(): SQL injection"], locations=["login"]),
            "Auth Expert": _make_result(60, findings=["login(): no rate limit"], locations=["login"]),
        }
        grouped, _ = _group_findings(results)
        assert len(grouped["login"]) == 2


class TestGenerateFinalReport:
    def test_agent_summary_present(self):
        results = {"Injection Expert": _make_result(95)}
        report = generate_final_report(results, None)
        assert "AGENT SUMMARY" in report

    def test_agent_name_in_summary(self):
        results = {"Injection Expert": _make_result(95)}
        report = generate_final_report(results, None)
        assert "Injection Expert" in report

    def test_contains_severity(self):
        results = {"Injection Expert": _make_result(95)}
        report = generate_final_report(results, None)
        assert "95/100" in report

    def test_severity_zero_shows_clean(self):
        results = {"Performance Expert": _make_result(0)}
        report = generate_final_report(results, None)
        assert "Clean" in report
        assert "Findings:" not in report

    def test_none_result_shows_unavailable(self):
        results = {"Auth Expert": None}
        report = generate_final_report(results, None)
        assert "No assessment available" in report

    def test_function_section_created_for_located_finding(self):
        results = {
            "Injection Expert": _make_result(
                95,
                findings=["login(): SQL injection via f-string"],
                locations=["login"],
            )
        }
        report = generate_final_report(results, None)
        assert "login" in report
        assert "[Injection Expert]" in report

    def test_module_level_shown_for_unattributed_findings(self):
        results = {
            "Secrets Expert": _make_result(
                70,
                findings=["DB_PASSWORD hardcoded in module"],
                locations=[],
            )
        }
        report = generate_final_report(results, None)
        assert "MODULE LEVEL" in report

    def test_function_sections_sorted_by_severity_desc(self):
        results = {
            "Injection Expert": AgentResponse(
                reasoning="r",
                findings=["login(): SQL injection", "signup(): XSS"],
                severity=95,
                locations=["login", "signup"],
            )
        }
        report = generate_final_report(results, None)
        # Both function names must appear
        assert "login" in report
        assert "signup" in report

    def test_no_locations_section_when_empty(self):
        results = {"Injection Expert": _make_result(95, locations=[])}
        report = generate_final_report(results, None)
        assert "Affected functions:" not in report

    def test_references_section_shown_when_present(self):
        result = _make_result(95)
        result.references = ["CWE-89 | SQL Injection | OWASP A03:2021 | CVSS 9.8"]
        report = generate_final_report({"Injection Expert": result}, None)
        assert "REFERENCES" in report
        assert "CWE-89" in report

    def test_references_deduplicated_across_agents(self):
        r1 = _make_result(95)
        r1.references = ["CWE-89 | SQL Injection"]
        r2 = _make_result(80)
        r2.references = ["CWE-89 | SQL Injection"]
        report = generate_final_report({"Injection Expert": r1, "Auth Expert": r2}, None)
        assert report.count("CWE-89") == 1

    def test_fix_section_present_when_fix_result_provided(self):
        results = {"Injection Expert": _make_result(95)}
        fix = FixResponse(
            fixed_code="import os\nDB = os.environ.get('DB', '')",
            changes=["Replaced hardcoded DB_PASSWORD"],
            unfixable=["Session management requires framework"],
        )
        report = generate_final_report(results, fix)
        assert "CHANGES APPLIED" in report
        assert "Replaced hardcoded DB_PASSWORD" in report

    def test_unfixable_section_present(self):
        results = {"Auth Expert": _make_result(92)}
        fix = FixResponse(
            fixed_code="",
            unfixable=["Rate limiting requires middleware"],
        )
        report = generate_final_report(results, fix)
        assert "MANUAL INTERVENTION" in report
        assert "Rate limiting requires middleware" in report

    def test_fixed_code_in_report(self):
        results = {"Injection Expert": _make_result(95)}
        fix = FixResponse(fixed_code="x = 1\n", changes=["Fixed x"])
        report = generate_final_report(results, fix)
        assert "COMPLETE FIXED FILE" in report
        assert "x = 1" in report

    def test_no_fix_section_when_fix_result_is_none(self):
        results = {"Injection Expert": _make_result(95)}
        report = generate_final_report(results, None)
        assert "CHANGES APPLIED" not in report
        assert "COMPLETE FIXED FILE" not in report

    def test_multiple_agents_all_in_summary(self):
        results = {
            "Injection Expert": _make_result(95),
            "Auth Expert": _make_result(92),
            "Secrets Expert": _make_result(0),
        }
        report = generate_final_report(results, None)
        assert "Injection Expert" in report
        assert "Auth Expert" in report
        assert "Secrets Expert" in report

    def test_empty_results_shows_no_issues(self):
        report = generate_final_report({}, None)
        assert "No issues found" in report

    def test_report_ends_with_footer(self):
        report = generate_final_report({}, None)
        assert "End of Autonomous Code Review Report" in report

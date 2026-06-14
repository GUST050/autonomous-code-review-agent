"""
Tests for Pydantic schemas — verifies that validation rules hold
and that defaults are correct. If a schema changes, these tests catch it.
"""
import pytest
from pydantic import ValidationError

from schemas.response import AgentResponse
from schemas.fix_response import FixResponse
from schemas.function_fix_response import FunctionFixResponse


class TestAgentResponse:
    def test_valid_minimal(self):
        r = AgentResponse(reasoning="ok", findings=["f1"])
        assert r.severity == 0
        assert r.confidence == 0
        assert r.locations == []

    def test_valid_full(self):
        r = AgentResponse(
            reasoning="found issues",
            findings=["login(): SQL injection via f-string in execute() — attacker can dump all users"],
            severity=95,
            confidence=98,
            locations=["login", "get_user"],
        )
        assert r.severity == 95
        assert len(r.locations) == 2

    def test_no_suggestions_field(self):
        r = AgentResponse(reasoning="ok", findings=["f1"])
        assert not hasattr(r, "suggestions")

    def test_severity_upper_bound(self):
        with pytest.raises(ValidationError):
            AgentResponse(reasoning="x", findings=["f"], severity=101)

    def test_severity_lower_bound(self):
        with pytest.raises(ValidationError):
            AgentResponse(reasoning="x", findings=["f"], severity=-1)

    def test_confidence_upper_bound(self):
        with pytest.raises(ValidationError):
            AgentResponse(reasoning="x", findings=["f"], confidence=101)

    def test_severity_default_is_zero(self):
        r = AgentResponse(reasoning="clean", findings=[])
        assert r.severity == 0

    def test_confidence_default_is_zero(self):
        r = AgentResponse(reasoning="clean", findings=[])
        assert r.confidence == 0

    def test_severity_zero_is_valid(self):
        r = AgentResponse(reasoning="clean", findings=[], severity=0)
        assert r.severity == 0

    def test_severity_100_is_valid(self):
        r = AgentResponse(reasoning="critical", findings=["f"], severity=100)
        assert r.severity == 100

    def test_empty_findings_allowed(self):
        r = AgentResponse(reasoning="nothing found", findings=[], severity=0)
        assert r.findings == []

    def test_references_default_empty(self):
        r = AgentResponse(reasoning="ok", findings=["f1"])
        assert r.references == []


class TestFixResponse:
    def test_valid_minimal(self):
        r = FixResponse(fixed_code="x = 1")
        assert r.changes == []
        assert r.unfixable == []

    def test_valid_full(self):
        r = FixResponse(
            fixed_code="import os\nDB_PASSWORD = os.environ.get('DB_PASSWORD', '')",
            changes=["Replaced hardcoded DB_PASSWORD with os.environ.get()"],
            unfixable=["Session management requires web framework"],
        )
        assert len(r.changes) == 1
        assert len(r.unfixable) == 1

    def test_empty_fixed_code_allowed(self):
        r = FixResponse(fixed_code="", unfixable=["All issues require manual fixes"])
        assert r.fixed_code == ""


class TestFunctionFixResponse:
    def test_valid_minimal(self):
        r = FunctionFixResponse(fixed_code="def f(): pass")
        assert r.changes == []
        assert r.needed_imports == []
        assert r.unfixable == []

    def test_valid_full(self):
        r = FunctionFixResponse(
            fixed_code="def login(u, p):\n    cursor.execute('SELECT ?', (u,))",
            changes=["Replaced f-string with parameterized query"],
            needed_imports=["import hashlib"],
            unfixable=["Rate limiting requires middleware"],
        )
        assert len(r.changes) == 1
        assert len(r.needed_imports) == 1
        assert len(r.unfixable) == 1

    def test_needed_imports_default_empty(self):
        r = FunctionFixResponse(fixed_code="def f(): pass")
        assert r.needed_imports == []

    def test_empty_fixed_code_allowed(self):
        r = FunctionFixResponse(fixed_code="")
        assert r.fixed_code == ""

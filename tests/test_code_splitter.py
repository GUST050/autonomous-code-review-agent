"""
Tests for utils/code_splitter.py — the AST-based source parser used by the
parallel fix agent to split code into independently fixable sections.
"""
import pytest

from utils.code_splitter import split_code, trailing_whitespace, slice_for_agent


# ── Fixtures ──────────────────────────────────────────────────────────────────

SIMPLE_SOURCE = '''\
import os

DB = "secret"


def login(u, p):
    return "ok"


def get_user(uid):
    return None
'''

NO_FUNCTIONS_SOURCE = '''\
import os

DB_PASSWORD = "admin123"
'''

ONE_FUNCTION_SOURCE = '''\
import os


def only_func(x):
    return x * 2
'''

ASYNC_SOURCE = '''\
import asyncio


async def fetch(url):
    return await asyncio.sleep(0)
'''

CLASS_SOURCE = '''\
import os


class MyClass:
    def method(self):
        pass


def standalone():
    pass
'''


# ── split_code: basic structure ───────────────────────────────────────────────

class TestSplitCodeStructure:
    def test_returns_header_plus_functions(self):
        sections = split_code(SIMPLE_SOURCE)
        types = [s.section_type for s in sections]
        assert types[0] == "header"
        assert all(t == "function" for t in types[1:])

    def test_function_names_extracted(self):
        sections = split_code(SIMPLE_SOURCE)
        names = [s.name for s in sections if s.section_type == "function"]
        assert names == ["login", "get_user"]

    def test_header_contains_imports_and_constants(self):
        sections = split_code(SIMPLE_SOURCE)
        header = sections[0]
        assert "import os" in header.source
        assert 'DB = "secret"' in header.source

    def test_function_source_starts_with_def(self):
        sections = split_code(SIMPLE_SOURCE)
        for s in sections[1:]:
            assert s.source.lstrip().startswith("def ") or s.source.lstrip().startswith("async def ")

    def test_no_functions_returns_single_header(self):
        sections = split_code(NO_FUNCTIONS_SOURCE)
        assert len(sections) == 1
        assert sections[0].section_type == "header"
        assert "DB_PASSWORD" in sections[0].source

    def test_one_function(self):
        sections = split_code(ONE_FUNCTION_SOURCE)
        assert len(sections) == 2
        assert sections[0].section_type == "header"
        assert sections[1].name == "only_func"

    def test_async_function_detected_as_function(self):
        sections = split_code(ASYNC_SOURCE)
        func_sections = [s for s in sections if s.section_type == "function"]
        assert len(func_sections) == 1
        assert func_sections[0].name == "fetch"

    def test_class_detected_as_class(self):
        sections = split_code(CLASS_SOURCE)
        class_sections = [s for s in sections if s.section_type == "class"]
        assert len(class_sections) == 1
        assert class_sections[0].name == "MyClass"

    def test_syntax_error_returns_single_other_section(self):
        sections = split_code("def broken(:\n    pass")
        assert len(sections) == 1
        assert sections[0].section_type == "other"


# ── split_code: round-trip integrity ─────────────────────────────────────────

class TestSplitCodeRoundTrip:
    """Joining sections must reproduce the original source exactly."""

    def test_simple_source_round_trip(self):
        sections = split_code(SIMPLE_SOURCE)
        assert "".join(s.source for s in sections) == SIMPLE_SOURCE

    def test_no_functions_round_trip(self):
        sections = split_code(NO_FUNCTIONS_SOURCE)
        assert "".join(s.source for s in sections) == NO_FUNCTIONS_SOURCE

    def test_one_function_round_trip(self):
        sections = split_code(ONE_FUNCTION_SOURCE)
        assert "".join(s.source for s in sections) == ONE_FUNCTION_SOURCE

    def test_class_source_round_trip(self):
        sections = split_code(CLASS_SOURCE)
        assert "".join(s.source for s in sections) == CLASS_SOURCE

    def test_sample_code_round_trip(self):
        """The actual sample_code.py used in end-to-end tests must round-trip."""
        import pathlib
        sample_path = pathlib.Path(__file__).parent.parent / "sample_code.py"
        if not sample_path.exists():
            pytest.skip("sample_code.py not found")
        source = sample_path.read_text()
        sections = split_code(source)
        assert "".join(s.source for s in sections) == source


# ── split_code: trailing whitespace preservation ──────────────────────────────

class TestTrailingWhitespace:
    def test_blank_lines_between_functions_preserved(self):
        """Two blank lines between defs must be captured in the first def's trailing."""
        source = "def a():\n    pass\n\n\ndef b():\n    pass\n"
        sections = split_code(source)
        func_a = next(s for s in sections if s.name == "a")
        assert func_a.source.endswith("\n\n\n")

    def test_trailing_whitespace_helper(self):
        assert trailing_whitespace("def a():\n    pass\n\n\n") == "\n\n\n"
        assert trailing_whitespace("def a():\n    pass\n") == "\n"
        assert trailing_whitespace("def a():\n    pass") == ""

    def test_no_trailing_blank_lines_on_last_function(self):
        """Last function ends at EOF with a single newline."""
        source = "def a():\n    pass\n\n\ndef b():\n    pass\n"
        sections = split_code(source)
        func_b = next(s for s in sections if s.name == "b")
        # Last section has only the function body newline, no extra blanks
        assert not func_b.source.endswith("\n\n")


# ── split_code: content correctness ──────────────────────────────────────────

class TestSplitCodeContent:
    def test_login_body_in_login_section(self):
        sections = split_code(SIMPLE_SOURCE)
        login = next(s for s in sections if s.name == "login")
        assert 'return "ok"' in login.source

    def test_get_user_body_in_get_user_section(self):
        sections = split_code(SIMPLE_SOURCE)
        get_user = next(s for s in sections if s.name == "get_user")
        assert "return None" in get_user.source

    def test_function_body_not_in_header(self):
        sections = split_code(SIMPLE_SOURCE)
        header = sections[0]
        assert "return" not in header.source
        assert "def login" not in header.source


# ── slice_for_agent ───────────────────────────────────────────────────────────

DB_SOURCE = '''\
import sqlite3

SECRET = "admin123"


def login(u, p):
    conn = sqlite3.connect("db")
    conn.execute("SELECT * FROM users WHERE u=?", (u,))
    return "ok"


def get_user(uid):
    conn = sqlite3.connect("db")
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def find_admins(users):
    return [u for u in users if u["role"] == "admin"]


def build_report(items):
    return ", ".join(item["name"] for item in items)
'''


class TestSliceForAgent:
    def test_empty_patterns_returns_all(self):
        sections = split_code(DB_SOURCE)
        result = slice_for_agent(sections, [])
        assert result == sections

    def test_matching_pattern_keeps_relevant_functions(self):
        sections = split_code(DB_SOURCE)
        result = slice_for_agent(sections, ["execute"])
        names = [s.name for s in result if s.section_type == "function"]
        assert "login" in names
        assert "get_user" in names

    def test_matching_pattern_drops_irrelevant_functions(self):
        sections = split_code(DB_SOURCE)
        result = slice_for_agent(sections, ["execute"])
        names = [s.name for s in result if s.section_type == "function"]
        assert "find_admins" not in names
        assert "build_report" not in names

    def test_header_always_included(self):
        sections = split_code(DB_SOURCE)
        result = slice_for_agent(sections, ["execute"])
        types = [s.section_type for s in result]
        assert "header" in types

    def test_header_contains_module_level_secrets(self):
        sections = split_code(DB_SOURCE)
        result = slice_for_agent(sections, ["execute"])
        header = next(s for s in result if s.section_type == "header")
        assert 'SECRET = "admin123"' in header.source

    def test_no_match_returns_full_file_as_fallback(self):
        sections = split_code(DB_SOURCE)
        result = slice_for_agent(sections, ["nonexistent_pattern_xyz"])
        assert result == sections

    def test_case_insensitive_matching(self):
        sections = split_code(DB_SOURCE)
        result_upper = slice_for_agent(sections, ["EXECUTE"])
        result_lower = slice_for_agent(sections, ["execute"])
        assert [s.name for s in result_upper] == [s.name for s in result_lower]

    def test_multiple_patterns_union(self):
        sections = split_code(DB_SOURCE)
        # "join(" matches build_report, "execute" matches login + get_user
        result = slice_for_agent(sections, ["execute", "join("])
        names = [s.name for s in result if s.section_type == "function"]
        assert "login" in names
        assert "get_user" in names
        assert "build_report" in names
        assert "find_admins" not in names

    def test_result_is_subset_of_original_sections(self):
        sections = split_code(DB_SOURCE)
        result = slice_for_agent(sections, ["execute"])
        result_names = {s.name for s in result}
        original_names = {s.name for s in sections}
        assert result_names.issubset(original_names)

    def test_injection_patterns_on_sample_file(self):
        """Injection-relevant patterns keep only DB-touching functions."""
        import pathlib
        sample_path = pathlib.Path(__file__).parent.parent / "sample_code.py"
        if not sample_path.exists():
            pytest.skip("sample_code.py not found")
        source = sample_path.read_text()
        sections = split_code(source)
        injection_patterns = [
            "execute", "cursor", "SELECT", "INSERT", "UPDATE", "DELETE",
            "sqlite3", "subprocess", "eval(", "exec(",
        ]
        result = slice_for_agent(sections, injection_patterns)
        kept = [s.name for s in result if s.section_type == "function"]
        # All 4 SQL functions should be kept
        assert "login" in kept
        assert "get_user" in kept
        assert "reset_password" in kept
        assert "get_user_emails" in kept
        # Pure quality/performance functions should be dropped
        assert "d" not in kept
        assert "proc" not in kept

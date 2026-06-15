"""
Tests for diff_parser.py — parse_diff_locations and extract_function_name.

All tests use synthetic diff strings; no real files or network calls.
"""
import pytest

from utils.diff_parser import (
    FunctionLocation,
    extract_function_name,
    get_diff_line_set,
    get_function_ranges,
    parse_diff_locations,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _diff(file_path: str, hunks: str) -> str:
    """Wrap hunk content in a minimal unified diff header for one file."""
    return (
        f"diff --git a/{file_path} b/{file_path}\n"
        f"index abc..def 100644\n"
        f"--- a/{file_path}\n"
        f"+++ b/{file_path}\n"
        f"{hunks}"
    )


# ── extract_function_name ─────────────────────────────────────────────────────

class TestExtractFunctionName:
    def test_severity_tagged_high(self):
        assert extract_function_name("[HIGH] login(): SQL injection") == "login"

    def test_severity_tagged_critical(self):
        assert extract_function_name("[CRITICAL] get_user(): IDOR") == "get_user"

    def test_severity_tagged_medium(self):
        assert extract_function_name("[MEDIUM] search(): path traversal") == "search"

    def test_severity_tagged_low(self):
        assert extract_function_name("[LOW] validate(): missing check") == "validate"

    def test_legacy_format_no_tag(self):
        assert extract_function_name("login(): SQL injection via f-string") == "login"

    def test_leading_whitespace_legacy(self):
        assert extract_function_name("  login(): issue") == "login"

    def test_underscore_in_name(self):
        assert extract_function_name("[HIGH] get_user_by_id(): IDOR") == "get_user_by_id"

    def test_returns_none_for_no_function(self):
        assert extract_function_name("module-level hardcoded secret") is None

    def test_returns_none_for_empty(self):
        assert extract_function_name("") is None

    def test_returns_none_for_unknown_severity_tag(self):
        # [WARN] is not a valid tag and string starts with '[' not a word char —
        # neither regex matches, so None is returned
        assert extract_function_name("[WARN] login(): issue") is None


# ── parse_diff_locations ──────────────────────────────────────────────────────

class TestParseDiffLocations:
    # ── Basic function detection ──────────────────────────────────────────

    def test_added_function_def_is_located(self):
        diff = _diff("app.py", "@@ -1,0 +1,3 @@\n+def login(user, pw):\n+    pass\n")
        locs = parse_diff_locations(diff)
        assert "login" in locs
        assert locs["login"].path == "app.py"
        assert locs["login"].line == 1

    def test_function_on_context_line_with_added_body(self):
        """Function def is unchanged but body has added lines — use first added body line."""
        diff = _diff(
            "app.py",
            "@@ -5,4 +5,5 @@\n"
            " def get_user(uid):\n"           # context line 5
            "+    query = f'SELECT * WHERE id={uid}'\n"  # added line 6
            "     return cursor.fetchone()\n",
        )
        locs = parse_diff_locations(diff)
        assert "get_user" in locs
        assert locs["get_user"].line == 6

    def test_function_with_no_added_lines_excluded(self):
        """Pure context function (no + lines inside) must NOT appear in result."""
        diff = _diff(
            "app.py",
            "@@ -1,3 +1,3 @@\n"
            " def safe_func():\n"   # context
            "     return 42\n"      # context
            " \n",
        )
        locs = parse_diff_locations(diff)
        assert "safe_func" not in locs

    def test_multiple_functions_in_one_file(self):
        diff = _diff(
            "app.py",
            "@@ -1,0 +1,6 @@\n"
            "+def login():\n"
            "+    pass\n"
            "+\n"
            "+def logout():\n"
            "+    pass\n",
        )
        locs = parse_diff_locations(diff)
        assert "login" in locs
        assert "logout" in locs
        assert locs["login"].line == 1
        assert locs["logout"].line == 4

    # ── Multi-file diffs ──────────────────────────────────────────────────

    def test_multiple_files_tracked_separately(self):
        diff = (
            "diff --git a/auth.py b/auth.py\n"
            "--- a/auth.py\n+++ b/auth.py\n"
            "@@ -1,0 +1,2 @@\n"
            "+def authenticate():\n"
            "+    pass\n"
            "diff --git a/db.py b/db.py\n"
            "--- a/db.py\n+++ b/db.py\n"
            "@@ -1,0 +1,2 @@\n"
            "+def query():\n"
            "+    pass\n"
        )
        locs = parse_diff_locations(diff)
        assert locs["authenticate"].path == "auth.py"
        assert locs["query"].path == "db.py"

    def test_first_file_wins_for_duplicate_function_name(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n+++ b/a.py\n"
            "@@ -1,0 +1,2 @@\n"
            "+def process():\n"
            "+    pass\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n+++ b/b.py\n"
            "@@ -1,0 +1,2 @@\n"
            "+def process():\n"
            "+    pass\n"
        )
        locs = parse_diff_locations(diff)
        assert locs["process"].path == "a.py"

    # ── Async functions ───────────────────────────────────────────────────

    def test_async_def_detected(self):
        diff = _diff("views.py", "@@ -1,0 +1,2 @@\n+async def fetch_data():\n+    pass\n")
        locs = parse_diff_locations(diff)
        assert "fetch_data" in locs

    # ── Class methods ─────────────────────────────────────────────────────

    def test_method_inside_class_detected(self):
        diff = _diff(
            "models.py",
            "@@ -1,0 +1,4 @@\n"
            "+class User:\n"
            "+    def save(self):\n"
            "+        pass\n",
        )
        locs = parse_diff_locations(diff)
        assert "save" in locs

    # ── Removed lines ─────────────────────────────────────────────────────

    def test_removed_function_not_located(self):
        """Lines starting with '-' are not in the new file — must not appear."""
        diff = _diff(
            "app.py",
            "@@ -1,2 +1,0 @@\n"
            "-def old_func():\n"
            "-    pass\n",
        )
        locs = parse_diff_locations(diff)
        assert "old_func" not in locs

    # ── Edge cases ────────────────────────────────────────────────────────

    def test_empty_diff_returns_empty(self):
        assert parse_diff_locations("") == {}

    def test_diff_with_only_non_python_files(self):
        diff = (
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n+++ b/README.md\n"
            "@@ -1 +1 @@\n"
            "-old line\n"
            "+new line\n"
        )
        # No function defs — result is empty
        locs = parse_diff_locations(diff)
        assert locs == {}

    def test_line_numbers_advance_correctly_across_hunks(self):
        """Two hunks in same file — line numbers must be correct for second hunk."""
        diff = _diff(
            "app.py",
            "@@ -1,2 +1,2 @@\n"
            " def untouched():\n"  # line 1 context
            "     pass\n"          # line 2 context
            "@@ -10,0 +10,2 @@\n"
            "+def added_later():\n"  # line 10 in new file
            "+    pass\n",
        )
        locs = parse_diff_locations(diff)
        assert "added_later" in locs
        assert locs["added_later"].line == 10

    def test_function_name_with_leading_indent(self):
        """Methods may be indented (4 spaces) — still detected."""
        diff = _diff(
            "app.py",
            "@@ -1,0 +1,3 @@\n"
            "+class Foo:\n"
            "+    def bar(self):\n"
            "+        pass\n",
        )
        locs = parse_diff_locations(diff)
        assert "bar" in locs


# ── get_function_ranges ───────────────────────────────────────────────────────

class TestGetFunctionRanges:
    def test_simple_function(self):
        src = "def foo():\n    return 1\n"
        ranges = get_function_ranges(src)
        assert "foo" in ranges
        assert ranges["foo"] == (1, 2)

    def test_multi_line_function(self):
        src = "def bar():\n    x = 1\n    y = 2\n    return x + y\n"
        ranges = get_function_ranges(src)
        assert ranges["bar"] == (1, 4)

    def test_multiple_functions(self):
        src = "def a():\n    pass\n\ndef b():\n    pass\n"
        ranges = get_function_ranges(src)
        assert "a" in ranges
        assert "b" in ranges
        assert ranges["a"][0] < ranges["b"][0]

    def test_async_function(self):
        src = "async def fetch():\n    pass\n"
        ranges = get_function_ranges(src)
        assert "fetch" in ranges

    def test_function_with_imports_before_it(self):
        src = "import os\n\ndef my_func():\n    pass\n"
        ranges = get_function_ranges(src)
        assert "my_func" in ranges
        assert ranges["my_func"] == (3, 4)

    def test_invalid_syntax_returns_empty(self):
        assert get_function_ranges("def broken(") == {}

    def test_empty_source_returns_empty(self):
        assert get_function_ranges("") == {}

    def test_class_method_not_in_top_level_result(self):
        src = "class Foo:\n    def method(self):\n        pass\n"
        ranges = get_function_ranges(src)
        # top-level def search only — method is not top-level
        assert "method" not in ranges

    def test_end_lineno_is_last_line_of_body(self):
        src = (
            "def func():\n"       # line 1
            "    x = 1\n"         # line 2
            "    y = 2\n"         # line 3
            "    return x + y\n"  # line 4
        )
        ranges = get_function_ranges(src)
        assert ranges["func"][1] == 4


# ── get_diff_line_set ─────────────────────────────────────────────────────────

class TestGetDiffLineSet:
    def test_added_lines_included(self):
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -1,0 +1,3 @@\n"
            "+def foo():\n"
            "+    pass\n"
            "+\n"
        )
        line_sets = get_diff_line_set(diff)
        assert "app.py" in line_sets
        assert 1 in line_sets["app.py"]
        assert 2 in line_sets["app.py"]
        assert 3 in line_sets["app.py"]

    def test_context_lines_included(self):
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -1,3 +1,4 @@\n"
            " def foo():\n"     # context line 1
            "     pass\n"       # context line 2
            "+    x = 1\n"      # added line 3
            " \n"               # context line 4
        )
        line_sets = get_diff_line_set(diff)
        assert 1 in line_sets["app.py"]  # context
        assert 2 in line_sets["app.py"]  # context
        assert 3 in line_sets["app.py"]  # added
        assert 4 in line_sets["app.py"]  # context

    def test_removed_lines_not_included(self):
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -1,2 +1,1 @@\n"
            "-def old():\n"
            "+def new():\n"
        )
        line_sets = get_diff_line_set(diff)
        # new file has only line 1
        assert line_sets["app.py"] == {1}

    def test_multiple_files_tracked_separately(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n+++ b/a.py\n"
            "@@ -1,0 +1,1 @@\n"
            "+x = 1\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n+++ b/b.py\n"
            "@@ -1,0 +1,1 @@\n"
            "+y = 2\n"
        )
        line_sets = get_diff_line_set(diff)
        assert "a.py" in line_sets
        assert "b.py" in line_sets
        assert line_sets["a.py"] == {1}
        assert line_sets["b.py"] == {1}

    def test_line_numbers_correct_across_multiple_hunks(self):
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " line1\n"   # line 1
            "+line2\n"   # line 2
            "@@ -10,1 +10,2 @@\n"
            " line10\n"  # line 10
            "+line11\n"  # line 11
        )
        line_sets = get_diff_line_set(diff)
        assert {1, 2, 10, 11} == line_sets["app.py"]

    def test_empty_diff_returns_empty(self):
        assert get_diff_line_set("") == {}


# ── FunctionLocation dataclass ────────────────────────────────────────────────

class TestFunctionLocation:
    def test_stores_path_and_line(self):
        loc = FunctionLocation(path="src/app.py", line=42)
        assert loc.path == "src/app.py"
        assert loc.line == 42

    def test_equality(self):
        assert FunctionLocation("a.py", 10) == FunctionLocation("a.py", 10)
        assert FunctionLocation("a.py", 10) != FunctionLocation("a.py", 11)

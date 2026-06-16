"""
Tests for github_client.py — no real HTTP calls made.
"""
import base64
from unittest.mock import MagicMock, patch

import pytest

from schemas.response import AgentResponse
from schemas.fix_response import FixResponse
from utils.diff_parser import FunctionLocation
from utils.github_client import (
    GitHubClient,
    _review_event,
    _severity_emoji,
    build_review_comments,
    build_suggestion_comments,
    format_pr_review,
)


class TestSeverityEmoji:
    def test_critical(self):
        assert _severity_emoji(90) == "🔴"

    def test_high(self):
        assert _severity_emoji(70) == "🟠"

    def test_medium(self):
        assert _severity_emoji(40) == "🟡"

    def test_low(self):
        assert _severity_emoji(10) == "🟢"

    def test_clean(self):
        assert _severity_emoji(0) == "✅"


class TestReviewEvent:
    def test_critical_requests_changes(self):
        assert _review_event(90) == "REQUEST_CHANGES"

    def test_low_severity_is_comment(self):
        assert _review_event(30) == "COMMENT"

    def test_clean_approves(self):
        assert _review_event(0) == "APPROVE"



class TestFormatPrReview:
    def _result(self, severity, findings, refs=None):
        return AgentResponse(
            reasoning="test",
            findings=findings,
            severity=severity,
            references=refs or [],
        )

    def test_clean_shows_no_issues(self):
        comment = format_pr_review({"Injection Expert": self._result(0, [])})
        assert "No issues found" in comment

    def test_finding_appears_in_review(self):
        comment = format_pr_review({
            "Injection Expert": self._result(95, ["login(): SQL injection"])
        })
        assert "login(): SQL injection" in comment

    def test_agent_summary_table_present(self):
        comment = format_pr_review({"Injection Expert": self._result(95, ["x"])})
        assert "Agent Summary" in comment
        assert "Injection Expert" in comment

    def test_references_shown(self):
        comment = format_pr_review({
            "Injection Expert": self._result(95, ["x"], refs=["CWE-89"])
        })
        assert "CWE-89" in comment

    def test_none_result_shows_unavailable(self):
        comment = format_pr_review({"Injection Expert": None})
        assert "Unavailable" in comment

    def test_critical_emoji_for_high_severity(self):
        comment = format_pr_review({"Injection Expert": self._result(95, ["x"])})
        assert "🔴" in comment

    def test_fix_changes_shown_when_provided(self):
        fix = FixResponse(fixed_code="x = 1\n", changes=["Replaced MD5 with SHA-256"])
        comment = format_pr_review({"Injection Expert": self._result(95, ["x"])}, fix)
        assert "Replaced MD5 with SHA-256" in comment

    def test_fix_section_absent_when_no_fix(self):
        comment = format_pr_review({"Injection Expert": self._result(95, ["x"])})
        assert "Suggested Fixes" not in comment

    def test_unfixable_shown(self):
        fix = FixResponse(
            fixed_code="",
            changes=[],
            unfixable=["Rate limiting requires middleware"],
        )
        comment = format_pr_review({"Auth Expert": self._result(70, ["x"])}, fix)
        assert "Rate limiting requires middleware" in comment

    def test_approve_header_when_clean(self):
        comment = format_pr_review({"Injection Expert": self._result(0, [])})
        assert "good to merge" in comment

    def test_findings_shown_when_issues_found(self):
        """Review body should include findings section when issues are present."""
        comment = format_pr_review({"Injection Expert": self._result(95, ["x"])})
        assert "injection expert" in comment.lower()


class TestBuildReviewComments:
    """Unit tests for build_review_comments — no HTTP calls."""

    def _result(self, severity, findings, locations=None):
        return AgentResponse(
            reasoning="test",
            findings=findings,
            severity=severity,
            locations=locations or [],
        )

    def _loc(self, path="src/app.py", line=10):
        return FunctionLocation(path=path, line=line)

    def test_finding_with_matching_location_creates_comment(self):
        results = {"Injection Expert": self._result(80, ["[HIGH] login(): SQL injection"])}
        locs = {"login": self._loc(line=42)}
        comments = build_review_comments(results, locs)
        assert len(comments) == 1
        assert comments[0]["path"] == "src/app.py"
        assert comments[0]["line"] == 42
        assert comments[0]["side"] == "RIGHT"

    def test_comment_body_contains_agent_name(self):
        results = {"Auth Expert": self._result(70, ["[HIGH] login(): missing rate limit"])}
        locs = {"login": self._loc()}
        comments = build_review_comments(results, locs)
        assert "Auth Expert" in comments[0]["body"]

    def test_comment_body_contains_finding_text(self):
        finding = "[HIGH] login(): SQL injection via f-string"
        results = {"Injection Expert": self._result(80, [finding])}
        locs = {"login": self._loc()}
        comments = build_review_comments(results, locs)
        assert finding in comments[0]["body"]

    def test_finding_with_no_matching_location_excluded(self):
        results = {"Injection Expert": self._result(80, ["[HIGH] unknown_func(): issue"])}
        locs = {"login": self._loc()}  # 'unknown_func' not in locs
        comments = build_review_comments(results, locs)
        assert comments == []

    def test_multiple_findings_same_function_create_multiple_comments(self):
        results = {"Injection Expert": self._result(80, [
            "[HIGH] login(): SQL injection",
            "[MEDIUM] login(): path traversal",
        ])}
        locs = {"login": self._loc(line=10)}
        comments = build_review_comments(results, locs)
        assert len(comments) == 2

    def test_different_agents_same_function_both_commented(self):
        results = {
            "Injection Expert": self._result(80, ["[HIGH] login(): SQL injection"]),
            "Auth Expert":      self._result(70, ["[HIGH] login(): missing rate limit"]),
        }
        locs = {"login": self._loc(line=10)}
        comments = build_review_comments(results, locs)
        assert len(comments) == 2
        agents = {c["body"].split("\n")[0] for c in comments}
        assert any("Injection Expert" in a for a in agents)
        assert any("Auth Expert" in a for a in agents)

    def test_duplicate_finding_deduplicated(self):
        finding = "[HIGH] login(): SQL injection"
        results = {"Injection Expert": self._result(80, [finding, finding])}
        locs = {"login": self._loc(line=10)}
        comments = build_review_comments(results, locs)
        assert len(comments) == 1

    def test_empty_results_returns_empty_list(self):
        assert build_review_comments({}, {}) == []

    def test_none_result_skipped(self):
        results = {"Injection Expert": None}
        locs = {"login": self._loc()}
        assert build_review_comments(results, locs) == []

    def test_finding_with_no_function_name_excluded(self):
        results = {"Secrets Expert": self._result(60, ["module-level hardcoded API key"])}
        locs = {"login": self._loc()}
        assert build_review_comments(results, locs) == []

    def test_critical_finding_has_emoji_prefix(self):
        results = {"Injection Expert": self._result(95, ["[CRITICAL] login(): RCE via eval()"])}
        locs = {"login": self._loc()}
        comments = build_review_comments(results, locs)
        assert "🔴" in comments[0]["body"]

    def test_correct_path_from_location(self):
        results = {"Injection Expert": self._result(80, ["[HIGH] query(): SQL injection"])}
        locs = {"query": FunctionLocation(path="db/queries.py", line=55)}
        comments = build_review_comments(results, locs)
        assert comments[0]["path"] == "db/queries.py"
        assert comments[0]["line"] == 55


class TestBuildSuggestionComments:
    """Tests for build_suggestion_comments — no HTTP calls."""

    _SIMPLE_SOURCE = (
        "def login(user, pw):\n"    # line 1
        "    return True\n"          # line 2
        "\n"                         # line 3
        "def logout():\n"            # line 4
        "    pass\n"                 # line 5
    )

    def _all_lines(self, source: str) -> set:
        """Return a set containing every line number in source (1-indexed)."""
        return set(range(1, source.count("\n") + 2))

    def test_function_in_diff_becomes_suggestion(self):
        fixed = {"login": "def login(user, pw):\n    check_rate_limit()\n    return True\n"}
        diff_lines = self._all_lines(self._SIMPLE_SOURCE)
        comments, fallback = build_suggestion_comments(
            fixed, "app.py", self._SIMPLE_SOURCE, diff_lines
        )
        assert len(comments) == 1
        assert fallback == []

    def test_suggestion_has_correct_path(self):
        fixed = {"login": "def login(user, pw):\n    pass\n"}
        diff_lines = self._all_lines(self._SIMPLE_SOURCE)
        comments, _ = build_suggestion_comments(
            fixed, "src/auth.py", self._SIMPLE_SOURCE, diff_lines
        )
        assert comments[0]["path"] == "src/auth.py"

    def test_suggestion_body_is_code_block(self):
        fixed_src = "def login(user, pw):\n    check()\n    return True"
        fixed = {"login": fixed_src}
        diff_lines = self._all_lines(self._SIMPLE_SOURCE)
        comments, _ = build_suggestion_comments(
            fixed, "app.py", self._SIMPLE_SOURCE, diff_lines
        )
        assert comments[0]["body"].startswith("```suggestion\n")
        assert "```" in comments[0]["body"]

    def test_suggestion_contains_fixed_code(self):
        fixed_src = "def login(user, pw):\n    check_rate_limit()\n    return True"
        fixed = {"login": fixed_src}
        diff_lines = self._all_lines(self._SIMPLE_SOURCE)
        comments, _ = build_suggestion_comments(
            fixed, "app.py", self._SIMPLE_SOURCE, diff_lines
        )
        assert "check_rate_limit" in comments[0]["body"]

    def test_multiline_suggestion_has_start_and_end_line(self):
        fixed = {"login": "def login(user, pw):\n    check()\n    return True\n"}
        diff_lines = self._all_lines(self._SIMPLE_SOURCE)
        comments, _ = build_suggestion_comments(
            fixed, "app.py", self._SIMPLE_SOURCE, diff_lines
        )
        assert "start_line" in comments[0]
        assert "line" in comments[0]
        assert comments[0]["start_line"] == 1
        assert comments[0]["line"] == 2

    def test_function_not_in_diff_goes_to_fallback(self):
        fixed = {"login": "def login(user, pw):\n    check()\n    return True\n"}
        diff_lines = {3, 4, 5}  # only logout() lines visible in diff
        comments, fallback = build_suggestion_comments(
            fixed, "app.py", self._SIMPLE_SOURCE, diff_lines
        )
        assert comments == []
        assert len(fallback) == 1
        assert fallback[0][0] == "login"

    def test_unknown_function_goes_to_fallback(self):
        fixed = {"nonexistent": "def nonexistent():\n    pass\n"}
        diff_lines = self._all_lines(self._SIMPLE_SOURCE)
        comments, fallback = build_suggestion_comments(
            fixed, "app.py", self._SIMPLE_SOURCE, diff_lines
        )
        assert comments == []
        assert fallback[0][0] == "nonexistent"

    def test_empty_function_fixes_returns_empty(self):
        comments, fallback = build_suggestion_comments(
            {}, "app.py", self._SIMPLE_SOURCE, {1, 2, 3}
        )
        assert comments == []
        assert fallback == []

    def test_invalid_source_puts_all_in_fallback(self):
        fixed = {"login": "def login():\n    pass\n"}
        comments, fallback = build_suggestion_comments(
            fixed, "app.py", "def broken(", {1, 2, 3}
        )
        assert comments == []
        assert len(fallback) == 1

    def test_side_is_right(self):
        fixed = {"login": "def login(user, pw):\n    pass\n"}
        diff_lines = self._all_lines(self._SIMPLE_SOURCE)
        comments, _ = build_suggestion_comments(
            fixed, "app.py", self._SIMPLE_SOURCE, diff_lines
        )
        assert comments[0]["side"] == "RIGHT"

    def test_multiple_functions_both_in_diff(self):
        fixed = {
            "login":  "def login(user, pw):\n    check()\n    return True\n",
            "logout": "def logout():\n    clear_session()\n",
        }
        diff_lines = self._all_lines(self._SIMPLE_SOURCE)
        comments, fallback = build_suggestion_comments(
            fixed, "app.py", self._SIMPLE_SOURCE, diff_lines
        )
        assert len(comments) == 2
        assert fallback == []

    def test_fallback_contains_fixed_source(self):
        fixed_src = "def login(user, pw):\n    check()\n    return True\n"
        fixed = {"login": fixed_src}
        diff_lines = set()  # nothing in diff
        _, fallback = build_suggestion_comments(
            fixed, "app.py", self._SIMPLE_SOURCE, diff_lines
        )
        assert fallback[0][1] == fixed_src


class TestGitHubClient:
    def _client(self):
        return GitHubClient(token="test-token")

    def _mock_get(self, json_data=None, text="", status=200):
        mock_response = MagicMock()
        mock_response.json.return_value = json_data or {}
        mock_response.text = text
        mock_response.raise_for_status = MagicMock()
        return mock_response

    def _mock_post_put(self):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        return mock_response

    # ── get_pr_diff ───────────────────────────────────────────────────────

    def test_get_pr_diff_calls_correct_url(self):
        client = self._client()
        mock_response = MagicMock()
        mock_response.text = "diff content"
        mock_response.raise_for_status = MagicMock()

        with patch("utils.github_client.requests.get", return_value=mock_response) as mock_get:
            result = client.get_pr_diff("owner/repo", 42)

        assert result == "diff content"
        called_url = mock_get.call_args[0][0]
        assert "owner/repo" in called_url
        assert "42" in called_url

    def test_get_pr_diff_raises_on_http_error(self):
        client = self._client()
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("404")

        with patch("utils.github_client.requests.get", return_value=mock_response):
            with pytest.raises(Exception, match="404"):
                client.get_pr_diff("owner/repo", 1)

    # ── get_pr_info ───────────────────────────────────────────────────────

    def test_get_pr_info_returns_json(self):
        client = self._client()
        pr_data = {"number": 42, "head": {"ref": "feature-branch"}}

        with patch("utils.github_client.requests.get", return_value=self._mock_get(pr_data)):
            result = client.get_pr_info("owner/repo", 42)

        assert result["head"]["ref"] == "feature-branch"

    def test_get_pr_info_hits_pulls_endpoint(self):
        client = self._client()
        with patch("utils.github_client.requests.get", return_value=self._mock_get()) as mock_get:
            client.get_pr_info("owner/repo", 5)
        assert "pulls/5" in mock_get.call_args[0][0]

    # ── get_pr_files ──────────────────────────────────────────────────────

    def test_get_pr_files_returns_list(self):
        client = self._client()
        files = [{"filename": "src/app.py", "status": "modified"}]

        with patch("utils.github_client.requests.get", return_value=self._mock_get(files)):
            result = client.get_pr_files("owner/repo", 1)

        assert result[0]["filename"] == "src/app.py"

    def test_get_pr_files_hits_files_endpoint(self):
        client = self._client()
        with patch("utils.github_client.requests.get", return_value=self._mock_get([])) as mock_get:
            client.get_pr_files("owner/repo", 7)
        assert "pulls/7/files" in mock_get.call_args[0][0]

    # ── get_file_content ──────────────────────────────────────────────────

    def test_get_file_content_decodes_base64(self):
        client = self._client()
        raw_content = "print('hello')\n"
        encoded = base64.b64encode(raw_content.encode()).decode()
        api_response = {"content": encoded, "sha": "abc123"}

        with patch("utils.github_client.requests.get", return_value=self._mock_get(api_response)):
            result = client.get_file_content("owner/repo", "main.py", "feature")

        assert result["content"] == raw_content

    def test_get_file_content_passes_ref_as_param(self):
        client = self._client()
        encoded = base64.b64encode(b"x").decode()
        api_response = {"content": encoded, "sha": "x"}

        with patch("utils.github_client.requests.get", return_value=self._mock_get(api_response)) as mock_get:
            client.get_file_content("owner/repo", "app.py", "my-branch")

        assert mock_get.call_args[1]["params"]["ref"] == "my-branch"

    # ── post_pr_review ────────────────────────────────────────────────────

    def test_post_pr_review_calls_reviews_endpoint(self):
        client = self._client()
        with patch("utils.github_client.requests.post", return_value=self._mock_post_put()) as mock_post:
            client.post_pr_review("owner/repo", 42, "body", "REQUEST_CHANGES")

        called_url = mock_post.call_args[0][0]
        assert "pulls/42/reviews" in called_url
        assert mock_post.call_args[1]["json"]["event"] == "REQUEST_CHANGES"
        assert mock_post.call_args[1]["json"]["body"] == "body"

    # ── post_pr_comment ───────────────────────────────────────────────────

    def test_post_pr_comment_calls_issues_endpoint(self):
        client = self._client()
        with patch("utils.github_client.requests.post", return_value=self._mock_post_put()) as mock_post:
            client.post_pr_comment("owner/repo", 42, "review body")

        called_url = mock_post.call_args[0][0]
        assert "issues/42/comments" in called_url

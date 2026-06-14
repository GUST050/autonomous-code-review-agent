"""
Tests for github_client.py — no real HTTP calls made.
"""
import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from schemas.response import AgentResponse
from schemas.fix_response import FixResponse
from utils.github_client import (
    GitHubClient,
    _embed_findings,
    _review_event,
    _severity_emoji,
    extract_findings_from_review,
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


class TestEmbedAndExtractFindings:
    """Round-trip tests for the hidden findings serialization."""

    def _result(self, severity, findings, locations=None):
        return AgentResponse(
            reasoning="test reasoning",
            findings=findings,
            severity=severity,
            locations=locations or [],
        )

    def test_round_trip_preserves_findings(self):
        results = {"Injection Expert": self._result(95, ["login(): SQL injection"])}
        body = _embed_findings(results)
        recovered = extract_findings_from_review(body)
        assert recovered["Injection Expert"]["findings"] == ["login(): SQL injection"]

    def test_round_trip_preserves_severity(self):
        results = {"Auth Expert": self._result(70, ["x"])}
        body = _embed_findings(results)
        recovered = extract_findings_from_review(body)
        assert recovered["Auth Expert"]["severity"] == 70

    def test_round_trip_preserves_locations(self):
        results = {"Injection Expert": self._result(80, ["x"], locations=["login", "signup"])}
        body = _embed_findings(results)
        recovered = extract_findings_from_review(body)
        assert recovered["Injection Expert"]["locations"] == ["login", "signup"]

    def test_round_trip_multiple_agents(self):
        results = {
            "Injection Expert": self._result(95, ["login(): SQLi"]),
            "Auth Expert":      self._result(70, ["session(): no expiry"]),
        }
        body = _embed_findings(results)
        recovered = extract_findings_from_review(body)
        assert "Injection Expert" in recovered
        assert "Auth Expert" in recovered

    def test_skips_none_results(self):
        results = {
            "Injection Expert": self._result(95, ["x"]),
            "Auth Expert": None,
        }
        body = _embed_findings(results)
        recovered = extract_findings_from_review(body)
        assert "Injection Expert" in recovered
        assert "Auth Expert" not in recovered

    def test_extract_returns_empty_for_no_marker(self):
        assert extract_findings_from_review("no marker here") == {}

    def test_extract_returns_empty_for_empty_body(self):
        assert extract_findings_from_review("") == {}

    def test_extract_returns_empty_for_none_body(self):
        assert extract_findings_from_review(None) == {}

    def test_extract_returns_empty_for_corrupt_base64(self):
        corrupt = "<!-- agent-findings-v1:!!NOTBASE64!! -->"
        assert extract_findings_from_review(corrupt) == {}

    def test_special_characters_in_findings(self):
        finding = 'login(): use `WHERE id = ?` instead of f"WHERE id = {id}"'
        results = {"Injection Expert": self._result(95, [finding])}
        body = _embed_findings(results)
        recovered = extract_findings_from_review(body)
        assert recovered["Injection Expert"]["findings"][0] == finding

    def test_extract_finds_marker_in_longer_body(self):
        results = {"Injection Expert": self._result(95, ["x"])}
        hidden = _embed_findings(results)
        full_body = f"## Review\n\nSome markdown text.\n\n---\n\n{hidden}"
        recovered = extract_findings_from_review(full_body)
        assert "Injection Expert" in recovered


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

    def test_findings_embedded_in_body(self):
        """format_pr_review must embed findings for the 'fix' command to recover."""
        results = {"Injection Expert": self._result(95, ["login(): SQL injection"])}
        body = format_pr_review(results)
        recovered = extract_findings_from_review(body)
        assert recovered["Injection Expert"]["findings"] == ["login(): SQL injection"]

    def test_fix_hint_shown_when_issues_found(self):
        """Review body should include 'fix' hint when issues are present."""
        comment = format_pr_review({"Injection Expert": self._result(95, ["x"])})
        assert "fix" in comment.lower()

    def test_no_embedded_findings_when_all_clean(self):
        """Clean reviews still embed findings (all severities 0) — used to detect no-issues."""
        results = {"Injection Expert": self._result(0, [])}
        body = format_pr_review(results)
        # The marker must be present even for clean reviews
        assert "agent-findings-v1" in body


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

    # ── get_pr_reviews ────────────────────────────────────────────────────

    def test_get_pr_reviews_returns_newest_first(self):
        client = self._client()
        reviews = [{"id": 1, "body": "first"}, {"id": 2, "body": "second"}]

        with patch("utils.github_client.requests.get", return_value=self._mock_get(reviews)):
            result = client.get_pr_reviews("owner/repo", 1)

        # Reversed: newest (id=2) should be first
        assert result[0]["id"] == 2

    def test_get_pr_reviews_hits_reviews_endpoint(self):
        client = self._client()
        with patch("utils.github_client.requests.get", return_value=self._mock_get([])) as mock_get:
            client.get_pr_reviews("owner/repo", 3)
        assert "pulls/3/reviews" in mock_get.call_args[0][0]

    # ── get_file_content ──────────────────────────────────────────────────

    def test_get_file_content_decodes_base64(self):
        client = self._client()
        raw_content = "print('hello')\n"
        encoded = base64.b64encode(raw_content.encode()).decode()
        api_response = {"content": encoded, "sha": "abc123"}

        with patch("utils.github_client.requests.get", return_value=self._mock_get(api_response)):
            result = client.get_file_content("owner/repo", "main.py", "feature")

        assert result["content"] == raw_content
        assert result["sha"] == "abc123"

    def test_get_file_content_passes_ref_as_param(self):
        client = self._client()
        encoded = base64.b64encode(b"x").decode()
        api_response = {"content": encoded, "sha": "x"}

        with patch("utils.github_client.requests.get", return_value=self._mock_get(api_response)) as mock_get:
            client.get_file_content("owner/repo", "app.py", "my-branch")

        assert mock_get.call_args[1]["params"]["ref"] == "my-branch"

    # ── commit_file ───────────────────────────────────────────────────────

    def test_commit_file_calls_put_with_correct_payload(self):
        client = self._client()
        with patch("utils.github_client.requests.put", return_value=self._mock_post_put()) as mock_put:
            client.commit_file(
                repo="owner/repo",
                path="src/app.py",
                content="fixed code\n",
                sha="abc123",
                branch="feature",
                message="fix: SQL injection",
            )

        body = mock_put.call_args[1]["json"]
        assert body["sha"]     == "abc123"
        assert body["branch"]  == "feature"
        assert body["message"] == "fix: SQL injection"
        # Content must be valid base64
        decoded = base64.b64decode(body["content"]).decode()
        assert decoded == "fixed code\n"

    def test_commit_file_hits_contents_endpoint(self):
        client = self._client()
        with patch("utils.github_client.requests.put", return_value=self._mock_post_put()) as mock_put:
            client.commit_file("owner/repo", "app.py", "x", "sha", "main", "msg")
        assert "contents/app.py" in mock_put.call_args[0][0]

    def test_commit_file_raises_on_http_error(self):
        client = self._client()
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("409 Conflict")
        with patch("utils.github_client.requests.put", return_value=mock_response):
            with pytest.raises(Exception, match="409"):
                client.commit_file("owner/repo", "app.py", "x", "sha", "main", "msg")

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

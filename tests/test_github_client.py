"""
Tests for github_client.py — no real HTTP calls made.
"""
from unittest.mock import MagicMock, patch

import pytest

from schemas.response import AgentResponse
from utils.github_client import GitHubClient, format_github_comment, _severity_emoji


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


class TestFormatGithubComment:
    def _result(self, severity, findings, refs=None):
        return AgentResponse(
            reasoning="test",
            findings=findings,
            severity=severity,
            references=refs or [],
        )

    def test_clean_result_shows_no_issues(self):
        comment = format_github_comment({"Injection Expert": self._result(0, [])})
        assert "No issues found" in comment

    def test_finding_appears_in_comment(self):
        comment = format_github_comment({
            "Injection Expert": self._result(95, ["login(): SQL injection"])
        })
        assert "login(): SQL injection" in comment

    def test_agent_summary_table_present(self):
        comment = format_github_comment({"Injection Expert": self._result(95, ["x"])})
        assert "Agent Summary" in comment
        assert "Injection Expert" in comment

    def test_references_shown(self):
        comment = format_github_comment({
            "Injection Expert": self._result(95, ["x"], refs=["CWE-89"])
        })
        assert "CWE-89" in comment

    def test_none_result_shows_unavailable(self):
        comment = format_github_comment({"Injection Expert": None})
        assert "Unavailable" in comment

    def test_multiple_agents_all_in_summary(self):
        results = {
            "Injection Expert": self._result(90, ["sql injection"]),
            "Auth Expert": self._result(0, []),
        }
        comment = format_github_comment(results)
        assert "Injection Expert" in comment
        assert "Auth Expert" in comment

    def test_critical_emoji_for_high_severity(self):
        comment = format_github_comment({
            "Injection Expert": self._result(95, ["x"])
        })
        assert "🔴" in comment


class TestGitHubClient:
    def _client(self):
        return GitHubClient(token="test-token")

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

    def test_post_pr_comment_calls_correct_url(self):
        client = self._client()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("utils.github_client.requests.post", return_value=mock_response) as mock_post:
            client.post_pr_comment("owner/repo", 42, "review body")

        called_url = mock_post.call_args[0][0]
        assert "owner/repo" in called_url
        assert "42" in called_url
        assert mock_post.call_args[1]["json"]["body"] == "review body"

    def test_get_pr_diff_raises_on_http_error(self):
        client = self._client()
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("404")

        with patch("utils.github_client.requests.get", return_value=mock_response):
            with pytest.raises(Exception, match="404"):
                client.get_pr_diff("owner/repo", 1)

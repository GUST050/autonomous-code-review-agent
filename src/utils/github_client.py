"""
github_client.py — GitHub REST API for fetching PR diffs and posting review comments.
"""
import logging
from typing import Dict, Optional

import requests

from config import APPROVAL_THRESHOLD
from schemas.response import AgentResponse

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"

_SEVERITY_EMOJI = {
    "critical": "🔴",
    "high":     "🟠",
    "medium":   "🟡",
    "low":      "🟢",
    "clean":    "✅",
}


def _severity_emoji(severity: int) -> str:
    if severity >= APPROVAL_THRESHOLD:
        return _SEVERITY_EMOJI["critical"]
    if severity >= 60:
        return _SEVERITY_EMOJI["high"]
    if severity >= 30:
        return _SEVERITY_EMOJI["medium"]
    if severity > 0:
        return _SEVERITY_EMOJI["low"]
    return _SEVERITY_EMOJI["clean"]


def format_github_comment(results: Dict[str, Optional[AgentResponse]]) -> str:
    """
    Convert agent results into a GitHub-flavoured markdown PR comment.
    """
    lines = ["## 🔍 Autonomous Code Review\n"]

    findings_found = False
    for agent_name, result in results.items():
        if not result or result.severity == 0 or not result.findings:
            continue
        findings_found = True
        emoji = _severity_emoji(result.severity)
        lines.append(f"### {emoji} {agent_name} — {result.severity}/100\n")
        for finding in result.findings:
            lines.append(f"- {finding}")
        refs = result.references or []
        if refs:
            lines.append("\n**References:** " + " · ".join(refs))
        lines.append("")

    if not findings_found:
        lines.append("✅ **No issues found** — all agents reported clean.\n")

    # Agent summary table
    lines += ["---", "### Agent Summary\n", "| Agent | Severity | Status |", "|-------|----------|--------|"]
    for agent_name, result in results.items():
        if result is None:
            lines.append(f"| {agent_name} | — | ⚠️ Unavailable |")
        else:
            emoji = _severity_emoji(result.severity)
            label = "Clean" if result.severity == 0 else f"{result.severity}/100"
            lines.append(f"| {agent_name} | {label} | {emoji} |")

    lines.append("\n*Powered by [Code Review Agent](https://github.com)*")
    return "\n".join(lines)


class GitHubClient:
    """Thin wrapper around the GitHub REST API."""

    def __init__(self, token: str):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def get_pr_diff(self, repo: str, pr_number: int) -> str:
        """Fetch the unified diff for a pull request."""
        url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}"
        response = requests.get(
            url,
            headers={**self._headers, "Accept": "application/vnd.github.v3.diff"},
            timeout=30,
        )
        response.raise_for_status()
        return response.text

    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        """Post a comment on the PR's conversation thread."""
        url = f"{_GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
        response = requests.post(
            url,
            headers=self._headers,
            json={"body": body},
            timeout=30,
        )
        response.raise_for_status()
        logger.info("Posted review comment on %s#%d", repo, pr_number)

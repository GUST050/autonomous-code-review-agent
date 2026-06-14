"""
github_client.py — GitHub REST API for fetching PR diffs and posting PR reviews.
"""
import logging
from typing import Dict, List, Optional

import requests

from config import APPROVAL_THRESHOLD
from schemas.response import AgentResponse
from schemas.fix_response import FixResponse

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"


def _severity_emoji(severity: int) -> str:
    if severity >= APPROVAL_THRESHOLD:
        return "🔴"
    if severity >= 60:
        return "🟠"
    if severity >= 30:
        return "🟡"
    if severity > 0:
        return "🟢"
    return "✅"


def _review_event(max_severity: int) -> str:
    """Decide GitHub review event based on worst severity found."""
    if max_severity >= APPROVAL_THRESHOLD:
        return "REQUEST_CHANGES"
    if max_severity > 0:
        return "COMMENT"
    return "APPROVE"


def _review_header(max_severity: int, issue_count: int) -> str:
    if max_severity == 0:
        return "✅ **No issues found** — this PR looks good to merge."
    emoji = _severity_emoji(max_severity)
    label = "critical issue" if max_severity >= APPROVAL_THRESHOLD else "issue"
    plural = "s" if issue_count != 1 else ""
    return f"{emoji} **{issue_count} {label}{plural} found** — severity {max_severity}/100"


def format_pr_review(
    results: Dict[str, Optional[AgentResponse]],
    fix_result: Optional[FixResponse] = None,
) -> str:
    """
    Build a professional GitHub PR review body in markdown.
    Includes findings per agent, fix suggestions, and a summary table.
    """
    max_severity = max(
        (r.severity for r in results.values() if r), default=0
    )
    all_findings = [
        (agent, r)
        for agent, r in results.items()
        if r and r.severity > 0 and r.findings
    ]
    issue_count = sum(len(r.findings) for _, r in all_findings)

    lines: List[str] = [
        "## 🔍 Autonomous Code Review",
        "",
        _review_header(max_severity, issue_count),
        "",
    ]

    # ── Per-agent findings ────────────────────────────────────────────────
    for agent_name, result in all_findings:
        emoji = _severity_emoji(result.severity)
        lines += [
            "---",
            f"### {emoji} {agent_name} — {result.severity}/100",
            "",
        ]
        for finding in result.findings:
            lines.append(f"- {finding}")

        if result.references:
            lines.append("")
            lines.append("**References:** " + " · ".join(result.references))

        lines.append("")

    # ── Fix suggestions ───────────────────────────────────────────────────
    if fix_result and (fix_result.changes or fix_result.unfixable):
        lines += ["---", "### 🔧 Suggested Fixes", ""]
        for i, change in enumerate(fix_result.changes, 1):
            lines.append(f"**{i}.** {change}")
        lines.append("")

        if fix_result.fixed_code:
            lines += [
                "<details>",
                "<summary>View complete fixed file</summary>",
                "",
                "```python",
                fix_result.fixed_code.strip(),
                "```",
                "",
                "</details>",
                "",
            ]

        if fix_result.unfixable:
            lines += ["**Requires manual intervention:**", ""]
            for item in fix_result.unfixable:
                lines.append(f"- ⚠️ {item}")
            lines.append("")

    # ── Agent summary table ───────────────────────────────────────────────
    lines += [
        "---",
        "### Agent Summary",
        "",
        "| Agent | Severity | Status |",
        "|-------|----------|--------|",
    ]
    for agent_name, result in results.items():
        if result is None:
            lines.append(f"| {agent_name} | — | ⚠️ Unavailable |")
        else:
            emoji = _severity_emoji(result.severity)
            label = "Clean" if result.severity == 0 else f"{result.severity}/100"
            lines.append(f"| {agent_name} | {label} | {emoji} |")

    lines += [
        "",
        "*Powered by [Autonomous Code Review Agent](https://github.com/GUST050/autonomous-code-review-agent)*",
    ]
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

    def post_pr_review(
        self,
        repo: str,
        pr_number: int,
        body: str,
        event: str,
    ) -> None:
        """
        Post a formal PR review (shows as Approved / Changes requested / Comment).
        event: "APPROVE" | "REQUEST_CHANGES" | "COMMENT"
        """
        url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews"
        response = requests.post(
            url,
            headers=self._headers,
            json={"body": body, "event": event},
            timeout=30,
        )
        response.raise_for_status()
        logger.info("Posted %s review on %s#%d", event, repo, pr_number)

    def post_pr_comment(self, repo: str, pr_number: int, body: str) -> None:
        """Post a plain comment on the PR conversation thread."""
        url = f"{_GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
        response = requests.post(
            url,
            headers=self._headers,
            json={"body": body},
            timeout=30,
        )
        response.raise_for_status()
        logger.info("Posted comment on %s#%d", repo, pr_number)

"""
github_client.py — GitHub REST API for fetching PR diffs and posting PR reviews.
"""
import base64
import json
import logging
import re
from typing import Dict, List, Optional

import requests

from config import APPROVAL_THRESHOLD
from schemas.response import AgentResponse
from schemas.fix_response import FixResponse
from utils.diff_parser import FunctionLocation, extract_function_name, get_function_ranges, get_diff_line_set

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"

# Delimiters for the hidden HTML comment that carries serialized findings.
# Using base64 avoids any issues with special characters inside findings text.
_FINDINGS_MARKER = "<!-- agent-findings-v1:"
_FINDINGS_CLOSE  = " -->"


# ── Findings serialization helpers ────────────────────────────────────────────

def _embed_findings(results: Dict[str, Optional[AgentResponse]]) -> str:
    """
    Serialize agent results to a hidden HTML comment.

    Embedded at the end of every PR review body so the human-in-the-loop
    'fix' command can retrieve previous findings without a database.
    """
    data = {}
    for agent, r in results.items():
        if r:
            data[agent] = {
                "findings":   r.findings,
                "severity":   r.severity,
                "confidence": r.confidence,
                "locations":  r.locations,
                "reasoning":  r.reasoning,
            }
    encoded = base64.b64encode(json.dumps(data).encode()).decode()
    return f"{_FINDINGS_MARKER}{encoded}{_FINDINGS_CLOSE}"


def extract_findings_from_review(body: str) -> Dict[str, dict]:
    """
    Extract and deserialize agent findings from a PR review body.

    Searches for the hidden HTML comment added by _embed_findings() and
    returns the decoded data dict. Returns {} if no findings are embedded
    or if decoding fails for any reason.
    """
    if not body:
        return {}
    pattern = (
        re.escape(_FINDINGS_MARKER)
        + r"([A-Za-z0-9+/=]+)"
        + re.escape(_FINDINGS_CLOSE)
    )
    match = re.search(pattern, body)
    if not match:
        return {}
    try:
        return json.loads(base64.b64decode(match.group(1)).decode())
    except Exception:
        return {}


# ── Inline comment builder ────────────────────────────────────────────────────

def build_review_comments(
    results: Dict[str, Optional[AgentResponse]],
    diff_locations: Dict[str, FunctionLocation],
) -> List[dict]:
    """
    Build the list of inline review comment dicts for the GitHub Reviews API.

    For each finding whose function can be located in the diff, create one
    inline comment placed on the first added line of that function.  Findings
    with no matching diff location are omitted here — they remain visible in
    the review body summary.

    Returns a list ready to pass as the 'comments' field of POST /pulls/reviews.
    Each dict contains: path, line, side ("RIGHT"), body.
    """
    comments: List[dict] = []
    seen: set = set()  # deduplicate on (path, line, finding_text)

    for agent_name, result in results.items():
        if not result or not result.findings:
            continue

        for finding in result.findings:
            func_name = extract_function_name(finding)
            if not func_name:
                continue

            loc = diff_locations.get(func_name)
            if not loc:
                continue

            key = (loc.path, loc.line, finding)
            if key in seen:
                continue
            seen.add(key)

            emoji = _finding_emoji(finding)
            body = f"{emoji}**{agent_name}**\n\n{finding}"
            comments.append({
                "path": loc.path,
                "line": loc.line,
                "side": "RIGHT",
                "body": body,
            })

    return comments


# ── Suggestion comment builder ───────────────────────────────────────────────

def build_suggestion_comments(
    function_fixes: Dict[str, str],
    file_path: str,
    original_source: str,
    diff_line_set: set,
) -> tuple:
    """
    Build GitHub suggestion comments for fixed functions.

    Uses AST to get exact function line ranges from the original source, then
    checks that every line in the range is visible in the diff.  Only functions
    whose full range is in the diff become suggestions — the user clicks
    "Commit suggestion" to apply each one individually.

    Functions whose lines are not fully visible in the diff are returned in
    fallback so the caller can post them as collapsible copy-paste comments.

    Args:
        function_fixes:  {func_name: fixed_source} — only changed functions
        file_path:       relative path of the file in the repo, e.g. "src/app.py"
        original_source: full content of the original file (used for AST ranges)
        diff_line_set:   set of line numbers visible in the diff for this file

    Returns:
        (comments, fallback) where:
          comments  — list of inline comment dicts for GitHub Reviews API
          fallback  — list of (func_name, fixed_source) for functions that
                      cannot be placed as inline suggestions
    """
    func_ranges = get_function_ranges(original_source)
    comments: List[dict] = []
    fallback: List[tuple] = []

    for func_name, fixed_source in function_fixes.items():
        if func_name not in func_ranges:
            fallback.append((func_name, fixed_source))
            continue

        start, end = func_ranges[func_name]

        # Every line in the function's range must appear in the diff.
        # GitHub rejects suggestions that reference lines outside any hunk.
        if not all(ln in diff_line_set for ln in range(start, end + 1)):
            fallback.append((func_name, fixed_source))
            continue

        code = fixed_source.rstrip("\n")
        body = f"```suggestion\n{code}\n```"
        comment: dict = {"path": file_path, "side": "RIGHT", "body": body}
        if start == end:
            comment["line"] = start
        else:
            comment["start_line"] = start
            comment["start_side"] = "RIGHT"
            comment["line"] = end

        comments.append(comment)

    return comments, fallback


# ── PR review body formatter ──────────────────────────────────────────────────

def _finding_emoji(finding: str) -> str:
    """Map [SEVERITY] tag at the start of a finding string to a leading emoji."""
    f = finding.upper()
    if f.startswith("[CRITICAL]"):
        return "🔴 "
    if f.startswith("[HIGH]"):
        return "🟠 "
    if f.startswith("[MEDIUM]"):
        return "🟡 "
    if f.startswith("[LOW]"):
        return "🟢 "
    return ""


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

    Embeds a hidden HTML comment with serialized findings so the 'fix' command
    can retrieve them later without a database.
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
            lines.append(f"- {_finding_emoji(finding)}{finding}")

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

    # ── Hint for fix command + hidden findings payload ────────────────────
    lines += [
        "",
        "*Powered by [Autonomous Code Review Agent](https://github.com/GUST050/autonomous-code-review-agent)*",
        "",
        _embed_findings(results),
    ]
    return "\n".join(lines)


# ── GitHub REST client ────────────────────────────────────────────────────────

class GitHubClient:
    """Thin wrapper around the GitHub REST API."""

    def __init__(self, token: str):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ── PR metadata ───────────────────────────────────────────────────────

    def get_commit_message(self, repo: str, sha: str) -> str:
        """Fetch the commit message for a given SHA."""
        url = f"{_GITHUB_API}/repos/{repo}/commits/{sha}"
        r = requests.get(url, headers=self._headers, timeout=30)
        r.raise_for_status()
        return r.json()["commit"]["message"]

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

    def get_pr_info(self, repo: str, pr_number: int) -> dict:
        """Fetch PR metadata: head branch name, base branch, author, etc."""
        url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}"
        r = requests.get(url, headers=self._headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_pr_files(self, repo: str, pr_number: int) -> list:
        """
        Get the list of files changed in a PR.
        Each entry contains 'filename', 'status' (added/modified/removed), etc.
        """
        url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}/files"
        r = requests.get(url, headers=self._headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_pr_reviews(self, repo: str, pr_number: int) -> list:
        """
        Get all formal reviews on a PR, newest first.
        Used by the 'fix' command to retrieve embedded findings.
        """
        url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews"
        r = requests.get(url, headers=self._headers, timeout=30)
        r.raise_for_status()
        return list(reversed(r.json()))

    # ── File content ──────────────────────────────────────────────────────

    def get_file_content(self, repo: str, path: str, ref: str) -> dict:
        """
        Fetch a file's decoded text content and blob SHA from a specific branch/ref.

        Returns {"content": str, "sha": str}.
        The SHA is required when updating the file via commit_file().
        """
        url = f"{_GITHUB_API}/repos/{repo}/contents/{path}"
        r = requests.get(url, headers=self._headers, params={"ref": ref}, timeout=30)
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return {"content": content, "sha": data["sha"]}

    def commit_file(
        self,
        repo: str,
        path: str,
        content: str,
        sha: str,
        branch: str,
        message: str,
    ) -> None:
        """
        Update a file on a branch via the Contents API.

        sha must be the current blob SHA of the file (from get_file_content).
        Raises requests.HTTPError on failure (e.g. 409 conflict).
        """
        url = f"{_GITHUB_API}/repos/{repo}/contents/{path}"
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        r = requests.put(
            url,
            headers=self._headers,
            json={
                "message": message,
                "content": encoded,
                "sha":     sha,
                "branch":  branch,
            },
            timeout=30,
        )
        r.raise_for_status()
        logger.info("Committed %s to %s on branch %s", path, repo, branch)

    # ── Review and comment posting ────────────────────────────────────────

    def post_pr_review(
        self,
        repo: str,
        pr_number: int,
        body: str,
        event: str,
        comments: Optional[List[dict]] = None,
    ) -> None:
        """
        Post a formal PR review (shows as Approved / Changes requested / Comment).
        event: "APPROVE" | "REQUEST_CHANGES" | "COMMENT"

        comments: optional list of inline comment dicts, each containing:
            path (str), line (int), side ("RIGHT"), body (str).
        If provided, they are attached to specific lines in the diff.
        If GitHub rejects the inline comments (422), the review is retried
        without them so the body summary is never lost.
        """
        url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews"
        payload: dict = {"body": body, "event": event}
        if comments:
            payload["comments"] = comments

        response = requests.post(url, headers=self._headers, json=payload, timeout=30)

        if response.status_code == 422 and comments:
            # GitHub rejected one or more inline comment positions — fall back
            # to body-only so the summary review is never silently dropped.
            logger.warning(
                "Inline comments rejected by GitHub (422) on %s#%d — retrying without them",
                repo, pr_number,
            )
            payload.pop("comments")
            response = requests.post(url, headers=self._headers, json=payload, timeout=30)

        response.raise_for_status()
        logger.info(
            "Posted %s review on %s#%d (%d inline comments)",
            event, repo, pr_number, len(comments or []),
        )

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

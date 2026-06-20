"""
webhook.py — Flask app that receives GitHub webhook events.

Handles pull_request events (opened, synchronize, reopened):
  1. Fetches diff + PR metadata + file list in parallel.
  2. Fetches all changed Python file contents in parallel.
  3. Runs all five review agents in parallel.
  4. Posts one PR review: findings in body, inline comments on diff lines.

Environment variables:
  GITHUB_TOKEN          Personal Access Token with repo + PR review permissions
  ANTHROPIC_API_KEY     Anthropic API key (Haiku agents)
  OPENAI_API_KEY        OpenAI API key (GPT-4o-mini for code quality)
  GITHUB_WEBHOOK_SECRET Secret set when configuring the webhook on GitHub
"""
import hashlib
import hmac
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from flask import Flask, jsonify, request

from agents.rag import RagEnricher
from review import run_fix_from_responses, run_review_multifile
from schemas.response import AgentResponse
from utils.code_splitter import split_code
from utils.diff_parser import extract_function_name, get_diff_line_set, parse_diff_locations
from utils.github_client import (
    GitHubClient,
    _review_event,
    build_review_comments,
    build_suggestion_comments,
    extract_findings_from_review,
    format_pr_review,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
_GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")


def _valid_signature(payload: bytes, signature: str) -> bool:
    """Return True if the X-Hub-Signature-256 header matches the HMAC of the payload."""
    if not _WEBHOOK_SECRET:
        logger.error("GITHUB_WEBHOOK_SECRET not configured — rejecting all webhook requests")
        return False
    expected = "sha256=" + hmac.new(
        _WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _check_env() -> Optional[str]:
    """Return an error message if any required environment variable is missing, else None."""
    if not _GITHUB_TOKEN:
        return "GITHUB_TOKEN not configured"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY not configured"
    return None


@app.route("/api/webhook", methods=["POST"])
def webhook():
    """Main webhook entry point. Validates signature then routes to the correct event handler."""
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _valid_signature(request.get_data(), signature):
        logger.warning("Rejected request with invalid signature")
        return jsonify({"error": "Invalid signature"}), 401

    event = request.headers.get("X-GitHub-Event", "")

    if event == "ping":
        return jsonify({"ok": True, "message": "pong"})

    if event == "pull_request":
        return _handle_pull_request()

    if event == "issue_comment":
        return _handle_issue_comment()

    return jsonify({"ok": True, "message": "ignored"})


def _handle_pull_request():
    """
    Handle pull_request events (opened, synchronize, reopened).
    Fetches the diff and file contents, runs all review agents in parallel,
    then posts a single PR review with inline findings.
    """
    payload   = request.json
    action    = payload.get("action", "")

    if action not in ("opened", "synchronize", "reopened"):
        return jsonify({"ok": True, "message": "ignored"})

    repo      = payload["repository"]["full_name"]
    pr_number = payload["pull_request"]["number"]
    logger.info("Reviewing %s#%d (action=%s)", repo, pr_number, action)

    env_error = _check_env()
    if env_error:
        return jsonify({"error": env_error}), 500

    client = GitHubClient(token=_GITHUB_TOKEN)

    # ── Phase 1: fetch diff + PR metadata in parallel ─────────────────────
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_diff  = pool.submit(client.get_pr_diff,  repo, pr_number)
        f_info  = pool.submit(client.get_pr_info,  repo, pr_number)
        f_files = pool.submit(client.get_pr_files, repo, pr_number)
        try:
            diff     = f_diff.result()
            pr_info  = f_info.result()
            pr_files = f_files.result()
        except Exception as exc:
            logger.error("GitHub API error for %s#%d: %s", repo, pr_number, exc)
            return jsonify({"error": str(exc)}), 500

    if not diff.strip():
        return jsonify({"ok": True, "message": "empty diff"})

    branch       = pr_info["head"]["ref"]
    python_files = [
        f["filename"] for f in pr_files
        if f["filename"].endswith(".py") and f["status"] != "removed"
    ]

    if not python_files:
        return jsonify({"ok": True, "message": "no Python files changed"})

    # ── Phase 2: fetch all file contents in parallel ──────────────────────
    file_contents: dict = {}
    with ThreadPoolExecutor(max_workers=len(python_files)) as pool:
        futures = {
            pool.submit(client.get_file_content, repo, path, branch): path
            for path in python_files
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                file_contents[path] = future.result()["content"]
            except Exception as exc:
                logger.warning("Could not fetch %s: %s", path, exc)

    if not file_contents:
        return jsonify({"ok": True, "message": "could not fetch file contents"})

    # ── Phase 3: run all five review agents in parallel ───────────────────
    try:
        results = run_review_multifile(file_contents)
        results = RagEnricher().enrich(results)
    except Exception as exc:
        logger.error("Review failed for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500
    max_severity = max((r.severity for r in results.values() if r), default=0)

    # ── Phase 4: build inline finding comments ────────────────────────────
    diff_locations   = parse_diff_locations(diff)
    finding_comments = build_review_comments(results, diff_locations)

    # ── Phase 5: post one unified review ──────────────────────────────────
    review_body  = format_pr_review(results)
    review_event = _review_event(max_severity)

    try:
        client.post_pr_review(repo, pr_number, review_body, review_event, finding_comments)
    except Exception as exc:
        logger.error("Could not post review on %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    logger.info(
        "Review complete for %s#%d — severity %d, event %s, %d findings",
        repo, pr_number, max_severity, review_event, len(finding_comments),
    )
    return jsonify({
        "ok":         True,
        "severity":   max_severity,
        "event":      review_event,
        "findings":   len(finding_comments),
    })


def _build_review_body(results: dict, suggestion_comments: list, fallback_fixes: list) -> str:
    """
    Build the review body for the /fix response.
    If results is provided, uses the normal PR review format.
    Otherwise builds a summary header with counts of inline suggestions and fallback fixes.
    Fallback fixes (functions outside the diff) are rendered as collapsible copy-paste blocks.
    """
    if results:
        body = format_pr_review(results)
    else:
        n = len(suggestion_comments) + len(fallback_fixes)
        body = (
            "## 🔧 Fix Suggestions\n\n"
            f"Found **{n} fix{'es' if n != 1 else ''}** for the issues in the code review above."
        )

    if not suggestion_comments and not fallback_fixes:
        return body

    lines = [body, "", "---", ""]
    if suggestion_comments:
        n = len(suggestion_comments)
        lines.append(
            f"💡 **{n} fix{'es' if n != 1 else ''} suggested inline** — "
            "click **Commit suggestion** on each fix you want to apply."
        )
    if fallback_fixes:
        lines += ["", "**Fixes outside this diff (copy-paste manually):**", ""]
        for func_name, fixed_src, file_path in fallback_fixes:
            lines += [
                "<details>",
                f"<summary><code>{func_name}()</code> in <code>{file_path}</code></summary>",
                "",
                "```python",
                fixed_src.strip(),
                "```",
                "",
                "</details>",
            ]
    return "\n".join(lines)


# ── /fix comment handler ─────────────────────────────────────────────────────

def _handle_issue_comment():
    """/fix command on a PR comment — runs fix agent using findings from the last review."""
    payload = request.json
    action  = payload.get("action", "")

    if action != "created":
        return jsonify({"ok": True, "message": "ignored"})

    # Only handle PR comments (issues don't have pull_request key)
    if "pull_request" not in payload.get("issue", {}):
        return jsonify({"ok": True, "message": "ignored"})

    comment = payload.get("comment", {}).get("body", "")
    if "/fix" not in comment:
        return jsonify({"ok": True, "message": "ignored"})

    repo      = payload["repository"]["full_name"]
    pr_number = payload["issue"]["number"]
    logger.info("Fix requested on %s#%d", repo, pr_number)

    env_error = _check_env()
    if env_error:
        return jsonify({"error": env_error}), 500

    client = GitHubClient(token=_GITHUB_TOKEN)

    # Find the latest bot review with embedded findings
    try:
        reviews = client.get_pr_reviews(repo, pr_number)
    except Exception as exc:
        logger.error("Could not fetch reviews for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    bot_review = next(
        (r for r in reviews if r.get("body") and "agent-findings-v1" in r["body"]),
        None,
    )
    if not bot_review:
        return jsonify({"ok": True, "message": "no review found — open a PR first"})

    findings_data = extract_findings_from_review(bot_review["body"])
    if not findings_data:
        return jsonify({"ok": True, "message": "no findings in review"})

    max_severity = max((d.get("severity", 0) for d in findings_data.values()), default=0)
    if max_severity == 0:
        return jsonify({"ok": True, "message": "no issues to fix"})

    # Reconstruct AgentResponse objects from serialized findings
    results = {
        agent: AgentResponse(
            reasoning="",
            findings=data["findings"],
            severity=data["severity"],
            locations=data.get("locations", []),
        )
        for agent, data in findings_data.items()
    }

    # Fetch PR files and diff in parallel
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_diff  = pool.submit(client.get_pr_diff,  repo, pr_number)
        f_info  = pool.submit(client.get_pr_info,  repo, pr_number)
        f_files = pool.submit(client.get_pr_files, repo, pr_number)
        try:
            diff     = f_diff.result()
            pr_info  = f_info.result()
            pr_files = f_files.result()
        except Exception as exc:
            logger.error("GitHub API error on /fix for %s#%d: %s", repo, pr_number, exc)
            return jsonify({"error": str(exc)}), 500

    branch       = pr_info["head"]["ref"]
    python_files = [
        f["filename"] for f in pr_files
        if f["filename"].endswith(".py") and f["status"] != "removed"
    ]

    if not python_files:
        return jsonify({"ok": True, "message": "no Python files"})

    # Fetch file contents in parallel
    file_contents: dict = {}
    with ThreadPoolExecutor(max_workers=len(python_files)) as pool:
        futures = {
            pool.submit(client.get_file_content, repo, path, branch): path
            for path in python_files
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                file_contents[path] = future.result()["content"]
            except Exception as exc:
                logger.warning("Could not fetch %s: %s", path, exc)

    # Run fix agent and collect suggestions
    diff_line_sets      = get_diff_line_set(diff)
    suggestion_comments: list = []
    fallback_fixes:      list = []

    for path, content in file_contents.items():
        try:
            # Filter merged findings to only those relevant to this file.
            # Findings that mention functions from other files would otherwise
            # be broadcast to all functions in this file by _map_findings().
            file_func_names = {
                s.name for s in split_code(content)
                if s.section_type in ("function", "class")
            }
            file_results: dict = {}
            for agent_name, resp in results.items():
                if not resp or not resp.findings:
                    continue
                relevant = [
                    f for f in resp.findings
                    if (fn := extract_function_name(f)) is None or fn in file_func_names
                ]
                if not relevant:
                    continue
                file_results[agent_name] = AgentResponse(
                    reasoning=resp.reasoning,
                    findings=relevant,
                    severity=resp.severity,
                    confidence=resp.confidence,
                    locations=[loc for loc in (resp.locations or []) if loc in file_func_names],
                )

            if not file_results:
                continue

            fix_result = run_fix_from_responses(content, file_results)
            if not fix_result.function_fixes:
                continue
            diff_lines = diff_line_sets.get(path, set())
            suggestions, fallback = build_suggestion_comments(
                fix_result.function_fixes, path, content, diff_lines,
            )
            suggestion_comments.extend(suggestions)
            fallback_fixes.extend((name, src, path) for name, src in fallback)
        except Exception as exc:
            logger.error("Fix failed for %s on %s#%d: %s", path, repo, pr_number, exc)

    if not suggestion_comments and not fallback_fixes:
        return jsonify({"ok": True, "message": "no auto-fixable issues found"})

    review_body = _build_review_body({}, suggestion_comments, fallback_fixes)

    try:
        client.post_pr_review(repo, pr_number, review_body, "COMMENT", suggestion_comments)
    except Exception as exc:
        logger.error("Could not post fix suggestions on %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    logger.info("Posted %d fix suggestion(s) on %s#%d", len(suggestion_comments), repo, pr_number)
    return jsonify({"ok": True, "suggestions": len(suggestion_comments)})


handler = app

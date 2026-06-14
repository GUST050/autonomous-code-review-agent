"""
webhook.py — Vercel serverless function that receives GitHub webhooks.

Handles two events:
  pull_request  — runs all five review agents and posts a formal PR review.
  issue_comment — if a PR comment body is exactly "fix", runs the fix agent
                  and commits corrected files directly to the PR branch.

Environment variables (set in Vercel dashboard):
  GITHUB_TOKEN          Personal Access Token with repo + PR review permissions
  ANTHROPIC_API_KEY     Your Anthropic API key
  OPENAI_API_KEY        Your OpenAI API key
  XAI_API_KEY           Your xAI API key
  GITHUB_WEBHOOK_SECRET Secret you chose when setting up the webhook on GitHub
"""
import hashlib
import hmac
import logging
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from flask import Flask, jsonify, request

from review import run_fix, run_review
from utils.github_client import (
    GitHubClient,
    _review_event,
    extract_findings_from_review,
    format_pr_review,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
_GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")


def _valid_signature(payload: bytes, signature: str) -> bool:
    if not _WEBHOOK_SECRET:
        logger.warning("GITHUB_WEBHOOK_SECRET not set — skipping signature check")
        return True
    expected = "sha256=" + hmac.new(
        _WEBHOOK_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _check_env() -> Optional[str]:
    """Return an error message if required env vars are missing, else None."""
    if not _GITHUB_TOKEN:
        return "GITHUB_TOKEN not configured"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY not configured"
    return None


@app.route("/api/webhook", methods=["POST"])
def webhook():
    # ── 1. Validate HMAC signature ────────────────────────────────────────
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


# ── pull_request handler ──────────────────────────────────────────────────────

def _handle_pull_request():
    """Review new/updated PR and post formal findings as a PR review."""
    payload = request.json
    action  = payload.get("action", "")

    if action not in ("opened", "synchronize", "reopened"):
        return jsonify({"ok": True, "message": "ignored"})

    repo      = payload["repository"]["full_name"]
    pr_number = payload["pull_request"]["number"]
    logger.info("Reviewing %s#%d (action=%s)", repo, pr_number, action)

    env_error = _check_env()
    if env_error:
        return jsonify({"error": env_error}), 500

    client = GitHubClient(token=_GITHUB_TOKEN)

    # Fetch PR diff (review agents only see what changed, not the whole repo)
    try:
        diff = client.get_pr_diff(repo, pr_number)
    except Exception as exc:
        logger.error("Could not fetch diff for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    if not diff.strip():
        return jsonify({"ok": True, "message": "empty diff — nothing to review"})

    # Run all five review agents (no fix generation yet — that's triggered by 'fix' comment)
    try:
        final_state = run_review(diff, fix=False)
    except Exception as exc:
        logger.error("Review failed for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    results    = final_state.get("results", {})
    fix_result = final_state.get("fix_result")

    # Post formal PR review (findings serialized into a hidden comment inside the body)
    max_severity = max((r.severity for r in results.values() if r), default=0)
    review_body  = format_pr_review(results, fix_result)
    review_event = _review_event(max_severity)

    try:
        client.post_pr_review(repo, pr_number, review_body, review_event)
    except Exception as exc:
        logger.error("Could not post review on %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    logger.info(
        "Review complete for %s#%d — severity %d, event %s",
        repo, pr_number, max_severity, review_event,
    )
    return jsonify({"ok": True, "severity": max_severity, "event": review_event})


# ── issue_comment handler ─────────────────────────────────────────────────────

def _handle_issue_comment():
    """
    When a PR comment body is exactly 'fix':
      1. Read the previous review body to recover serialized agent findings.
      2. Fetch each changed Python file from the PR branch.
      3. Run the fix agent on each file.
      4. Commit corrected files directly to the PR branch.
      5. Post a summary comment listing what was changed.
    """
    payload = request.json
    action  = payload.get("action", "")

    # Only react to newly created comments
    if action != "created":
        return jsonify({"ok": True, "message": "ignored"})

    # Ignore comments on regular issues (not PRs)
    issue = payload.get("issue", {})
    if "pull_request" not in issue:
        return jsonify({"ok": True, "message": "not a PR comment"})

    comment_body = payload.get("comment", {}).get("body", "").strip().lower()
    if comment_body != "fix":
        return jsonify({"ok": True, "message": "not a fix command"})

    pr_number = issue["number"]
    repo      = payload["repository"]["full_name"]
    logger.info("Fix command received for %s#%d", repo, pr_number)

    env_error = _check_env()
    if env_error:
        return jsonify({"error": env_error}), 500

    client = GitHubClient(token=_GITHUB_TOKEN)

    # ── Step 1: Get PR branch name ────────────────────────────────────────
    try:
        pr_info = client.get_pr_info(repo, pr_number)
        branch  = pr_info["head"]["ref"]
    except Exception as exc:
        logger.error("Could not get PR info for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    # ── Step 2: Recover findings from the most recent bot review ─────────
    try:
        reviews = client.get_pr_reviews(repo, pr_number)
    except Exception as exc:
        logger.error("Could not fetch reviews for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    findings_data: dict = {}
    for review in reviews:  # reviews are newest-first
        findings_data = extract_findings_from_review(review.get("body") or "")
        if findings_data:
            break

    if not findings_data:
        client.post_pr_comment(
            repo, pr_number,
            "⚠️ No previous review findings found. "
            "Please wait for the initial review to complete before running `fix`.",
        )
        return jsonify({"ok": True, "message": "no findings"})

    has_issues = any(d.get("severity", 0) > 0 for d in findings_data.values())
    if not has_issues:
        client.post_pr_comment(
            repo, pr_number,
            "✅ The previous review found no issues — nothing to fix!",
        )
        return jsonify({"ok": True, "message": "no issues"})

    # ── Step 3: Get changed Python files ─────────────────────────────────
    try:
        pr_files = client.get_pr_files(repo, pr_number)
    except Exception as exc:
        logger.error("Could not get PR files for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    python_files = [
        f for f in pr_files
        if f["filename"].endswith(".py") and f["status"] != "removed"
    ]

    if not python_files:
        client.post_pr_comment(repo, pr_number, "ℹ️ No Python files changed in this PR.")
        return jsonify({"ok": True, "message": "no python files"})

    # ── Step 4: Fix and commit each file ─────────────────────────────────
    all_changes:   list = []
    all_unfixable: list = []
    errors:        list = []

    for file_info in python_files:
        path = file_info["filename"]
        logger.info("Fixing %s on branch %s", path, branch)

        try:
            file_data  = client.get_file_content(repo, path, branch)
            fix_result = run_fix(file_data["content"], findings_data)

            code_changed = (
                fix_result.fixed_code
                and fix_result.fixed_code != file_data["content"]
            )
            if code_changed:
                commit_msg = "fix: " + "; ".join(fix_result.changes[:3])
                if len(fix_result.changes) > 3:
                    commit_msg += f" (+{len(fix_result.changes) - 3} more)"

                client.commit_file(
                    repo=repo,
                    path=path,
                    content=fix_result.fixed_code,
                    sha=file_data["sha"],
                    branch=branch,
                    message=commit_msg,
                )
                all_changes.extend(fix_result.changes)
                logger.info(
                    "Committed %d fix(es) to %s on branch %s",
                    len(fix_result.changes), path, branch,
                )
            else:
                logger.info("No code change in %s — skipping commit", path)

            all_unfixable.extend(fix_result.unfixable or [])

        except Exception as exc:
            logger.error("Failed to fix %s: %s", path, exc)
            errors.append(f"`{path}`: {exc}")

    # ── Step 5: Post summary comment ──────────────────────────────────────
    summary_lines = ["## 🔧 Auto-Fix Results\n"]

    if all_changes:
        summary_lines.append("**Applied fixes:**")
        for change in all_changes:
            summary_lines.append(f"- ✅ {change}")
        summary_lines.append("")

    if all_unfixable:
        summary_lines.append("**Requires manual intervention:**")
        for item in all_unfixable:
            summary_lines.append(f"- ⚠️ {item}")
        summary_lines.append("")

    if errors:
        summary_lines.append("**Errors:**")
        for err in errors:
            summary_lines.append(f"- ❌ {err}")
        summary_lines.append("")

    if not all_changes and not errors:
        summary_lines.append("ℹ️ No auto-fixable issues were found in the changed files.")

    client.post_pr_comment(repo, pr_number, "\n".join(summary_lines))

    return jsonify({
        "ok":       True,
        "changes":  len(all_changes),
        "unfixable": len(all_unfixable),
        "errors":   len(errors),
    })


handler = app

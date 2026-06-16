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

from review import run_fix, run_fix_from_responses, run_review
from utils.diff_parser import get_diff_line_set, parse_diff_locations
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
    head_sha  = payload["pull_request"]["head"]["sha"]
    logger.info("Reviewing %s#%d (action=%s)", repo, pr_number, action)

    env_error = _check_env()
    if env_error:
        return jsonify({"error": env_error}), 500

    client = GitHubClient(token=_GITHUB_TOKEN)

    # Skip review if this synchronize was triggered by our own auto-fix commit.
    # Without this, every fix commit would re-trigger a review of the already-fixed code.
    if action == "synchronize":
        try:
            commit_msg = client.get_commit_message(repo, head_sha)
            if commit_msg.startswith("auto-fix:"):
                logger.info("Skipping review — triggered by auto-fix commit on %s#%d", repo, pr_number)
                return jsonify({"ok": True, "message": "skipped — auto-fix commit"})
        except Exception as exc:
            logger.warning("Could not check commit message: %s — proceeding with review", exc)

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

    # Build inline comments — each finding placed on the relevant line in the diff
    diff_locations   = parse_diff_locations(diff)
    inline_comments  = build_review_comments(results, diff_locations)

    max_severity = max((r.severity for r in results.values() if r), default=0)
    review_body  = format_pr_review(results, fix_result)
    review_event = _review_event(max_severity)

    try:
        client.post_pr_review(repo, pr_number, review_body, review_event, inline_comments)
    except Exception as exc:
        logger.error("Could not post review on %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    # Automatically post fix suggestions when issues are found — no /fix command needed
    suggestions_posted = 0
    if max_severity > 0:
        suggestions_posted = _post_fix_suggestions(client, repo, pr_number, results, diff)

    logger.info(
        "Review complete for %s#%d — severity %d, event %s, suggestions %d",
        repo, pr_number, max_severity, review_event, suggestions_posted,
    )
    return jsonify({
        "ok": True,
        "severity": max_severity,
        "event": review_event,
        "suggestions": suggestions_posted,
    })


# ── auto fix suggestions ──────────────────────────────────────────────────────

def _post_fix_suggestions(
    client: GitHubClient,
    repo: str,
    pr_number: int,
    results: dict,
    diff: str,
) -> int:
    """
    Run the fix agent on all changed Python files and post GitHub Suggestions.
    Returns the number of inline suggestions posted.
    """
    try:
        pr_files = client.get_pr_files(repo, pr_number)
        pr_info  = client.get_pr_info(repo, pr_number)
        branch   = pr_info["head"]["ref"]
    except Exception as exc:
        logger.warning("Could not get PR files for auto-suggestions on %s#%d: %s", repo, pr_number, exc)
        return 0

    python_files = [
        f for f in pr_files
        if f["filename"].endswith(".py") and f["status"] != "removed"
    ]
    if not python_files:
        return 0

    diff_line_sets          = get_diff_line_set(diff)
    all_suggestion_comments: list = []
    all_fallback:            list = []

    for file_info in python_files:
        path = file_info["filename"]
        try:
            file_data  = client.get_file_content(repo, path, branch)
            fix_result = run_fix_from_responses(file_data["content"], results)

            if not fix_result.function_fixes:
                continue

            diff_lines = diff_line_sets.get(path, set())
            suggestions, fallback = build_suggestion_comments(
                fix_result.function_fixes, path, file_data["content"], diff_lines,
            )
            all_suggestion_comments.extend(suggestions)
            all_fallback.extend((name, src, path) for name, src in fallback)
            logger.info("%s: %d suggestion(s), %d fallback(s)", path, len(suggestions), len(fallback))
        except Exception as exc:
            logger.error("Could not generate suggestions for %s on %s#%d: %s", path, repo, pr_number, exc)

    if not all_suggestion_comments and not all_fallback:
        return 0

    body_lines = [
        "## 🔧 Suggested Fixes",
        "",
        "Click **Commit suggestion** on each fix you want to apply.",
    ]

    if all_fallback:
        body_lines.append("\n**Fixes for functions outside this PR's diff (copy-paste manually):**\n")
        for func_name, fixed_src, file_path in all_fallback:
            body_lines += [
                "<details>",
                f"<summary><code>{func_name}()</code> in <code>{file_path}</code></summary>",
                "",
                "```python",
                fixed_src.strip(),
                "```",
                "",
                "</details>",
                "",
            ]

    try:
        client.post_pr_review(
            repo, pr_number, "\n".join(body_lines), "COMMENT", all_suggestion_comments,
        )
        logger.info("Auto-posted %d suggestion(s) on %s#%d", len(all_suggestion_comments), repo, pr_number)
    except Exception as exc:
        logger.error("Could not post auto-suggestions on %s#%d: %s", repo, pr_number, exc)
        return 0

    return len(all_suggestion_comments)


# ── issue_comment handler ─────────────────────────────────────────────────────

def _handle_issue_comment():
    """
    When a PR comment body is exactly '/fix':
      1. Post an immediate acknowledgement so the user knows the agent is running.
      2. Read the previous review body to recover serialized agent findings.
      3. Fetch each changed Python file from the PR branch.
      4. Run the fix agent on each file.
      5. Commit corrected files directly to the PR branch.
      6. Post a summary comment listing what was changed.
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
    if comment_body != "/fix":
        return jsonify({"ok": True, "message": "not a fix command"})

    pr_number = issue["number"]
    repo      = payload["repository"]["full_name"]
    logger.info("Fix command received for %s#%d", repo, pr_number)

    env_error = _check_env()
    if env_error:
        return jsonify({"error": env_error}), 500

    client = GitHubClient(token=_GITHUB_TOKEN)

    # Acknowledge immediately so the user knows the agent is working.
    # The fix agent takes 30-60 seconds — without this the PR looks unresponsive.
    try:
        client.post_pr_comment(
            repo, pr_number,
            "⏳ Fix agent is running — this usually takes 30–40 seconds. "
            "A summary will appear when done.",
        )
    except Exception as exc:
        logger.warning("Could not post acknowledgement comment: %s", exc)

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

    # ── Step 4: Fetch diff to know which lines are visible ────────────────
    try:
        pr_diff      = client.get_pr_diff(repo, pr_number)
        diff_line_sets = get_diff_line_set(pr_diff)
    except Exception as exc:
        logger.warning("Could not fetch diff for suggestions: %s — continuing", exc)
        diff_line_sets = {}

    # ── Step 5: Fix each file and build per-function suggestions ──────────
    all_suggestion_comments: list = []
    all_fallback:            list = []   # [(func_name, fixed_src, file_path)]
    all_unfixable:           list = []
    all_changes:             list = []
    errors:                  list = []

    for file_info in python_files:
        path = file_info["filename"]
        logger.info("Fixing %s", path)

        try:
            file_data  = client.get_file_content(repo, path, branch)
            fix_result = run_fix(file_data["content"], findings_data)

            all_changes.extend(fix_result.changes)
            all_unfixable.extend(fix_result.unfixable or [])

            if not fix_result.function_fixes:
                logger.info("No function-level changes for %s", path)
                continue

            # Build suggestions for functions visible in the diff;
            # collect fallbacks for functions outside any diff hunk.
            diff_lines = diff_line_sets.get(path, set())
            suggestions, fallback = build_suggestion_comments(
                fix_result.function_fixes,
                path,
                file_data["content"],
                diff_lines,
            )
            all_suggestion_comments.extend(suggestions)
            all_fallback.extend((name, src, path) for name, src in fallback)
            logger.info(
                "%s: %d suggestion(s), %d fallback(s)",
                path, len(suggestions), len(fallback),
            )

        except Exception as exc:
            logger.error("Failed to fix %s: %s", path, exc)
            errors.append(f"`{path}`: {exc}")

    # ── Step 6: Post suggestions as a review (user accepts per fix) ───────
    if all_suggestion_comments:
        suggestion_body = (
            "## 🔧 Fix Suggestions\n\n"
            "Each suggestion below replaces one function. "
            "Click **Commit suggestion** on those you want — skip the ones you don't."
        )
        try:
            client.post_pr_review(
                repo, pr_number, suggestion_body, "COMMENT", all_suggestion_comments
            )
            logger.info(
                "Posted %d suggestion(s) on %s#%d",
                len(all_suggestion_comments), repo, pr_number,
            )
        except Exception as exc:
            logger.error("Could not post suggestions on %s#%d: %s", repo, pr_number, exc)
            errors.append(f"Could not post suggestions: {exc}")

    # ── Step 7: Post summary comment ──────────────────────────────────────
    summary_lines = ["## 🔧 Fix Summary\n"]

    if all_suggestion_comments:
        n = len(all_suggestion_comments)
        summary_lines.append(
            f"**{n} inline suggestion{'s' if n != 1 else ''} posted above** — "
            "accept each one individually with **Commit suggestion**.\n"
        )

    if all_fallback:
        summary_lines.append(
            "**Fixes for functions outside this PR's diff (copy-paste manually):**\n"
        )
        for func_name, fixed_src, file_path in all_fallback:
            summary_lines += [
                "<details>",
                f"<summary><code>{func_name}()</code> in <code>{file_path}</code></summary>",
                "",
                "```python",
                fixed_src.strip(),
                "```",
                "",
                "</details>",
                "",
            ]

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

    if not all_suggestion_comments and not all_fallback and not errors:
        summary_lines.append("ℹ️ No auto-fixable issues were found in the changed files.")

    client.post_pr_comment(repo, pr_number, "\n".join(summary_lines))

    return jsonify({
        "ok":          True,
        "suggestions": len(all_suggestion_comments),
        "fallback":    len(all_fallback),
        "unfixable":   len(all_unfixable),
        "errors":      len(errors),
    })


handler = app

"""
webhook.py — Flask app that receives GitHub webhook events.

Handles pull_request events (opened, synchronize, reopened):
  1. Fetches the full content of every changed Python file.
  2. Runs all five review agents in parallel.
  3. If issues are found, runs the fix agent immediately (part of the same pipeline).
  4. Posts one unified PR review: findings in the body, fixes as inline suggestions.

Environment variables:
  GITHUB_TOKEN          Personal Access Token with repo + PR review permissions
  ANTHROPIC_API_KEY     Anthropic API key (Haiku for review, Sonnet for fixes)
  OPENAI_API_KEY        OpenAI API key (GPT-4o-mini for code quality)
  GITHUB_WEBHOOK_SECRET Secret set when configuring the webhook on GitHub
"""
import hashlib
import hmac
import logging
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from flask import Flask, jsonify, request

from review import run_fix_from_responses, run_review
from utils.diff_parser import get_diff_line_set, parse_diff_locations
from utils.github_client import (
    GitHubClient,
    _review_event,
    build_review_comments,
    build_suggestion_comments,
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

    return jsonify({"ok": True, "message": "ignored"})


# ── pull_request handler ──────────────────────────────────────────────────────

def _handle_pull_request():
    """Review new/updated PR: run all agents, fix inline, post one unified review."""
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

    # Diff is used only for positioning inline comments — review agents get full files.
    try:
        diff = client.get_pr_diff(repo, pr_number)
    except Exception as exc:
        logger.error("Could not fetch diff for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    if not diff.strip():
        return jsonify({"ok": True, "message": "empty diff — nothing to review"})

    try:
        pr_info      = client.get_pr_info(repo, pr_number)
        pr_files     = client.get_pr_files(repo, pr_number)
        branch       = pr_info["head"]["ref"]
        python_files = [
            f for f in pr_files
            if f["filename"].endswith(".py") and f["status"] != "removed"
        ]
    except Exception as exc:
        logger.error("Could not fetch PR files for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    if not python_files:
        return jsonify({"ok": True, "message": "no Python files changed"})

    combined_code = ""
    file_contents: dict = {}
    for file_info in python_files:
        path = file_info["filename"]
        try:
            file_data = client.get_file_content(repo, path, branch)
            file_contents[path] = file_data["content"]
            combined_code += f"# === {path} ===\n{file_data['content']}\n\n"
        except Exception as exc:
            logger.warning("Could not fetch %s: %s", path, exc)

    if not combined_code.strip():
        return jsonify({"ok": True, "message": "could not fetch file contents"})

    try:
        final_state = run_review(combined_code, fix=False)
    except Exception as exc:
        logger.error("Review failed for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    results      = final_state.get("results", {})
    max_severity = max((r.severity for r in results.values() if r), default=0)

    # ── Build inline finding comments ─────────────────────────────────────
    diff_locations   = parse_diff_locations(diff)
    finding_comments = build_review_comments(results, diff_locations)

    # ── Run fix agent as part of the pipeline (before posting anything) ───
    # Findings → fixes → one unified review.  The author sees a single event:
    # findings in the body, clickable suggestions inline on the diff.
    suggestion_comments: list = []
    fallback_fixes:      list = []
    if max_severity > 0:
        diff_line_sets = get_diff_line_set(diff)
        for path, content in file_contents.items():
            try:
                fix_result = run_fix_from_responses(content, results)
                if not fix_result.function_fixes:
                    continue
                diff_lines = diff_line_sets.get(path, set())
                suggestions, fallback = build_suggestion_comments(
                    fix_result.function_fixes, path, content, diff_lines,
                )
                suggestion_comments.extend(suggestions)
                fallback_fixes.extend((name, src, path) for name, src in fallback)
                logger.info("%s: %d suggestion(s), %d fallback(s)", path, len(suggestions), len(fallback))
            except Exception as exc:
                logger.error("Fix generation failed for %s on %s#%d: %s", path, repo, pr_number, exc)

    # ── Post one unified review ────────────────────────────────────────────
    review_body  = _build_review_body(results, suggestion_comments, fallback_fixes)
    review_event = _review_event(max_severity)
    all_inline   = finding_comments + suggestion_comments

    try:
        client.post_pr_review(repo, pr_number, review_body, review_event, all_inline)
    except Exception as exc:
        logger.error("Could not post review on %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    logger.info(
        "Review complete for %s#%d — severity %d, event %s, %d findings, %d suggestions",
        repo, pr_number, max_severity, review_event,
        len(finding_comments), len(suggestion_comments),
    )
    return jsonify({
        "ok": True,
        "severity":    max_severity,
        "event":       review_event,
        "findings":    len(finding_comments),
        "suggestions": len(suggestion_comments),
    })


def _build_review_body(results: dict, suggestion_comments: list, fallback_fixes: list) -> str:
    """Findings summary + inline suggestion count + fallback fixes — all in one body."""
    body = format_pr_review(results)

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
        lines += ["", "**Fixes for functions outside this diff (copy-paste manually):**", ""]
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


handler = app

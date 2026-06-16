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

from review import run_review
from utils.diff_parser import parse_diff_locations
from utils.github_client import (
    GitHubClient,
    _review_event,
    build_review_comments,
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
    if not _GITHUB_TOKEN:
        return "GITHUB_TOKEN not configured"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY not configured"
    return None


@app.route("/api/webhook", methods=["POST"])
def webhook():
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


def _handle_pull_request():
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

    combined_code = "".join(
        f"# === {path} ===\n{content}\n\n"
        for path, content in file_contents.items()
    )

    # ── Phase 3: run all five review agents in parallel ───────────────────
    try:
        final_state = run_review(combined_code, fix=False)
    except Exception as exc:
        logger.error("Review failed for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    results      = final_state.get("results", {})
    max_severity = max((r.severity for r in results.values() if r), default=0)

    # ── Phase 4: post one unified review ──────────────────────────────────
    diff_locations   = parse_diff_locations(diff)
    inline_comments  = build_review_comments(results, diff_locations)
    review_body      = format_pr_review(results)
    review_event     = _review_event(max_severity)

    try:
        client.post_pr_review(repo, pr_number, review_body, review_event, inline_comments)
    except Exception as exc:
        logger.error("Could not post review on %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    logger.info(
        "Review complete for %s#%d — severity %d, event %s, %d inline comments",
        repo, pr_number, max_severity, review_event, len(inline_comments),
    )
    return jsonify({
        "ok":       True,
        "severity": max_severity,
        "event":    review_event,
        "comments": len(inline_comments),
    })


handler = app

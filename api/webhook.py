"""
webhook.py — Vercel serverless function that receives GitHub webhooks and
runs the code review pipeline on every pull request.

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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from flask import Flask, jsonify, request

from review import run_review
from utils.github_client import GitHubClient, format_pr_review, _review_event

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


@app.route("/api/webhook", methods=["POST"])
def webhook():
    # ── 1. Validate signature ─────────────────────────────────────────────
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _valid_signature(request.get_data(), signature):
        logger.warning("Rejected request with invalid signature")
        return jsonify({"error": "Invalid signature"}), 401

    event = request.headers.get("X-GitHub-Event", "")

    if event == "ping":
        return jsonify({"ok": True, "message": "pong"})

    if event != "pull_request":
        return jsonify({"ok": True, "message": "ignored"})

    payload  = request.json
    action   = payload.get("action", "")

    if action not in ("opened", "synchronize", "reopened"):
        return jsonify({"ok": True, "message": "ignored"})

    repo      = payload["repository"]["full_name"]
    pr_number = payload["pull_request"]["number"]
    logger.info("Reviewing %s#%d (action=%s)", repo, pr_number, action)

    # ── 2. Check required env vars ────────────────────────────────────────
    if not _GITHUB_TOKEN:
        return jsonify({"error": "GITHUB_TOKEN not configured"}), 500
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 500

    client = GitHubClient(token=_GITHUB_TOKEN)

    # ── 3. Fetch PR diff ─────────────────────────────────────────────────
    try:
        diff = client.get_pr_diff(repo, pr_number)
    except Exception as exc:
        logger.error("Could not fetch diff for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    if not diff.strip():
        return jsonify({"ok": True, "message": "empty diff — nothing to review"})

    # ── 4. Run review agents + fix generator ─────────────────────────────
    try:
        final_state = run_review(diff, fix=True)
    except Exception as exc:
        logger.error("Review failed for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    results    = final_state.get("results", {})
    fix_result = final_state.get("fix_result")

    # ── 5. Post formal PR review ─────────────────────────────────────────
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


handler = app

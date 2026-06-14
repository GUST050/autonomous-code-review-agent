"""
webhook.py — Vercel serverless function that receives GitHub webhooks and
runs the code review pipeline on every pull request.

Environment variables (set in Vercel dashboard):
  GITHUB_TOKEN          Personal Access Token with repo + PR comment permissions
  ANTHROPIC_API_KEY     Your Anthropic API key
  GITHUB_WEBHOOK_SECRET Secret you chose when setting up the webhook on GitHub
"""
import hashlib
import hmac
import logging
import os
import sys

# Make src/ importable on Vercel (PYTHONPATH=src is set in vercel.json)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from flask import Flask, jsonify, request

from review import run_review
from utils.github_client import GitHubClient, format_github_comment

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
_GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")


def _valid_signature(payload: bytes, signature: str) -> bool:
    """Return True if the HMAC-SHA256 signature matches the webhook secret."""
    if not _WEBHOOK_SECRET:
        logger.warning("GITHUB_WEBHOOK_SECRET is not set — skipping signature check")
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

    # ── 2. Acknowledge ping (GitHub sends this on webhook creation) ───────
    if event == "ping":
        return jsonify({"ok": True, "message": "pong"})

    # ── 3. Only handle pull_request events ───────────────────────────────
    if event != "pull_request":
        return jsonify({"ok": True, "message": "ignored"})

    payload  = request.json
    action   = payload.get("action", "")

    if action not in ("opened", "synchronize", "reopened"):
        return jsonify({"ok": True, "message": "ignored"})

    repo      = payload["repository"]["full_name"]
    pr_number = payload["pull_request"]["number"]
    logger.info("Reviewing %s#%d (action=%s)", repo, pr_number, action)

    # ── 4. Check required env vars ────────────────────────────────────────
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not _GITHUB_TOKEN:
        return jsonify({"error": "GITHUB_TOKEN not configured"}), 500
    if not anthropic_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 500

    client = GitHubClient(token=_GITHUB_TOKEN)

    # ── 5. Fetch PR diff ─────────────────────────────────────────────────
    try:
        diff = client.get_pr_diff(repo, pr_number)
    except Exception as exc:
        logger.error("Could not fetch diff for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    if not diff.strip():
        return jsonify({"ok": True, "message": "empty diff — nothing to review"})

    # ── 6. Run review agents ─────────────────────────────────────────────
    try:
        final_state = run_review(diff)
    except Exception as exc:
        logger.error("Review failed for %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    # ── 7. Post results as PR comment ────────────────────────────────────
    results = final_state.get("results", {})
    comment = format_github_comment(results)

    try:
        client.post_pr_comment(repo, pr_number, comment)
    except Exception as exc:
        logger.error("Could not post comment on %s#%d: %s", repo, pr_number, exc)
        return jsonify({"error": str(exc)}), 500

    logger.info("Review complete for %s#%d", repo, pr_number)
    return jsonify({"ok": True})


# Vercel looks for a callable named `handler`
handler = app

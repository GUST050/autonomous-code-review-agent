# Autonomous Code Review Agent

> Five AI specialists review every pull request in parallel — security vulnerabilities, performance issues, and code quality posted as a formal GitHub review within 30–45 seconds.

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://python.org)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2+-orange)](https://langchain-ai.github.io/langgraph/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## What it does

Open a pull request → five specialist AI agents review it in parallel → a formal GitHub review is posted with findings grouped by type and severity.

Comment `/fix` on any PR → the fix agent commits corrected code directly to the branch.

**The five review specialists:**

| Agent | Model | What it looks for |
|-------|-------|-------------------|
| Injection Expert | Claude Haiku | SQL injection, XSS, SSTI, SSRF, path traversal, insecure deserialization |
| Auth Expert | Claude Haiku | Broken auth, IDOR, CSRF, missing rate limiting, privilege escalation |
| Secrets Expert | Claude Haiku | Hardcoded credentials, weak cryptography, plaintext sensitive data |
| Performance Expert | Grok Mini | O(n²) algorithms, N+1 queries, inefficient data structures |
| Code Quality Expert | GPT-4o Mini | Naming, type hints, SRP violations, cognitive complexity |

Each finding includes a severity tag (`[CRITICAL]`, `[HIGH]`, `[MEDIUM]`, `[LOW]`), the affected function, and the concrete impact.

---

## Setup

### 1. Deploy to Vercel

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2FGUST050%2Fautonomous-code-review-agent&env=ANTHROPIC_API_KEY,OPENAI_API_KEY,GITHUB_TOKEN,GITHUB_WEBHOOK_SECRET&envDescription=API+keys+for+LLM+providers+and+GitHub+integration&project-name=code-review-agent&repository-name=autonomous-code-review-agent)

Vercel will ask for five environment variables during deployment:

| Variable | Where to get it |
|----------|----------------|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com) |
| `GITHUB_TOKEN` | GitHub → Settings → Developer settings → Personal access tokens → Generate new token (classic). Required scopes: `repo` |
| `GITHUB_WEBHOOK_SECRET` | Any random string — you'll use the same value on GitHub in the next step |

> **Vercel plan note:** The five review agents typically finish in 30–45 seconds, within the free Hobby plan limit. The `/fix` command can take up to 60 seconds — if it times out, upgrade to a Pro plan.

### 2. Add the webhook to your repository

In the GitHub repository you want reviewed, go to **Settings → Webhooks → Add webhook**:

- **Payload URL:** `https://your-deployment.vercel.app/api/webhook`
- **Content type:** `application/json`
- **Secret:** the same value you used for `GITHUB_WEBHOOK_SECRET`
- **Which events:** select **Let me select individual events**, then check:
  - ✅ Pull requests
  - ✅ Issue comments

Click **Add webhook**. GitHub sends a ping — a green checkmark means it's working.

---

## Usage

**Automatic review**

Open a pull request or push new commits. A formal review appears within 30–45 seconds, with findings like:

```
## 🔍 Autonomous Code Review

🟠 2 issues found — severity 72/100

---
### 🟠 Injection Expert — 72/100

- 🟠 [HIGH] get_user(): SQL query built with f-string — attacker can dump entire users table with ' OR 1=1--
- 🟡 [MEDIUM] search(): user input passed to LIKE clause without parameterization

---
### Agent Summary

| Agent            | Severity | Status |
|------------------|----------|--------|
| Injection Expert | 72/100   | 🟠     |
| Auth Expert      | Clean    | ✅     |
| Secrets Expert   | Clean    | ✅     |
| Performance Expert | Clean  | ✅     |
| Code Quality Expert | 25/100 | 🟢   |

> 💬 Comment `/fix` on this PR to let the agent commit fixes directly to the branch.
```

**Auto-fix**

Comment `/fix` on the PR. The fix agent:
1. Posts an acknowledgement comment immediately
2. Reads the stored findings from the previous review
3. Commits corrected Python files directly to your branch
4. Posts a summary of every change applied

---

## Local development

```bash
git clone https://github.com/GUST050/autonomous-code-review-agent.git
cd autonomous-code-review-agent

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in your API keys in .env

# Review a local file
python src/main.py path/to/file.py

# Run tests
pytest tests/
```

---

## Environment variables

```env
# LLM API Keys
ANTHROPIC_API_KEY=sk-ant-...      # Injection Expert, Auth Expert, Secrets Expert, Performance Expert, Fix Generator
OPENAI_API_KEY=sk-...             # Code Quality Expert

# GitHub
GITHUB_TOKEN=ghp_...              # PAT with repo scope
GITHUB_WEBHOOK_SECRET=...         # Arbitrary secret for HMAC-SHA256 webhook validation

# LangSmith (optional — enables tracing at smith.langchain.com)
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=autonomous-code-review
```

---

## Architecture

```
GitHub webhook
      │
      ▼
api/webhook.py  (Vercel serverless function)
      │
      ├── pull_request (opened / synchronize / reopened)
      │         │
      │         ├── skip if commit message starts with "auto-fix:"
      │         │
      │         ▼
      │   LangGraph review graph
      │   ┌──────────────────────────────────────────┐
      │   │  5 agents run in parallel                 │
      │   │  InjectionAgent   AuthAgent               │
      │   │  SecretsAgent     PerformanceAgent        │
      │   │  QualityAgent                             │
      │   └──────────────────────────────────────────┘
      │         │
      │         ▼
      │   Findings serialized → hidden HTML comment in review body
      │   → Posted as formal GitHub PR review (APPROVE / COMMENT / REQUEST_CHANGES)
      │
      └── issue_comment (body == "/fix")
                │
                ▼
          Deserialize findings from previous review body
          → FixGeneratorAgent runs per-function fixes in parallel
          → Commits corrected files to PR branch
          → Posts summary comment
```

**How findings persist without a database:** Findings from each review are base64-encoded into a hidden HTML comment inside the PR review body. When `/fix` is triggered, the agent decodes them directly from GitHub — no external storage needed.

---

## Cost

A typical review of a small-to-medium PR costs approximately **$0.002–$0.008** depending on diff size. The `/fix` command adds another **$0.01–$0.03** (Claude Sonnet for code generation).

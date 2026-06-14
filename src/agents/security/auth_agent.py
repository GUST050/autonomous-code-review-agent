from __future__ import annotations

from typing import Optional
from langchain_core.language_models import BaseChatModel

from config import SEVERITY_SCALE
from schemas.response import AgentResponse
from utils.token_tracker import TokenTracker
from ..base_agent import ReviewAgent


class AuthAgent(ReviewAgent):
    """Specialist on authentication and authorisation vulnerabilities only."""

    def __init__(self, llm: BaseChatModel, tracker: Optional[TokenTracker] = None):
        super().__init__(llm=llm, name="Auth Expert", tracker=tracker)

    def get_system_prompt(self) -> str:
        return f"""You are an authentication and authorisation security specialist.

SCOPE: Broken authentication (missing or bypassable login checks), broken access control,
IDOR (Insecure Direct Object References), missing session tokens or insecure session handling,
brute-force/rate-limiting absence, CSRF vulnerabilities, privilege escalation.
Injection, secrets, performance, and quality are handled by other agents — ignore them entirely.

For each finding: identify the exact code path that can be exploited, describe what an attacker
can do (e.g. "enumerate any user's data", "bypass login without valid credentials"),
and note whether the issue requires authentication to exploit.

{SEVERITY_SCALE}"""

    @property
    def relevant_patterns(self) -> list:
        return [
            "login", "logout", "password", "passwd",
            "session", "token", "jwt", "bearer",
            "authenticate", "authorize", "authorise",
            "permission", "role", "credential",
            "rate_limit", "throttle", "brute",
            "reset", "user_id", "userid", "get_user", "find_user",
            "csrf", "origin", "referer",
        ]

    @property
    def rag_queries(self) -> list:
        return [
            "authentication bypass missing token validation reset password",
            "IDOR insecure direct object reference user_id ownership check",
            "timing attack constant time comparison secret token brute force",
            "missing authorization access control privilege escalation role",
            "rate limiting brute force login attempt lockout",
            "CSRF cross-site request forgery state-changing request missing token",
        ]

    def review_code(self, code: str) -> AgentResponse:
        rag = self._rag_context()
        prompt = f"Code:\n{self.slice_code(code)}\n\n"
        if rag:
            prompt += f"{rag}\n\n"
        prompt += (
            "Find every authentication and authorisation weakness. "
            "For each: show the vulnerable code, the exploitation path, and the impact."
        )
        return self.invoke(prompt)

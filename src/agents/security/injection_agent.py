from __future__ import annotations

from typing import Optional
from langchain_core.language_models import BaseChatModel

from config import SEVERITY_SCALE
from schemas.response import AgentResponse
from utils.token_tracker import TokenTracker
from ..base_agent import ReviewAgent


class InjectionAgent(ReviewAgent):
    """Specialist on injection vulnerabilities only."""

    def __init__(self, llm: BaseChatModel, tracker: Optional[TokenTracker] = None):
        super().__init__(llm=llm, name="Injection Expert", tracker=tracker)

    def get_system_prompt(self) -> str:
        return f"""You are an injection vulnerability specialist.

SCOPE: SQL injection (classic, blind, time-based, UNION), command injection, XSS (reflected/stored/DOM),
template injection (SSTI), LDAP injection, path traversal, XML/XPath injection,
insecure deserialization (pickle.loads/marshal.loads/yaml.load with untrusted input — enables RCE),
SSRF (Server-Side Request Forgery — user-controlled URL passed to requests.get/urllib/httpx).
Auth, secrets, performance, and quality are handled by other agents — ignore them entirely.

DO NOT FLAG these safe patterns (they are correct code, not vulnerabilities):
- Parameterized queries: cursor.execute(sql, (param,)) or cursor.execute(sql, [param]) — SAFE
- ORM queries: Model.objects.filter(field=value), session.query(Model).filter_by(...) — SAFE
- SQL strings with only hardcoded/constant values and no user input flowing in — SAFE
- requests.get(url) where url is a hardcoded string or from config, not from user input — SAFE
- Jinja2 {{ variable }} templates (auto-escaped by default) — SAFE unless Markup() wraps untrusted input
- yaml.safe_load() — SAFE (only yaml.load() with Loader=Loader or no loader is dangerous)

For each finding you DO report: name the injection type, show the exact vulnerable code pattern,
describe a concrete attack payload, and explain what an attacker can achieve.

{SEVERITY_SCALE}"""

    @property
    def relevant_patterns(self) -> list:
        return [
            "execute", "cursor", "SELECT", "INSERT", "UPDATE", "DELETE", "FROM ",
            "sqlite3", "psycopg", "pymysql", "pymongo",
            "subprocess", "os.system", "os.popen", "shell=True",
            "eval(", "exec(", "pickle",
            "render(", "render_template", "Markup(", "jinja",
            "requests.", "urllib", "httpx", "http.client",
        ]

    @property
    def rag_queries(self) -> list:
        return [
            "SQL injection string concatenation database query execute",
            "pickle deserialization untrusted input remote code execution",
            "eval exec arbitrary code injection template SSTI",
            "command injection subprocess shell os.system user input",
            "path traversal directory file access user controlled",
            "XSS cross-site scripting user input HTML render template output",
            "SSRF server-side request forgery URL fetch requests urllib",
        ]

    def review_code(self, code: str) -> AgentResponse:
        rag = self._rag_context()
        prompt = f"Code:\n{self.slice_code(code)}\n\n"
        if rag:
            prompt += f"{rag}\n\n"
        prompt += (
            "Find every injection vulnerability. "
            "For each: show the vulnerable expression, a working attack payload, and exploitability."
        )
        return self.invoke(prompt)

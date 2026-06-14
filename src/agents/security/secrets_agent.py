from __future__ import annotations

from typing import Optional
from langchain_core.language_models import BaseChatModel

from config import SEVERITY_SCALE
from schemas.response import AgentResponse
from utils.token_tracker import TokenTracker
from ..base_agent import ReviewAgent


class SecretsAgent(ReviewAgent):
    """Specialist on exposed secrets and cryptographic weaknesses only."""

    def __init__(self, llm: BaseChatModel, tracker: Optional[TokenTracker] = None):
        super().__init__(llm=llm, name="Secrets Expert", tracker=tracker)

    def get_system_prompt(self) -> str:
        return f"""You are a secrets-exposure and cryptography specialist.

SCOPE:
- Hardcoded credentials — look for patterns: PASSWORD=, API_KEY=, SECRET=, TOKEN=, private_key,
  aws_access_key_id, db_password, connection strings with embedded credentials
- Weak or broken cryptography: MD5/SHA1 for passwords, ECB mode, weak key sizes,
  predictable IVs, insecure random for security purposes
- Sensitive data in plaintext: passwords stored/transmitted without hashing,
  PII in logs or error messages, unencrypted secrets in config files
- Private keys or certificates committed directly in code

Injection, auth, performance, and quality are handled by other agents — ignore them entirely.

For each finding: quote the exact line or pattern, state what is exposed or weakened,
and describe the real-world consequence if exploited.

{SEVERITY_SCALE}"""

    @property
    def relevant_patterns(self) -> list:
        # Header always included — catches module-level DB_PASSWORD, SECRET_KEY, API_TOKEN.
        # Patterns here target functions with crypto or credential handling.
        return [
            "password", "passwd", "secret", "credential",
            "api_key", "apikey", "token", "private_key",
            "hashlib", "md5", "sha1", "sha256",
            "encrypt", "decrypt", "hmac", "fernet",
            "aws_", "bearer", "sk-",
        ]

    @property
    def rag_queries(self) -> list:
        return [
            "hardcoded API key secret token credential password source code",
            "MD5 SHA1 weak hashing password cryptography broken",
            "private key certificate committed source code exposure",
            "predictable random insecure secret key generation",
            "plaintext password PII sensitive data logging exposure",
            "weak encryption key size RSA 1024 AES 56 inadequate strength",
            "missing encryption plaintext sensitive data storage transmission",
        ]

    def review_code(self, code: str) -> AgentResponse:
        rag = self._rag_context()
        prompt = f"Code:\n{self.slice_code(code)}\n\n"
        if rag:
            prompt += f"{rag}\n\n"
        prompt += (
            "Find every hardcoded secret, plaintext sensitive value, and cryptographic weakness. "
            "Quote the exact offending line and describe the real-world risk."
        )
        return self.invoke(prompt)

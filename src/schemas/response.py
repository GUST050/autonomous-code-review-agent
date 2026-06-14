from pydantic import BaseModel, Field
from typing import List


class AgentResponse(BaseModel):
    """
    Standardized response format for all review agents.
    Severity is a numeric 0-100 scale (0=clean, 100=critical).
    """
    reasoning: str = Field(
        description=(
            "One sentence: what was analyzed and what category of issues was found "
            "(or 'No issues found in this domain' if clean)."
        )
    )
    findings: List[str] = Field(
        description=(
            "Specific issues found — up to 10 items. "
            "Each entry MUST start with the affected function name followed by a colon, "
            "then describe the vulnerability and its impact. "
            "Example: 'login(): SQL injection via f-string in execute() — attacker can dump all user passwords'. "
            "Empty list if no issues found."
        )
    )
    severity: int = Field(
        description=(
            "Overall severity of the worst issue found, 0–100. "
            "0 = nothing found. 90–100 = directly exploitable critical issue."
        ),
        default=0,
        ge=0,
        le=100,
    )
    confidence: int = Field(
        description=(
            "Confidence in this assessment, 0–100. "
            "100 = clearly exploitable with a concrete payload. "
            "50 = plausible but context-dependent. "
            "0 = speculative or no findings."
        ),
        default=0,
        ge=0,
        le=100,
    )
    locations: List[str] = Field(
        description=(
            "Names of every function or method that contains at least one issue. "
            "Use the bare name without parentheses, e.g. ['login', 'get_user']. "
            "Must be consistent with the function names used in findings."
        ),
        default_factory=list,
    )
    references: List[str] = Field(
        description="CVE/CWE/OWASP references — populated by RAG enrichment after analysis, not by the LLM.",
        default_factory=list,
    )

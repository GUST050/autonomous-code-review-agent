from pydantic import BaseModel, Field
from typing import Dict, List


class FixResponse(BaseModel):
    """Output from FixGeneratorAgent — a complete, corrected source file."""

    fixed_code: str = Field(
        description=(
            "The COMPLETE source file with every fixable issue resolved. "
            "Must be valid, runnable code. Include the entire file — not snippets or diffs."
        )
    )
    changes: List[str] = Field(
        description="One-line summary per fix applied, e.g. 'Replaced MD5 with sha256 in login()'",
        default_factory=list,
    )
    unfixable: List[str] = Field(
        description=(
            "Issues that require manual intervention (framework config, external services, "
            "architectural decisions). One sentence each explaining why it cannot be auto-fixed."
        ),
        default_factory=list,
    )
    function_fixes: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-function fixed source code for functions that were actually changed. "
            "key = function name, value = complete fixed function source (no trailing blanks). "
            "Populated by FixGeneratorAgent.generate_fixes(); not set by the LLM."
        ),
    )

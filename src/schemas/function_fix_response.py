from typing import List

from pydantic import BaseModel, Field


class FunctionFixResponse(BaseModel):
    """Fix result for a single function or header section."""

    fixed_code: str
    changes: List[str] = Field(default_factory=list)
    needed_imports: List[str] = Field(default_factory=list)  # deduped and merged in header fix
    unfixable: List[str] = Field(default_factory=list)

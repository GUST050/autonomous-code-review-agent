from typing import List

from pydantic import BaseModel


class FunctionFixResponse(BaseModel):
    """Fix result for a single function or header section."""

    fixed_code: str
    changes: List[str] = []
    needed_imports: List[str] = []  # new imports required; deduped and merged in header fix
    unfixable: List[str] = []

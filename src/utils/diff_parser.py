"""
diff_parser.py — Parse unified diffs to map function names to file positions.

Used by the review webhook to place inline GitHub review comments directly
on the added lines where issues were found, rather than in a single review
body block at the bottom of the PR.

Also provides get_function_ranges() (AST-based) and get_diff_line_set() so
the fix handler can build GitHub suggestions placed on the correct line range.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple


@dataclass
class FunctionLocation:
    """The best line to place an inline comment for one function in a PR diff."""
    path: str   # relative file path, e.g. "src/app.py"
    line: int   # line number in the new file (RIGHT side, 1-indexed)


def parse_diff_locations(diff_text: str) -> Dict[str, FunctionLocation]:
    """
    Parse a unified diff and return a mapping from function name to the
    best line to place an inline comment for that function.

    The "best line" is the first added (+) line inside the function body.
    Functions with no added lines are excluded — GitHub only allows inline
    comments on lines that appear in the diff as added or context lines, and
    placing comments on unchanged context risks a 422 from the API.

    When the same function name appears in multiple files, the first
    occurrence (topmost file in the diff) wins.

    Returns:
        dict mapping bare function name (str) → FunctionLocation
    """
    locations: Dict[str, FunctionLocation] = {}
    current_file: Optional[str] = None
    new_line_num = 0
    current_func: Optional[str] = None
    first_added_in_func: Optional[int] = None

    def _flush() -> None:
        nonlocal current_func, first_added_in_func
        if current_func and first_added_in_func is not None and current_file:
            if current_func not in locations:
                locations[current_func] = FunctionLocation(
                    path=current_file,
                    line=first_added_in_func,
                )
        current_func = None
        first_added_in_func = None

    for raw in diff_text.splitlines():
        # ── New file ──────────────────────────────────────────────────────
        if raw.startswith("+++ b/"):
            _flush()
            current_file = raw[6:]
            new_line_num = 0
            continue

        # ── Diff metadata — skip ─────────────────────────────────────────
        if (
            raw.startswith("diff --git")
            or raw.startswith("--- ")
            or raw.startswith("+++ ")
            or raw.startswith("index ")
            or raw.startswith("new file")
            or raw.startswith("deleted file")
            or raw.startswith("\\ No newline")
        ):
            continue

        # ── Hunk header — reset new-file line counter ─────────────────────
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if hunk:
            new_line_num = int(hunk.group(1)) - 1  # incremented before first use
            continue

        if current_file is None:
            continue

        # ── Parse line type ───────────────────────────────────────────────
        if raw.startswith("-"):
            # Removed line — does not advance new-file counter
            continue
        elif raw.startswith("+"):
            new_line_num += 1
            content = raw[1:]
            is_added = True
        else:
            # Context line (space-prefixed or bare)
            new_line_num += 1
            content = raw[1:] if raw.startswith(" ") else raw
            is_added = False

        # ── Detect function / method definition ───────────────────────────
        func_match = re.match(r"[ \t]*(?:async\s+)?def\s+(\w+)\s*\(", content)
        if func_match:
            _flush()
            current_func = func_match.group(1)
            if is_added:
                # The def line itself is added — use it
                first_added_in_func = new_line_num
        elif current_func and is_added and first_added_in_func is None:
            # First added line inside this function
            first_added_in_func = new_line_num

    _flush()
    return locations


def get_function_ranges(source: str) -> Dict[str, Tuple[int, int]]:
    """
    AST-parse a Python source file and return the line range of every
    top-level function or async def.

    Returns {function_name: (start_line, end_line)} where both values are
    1-indexed and end_line is the last line of the function body inclusive.
    Returns {} if the source cannot be parsed (syntax error or empty).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    ranges: Dict[str, Tuple[int, int]] = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ranges[node.name] = (node.lineno, node.end_lineno)
    return ranges


def get_diff_line_set(diff_text: str) -> Dict[str, Set[int]]:
    """
    Parse a unified diff and return the set of line numbers visible in
    each file — both added (+) and context lines.

    Used to verify that a start_line/end_line range is valid for a GitHub
    suggestion comment before posting.  GitHub rejects suggestions whose
    line numbers do not appear in any diff hunk.

    Returns {file_path: set_of_line_numbers}.
    """
    line_sets: Dict[str, Set[int]] = {}
    current_file: Optional[str] = None
    new_line_num = 0

    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:]
            line_sets.setdefault(current_file, set())
            new_line_num = 0
            continue
        if (
            raw.startswith("diff --git")
            or raw.startswith("--- ")
            or raw.startswith("+++ ")
            or raw.startswith("index ")
            or raw.startswith("new file")
            or raw.startswith("deleted file")
            or raw.startswith("\\ No newline")
        ):
            continue

        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if hunk:
            new_line_num = int(hunk.group(1)) - 1
            continue

        if current_file is None:
            continue

        if raw.startswith("-"):
            continue  # removed lines don't exist in new file
        elif raw.startswith("+"):
            new_line_num += 1
            line_sets[current_file].add(new_line_num)
        else:
            new_line_num += 1
            line_sets[current_file].add(new_line_num)

    return line_sets


def extract_function_name(finding: str) -> Optional[str]:
    """
    Extract the bare function name from a formatted finding string.

    Handles both the new severity-tagged format and the legacy format:
        "[HIGH] login(): SQL injection via f-string — attacker can..."
        "login(): SQL injection via f-string"

    Returns the function name as a bare string (no parentheses),
    or None if no function name can be parsed.
    """
    # New format: [SEVERITY] func_name(
    match = re.search(r"\[(?:CRITICAL|HIGH|MEDIUM|LOW)\]\s+(\w+)\s*\(", finding)
    if match:
        return match.group(1)
    # Legacy format: func_name():
    match = re.match(r"(\w+)\s*\(", finding.lstrip())
    if match:
        return match.group(1)
    return None

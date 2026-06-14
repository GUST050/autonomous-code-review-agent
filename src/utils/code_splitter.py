"""
code_splitter.py — Splits Python source into a header section and one section
per top-level function or class definition using Python's ast module.

Trailing blank lines between definitions are attached to the PRECEDING section
so that `"".join(s.source for s in sections)` reconstructs the original file
exactly — no whitespace is lost or duplicated during assembly.
"""
import ast
from dataclasses import dataclass
from typing import List


@dataclass
class CodeSection:
    name: str           # "__header__" | "__other__" | function/class name
    source: str         # raw source text for this section (including trailing blanks)
    section_type: str   # "header" | "function" | "class" | "other"


def split_code(source: str) -> List[CodeSection]:
    """
    Parse a Python source string into a list of CodeSections.

    The header section captures everything before the first top-level
    function or class: module docstring, imports, and module-level constants.

    Each subsequent section holds one top-level def/class and all blank lines
    that follow it up to the next definition (or EOF).

    Falls back to a single "other" section if the source cannot be parsed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [CodeSection("__other__", source, "other")]

    lines = source.splitlines(keepends=True)

    top_level = sorted(
        [
            node
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ],
        key=lambda n: n.lineno,
    )

    if not top_level:
        return [CodeSection("__header__", source, "header")]

    sections: List[CodeSection] = []

    # Header: everything before the first top-level definition (0-indexed line)
    first_def_line = top_level[0].lineno - 1
    sections.append(CodeSection("__header__", "".join(lines[:first_def_line]), "header"))

    # One section per top-level def/class; ends just before the next one (or EOF)
    for i, node in enumerate(top_level):
        start = node.lineno - 1
        end = top_level[i + 1].lineno - 1 if i + 1 < len(top_level) else len(lines)
        src = "".join(lines[start:end])
        kind = (
            "function"
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            else "class"
        )
        sections.append(CodeSection(node.name, src, kind))

    return sections


def trailing_whitespace(source: str) -> str:
    """Return the trailing newlines of a section source (blank lines between defs)."""
    stripped = source.rstrip("\n")
    return source[len(stripped):]


def slice_for_agent(sections: List[CodeSection], patterns: List[str]) -> List[CodeSection]:
    """
    Return only the sections relevant to an agent's domain.

    The header section is always included — it contains imports and module-level
    constants that every agent may need for context.

    A function/class section is included when its source contains at least one
    of the given patterns (case-insensitive). If no functions match at all,
    the full section list is returned unchanged so the agent never runs blind.

    Args:
        sections:  Output of split_code().
        patterns:  Keyword strings that identify relevant code for this agent.
                   Empty list → no filtering (return all sections).
    """
    if not patterns:
        return sections

    header = [s for s in sections if s.section_type == "header"]
    func_sections = [s for s in sections if s.section_type in ("function", "class")]

    lower_patterns = [p.lower() for p in patterns]
    relevant = [
        s for s in func_sections
        if any(p in s.source.lower() for p in lower_patterns)
    ]

    # Fallback: if nothing matched, send everything (agent will find nothing but won't error)
    if not relevant:
        return sections

    return header + relevant

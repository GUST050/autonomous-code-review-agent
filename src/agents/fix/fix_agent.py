"""
fix_agent.py — Parallel per-function fix generation with safe header merging.

Architecture:
  1. split_code()       — parse source into header + one section per function
  2. _map_findings()    — assign each finding to the most specific section
  3. parallel fixes     — each function fixed independently via ThreadPoolExecutor
  4. _fix_header()      — one final call merges imports + fixes module-level issues
  5. assembly           — sections joined back in original order

Merge safety:
  - Each function prompt is scoped to ONE function: "Fix ONLY {name}()."
  - Functions report needed_imports; the header fix is the single authority on imports.
  - Trailing whitespace from the original section is preserved, so re-joining
    produces identical spacing to the original file.
  - If a function fix fails, the original source is kept and the error is noted
    in unfixable — no partial or corrupt output.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from langchain_core.language_models import BaseChatModel as _BaseChatModel

from config import APPROVAL_THRESHOLD, FIX_MAX_WORKERS, FIX_TIMEOUT
from schemas.response import AgentResponse
from schemas.fix_response import FixResponse
from schemas.function_fix_response import FunctionFixResponse
from utils.code_splitter import CodeSection, split_code, trailing_whitespace, finding_names_function
from utils.token_tracker import TokenTracker
from ..base_agent import BaseAgent

logger = logging.getLogger(__name__)

# Findings tuple: (agent_name, finding_text, severity, rag_references)
_Finding = Tuple[str, str, int, List[str]]



_FUNCTION_SYSTEM_PROMPT = """\
You are a senior software engineer performing automated code remediation.

You will receive ONE Python function and a list of issues found in it.
Fix every auto-fixable issue and return the complete corrected function.

WHAT YOU MUST FIX:
  - SQL injection        → parameterized queries (? placeholders for sqlite3)
  - Broken crypto (MD5)  → hashlib.sha256 minimum; note bcrypt/argon2 better for passwords
  - Plaintext passwords  → hash before storing or comparing
  - O(n²) algorithms     → O(n) list comprehension, set, or dict
  - N+1 database queries → single IN-clause query + dict lookup
  - String concat loops  → ", ".join(...) or "".join(...)
  - Obfuscated names     → descriptive names + type hints
  - Missing docstrings   → concise docstring on non-trivial functions

WHAT YOU MUST NOT TOUCH (add to unfixable instead):
  - Session / JWT / tokens — requires web framework
  - Rate limiting — requires middleware or caching layer
  - Authorization / IDOR — requires application role model
  - Module-level hardcoded secrets — handled separately by the header fix pass
  - Function signatures — do not add, remove, or rename parameters

STRICT RULES:
  1. fixed_code must contain the COMPLETE function — every line, starting with def/async def.
  2. Preserve existing WHY-comments; update docstrings only when the function is renamed.
  3. needed_imports: list only NEW imports this fix requires that are not in the original file.
     Use exact Python import statements, e.g. ["import os", "from typing import Optional"].
  4. One entry per change in changes (one sentence each, e.g. "Replaced MD5 with SHA-256").
  5. One entry per manual item in unfixable."""


_HEADER_SYSTEM_PROMPT = """\
You are a senior software engineer performing automated code remediation.

You will receive the module header of a Python file (docstring, imports, module-level
constants), a list of issues found at module level, and a list of new imports that fixed
functions elsewhere in the file require.

YOUR TASK: Output the complete corrected header — everything from the module docstring
through the last import or module-level constant, ending with a trailing newline.
Do NOT include any function or class definitions.

WHAT YOU MUST FIX:
  - Hardcoded secrets (passwords, API keys, tokens) → os.environ.get("VAR_NAME", "")
    Add "import os" if not already present.
  - Merge all needed_imports from fixed functions: deduplicate, then sort PEP8-compliant
    (stdlib → third-party → local), preserving any existing imports.
  - Keep the module docstring and inline comments exactly as-is.

STRICT RULES:
  1. fixed_code must contain ONLY the header — no function definitions.
  2. One entry per change in changes.
  3. One entry per manual item in unfixable."""


class FixGeneratorAgent(BaseAgent):
    """
    Generates a complete corrected source file by fixing each function
    independently in parallel, then merging via a single header-fix pass.

    Model selection:
      - full model (Sonnet) when max severity >= APPROVAL_THRESHOLD
      - fast model (Haiku) when max severity < APPROVAL_THRESHOLD and fast_llm provided
    """

    def __init__(
        self,
        llm: _BaseChatModel,
        fast_llm: Optional[_BaseChatModel] = None,
        tracker: Optional[TokenTracker] = None,
    ):
        super().__init__(llm=llm, name="Fix Generator", tracker=tracker)
        self._fast_llm = fast_llm

    def get_system_prompt(self) -> str:
        # Not used directly; _FUNCTION_SYSTEM_PROMPT and _HEADER_SYSTEM_PROMPT
        # are passed per-call via _system_prompt override in invoke().
        return _FUNCTION_SYSTEM_PROMPT

    # ── Public API ────────────────────────────────────────────────────────────

    def generate_fixes(
        self,
        code: str,
        results: Dict[str, Optional[AgentResponse]],
    ) -> FixResponse:
        """
        Main entry point: fix all flagged functions in the given source file.

        Parses code into sections, maps findings to their sections, fixes each
        function in parallel, fixes the header last, then assembles the complete
        corrected file. Returns a FixResponse with fixed_code, per-change summaries,
        and any issues that require manual intervention.
        """
        sections = split_code(code)
        findings_by_section = self._map_findings(results, sections)

        max_sev = max((r.severity for r in results.values() if r), default=0)
        use_fast = self._fast_llm is not None and max_sev < APPROVAL_THRESHOLD
        llm = self._fast_llm if use_fast else None  # None → invoke() uses self.llm
        model_label = (
            self._fast_llm.__class__.__name__ if use_fast else self.llm.__class__.__name__
        )
        logger.info(
            "Generating fixes with %s (max severity %d — %s model)",
            model_label, max_sev, "fast" if use_fast else "full",
        )

        # ── Step 1: Fix functions in parallel ─────────────────────────────
        fixable = [
            s for s in sections
            if s.section_type in ("function", "class") and s.name in findings_by_section
        ]
        func_fixes: Dict[str, FunctionFixResponse] = (
            self._fix_functions_parallel(fixable, findings_by_section, llm)
            if fixable
            else {}
        )
        logger.info(
            "Function fixes complete — %d/%d functions modified",
            len(func_fixes), sum(1 for s in sections if s.section_type in ("function", "class")),
        )

        # ── Step 2: Collect imports needed by all function fixes ───────────
        all_needed_imports: List[str] = [
            imp for fix in func_fixes.values() for imp in fix.needed_imports
        ]

        # ── Step 3: Fix header (imports + module-level secrets) ───────────
        header = next((s for s in sections if s.section_type == "header"), None)
        header_findings = findings_by_section.get("__header__", [])
        if header and (header_findings or all_needed_imports):
            header_fix = self._fix_header(header, header_findings, all_needed_imports, llm)
        elif header:
            # Nothing to change in the header — return as-is, no LLM call needed
            header_fix = FunctionFixResponse(fixed_code=header.source)
        else:
            header_fix = FunctionFixResponse(fixed_code="")

        # ── Step 4: Assemble fixed file in original section order ─────────
        fixed_parts: List[str] = []
        for section in sections:
            if section.section_type == "header" and header:
                fixed_parts.append(header_fix.fixed_code)
            elif section.name in func_fixes:
                fixed_src = func_fixes[section.name].fixed_code
                # Restore original trailing blank lines (spacing between defs)
                trail = trailing_whitespace(section.source)
                fixed_parts.append(fixed_src.rstrip("\n") + trail)
            else:
                fixed_parts.append(section.source)

        fixed_code = "".join(fixed_parts)

        # ── Step 5: Aggregate changes and unfixable items ─────────────────
        # Deduplicate unfixable items: same issue can appear in multiple function
        # fixes (e.g., "handled in header pass" noted by login AND reset_password).
        # Preserve insertion order via dict keys.
        all_changes: List[str] = list(header_fix.changes)
        seen_unfixable: dict = {}
        for item in header_fix.unfixable:
            seen_unfixable[item] = None
        for fix in func_fixes.values():
            all_changes.extend(fix.changes)
            for item in fix.unfixable:
                seen_unfixable[item] = None

        # Collect per-function fixed sources for functions that actually changed.
        # Used by the webhook to post GitHub suggestions instead of auto-committing.
        function_fixes: dict = {}
        for section in sections:
            if section.name in func_fixes:
                fix = func_fixes[section.name]
                if fix.fixed_code.strip() != section.source.strip():
                    function_fixes[section.name] = fix.fixed_code

        return FixResponse(
            fixed_code=fixed_code,
            changes=all_changes,
            unfixable=list(seen_unfixable.keys()),
            function_fixes=function_fixes,
        )

    # ── Private: parallel orchestration ──────────────────────────────────────

    def _fix_functions_parallel(
        self,
        sections: List[CodeSection],
        findings_by_section: Dict[str, List[_Finding]],
        llm: Optional[_BaseChatModel],
    ) -> Dict[str, FunctionFixResponse]:
        """Fix all fixable functions concurrently; fall back to original on error."""
        fixes: Dict[str, FunctionFixResponse] = {}
        with ThreadPoolExecutor(max_workers=min(len(sections), FIX_MAX_WORKERS)) as pool:
            futures = {
                pool.submit(
                    self._fix_function,
                    section,
                    findings_by_section[section.name],
                    llm,
                ): section
                for section in sections
            }
            for future in as_completed(futures):
                section = futures[future]
                try:
                    fix = future.result()
                    fixes[section.name] = fix
                    logger.info(
                        "  %s(): %d change(s), %d manual item(s)",
                        section.name, len(fix.changes), len(fix.unfixable),
                    )
                except Exception as exc:
                    logger.error(
                        "  %s(): fix failed (%s) — keeping original", section.name, exc
                    )
                    fixes[section.name] = FunctionFixResponse(
                        fixed_code=section.source,
                        unfixable=[f"Auto-fix failed for {section.name}(): {exc}"],
                    )
        return fixes

    # ── Private: single-function fix ─────────────────────────────────────────

    def _fix_function(
        self,
        section: CodeSection,
        findings: List[_Finding],
        llm: Optional[_BaseChatModel],
    ) -> FunctionFixResponse:
        """Send one function + its findings to the LLM and return the corrected FunctionFixResponse."""
        findings_text = self._format_findings(findings)
        prompt = (
            f"Fix ONLY the `{section.name}` function shown below. "
            f"Do not modify any other function.\n\n"
            f"FUNCTION SOURCE:\n```python\n{section.source.rstrip()}\n```\n\n"
            f"FINDINGS (apply only what is relevant to this specific function):\n"
            f"{findings_text}\n\n"
            f"Return the complete fixed function in `fixed_code`.\n"
            f"List any NEW imports in `needed_imports` (do not repeat imports already in the file)."
        )
        return self.invoke(
            prompt,
            response_schema=FunctionFixResponse,
            _llm=llm,
            _system_prompt=_FUNCTION_SYSTEM_PROMPT,
            _timeout=FIX_TIMEOUT,
        )

    # ── Private: header fix ───────────────────────────────────────────────────

    def _fix_header(
        self,
        header: CodeSection,
        findings: List[_Finding],
        needed_imports: List[str],
        llm: Optional[_BaseChatModel],
    ) -> FunctionFixResponse:
        """
        Fix the module header: merges new imports from fixed functions, removes hardcoded
        secrets, and returns the corrected header source via LLM.
        """
        findings_text = (
            self._format_findings(findings) if findings else "No module-level findings."
        )
        # Deduplicate needed_imports before sending (order preserved via dict)
        deduped_imports = list(dict.fromkeys(needed_imports))
        imports_text = (
            "\n".join(f"  {imp}" for imp in deduped_imports)
            if deduped_imports
            else "  (none)"
        )
        prompt = (
            f"Fix the module header shown below.\n\n"
            f"HEADER SOURCE:\n```python\n{header.source.rstrip()}\n```\n\n"
            f"MODULE-LEVEL FINDINGS:\n{findings_text}\n\n"
            f"NEW IMPORTS REQUIRED BY FIXED FUNCTIONS (merge and deduplicate into header):\n"
            f"{imports_text}\n\n"
            f"Return the complete fixed header in `fixed_code`. "
            f"End fixed_code with exactly one trailing newline followed by a blank line."
        )
        return self.invoke(
            prompt,
            response_schema=FunctionFixResponse,
            _llm=llm,
            _system_prompt=_HEADER_SYSTEM_PROMPT,
            _timeout=FIX_TIMEOUT,
        )

    # ── Private: findings mapping ─────────────────────────────────────────────

    def _map_findings(
        self,
        results: Dict[str, Optional[AgentResponse]],
        sections: List[CodeSection],
    ) -> Dict[str, List[_Finding]]:
        """
        Assign each finding to the most specific code section.

        Strategy (in priority order):
          1. If the finding TEXT names a specific function using a recognisable
             pattern (e.g. "login()", "'proc'", "find_admins:"), assign it
             exclusively to that function. This prevents cross-contamination
             when one agent reports findings for several functions at once.
          2. If no text-match is found, broadcast the finding to ALL reported
             locations and let the per-function LLM ignore what is irrelevant.
          3. If there are no locations at all, assign to __header__ (module-level).

        Text matching uses multiple patterns to handle agent formatting differences:
          "{name}("   → "login("                    — Injection/Auth agents
          "'{name}'"  → "'proc'"                    — Quality agent
          '"{name}"'  → rare but handled
          "{name}:"   → "find_admins:"              — Performance agent
          "{name}/"   → "build_order_summary/..."   — compound slash findings
        Single-character names (e.g. "d") only match these explicit patterns,
        never plain substring, avoiding false positives in any English text.
        A finding may match multiple keys (e.g. "build_order_summary/export_csv:");
        all matching sections receive the finding.
        """
        func_names = {
            s.name for s in sections if s.section_type in ("function", "class")
        }
        by_section: Dict[str, List[_Finding]] = defaultdict(list)

        for agent_name, result in results.items():
            if not result or not result.findings:
                continue
            sev = result.severity
            locations = result.locations or []
            location_keys = [
                loc.split("(")[0].split(".")[0].strip() for loc in locations
            ]
            known_locations = [k for k in location_keys if k in func_names]

            for finding in result.findings:
                matched_keys = [
                    key for key in known_locations if finding_names_function(key, finding)
                ]

                refs = result.references or []
                if matched_keys:
                    for key in matched_keys:
                        by_section[key].append((agent_name, finding, sev, refs))
                    continue

                if known_locations:
                    for key in known_locations:
                        by_section[key].append((agent_name, finding, sev, refs))
                else:
                    by_section["__header__"].append((agent_name, finding, sev, refs))

        return dict(by_section)

    # ── Private: formatting helpers ───────────────────────────────────────────

    @staticmethod
    def _format_findings(findings: List[_Finding]) -> str:
        """Format findings as a readable block, including RAG remediation hints."""
        if not findings:
            return "No findings."
        lines = []
        seen_refs: set = set()
        for agent_name, finding, sev, refs in sorted(findings, key=lambda x: x[2], reverse=True):
            lines.append(f"  [{agent_name}] (severity {sev}) {finding}")
            for ref in refs:
                if ref not in seen_refs:
                    seen_refs.add(ref)
                    lines.append(f"    ↳ {ref}")
        return "\n".join(lines)

    # ── Kept for backward compatibility with existing tests ───────────────────

    def _build_findings_block(self, results: Dict[str, Optional[AgentResponse]]) -> str:
        """
        Group all findings by affected function and return a formatted string.
        Used by tests and as a diagnostic helper.
        """
        by_location: Dict[str, List[_Finding]] = defaultdict(list)
        global_findings: List[_Finding] = []

        for agent_name, result in results.items():
            if not result or not result.findings:
                continue
            sev = result.severity
            if result.locations:
                for loc in result.locations:
                    for finding in result.findings:
                        by_location[loc].append((sev, agent_name, finding))
            else:
                for finding in result.findings:
                    global_findings.append((sev, agent_name, finding))

        lines: List[str] = []
        for func, items in sorted(
            by_location.items(),
            key=lambda kv: max(s for s, _, _ in kv[1]),
            reverse=True,
        ):
            max_sev = max(s for s, _, _ in items)
            lines.append(f"\n{func}  [max severity: {max_sev}/100]")
            for sev, agent, finding in sorted(items, key=lambda x: x[0], reverse=True):
                lines.append(f"  [{agent}] {finding}  (severity {sev})")

        if global_findings:
            lines.append("\n[GLOBAL / NOT FUNCTION-SPECIFIC]")
            for sev, agent, finding in sorted(global_findings, key=lambda x: x[0], reverse=True):
                lines.append(f"  [{agent}] {finding}  (severity {sev})")

        return "\n".join(lines) if lines else "No findings to fix."

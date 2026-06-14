"""
report.py — Final report generation, isolated and independently testable.
"""
from typing import Dict, List, Optional, Tuple

from config import APPROVAL_THRESHOLD
from schemas.response import AgentResponse
from schemas.fix_response import FixResponse

_SEP = "-" * 60
_MODULE_LEVEL = "MODULE LEVEL"


def _severity_label(severity: int) -> str:
    if severity >= APPROVAL_THRESHOLD:
        return "CRITICAL — fix immediately before merging"
    if severity >= 60:
        return "HIGH — should be addressed soon"
    if severity > 0:
        return "LOW — minor issues found"
    return "Clean — no issues found"


def _names_function(func_name: str, finding: str) -> bool:
    return (
        f"{func_name}(" in finding
        or f"{func_name}:" in finding
        or f"{func_name}/" in finding
        or f"'{func_name}'" in finding
        or f'"{func_name}"' in finding
        or finding.startswith(func_name + " ")
    )


def _route_finding(finding: str, locations: List[str]) -> str:
    """Return the bare function name this finding belongs to, or _MODULE_LEVEL."""
    for loc in locations:
        if loc and _names_function(loc, finding):
            return loc
    if len(locations) == 1:
        return locations[0] or _MODULE_LEVEL
    return _MODULE_LEVEL


def _group_findings(
    results: Dict[str, Optional[AgentResponse]],
) -> Tuple[Dict[str, List[Tuple[str, str]]], Dict[str, int]]:
    """
    Returns:
      grouped:       {func_name: [(agent_name, finding_text), ...]}
      func_severity: {func_name: max severity across all agents mentioning it}
    """
    grouped: Dict[str, List[Tuple[str, str]]] = {}
    func_severity: Dict[str, int] = {}

    for agent_name, result in results.items():
        if not result or result.severity == 0 or not result.findings:
            continue
        locations = [
            loc.split("(")[0].split(".")[0].strip()
            for loc in (result.locations or [])
        ]
        locations = [l for l in locations if l]

        for finding in result.findings:
            func = _route_finding(finding, locations)
            grouped.setdefault(func, []).append((agent_name, finding))
            func_severity[func] = max(func_severity.get(func, 0), result.severity)

    return grouped, func_severity


def generate_final_report(
    results: Dict[str, Optional[AgentResponse]],
    fix_result: Optional[FixResponse],
) -> str:
    lines: list[str] = ["\n=== FINAL CODE REVIEW REPORT ==="]

    grouped, func_severity = _group_findings(results)

    agent_col = max((len(n) for n in results), default=10)

    # ── Function sections sorted by severity desc ────────────────────────────
    func_names = sorted(
        [k for k in grouped if k != _MODULE_LEVEL],
        key=lambda k: func_severity.get(k, 0),
        reverse=True,
    )

    for func in func_names:
        sev = func_severity[func]
        label = _severity_label(sev).split(" — ")[0]
        lines += [f"\n {func}  [{sev}/100 — {label}]", _SEP]
        for agent_name, finding in grouped[func]:
            tag = f"[{agent_name}]".ljust(agent_col + 2)
            lines.append(f"  {tag}  {finding}")

    # ── Module-level (unattributed) findings ─────────────────────────────────
    if _MODULE_LEVEL in grouped:
        sev = func_severity[_MODULE_LEVEL]
        label = _severity_label(sev).split(" — ")[0]
        lines += [f"\n {_MODULE_LEVEL}  [{sev}/100 — {label}]", _SEP]
        for agent_name, finding in grouped[_MODULE_LEVEL]:
            tag = f"[{agent_name}]".ljust(agent_col + 2)
            lines.append(f"  {tag}  {finding}")

    if not grouped:
        lines.append("\n  No issues found — all agents reported clean.")

    # ── Aggregated references ────────────────────────────────────────────────
    all_refs: list[str] = []
    seen: set[str] = set()
    for result in results.values():
        if result and result.references:
            for ref in result.references:
                if ref not in seen:
                    seen.add(ref)
                    all_refs.append(ref)

    if all_refs:
        lines += ["\n REFERENCES", _SEP]
        for ref in all_refs:
            lines.append(f"  • {ref}")

    # ── Agent summary ────────────────────────────────────────────────────────
    lines += ["\n AGENT SUMMARY", _SEP]
    for agent_name, result in results.items():
        if result is None:
            lines.append(f"  {agent_name:<{agent_col}}  No assessment available")
        elif result.severity == 0:
            lines.append(f"  {agent_name:<{agent_col}}   0/100  Clean")
        else:
            label = _severity_label(result.severity).split(" — ")[0]
            lines.append(f"  {agent_name:<{agent_col}}  {result.severity:>3}/100  {label}")

    # ── Fix section ──────────────────────────────────────────────────────────
    if fix_result:
        lines += ["\n\n=== CHANGES APPLIED ==="]
        if fix_result.changes:
            for i, change in enumerate(fix_result.changes, 1):
                lines.append(f"  {i}. {change}")
        else:
            lines.append("  No auto-fixable issues found.")

        if fix_result.unfixable:
            lines += ["\n=== REQUIRES MANUAL INTERVENTION ==="]
            for item in fix_result.unfixable:
                lines.append(f"  • {item}")

        if fix_result.fixed_code:
            lines += ["\n=== COMPLETE FIXED FILE ===", fix_result.fixed_code]

    lines += ["\n" + "=" * 70, "End of Autonomous Code Review Report"]
    return "\n".join(lines)

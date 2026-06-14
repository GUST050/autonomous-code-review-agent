import logging
import sys
import argparse

sys.stdout.reconfigure(line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)

from typing import Optional
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_xai import ChatXAI
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from agents import InjectionAgent, AuthAgent, SecretsAgent, QualityAgent, PerformanceAgent, FixGeneratorAgent
from config import AGENT_CONFIGS, APPROVAL_THRESHOLD, FIX_GENERATOR_FAST
from graph import create_review_graph
from runner import ReviewRunner
from utils import TokenTracker, combined_report, read_from_file, read_from_git_diff

logger = logging.getLogger(__name__)

_DEMO_CODE = '''
def login_user(username, password):
    """Login function - contains several security and quality issues"""
    query = f"SELECT * FROM users WHERE username = \'{username}\' AND password = \'{password}\'"
    result = db.execute(query)  # Direct SQL injection risk

    if result:
        return "Login successful"
    return "Invalid credentials"
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="code-review-agent",
        description="Autonomous Code Review Agent — reviews code with 5 parallel AI agents.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--file", "-f", metavar="PATH", help="Review a specific file")
    group.add_argument("--diff", "-d", action="store_true", help="Review your git changes")
    parser.add_argument(
        "--fix", action="store_true",
        help="Generate fixes for found issues after review",
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="Non-interactive mode: no prompts, exit 1 if severity >= threshold",
    )
    parser.add_argument(
        "--output", "-o", metavar="PATH",
        help="Write the fixed file to PATH (requires --fix)",
    )
    return parser.parse_args()


def load_code(args: argparse.Namespace) -> tuple[str, str]:
    if args.file:
        return read_from_file(args.file)
    if args.diff:
        return read_from_git_diff()
    return _DEMO_CODE, "Demo (hardcoded test code)"


def _build_llm(config):
    """Instantiate the correct LLM provider based on config.provider."""
    if config.provider == "anthropic":
        return ChatAnthropic(model=config.model, temperature=config.temperature, max_tokens=config.max_tokens)
    if config.provider == "xai":
        return ChatXAI(model=config.model, temperature=config.temperature, max_tokens=config.max_tokens)
    return ChatOpenAI(model=config.model, temperature=config.temperature, max_tokens=config.max_tokens)


def build_agents() -> tuple[list, FixGeneratorAgent, list[TokenTracker]]:
    cfg = AGENT_CONFIGS
    trackers = {name: TokenTracker.from_config(name, model) for name, model in cfg.items()}

    review_agents = [
        InjectionAgent(llm=_build_llm(cfg["Injection Expert"]),    tracker=trackers["Injection Expert"]),
        AuthAgent(     llm=_build_llm(cfg["Auth Expert"]),          tracker=trackers["Auth Expert"]),
        SecretsAgent(  llm=_build_llm(cfg["Secrets Expert"]),       tracker=trackers["Secrets Expert"]),
        PerformanceAgent(llm=_build_llm(cfg["Performance Expert"]), tracker=trackers["Performance Expert"]),
        QualityAgent(  llm=_build_llm(cfg["Code Quality Expert"]),  tracker=trackers["Code Quality Expert"]),
    ]

    fix_agent = FixGeneratorAgent(
        llm=_build_llm(cfg["Fix Generator"]),
        fast_llm=_build_llm(FIX_GENERATOR_FAST),
        tracker=trackers["Fix Generator"],
    )

    return review_agents, fix_agent, list(trackers.values())


def _save_output(final_state: dict, output_path: str) -> None:
    fix_result = final_state.get("fix_result")
    if not fix_result or not fix_result.fixed_code:
        print("\nNothing to save — no fixed code was generated.")
        return
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fix_result.fixed_code, encoding="utf-8")
    print(f"\nFixed file saved to: {path.resolve()}")


def _prompt_accept_fixes(final_state: dict, output_path: Optional[str]) -> None:
    """Show all fixes, then ask once whether to save the corrected file."""
    fix_result = final_state.get("fix_result")
    if not fix_result or not fix_result.fixed_code:
        return

    changes = fix_result.changes or []
    print()

    if changes:
        for i, change in enumerate(changes, 1):
            print(f"  [{i}/{len(changes)}] {change}")
        print()

    answer = input(f"  Apply all {len(changes)} fix(es)? (yes/no): ").strip().lower()
    if answer in ("yes", "ja", "y", "j"):
        if output_path:
            _save_output(final_state, output_path)
        else:
            print("  Fixes accepted — use --output PATH to save to a file.")
    else:
        print("  Fixes discarded.")


def main():
    args = parse_args()

    if args.output and not args.fix:
        print("Warning: --output has no effect without --fix")

    try:
        code, label = load_code(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    print("=" * 70)
    print("AUTONOMOUS CODE REVIEW AGENT")
    print("=" * 70)

    review_agents, fix_agent, all_trackers = build_agents()

    for agent in review_agents:
        print(f"  {agent}")
    if args.fix:
        print(f"  {fix_agent}")
    print(f"\n  {len(review_agents)} review agents running in parallel")
    print("-" * 70)
    print(f"Source: {label}")
    print("-" * 70)
    print(code[:800] + ("\n[... truncated for display ...]" if len(code) > 800 else ""))
    print("-" * 70)
    print(f"Running parallel code review with {len(review_agents)} agents...\n")

    graph = create_review_graph(review_agents, fix_agent)
    runner = ReviewRunner(graph)
    final_state = runner.run(code, fix=args.fix)

    print(final_state.get("final_report", "No report generated."))

    print("=" * 70)
    print("DONE")
    print("=" * 70)

    results = final_state.get("results", {})
    for agent_name, result in results.items():
        severity = result.severity if result else "N/A"
        print(f"  {agent_name:<24} {severity:>3}/100")

    print(combined_report(all_trackers))

    if args.fix and not args.ci:
        _prompt_accept_fixes(final_state, args.output)
    elif args.fix and args.ci and args.output:
        _save_output(final_state, args.output)
    elif args.fix and args.ci:
        print("\nCI: --fix was requested but no --output PATH specified — fixes not saved.")

    if args.ci:
        max_sev = max((r.severity for r in results.values() if r), default=0)
        if max_sev >= APPROVAL_THRESHOLD:
            print(f"\nCI: severity {max_sev}/100 >= threshold {APPROVAL_THRESHOLD} — exit 1")
            sys.exit(1)
        print(f"\nCI: max severity {max_sev}/100 — exit 0")
        sys.exit(0)


if __name__ == "__main__":
    main()

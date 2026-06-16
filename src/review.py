"""
review.py — Programmatic entry point for running a code review.

Used by the webhook handler and any other non-CLI caller.
The CLI (main.py) handles argument parsing and user prompts separately.
"""
import logging
from typing import Dict, Optional

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI

from agents import (
    InjectionAgent,
    AuthAgent,
    SecretsAgent,
    QualityAgent,
    PerformanceAgent,
    FixGeneratorAgent,
)
from config import AGENT_CONFIGS, FIX_GENERATOR_FAST
from graph import create_review_graph
from runner import ReviewRunner
from schemas.fix_response import FixResponse
from schemas.response import AgentResponse
from utils.token_tracker import TokenTracker

logger = logging.getLogger(__name__)


def _build_llm(config):
    """Instantiate the correct LLM provider from a ModelConfig."""
    if config.provider == "anthropic":
        return ChatAnthropic(
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    return ChatOpenAI(
        model=config.model,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )


def run_review(code: str, fix: bool = False) -> dict:
    """
    Run all review agents on `code` and return the final LangGraph state.

    The returned dict contains:
      - results:      {agent_name: AgentResponse}
      - final_report: formatted report string
      - fix_result:   FixResponse if fix=True, else None
    """
    cfg = AGENT_CONFIGS
    trackers = {name: TokenTracker.from_config(name, model) for name, model in cfg.items()}

    review_agents = [
        InjectionAgent(llm=_build_llm(cfg["Injection Expert"]),     tracker=trackers["Injection Expert"]),
        AuthAgent(     llm=_build_llm(cfg["Auth Expert"]),           tracker=trackers["Auth Expert"]),
        SecretsAgent(  llm=_build_llm(cfg["Secrets Expert"]),        tracker=trackers["Secrets Expert"]),
        PerformanceAgent(llm=_build_llm(cfg["Performance Expert"]),  tracker=trackers["Performance Expert"]),
        QualityAgent(  llm=_build_llm(cfg["Code Quality Expert"]),   tracker=trackers["Code Quality Expert"]),
    ]

    fix_agent = FixGeneratorAgent(
        llm=_build_llm(cfg["Fix Generator"]),
        fast_llm=_build_llm(FIX_GENERATOR_FAST),
        tracker=trackers.get("Fix Generator"),
    )

    graph = create_review_graph(review_agents, fix_agent)
    return ReviewRunner(graph).run(code, fix=fix)


def run_fix_from_responses(code: str, results: Dict[str, Optional[AgentResponse]]) -> FixResponse:
    """Run the fix agent directly with AgentResponse objects from a just-completed review."""
    cfg = AGENT_CONFIGS
    fix_agent = FixGeneratorAgent(
        llm=_build_llm(cfg["Fix Generator"]),
        fast_llm=_build_llm(FIX_GENERATOR_FAST),
    )
    return fix_agent.generate_fixes(code, results)


def run_fix(code: str, findings_data: Dict[str, dict]) -> FixResponse:
    """
    Run only the fix agent using pre-computed review findings.

    Used by the human-in-the-loop 'fix' command so we don't re-run all
    five review agents — the findings were captured during the initial
    review and stored in the PR review body as a hidden HTML comment.

    findings_data: the dict returned by extract_findings_from_review(),
    keyed by agent name, each value a dict with 'findings', 'severity',
    'confidence', 'locations', 'reasoning'.
    """
    agent_results: Dict[str, Optional[AgentResponse]] = {
        agent_name: AgentResponse(
            reasoning=data.get("reasoning", "Restored from previous review"),
            findings=data.get("findings", []),
            severity=data.get("severity", 0),
            confidence=data.get("confidence", 0),
            locations=data.get("locations", []),
        )
        for agent_name, data in findings_data.items()
    }

    cfg = AGENT_CONFIGS
    fix_agent = FixGeneratorAgent(
        llm=_build_llm(cfg["Fix Generator"]),
        fast_llm=_build_llm(FIX_GENERATOR_FAST),
    )
    return fix_agent.generate_fixes(code, agent_results)

"""
review.py — Programmatic entry point for running a code review.

Used by the webhook handler and any other non-CLI caller.
The CLI (main.py) handles argument parsing and user prompts separately.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def run_review_parallel(code: str) -> Dict[str, Optional[AgentResponse]]:
    """
    Run all five review agents in parallel with a hard total deadline.

    Bypasses LangGraph (which runs sync nodes sequentially in 0.6+) so all
    five agents truly execute simultaneously.  The overall budget is
    LLM_TIMEOUT + 0.5s — agents still running after that window are abandoned
    via shutdown(wait=False) and counted as empty (severity=0).  This bounds
    the entire review to at most LLM_TIMEOUT + 0.5s regardless of LLM latency.
    """
    from concurrent.futures import wait as _wait, ALL_COMPLETED
    from config import LLM_TIMEOUT

    cfg = AGENT_CONFIGS
    trackers = {name: TokenTracker.from_config(name, model) for name, model in cfg.items()}

    agents = [
        InjectionAgent(   llm=_build_llm(cfg["Injection Expert"]),   tracker=trackers["Injection Expert"]),
        AuthAgent(        llm=_build_llm(cfg["Auth Expert"]),         tracker=trackers["Auth Expert"]),
        SecretsAgent(     llm=_build_llm(cfg["Secrets Expert"]),      tracker=trackers["Secrets Expert"]),
        PerformanceAgent( llm=_build_llm(cfg["Performance Expert"]),  tracker=trackers["Performance Expert"]),
        QualityAgent(     llm=_build_llm(cfg["Code Quality Expert"]), tracker=trackers["Code Quality Expert"]),
    ]

    pool = ThreadPoolExecutor(max_workers=len(agents))
    future_to_name = {pool.submit(agent.review_code, code): agent.name for agent in agents}

    # Wait at most LLM_TIMEOUT + 0.5s for all agents; abandon stragglers.
    done, not_done = _wait(future_to_name.keys(), timeout=LLM_TIMEOUT + 0.5)
    pool.shutdown(wait=False)

    if not_done:
        names = [future_to_name[f] for f in not_done]
        logger.warning("Abandoned slow agents after %ds: %s", LLM_TIMEOUT, names)

    empty = AgentResponse(reasoning="Timed out", findings=[], severity=0, confidence=0)
    results: Dict[str, Optional[AgentResponse]] = {
        future_to_name[f]: empty for f in not_done
    }
    for future in done:
        name = future_to_name[future]
        try:
            results[name] = future.result()
        except Exception as exc:
            logger.error("[%s] agent failed: %s", name, exc)
            results[name] = AgentResponse(
                reasoning=f"Agent failed: {exc}", findings=[], severity=0, confidence=0,
            )
    return results


def run_fix_from_responses(code: str, results: Dict[str, Optional[AgentResponse]]) -> FixResponse:
    """Run the fix agent directly with AgentResponse objects from a just-completed review."""
    cfg = AGENT_CONFIGS
    fix_agent = FixGeneratorAgent(
        llm=_build_llm(cfg["Fix Generator"]),
        fast_llm=_build_llm(FIX_GENERATOR_FAST),
    )
    return fix_agent.generate_fixes(code, results)

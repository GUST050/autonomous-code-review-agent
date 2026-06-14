"""
review.py — Programmatic entry point for running a code review.

Used by the webhook handler and any other non-CLI caller.
The CLI (main.py) handles argument parsing and user prompts separately.
"""
import logging
from typing import Optional

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_xai import ChatXAI

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
from utils.token_tracker import TokenTracker

logger = logging.getLogger(__name__)


def _build_llm(config):
    if config.provider == "anthropic":
        return ChatAnthropic(
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    if config.provider == "xai":
        return ChatXAI(
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

"""
review.py — Programmatic entry point for running a code review.

Used by the webhook handler and any other non-CLI caller.
The CLI (main.py) handles argument parsing and user prompts separately.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed, wait as _concurrent_wait
from typing import Dict, List, Optional

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
from config import AGENT_CONFIGS, FIX_GENERATOR_FAST, LLM_TIMEOUT
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


def run_review(
    code: str,
    fix: bool = False,
    _agents: Optional[List] = None,
    _fix_agent=None,
) -> dict:
    """
    Run all review agents on `code` and return the final state dict.

    Bypasses LangGraph — agents run truly in parallel via ThreadPoolExecutor.
    Applies RAG enrichment and optionally generates fixes.

    _agents / _fix_agent: pass pre-built agents from main.py (with trackers).
    When omitted, agents are created internally with fresh trackers.

    The returned dict contains:
      - results:      {agent_name: AgentResponse}
      - final_report: formatted report string
      - fix_result:   FixResponse if fix=True, else None
      - trackers:     list of TokenTracker (empty when _agents were passed in)
    """
    cfg = AGENT_CONFIGS

    if _agents is not None:
        agents = _agents
        trackers: List = []
    else:
        trackers_map = {name: TokenTracker.from_config(name, model) for name, model in cfg.items()}
        agents = [
            InjectionAgent(   llm=_build_llm(cfg["Injection Expert"]),   tracker=trackers_map["Injection Expert"]),
            AuthAgent(        llm=_build_llm(cfg["Auth Expert"]),         tracker=trackers_map["Auth Expert"]),
            SecretsAgent(     llm=_build_llm(cfg["Secrets Expert"]),      tracker=trackers_map["Secrets Expert"]),
            PerformanceAgent( llm=_build_llm(cfg["Performance Expert"]),  tracker=trackers_map["Performance Expert"]),
            QualityAgent(     llm=_build_llm(cfg["Code Quality Expert"]), tracker=trackers_map["Code Quality Expert"]),
        ]
        trackers = list(trackers_map.values())

    fix_ag = _fix_agent if _fix_agent is not None else FixGeneratorAgent(
        llm=_build_llm(cfg["Fix Generator"]),
        fast_llm=_build_llm(FIX_GENERATOR_FAST),
    )

    # True parallel execution — LangGraph 0.6 runs sync nodes sequentially,
    # so we bypass it and manage the ThreadPoolExecutor directly.
    pool = ThreadPoolExecutor(max_workers=len(agents))
    future_to_name = {pool.submit(agent.review_code, code): agent.name for agent in agents}
    done, not_done = _concurrent_wait(future_to_name.keys(), timeout=LLM_TIMEOUT + 0.5)
    pool.shutdown(wait=False)

    if not_done:
        names = [future_to_name[f] for f in not_done]
        logger.warning("Abandoned slow agents after %ds: %s", LLM_TIMEOUT, names)

    empty = AgentResponse(reasoning="Timed out", findings=[], severity=0, confidence=0)
    results: Dict[str, Optional[AgentResponse]] = {future_to_name[f]: empty for f in not_done}
    for future in done:
        name = future_to_name[future]
        try:
            results[name] = future.result()
        except Exception as exc:
            logger.error("[%s] agent failed: %s", name, exc)
            results[name] = AgentResponse(
                reasoning=f"Agent failed: {exc}", findings=[], severity=0, confidence=0,
            )

    # RAG enrichment — lazy import to avoid Chroma/vector-store cold start in webhook
    from agents.rag import RagEnricher
    results = RagEnricher().enrich(results)

    fix_result: Optional[FixResponse] = None
    if fix:
        fix_result = fix_ag.generate_fixes(code, results)

    from graph.report import generate_final_report
    final_report = generate_final_report(results, fix_result)

    return {
        "code": code,
        "results": results,
        "fix_result": fix_result,
        "final_report": final_report,
        "fix_enabled": fix,
        "trackers": trackers,
    }


def run_review_parallel(code: str) -> Dict[str, Optional[AgentResponse]]:
    """
    Run all five review agents in parallel with a hard total deadline.

    Bypasses LangGraph (which runs sync nodes sequentially in 0.6+) so all
    five agents truly execute simultaneously.  The overall budget is
    LLM_TIMEOUT + 0.5s — agents still running after that window are abandoned
    via shutdown(wait=False) and counted as empty (severity=0).  This bounds
    the entire review to at most LLM_TIMEOUT + 0.5s regardless of LLM latency.
    """
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

    done, not_done = _concurrent_wait(future_to_name.keys(), timeout=LLM_TIMEOUT + 0.5)
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


def run_review_multifile(file_contents: Dict[str, str]) -> Dict[str, Optional[AgentResponse]]:
    """
    Run all five review agents on every file independently, all in parallel.

    Instead of concatenating files into one large blob (which causes timeouts on
    larger PRs), each (agent, file) pair is submitted as a separate task to a
    single ThreadPoolExecutor.  The total wall-clock time is still bounded by
    LLM_TIMEOUT + 0.5s regardless of how many files the PR touches, because all
    tasks run simultaneously.

    Results for each agent are merged across files: findings and locations are
    combined; severity and confidence take the maximum across files.
    """
    if not file_contents:
        return {}

    cfg = AGENT_CONFIGS
    agent_names = ["Injection Expert", "Auth Expert", "Secrets Expert",
                   "Performance Expert", "Code Quality Expert"]

    # Build ONE set of shared LLM instances — ChatAnthropic/ChatOpenAI are
    # stateless for HTTP calls so sharing across threads is safe.
    shared_llms = {name: _build_llm(cfg[name]) for name in agent_names}

    def _make_agents() -> list:
        return [
            InjectionAgent(   llm=shared_llms["Injection Expert"]),
            AuthAgent(        llm=shared_llms["Auth Expert"]),
            SecretsAgent(     llm=shared_llms["Secrets Expert"]),
            PerformanceAgent( llm=shared_llms["Performance Expert"]),
            QualityAgent(     llm=shared_llms["Code Quality Expert"]),
        ]

    pool = ThreadPoolExecutor(max_workers=len(agent_names) * len(file_contents))
    future_to_key: dict = {}

    for path, content in file_contents.items():
        file_agents = _make_agents()
        for agent in file_agents:
            code = f"# === {path} ===\n{content}\n"
            future = pool.submit(agent.review_code, code)
            future_to_key[future] = (agent.name, path)

    done, not_done = _concurrent_wait(future_to_key.keys(), timeout=LLM_TIMEOUT + 0.5)
    pool.shutdown(wait=False)

    if not_done:
        slow = [f"{future_to_key[f][0]}@{future_to_key[f][1]}" for f in not_done]
        logger.warning("Abandoned %d slow tasks after %ds: %s", len(not_done), LLM_TIMEOUT, slow)

    per_agent: dict = {name: [] for name in agent_names}
    for future in done:
        agent_name, path = future_to_key[future]
        try:
            resp = future.result()
        except Exception as exc:
            logger.error("[%s] failed on %s: %s", agent_name, path, exc)
            resp = AgentResponse(reasoning=f"Failed: {exc}", findings=[], severity=0, confidence=0)
        per_agent[agent_name].append(resp)

    empty = AgentResponse(reasoning="Timed out", findings=[], severity=0, confidence=0)
    results: Dict[str, Optional[AgentResponse]] = {}

    for name in agent_names:
        file_resps = [r for r in per_agent[name] if r is not None]
        if not file_resps:
            results[name] = empty
            continue

        findings: list = []
        locations: list = []
        max_sev = 0
        max_conf = 0
        best_reasoning = "No issues found in this domain."

        for resp in file_resps:
            if resp.severity > max_sev:
                max_sev = resp.severity
                best_reasoning = resp.reasoning
            max_conf = max(max_conf, resp.confidence)
            findings.extend(resp.findings)
            locations.extend(resp.locations)

        results[name] = AgentResponse(
            reasoning=best_reasoning,
            findings=findings,
            severity=max_sev,
            confidence=max_conf,
            locations=list(dict.fromkeys(locations)),  # deduplicate, preserve order
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

import logging
import re
from typing import Annotated, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, START, END

from agents.base_agent import ReviewAgent
from agents.fix import FixGeneratorAgent
from agents.rag import RagEnricher
from schemas.response import AgentResponse
from schemas.fix_response import FixResponse
from graph.report import generate_final_report

logger = logging.getLogger(__name__)


def _merge_results(a: Dict, b: Dict) -> Dict:
    """Reducer that merges parallel agent result dicts without overwriting."""
    return {**a, **b}


class ReviewState(TypedDict):
    code: str
    results: Annotated[Dict[str, Optional[AgentResponse]], _merge_results]
    fix_result: Optional[FixResponse]
    final_report: Optional[str]
    fix_enabled: bool


def _node_name(agent_name: str) -> str:
    """Convert agent name to a valid LangGraph node identifier."""
    return re.sub(r"[^a-z0-9]+", "_", agent_name.lower()).strip("_")


def _make_review_node(agent: ReviewAgent):
    """Factory that captures `agent` correctly in a closure for parallel execution."""
    def node(state: ReviewState) -> dict:
        logger.info("Starting %s", agent.name)
        result = agent.review_code(state["code"])
        logger.info("%s complete — severity %d/100", agent.name, result.severity)
        return {"results": {agent.name: result}}
    return node


def create_review_graph(
    review_agents: List[ReviewAgent],
    fix_agent: FixGeneratorAgent,
    rag_enricher: Optional[RagEnricher] = None,
):
    """
    Build a LangGraph with:
      - All review agents running in parallel (fan-out from START)
      - RAG enrichment after all agents complete
      - Optional fix-generator node (only when fix_enabled=True in state)
      - Final report node

    Adding a new review agent only requires passing it in this list —
    no other file needs to change.
    """
    workflow = StateGraph(ReviewState)

    # ── Parallel review nodes ─────────────────────────────────────────────
    review_node_names: List[str] = []
    for agent in review_agents:
        name = _node_name(agent.name)
        workflow.add_node(name, _make_review_node(agent))
        workflow.add_edge(START, name)
        workflow.add_edge(name, "rag_enrichment")
        review_node_names.append(name)

    logger.debug("Registered review nodes: %s", review_node_names)

    # ── RAG enrichment (fan-in after all parallel agents) ─────────────────
    _enricher = rag_enricher or RagEnricher()

    def rag_enrichment_node(state: ReviewState) -> dict:
        enriched = _enricher.enrich(dict(state.get("results", {})))
        return {"results": enriched}

    workflow.add_node("rag_enrichment", rag_enrichment_node)
    workflow.add_conditional_edges(
        "rag_enrichment",
        lambda state: "fix_generator" if state.get("fix_enabled") else "final",
    )

    # ── Fix generator ─────────────────────────────────────────────────────
    def fix_generator_node(state: ReviewState) -> dict:
        fix_result = fix_agent.generate_fixes(state["code"], state.get("results", {}))
        logger.info(
            "Fix generator complete — %d changes applied, %d require manual intervention",
            len(fix_result.changes),
            len(fix_result.unfixable),
        )
        return {"fix_result": fix_result}

    # ── Final report ──────────────────────────────────────────────────────
    def final_node(state: ReviewState) -> dict:
        report = generate_final_report(
            state.get("results", {}),
            state.get("fix_result"),
        )
        return {"final_report": report}

    workflow.add_node("fix_generator", fix_generator_node)
    workflow.add_node("final", final_node)

    workflow.add_edge("fix_generator", "final")
    workflow.add_edge("final", END)

    return workflow.compile()

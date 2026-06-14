"""
runner.py — Orchestration layer between the CLI and the LangGraph.
"""
import logging

from graph.review_graph import ReviewState

logger = logging.getLogger(__name__)


class ReviewRunner:
    """Drives a compiled LangGraph through its full lifecycle."""

    def __init__(self, graph):
        self.graph = graph

    def run(self, code: str, fix: bool = False) -> ReviewState:
        """
        Run the full review pipeline and return the final state.
        `fix=True` enables the fix-generation step.
        """
        initial_state = {
            "code": code,
            "results": {},
            "fix_result": None,
            "final_report": None,
            "fix_enabled": fix,
        }
        logger.info("Invoking review graph")
        return self.graph.invoke(initial_state)

"""
rag_enricher.py — Adds CVE/CWE/OWASP references to agent findings.

After all review agents complete, RagEnricher queries the knowledge store for
each finding and attaches the best-matching references to AgentResponse.references.
This does not call any external LLM — it is pure vector similarity search.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from schemas.response import AgentResponse
from utils.rag_store import RagStore

logger = logging.getLogger(__name__)

_RESULTS_PER_FINDING = 1   # top-1 per finding keeps the report concise


class RagEnricher:
    """
    Enriches a dict of AgentResponse objects with knowledge-base references.

    Each unique finding is queried once; duplicate references across findings
    are collapsed so the references list per agent shows only distinct entries.
    """

    def __init__(self, store: Optional[RagStore] = None):
        self._store = store  # injected in tests; loaded lazily in production

    def enrich(
        self, results: Dict[str, Optional[AgentResponse]]
    ) -> Dict[str, Optional[AgentResponse]]:
        """
        For every AgentResponse, populate its .references list with the
        most relevant KbEntry labels found for that agent's findings.
        Returns the same dict (mutated in-place) for graph reducer compatibility.
        """
        store = self._store or RagStore.load()

        for agent_name, result in results.items():
            if not result or not result.findings:
                continue

            seen_ids: set[str] = set()
            refs: list[str] = []

            for finding in result.findings:
                for entry in store.query(finding, n_results=_RESULTS_PER_FINDING):
                    if entry.id not in seen_ids:
                        seen_ids.add(entry.id)
                        refs.append(entry.label())

            if refs:
                result.references = refs
                logger.info(
                    "[RAG] %s — %d reference(s) added: %s",
                    agent_name, len(refs), refs,
                )

        return results

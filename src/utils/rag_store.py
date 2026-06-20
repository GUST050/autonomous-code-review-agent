"""
rag_store.py — ChromaDB-backed knowledge store for CVE/CWE/OWASP retrieval.

The store is built once on first use (lazy init) from data/rag/knowledge_base.json.
Each entry's description is embedded with the default MiniLM model (local, no API cost).
query() returns the top-k most semantically similar entries for a given finding text.
"""
from __future__ import annotations

import json
import logging
import pathlib
import threading
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_KB = (
    pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "rag" / "knowledge_base.json"
)
_COLLECTION = "security_knowledge"
_SIMILARITY_THRESHOLD = 0.5   # cosine distance above this = irrelevant match (0=identical, 2=opposite)


@dataclass
class KbEntry:
    id: str
    title: str
    description: str
    owasp: Optional[str]
    cvss: Optional[float]
    remediation: str

    def label(self) -> str:
        """Short reference label shown in the report."""
        parts = [self.id, self.title]
        if self.owasp:
            parts.append(f"OWASP {self.owasp}")
        if self.cvss is not None:
            parts.append(f"CVSS {self.cvss}")
        return " | ".join(parts)


class RagStore:
    """
    Thin wrapper around a ChromaDB in-memory collection.

    Usage:
        store = RagStore.load()          # builds once, cached as singleton
        refs  = store.query(finding, 2)  # returns list[KbEntry]
    """

    _instance: Optional["RagStore"] = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self, kb_path: pathlib.Path = _DEFAULT_KB):
        import os
        import chromadb
        from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction

        self._client = chromadb.Client()
        self._ef = OpenAIEmbeddingFunction(
            api_key=os.environ["OPENAI_API_KEY"],
            model_name="text-embedding-3-small",
        )
        self._col = self._client.get_or_create_collection(
            name=_COLLECTION,
            embedding_function=self._ef,
            metadata={"hnsw:space": "cosine"},
        )
        self._entries: dict[str, KbEntry] = {}
        self._build(kb_path)

    # ── Public API ─────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, kb_path: pathlib.Path = _DEFAULT_KB) -> "RagStore":
        """Return the singleton store, building it on first call. Thread-safe."""
        if cls._instance is not None:
            return cls._instance
        with cls._lock:
            if cls._instance is None:
                logger.info("[RAG] Building knowledge store from %s", kb_path.name)
                cls._instance = cls(kb_path)
                logger.info("[RAG] Store ready — %d entries indexed", cls._instance._col.count())
        return cls._instance

    def query(self, text: str, n_results: int = 2) -> List[KbEntry]:
        """
        Return up to n_results KbEntries whose description is semantically
        closest to text.  Entries with L2 distance > threshold are excluded.
        """
        if not text.strip():
            return []
        results = self._col.query(
            query_texts=[text],
            n_results=min(n_results, self._col.count()),
        )
        entries: List[KbEntry] = []
        for entry_id, dist in zip(
            results["ids"][0], results["distances"][0]
        ):
            if dist <= _SIMILARITY_THRESHOLD:
                entries.append(self._entries[entry_id])
        return entries

    # ── Private ────────────────────────────────────────────────────────────────

    def _build(self, kb_path: pathlib.Path) -> None:
        with open(kb_path, encoding="utf-8") as fh:
            raw = json.load(fh)

        ids, docs, metas = [], [], []
        for item in raw:
            entry = KbEntry(
                id=item["id"],
                title=item["title"],
                description=item.get("description", ""),
                owasp=item.get("owasp"),
                cvss=item.get("cvss"),
                remediation=item.get("remediation", ""),
            )
            self._entries[entry.id] = entry
            ids.append(entry.id)
            docs.append(item["description"])
            metas.append({
                "title": entry.title,
                "owasp": entry.owasp or "",
                "cvss": str(entry.cvss) if entry.cvss is not None else "",
            })

        self._col.add(documents=docs, ids=ids, metadatas=metas)

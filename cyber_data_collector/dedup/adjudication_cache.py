"""Stored embeddings and adjudication answers, so a rerun asks only what changed.

Two caches in the database, both keyed so that a stale entry can never be
reused:

* **Embeddings** (``EmbeddingCache``) - keyed by a hash of the exact text
  embedded, the embedding model and the version of the code that builds the
  text (``EMBED_TEXT_VERSION``). They were kept in memory only, so every
  ``adjudicate-candidates`` run embedded every active event again.
* **Adjudication answers** (``AdjudicationCache``) - keyed by a hash of
  everything the model was sent (system prompt, rendered evidence), the model
  and the prompt and evidence versions. A pair or cluster whose evidence,
  prompt or model is unchanged is not asked again; if any of the three
  changes, the key changes and it is asked once more. Only valid answers are
  stored: a failed or invalid call is not cached, so it is retried.

Like the Jev judgements (:mod:`utils.jev`), a stored answer is the record of
that decision; a replication with the published database reproduces it.
Vectors are stored as float32, and live results are rounded the same way, so a
cached run and a fresh run compare identical numbers.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from array import array
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS EmbeddingCache (
    content_hash TEXT NOT NULL,       -- sha256 of the exact text embedded
    model        TEXT NOT NULL,       -- embedding model requested
    version      TEXT NOT NULL,       -- EMBED_TEXT_VERSION: how the text was built
    dimensions   INTEGER NOT NULL,
    vector       BLOB NOT NULL,       -- little-endian float32
    created_at   TEXT NOT NULL,
    PRIMARY KEY (content_hash, model, version)
);
CREATE TABLE IF NOT EXISTS AdjudicationCache (
    cache_key        TEXT PRIMARY KEY,  -- sha256 of kind, versions, model and the exact messages
    kind             TEXT NOT NULL,     -- 'pair' or 'cluster'
    prompt_version   TEXT NOT NULL,
    evidence_version TEXT NOT NULL,
    model            TEXT NOT NULL,
    subject_ids      TEXT NOT NULL,     -- JSON list of the ids judged, in the order shown
    response_json    TEXT NOT NULL,     -- the validated answer
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adjudicationcache_kind ON AdjudicationCache (kind, prompt_version);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pack(vector: Sequence[float]) -> bytes:
    return array("f", vector).tobytes()


def unpack(blob: bytes) -> List[float]:
    values = array("f")
    values.frombytes(blob)
    return list(values)


def decision_key(kind: str, prompt_version: str, evidence_version: str, model: str,
                 messages: Sequence[dict]) -> str:
    """The key of one adjudication: everything the model was sent, and which versions sent it."""
    blob = json.dumps({"kind": kind, "prompt": prompt_version, "evidence": evidence_version, "model": model,
                       "messages": list(messages)}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AdjudicationCache:
    """Reads and writes the two caches on one connection; counts what it served."""

    def __init__(self, conn: sqlite3.Connection, create: bool = True):
        """``create=False`` reads a database opened read-only: absent tables read as empty, writes raise."""
        self.conn = conn
        if create:
            ensure_schema(conn)
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('EmbeddingCache', 'AdjudicationCache')")}
        self.available = have == {"EmbeddingCache", "AdjudicationCache"}
        self.stats: Dict[str, int] = {"decisions_cached": 0, "decisions_new": 0}

    def _require_writable(self) -> None:
        if not self.available:
            raise RuntimeError("the adjudication cache tables do not exist and the database is read-only")

    def _commit(self, was_open: bool) -> None:
        # Never commit work that belongs to the caller's open transaction.
        if not was_open:
            self.conn.commit()

    # -- embeddings -----------------------------------------------------

    def embeddings(self, hashes: Iterable[str], model: str, version: str) -> Dict[str, List[float]]:
        wanted = sorted(set(hashes))
        found: Dict[str, List[float]] = {}
        if not self.available:
            return found
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for row in self.conn.execute(
                    f"SELECT content_hash, vector FROM EmbeddingCache WHERE model = ? AND version = ? "
                    f"AND content_hash IN ({marks})", (model, version, *chunk)):
                found[row[0]] = unpack(row[1])
        return found

    def store_embeddings(self, vectors: Dict[str, Sequence[float]], model: str, version: str) -> Dict[str, List[float]]:
        """Store ``hash -> vector``; returns the stored (float32) values for the caller to use."""
        self._require_writable()
        was_open = self.conn.in_transaction
        now = datetime.now().isoformat()
        rounded = {h: unpack(pack(v)) for h, v in vectors.items()}
        self.conn.executemany(
            "INSERT OR REPLACE INTO EmbeddingCache VALUES (?, ?, ?, ?, ?, ?)",
            [(h, model, version, len(v), pack(v), now) for h, v in vectors.items()])
        self._commit(was_open)
        return rounded

    # -- adjudication answers -------------------------------------------

    def decision(self, key: str) -> Optional[dict]:
        if not self.available:
            return None
        row = self.conn.execute("SELECT response_json FROM AdjudicationCache WHERE cache_key = ?", (key,)).fetchone()
        if row is None:
            return None
        self.stats["decisions_cached"] += 1
        return json.loads(row[0])

    def store_decision(self, key: str, kind: str, prompt_version: str, evidence_version: str, model: str,
                       subject_ids: Sequence[str], response: dict) -> None:
        self._require_writable()
        was_open = self.conn.in_transaction
        self.conn.execute(
            "INSERT OR REPLACE INTO AdjudicationCache VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, kind, prompt_version, evidence_version, model, json.dumps(list(subject_ids)),
             json.dumps(response, ensure_ascii=False), datetime.now().isoformat()))
        self.stats["decisions_new"] += 1
        self._commit(was_open)

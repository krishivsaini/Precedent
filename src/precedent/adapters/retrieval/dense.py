"""Dense retrieval over the precedent corpus, backed by `sqlite-vec`.

## On the default embedder — stated plainly

`HashingEmbedder` is a feature-hashing bag-of-words projection. It is **not semantic**: it
cannot tell that "withholding" and "TDS" refer to the same thing, because it only ever sees
surface tokens. It exists so the dense path is runnable, testable, and reproducible with no
credentials and no model download — not because it is a good embedder.

The honest consequence: with `HashingEmbedder`, the dense arm is a second lexical retriever
wearing different clothes, and "hybrid" will beat BM25 alone by very little. Any claim that
hybrid retrieval helps must be measured with a *real* embedder behind the `Embedder`
protocol, and the eval must record which one was used. Reporting a hybrid win obtained with
the hashing embedder would be measuring rank fusion, not semantics.

## On sqlite-vec

At corpus sizes under a few hundred, a brute-force scan would be just as fast; `vec0` is used
because it is the store the system carries forward, and because it keeps embeddings beside
the precedents rather than in a second system that can drift out of sync.
"""

import hashlib
import sqlite3
import struct
from typing import Protocol, runtime_checkable

import sqlite_vec

from precedent.adapters.retrieval.base import RetrievedPrecedent, tokenize
from precedent.adapters.storage.records import PrecedentRecord
from precedent.domain.precedent import Precedent

DEFAULT_DIMENSIONS = 384


@runtime_checkable
class Embedder(Protocol):
    dimensions: int

    def embed(self, text: str) -> list[float]:
        """A unit-length vector of length `dimensions`. Zero vector for empty text."""
        ...


class HashingEmbedder:
    """Deterministic feature hashing. Offline, credential-free, and openly non-semantic.

    Uses blake2b rather than Python's `hash()`, which is salted per process — a salted hash
    would make embeddings differ between runs and silently destroy reproducibility of every
    committed eval result.
    """

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS):
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            # Signed feature hashing: the sign bit makes collisions cancel on average
            # instead of always reinforcing.
            index = value % self.dimensions
            vector[index] += 1.0 if (value >> 63) & 1 else -1.0
        norm = sum(component * component for component in vector) ** 0.5
        if norm == 0.0:
            return vector
        return [component / norm for component in vector]


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


class DenseRetriever:
    def __init__(
        self,
        records: list[PrecedentRecord],
        embedder: Embedder | None = None,
        conn: sqlite3.Connection | None = None,
    ):
        self._records = list(records)
        self._embedder = embedder or HashingEmbedder()
        self._owns_conn = conn is None
        self._conn = conn or sqlite3.connect(":memory:")
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_precedents "
            f"USING vec0(embedding float[{self._embedder.dimensions}])"
        )
        for rowid, record in enumerate(self._records):
            vector = self._embedder.embed(Precedent.from_record(record).retrieval_text())
            self._conn.execute(
                "INSERT INTO vec_precedents(rowid, embedding) VALUES (?, ?)",
                (rowid, _pack(vector)),
            )

    def retrieve(self, query: str, k: int) -> list[RetrievedPrecedent]:
        if not self._records or k <= 0:
            return []
        vector = self._embedder.embed(query)
        rows = self._conn.execute(
            """
            SELECT rowid, distance FROM vec_precedents
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
            """,
            (_pack(vector), min(k, len(self._records))),
        ).fetchall()
        # vec0 returns squared-L2 distance. For unit vectors d² = 2 - 2·cos, so this
        # recovers cosine similarity exactly and keeps the ordering identical.
        return [
            RetrievedPrecedent(self._records[rowid], 1.0 - (distance / 2.0))
            for rowid, distance in rows
        ]

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()

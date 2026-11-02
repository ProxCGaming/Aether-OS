"""Safe serialisation for float-vector embeddings stored in SQLite.

Embeddings used to be persisted with ``pickle.dumps`` and read back
with ``pickle.loads`` (findings S1-M7 + S4-M13): anyone able to supply
a database file — import, backup, or restore surfaces — could achieve
arbitrary code execution the next time a semantic query deserialised
a row.

The replacement format is deliberately boring and data-only:

    b"aether-emb-v1:" + UTF-8 JSON array of numbers

JSON preserves Python floats exactly (repr round-trip), so vectors
keep the float64 fidelity the pickle format had, with no executable
surface. ``decode_embedding`` accepts ONLY blobs carrying the tag:
anything else — legacy pickle rows included — decodes to ``None``,
which every store already treats as "no embedding". Embeddings are
recomputable, so legacy rows are simply re-embedded by the existing
backfill paths instead of being unpickled.
"""
from __future__ import annotations

import json
import logging
import math

logger = logging.getLogger(__name__)

EMBEDDING_TAG = b"aether-emb-v1:"


def encode_embedding(embedding) -> bytes:
    """Serialise a float vector to the tagged JSON byte format."""
    values = [float(x) for x in embedding]
    return EMBEDDING_TAG + json.dumps(values, separators=(",", ":")).encode("utf-8")


def decode_embedding(blob) -> list[float] | None:
    """Decode a stored embedding blob, or ``None`` when it is not a
    valid tagged embedding (absent, legacy pickle, or corrupt).

    Never raises and never deserialises anything executable: an
    untagged blob is rejected before any parsing happens.
    """
    if blob is None:
        return None
    if isinstance(blob, (bytearray, memoryview)):
        blob = bytes(blob)
    if not isinstance(blob, bytes) or not blob.startswith(EMBEDDING_TAG):
        if blob:
            logger.warning(
                "Ignoring embedding blob without the %r format tag "
                "(legacy or corrupt row; it will be re-embedded)",
                EMBEDDING_TAG.decode("ascii"))
        return None
    try:
        data = json.loads(blob[len(EMBEDDING_TAG):].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("Ignoring corrupt tagged embedding blob")
        return None
    if not isinstance(data, list):
        return None
    values: list[float] = []
    for item in data:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        value = float(item)
        if not math.isfinite(value):
            return None
        values.append(value)
    return values

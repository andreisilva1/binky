"""A deterministic stand-in for a real embedding model.

This example is about *isolation*, not about retrieval quality, so it earns its
vectors from a hash instead of a 400 MB model download — same dimensionality,
same cost shape, no dependency. Two texts land near each other when they share
tokens, which is all the demo query needs.

DIM is 384 on purpose: it matches `all-MiniLM-L6-v2`, so swapping `embed` for a
real encoder (see README.md) needs no schema change. It also has to be this
large to work at all — unrelated hash vectors are only *approximately*
orthogonal, and that approximation error falls as 1/sqrt(DIM). At 64 dimensions
the noise floor across a few thousand documents is higher than the signal from
a two-word query, and the top hits are simply the luckiest collisions.
"""

from __future__ import annotations

import hashlib
import math

DIM = 384


def _token_vector(token: str) -> list[float]:
    """A fixed pseudo-random vector for one token, centred on zero.

    shake_256 is an extendable-output hash, so it yields exactly the bytes we
    need in one shot; slicing a fixed-width digest would repeat itself and make
    dimensions correlate.
    """
    raw = hashlib.shake_256(token.encode()).digest(2 * DIM)
    return [((raw[2 * i] << 8 | raw[2 * i + 1]) / 65535.0) - 0.5 for i in range(DIM)]


def embed(text: str) -> list[float]:
    """A unit-length DIM-dimensional vector for the text's bag of tokens."""
    vec = [0.0] * DIM
    for token in text.lower().split():
        for i, value in enumerate(_token_vector(token)):
            vec[i] += value
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def literal(vector: list[float]) -> str:
    """pgvector's text input format, e.g. ``[0.1,-0.2,...]``."""
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"

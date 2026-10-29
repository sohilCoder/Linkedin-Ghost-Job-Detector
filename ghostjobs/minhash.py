"""MinHash signatures and locality-sensitive hashing, implemented from scratch.

The problem: a company reposts the same job with small edits. Exact matching
fails, and comparing every posting with every other one is O(n^2).

MinHash: for a random hash function h, P[min h(A) == min h(B)] equals the
Jaccard similarity of sets A and B. Using `num_perm` independent hash
functions gives a short signature whose agreement rate estimates Jaccard.

LSH banding: split the signature into `bands` bands of `rows` rows each.
Two documents become *candidates* if any band matches exactly. With true
similarity s, P(candidate) = 1 - (1 - s^rows)^bands, an S-curve: near-
duplicates almost always collide, unrelated postings almost never do.
Candidates are then verified with exact Jaccard.
"""

from __future__ import annotations

import hashlib
import random
import struct
from array import array
from collections import defaultdict
from typing import Hashable, Iterable

_PRIME = (1 << 61) - 1          # Mersenne prime for universal hashing
_MAX = (1 << 61) - 1


def stable_hash64(token: str) -> int:
    """64-bit hash that is the same on every run and machine.
    (Python's built-in hash() is randomized per process.)"""
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "little")


class MinHasher:
    def __init__(self, num_perm: int = 64, seed: int = 1):
        rng = random.Random(seed)
        self.num_perm = num_perm
        self.params = [(rng.randrange(1, _PRIME), rng.randrange(0, _PRIME)) for _ in range(num_perm)]

    def signature(self, tokens: Iterable[str]) -> tuple[int, ...]:
        xs = [stable_hash64(t) for t in set(tokens)]
        if not xs:
            return tuple([_MAX] * self.num_perm)
        return tuple(min((a * x + b) % _PRIME for x in xs) for a, b in self.params)

    @staticmethod
    def estimate(sig_a, sig_b) -> float:
        """Fraction of agreeing positions = estimated Jaccard similarity."""
        return sum(1 for x, y in zip(sig_a, sig_b) if x == y) / len(sig_a)

    # compact storage for the signature cache
    @staticmethod
    def to_bytes(sig) -> bytes:
        return array("Q", sig).tobytes()

    @staticmethod
    def from_bytes(blob: bytes) -> tuple[int, ...]:
        arr = array("Q")
        arr.frombytes(blob)
        return tuple(arr)


def candidate_probability(similarity: float, bands: int, rows: int) -> float:
    """P(two documents with this Jaccard similarity share at least one band)."""
    return 1 - (1 - similarity ** rows) ** bands


def threshold(bands: int, rows: int) -> float:
    """Approximate similarity where the S-curve crosses 50%."""
    return (1 / bands) ** (1 / rows)


class LSHIndex:
    """Buckets signatures by band. `scope` (e.g. the company) is folded into
    the bucket key, so postings only collide with others in the same scope."""

    def __init__(self, num_perm: int = 64, bands: int = 16):
        if num_perm % bands:
            raise ValueError("num_perm must be divisible by bands")
        self.bands = bands
        self.rows = num_perm // bands
        self.buckets: dict[tuple, list[Hashable]] = defaultdict(list)

    def _keys(self, sig, scope: Hashable):
        r = self.rows
        for band in range(self.bands):
            chunk = sig[band * r:(band + 1) * r]
            yield (scope, band, hashlib.blake2b(struct.pack(f"{r}Q", *chunk), digest_size=8).digest())

    def add(self, key: Hashable, sig, scope: Hashable = None) -> None:
        for bucket in self._keys(sig, scope):
            self.buckets[bucket].append(key)

    def query(self, sig, scope: Hashable = None) -> set:
        found = set()
        for bucket in self._keys(sig, scope):
            found.update(self.buckets.get(bucket, ()))
        return found

    def candidate_pairs(self) -> set[tuple]:
        pairs = set()
        for members in self.buckets.values():
            if len(members) > 1:
                ordered = sorted(set(members))
                for i, a in enumerate(ordered):
                    for b in ordered[i + 1:]:
                        pairs.add((a, b))
        return pairs

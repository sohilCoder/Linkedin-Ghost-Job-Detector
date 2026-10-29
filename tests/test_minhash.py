import random

import pytest

from ghostjobs.minhash import LSHIndex, MinHasher, candidate_probability, stable_hash64, threshold


def overlapping_sets(rng, target, size=200):
    shared = int(round(2 * size * target / (1 + target)))   # |A∩B| for Jaccard = target
    common = {f"c{i}" for i in range(shared)}
    a = common | {f"a{i}" for i in range(size - shared)}
    b = common | {f"b{i}" for i in range(size - shared)}
    return a, b


def test_stable_hash_is_deterministic():
    assert stable_hash64("hello") == stable_hash64("hello")
    assert stable_hash64("hello") != stable_hash64("hellp")


@pytest.mark.parametrize("target", [0.1, 0.4, 0.7, 0.9])
def test_minhash_estimates_jaccard(target):
    rng = random.Random(0)
    a, b = overlapping_sets(rng, target)
    true = len(a & b) / len(a | b)
    mh = MinHasher(num_perm=256, seed=3)
    assert abs(MinHasher.estimate(mh.signature(a), mh.signature(b)) - true) < 0.1


def test_signature_roundtrip_and_empty_set():
    mh = MinHasher(64)
    sig = mh.signature({"x y z", "y z w"})
    assert MinHasher.from_bytes(MinHasher.to_bytes(sig)) == sig
    assert len(mh.signature(set())) == 64


def test_lsh_collision_rate_follows_the_s_curve():
    mh = MinHasher(64, seed=5)
    rng = random.Random(1)
    for target, expected in ((0.85, candidate_probability(0.85, 16, 4)), (0.2, candidate_probability(0.2, 16, 4))):
        hits = 0
        for trial in range(60):
            a, b = overlapping_sets(rng, target, size=120)
            a = {f"{trial}-{x}" for x in a}
            b = {f"{trial}-{x}" for x in b}
            index = LSHIndex(64, 16)
            index.add("a", mh.signature(a))
            hits += "a" in index.query(mh.signature(b))
        assert abs(hits / 60 - expected) < 0.2


def test_scopes_do_not_collide():
    mh = MinHasher(64)
    sig = mh.signature({"same text here"})
    index = LSHIndex(64, 16)
    index.add(1, sig, scope="acme")
    index.add(2, sig, scope="globex")
    assert index.query(sig, scope="acme") == {1}
    assert index.candidate_pairs() == set()
    index.add(3, sig, scope="acme")
    assert index.candidate_pairs() == {(1, 3)}


def test_threshold_and_validation():
    assert threshold(16, 4) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        LSHIndex(64, 10)

"""From individual postings to job *lineages*: the history of one job.

A company that reposts a role creates a new posting (new id, new "posted"
date, slightly reworded). A lineage stitches those back together:

1. Shingle every posting's title + text.
2. MinHash + LSH (scoped per company) finds candidate near-duplicates
   without comparing every pair.
3. Candidates are verified with exact Jaccard and a title check, then
   merged with union-find into *clusters* of the same role.
4. A cluster is split by location into lineages: the same role posted in
   five cities at once is "bulk hiring", not five reposts.

Thresholds were chosen from measured similarity distributions on simulated
data (see README): true reposts are >= ~0.62 similar, different jobs at the
same company rarely exceed that.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .minhash import LSHIndex, MinHasher
from .store import Store, as_date
from .text import normalize_location, normalize_title, shingles, jaccard, title_similarity

TEXT_THRESHOLD = 0.62
TITLE_THRESHOLD = 0.5
NUM_PERM = 64
BANDS = 16
OVERLAP_GRACE_DAYS = 3   # a new posting up to 3 days before the old one closes is still a repost


@dataclass
class PostingView:
    id: int
    board: str
    external_id: str
    title: str
    location: str
    url: str
    text: str
    salary_low: float | None
    salary_high: float | None
    first_published: date | None
    first_seen: date
    last_seen: date
    is_open: bool

    @property
    def start(self) -> date:
        """When it went up: the board's own publish date if it gave one."""
        if self.first_published and self.first_published <= self.first_seen:
            return self.first_published
        return self.first_seen


@dataclass
class Lineage:
    id: int
    board: str
    cluster_id: int
    location: str
    postings: list[PostingView] = field(default_factory=list)   # oldest first
    reposts: int = 0          # postings that replaced an earlier one
    parallel: int = 0         # duplicates open at the same time, same place
    concurrent_siblings: int = 0   # same role open elsewhere right now (bulk)
    left_censored: bool = False    # history may start before tracking did

    @property
    def latest(self) -> PostingView:
        return max(self.postings, key=lambda p: (p.first_seen, p.id))

    @property
    def start(self) -> date:
        return min(p.start for p in self.postings)

    @property
    def end(self) -> date:
        return max(p.last_seen for p in self.postings)

    @property
    def is_open(self) -> bool:
        return any(p.is_open for p in self.postings)

    def age_days(self, as_of: date) -> int:
        return ((as_of if self.is_open else self.end) - self.start).days


class UnionFind:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _sig_key(sh: set[str]) -> str:
    return hashlib.sha1("\x1f".join(sorted(sh)).encode("utf-8")).hexdigest()


@dataclass
class Analysis:
    as_of: date
    postings: dict[int, PostingView]
    lineages: list[Lineage]
    lineage_of: dict[int, int]            # posting id -> lineage id
    board_days: dict[str, tuple[str, str, int]]
    board_names: dict[str, str]
    index: LSHIndex                       # global index for pasted postings
    hasher: MinHasher
    shingle_sets: dict[int, set[str]]
    stats: dict


def build(store: Store, *, link_mode: str = "minhash") -> Analysis:
    """Build lineages from everything in the store.

    link_mode is for evaluation baselines:
      'minhash'    near-duplicate detection (the real method)
      'exact'      only identical title + text count as the same job
      'title'      same normalized title = same job (what a naive tracker does)
    """
    board_days = store.board_days()
    names = store.board_names()
    as_of = max((date.fromisoformat(v[1]) for v in board_days.values()), default=date.today())

    posts: dict[int, PostingView] = {}
    for r in store.postings():
        last_day = board_days.get(r["board"], (None, r["last_seen"], 0))[1]
        posts[r["id"]] = PostingView(
            r["id"], r["board"], r["external_id"], r["title"], r["location"], r["url"], r["text"],
            r["salary_low"], r["salary_high"], as_date(r["first_published"]),
            date.fromisoformat(r["first_seen"]), date.fromisoformat(r["last_seen"]),
            r["last_seen"] == last_day,
        )

    hasher = MinHasher(NUM_PERM, seed=7)
    shingle_sets = {pid: shingles(f"{p.title}\n{p.text}") for pid, p in posts.items()}
    cache = store.cached_signatures()
    new_sigs: dict[str, bytes] = {}
    scoped = LSHIndex(NUM_PERM, BANDS)
    global_index = LSHIndex(NUM_PERM, BANDS)
    for pid, sh in shingle_sets.items():
        key = _sig_key(sh)
        if key in cache:
            sig = MinHasher.from_bytes(cache[key])
        else:
            sig = hasher.signature(sh)
            new_sigs[key] = MinHasher.to_bytes(sig)
        scoped.add(pid, sig, scope=posts[pid].board)
        global_index.add(pid, sig)
    if new_sigs:
        store.save_signatures(new_sigs)

    uf = UnionFind(posts)
    candidates = verified = 0
    if link_mode == "minhash":
        for a, b in scoped.candidate_pairs():
            candidates += 1
            pa, pb = posts[a], posts[b]
            if (title_similarity(pa.title, pb.title) >= TITLE_THRESHOLD
                    and jaccard(shingle_sets[a], shingle_sets[b]) >= TEXT_THRESHOLD):
                verified += 1
                uf.union(a, b)
    else:
        groups: dict[tuple, list[int]] = defaultdict(list)
        for pid, p in posts.items():
            key = (p.board, normalize_title(p.title)) if link_mode == "title" else (p.board, p.title, p.text)
            groups[key].append(pid)
        for ids in groups.values():
            for other in ids[1:]:
                uf.union(ids[0], other)

    clusters: dict[int, list[int]] = defaultdict(list)
    for pid in posts:
        clusters[uf.find(pid)].append(pid)

    lineages: list[Lineage] = []
    lineage_of: dict[int, int] = {}
    for cluster_id, ids in clusters.items():
        by_loc: dict[str, list[PostingView]] = defaultdict(list)
        for pid in ids:
            by_loc[normalize_location(posts[pid].location)].append(posts[pid])
        cluster_lineages = []
        for loc, group in by_loc.items():
            group.sort(key=lambda p: (p.start, p.first_seen, p.id))
            lin = Lineage(len(lineages), group[0].board, cluster_id, group[-1].location, group)
            _count_reposts(lin)
            first_obs = board_days.get(lin.board, (None,))[0]
            lin.left_censored = bool(first_obs) and group[0].first_seen.isoformat() == first_obs \
                and group[0].first_published is None
            lineages.append(lin)
            cluster_lineages.append(lin)
            for p in group:
                lineage_of[p.id] = lin.id
        for lin in cluster_lineages:
            latest = lin.latest
            lin.concurrent_siblings = sum(
                1 for other in cluster_lineages if other is not lin and any(
                    q.first_seen <= latest.last_seen and latest.first_seen <= q.last_seen
                    for q in other.postings))

    stats = {
        "postings": len(posts),
        "lineages": len(lineages),
        "open_lineages": sum(1 for l in lineages if l.is_open),
        "candidate_pairs": candidates,
        "verified_links": verified,
        "signatures_computed": len(new_sigs),
        "signatures_cached": len(posts) - len(new_sigs),
    }
    return Analysis(as_of, posts, lineages, lineage_of, board_days, names,
                    global_index, hasher, shingle_sets, stats)


def _count_reposts(lin: Lineage) -> None:
    """Walk postings in time order. One that starts around or after the
    previous ones ended is a repost; one that overlaps substantially is a
    parallel duplicate (e.g. two openings for the same team)."""
    latest_end: date | None = None
    for p in lin.postings:
        if latest_end is not None:
            if (latest_end - p.first_seen).days <= OVERLAP_GRACE_DAYS:
                lin.reposts += 1
            else:
                lin.parallel += 1
        latest_end = p.last_seen if latest_end is None else max(latest_end, p.last_seen)

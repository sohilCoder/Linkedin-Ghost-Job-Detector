"""Explainable ghost-risk scoring.

Each signal adds points and a sentence saying why. The score is the sum,
capped at 100. No black box: every point on screen traces to one rule.

The weights are hand-set from reasoning about how real hiring works, then
sanity-checked on simulated data (evaluated on different random seeds than
the one used while building, to avoid tuning to the test).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .lineage import Analysis, Lineage, PostingView, TEXT_THRESHOLD, TITLE_THRESHOLD
from .text import TextSignals, jaccard, shingles, text_signals, title_similarity

HIGH, MEDIUM = 55, 30


@dataclass
class Reason:
    points: int
    text: str
    kind: str        # "history" | "text"


@dataclass
class Score:
    points: int
    reasons: list[Reason] = field(default_factory=list)

    @property
    def risk(self) -> str:
        return "high" if self.points >= HIGH else "medium" if self.points >= MEDIUM else "low"

    def to_dict(self) -> dict:
        return {
            "score": self.points,
            "risk": self.risk,
            "reasons": [{"points": r.points, "text": r.text, "kind": r.kind} for r in self.reasons],
        }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def history_reasons(lin: Lineage, as_of: date) -> list[Reason]:
    out: list[Reason] = []
    if lin.reposts:
        pts = {1: 12, 2: 28}.get(lin.reposts, min(50, 40 + 5 * (lin.reposts - 3)))
        since = lin.start.strftime("%b %d")
        out.append(Reason(pts, f"Reposted {_plural(lin.reposts, 'time')} since {since}, each time as a 'new' posting", "history"))

    age = lin.age_days(as_of)
    if lin.is_open and age >= 45:
        pts = 30 if age >= 180 else 20 if age >= 90 else 10
        note = " (at least; it was already up when tracking began)" if lin.left_censored else ""
        if lin.reposts:
            text = f"This job has been advertised for {age} days in total{note}"
        else:
            text = f"Open for {age} days{note}"
        out.append(Reason(pts, text, "history"))

    if lin.concurrent_siblings >= 4:
        out.append(Reason(5, f"Posted in {lin.concurrent_siblings + 1} locations at once", "history"))
    return out


def text_reasons(sig: TextSignals) -> list[Reason]:
    out: list[Reason] = []
    if sig.pipeline_phrases:
        quoted = ", ".join(f'"{p}"' for p in sig.pipeline_phrases[:2])
        out.append(Reason(25, f"Says it may not be a specific opening: {quoted}", "text"))
    if sig.salary is None:
        out.append(Reason(4, "No salary range listed", "text"))
    elif sig.salary.ratio >= 2.0:
        out.append(Reason(15, f"Very wide salary range: ${sig.salary.low:,.0f} to ${sig.salary.high:,.0f} "
                              f"(top is {sig.salary.ratio:.1f}x the bottom)", "text"))
    elif sig.salary.ratio >= 1.6:
        out.append(Reason(8, f"Wide salary range (top is {sig.salary.ratio:.1f}x the bottom)", "text"))
    if sig.vagueness >= 0.5:
        pts = 12 if sig.vagueness >= 0.75 else 6
        detail = f"buzzwords like {', '.join(repr(b) for b in sig.buzzwords[:2])}" if sig.buzzwords \
            else "few concrete details about the team or work"
        out.append(Reason(pts, f"Vague description: {detail}", "text"))
    if sig.word_count < 120:
        out.append(Reason(5, f"Very short description ({sig.word_count} words)", "text"))
    return out


def _combine(reasons: list[Reason]) -> Score:
    reasons = sorted(reasons, key=lambda r: -r.points)
    return Score(min(100, sum(r.points for r in reasons)), reasons)


def score_lineage(lin: Lineage, as_of: date, *, use_text: bool = True) -> Score:
    reasons = history_reasons(lin, as_of)
    if use_text:
        reasons += text_reasons(text_signals(lin.latest.text))
    return _combine(reasons)


def score_naive(p: PostingView, as_of: date) -> int:
    """What a job seeker sees: days since the *current* posting went up."""
    return (as_of - p.start).days


def posting_age_reason(published: date, today: date) -> Reason | None:
    """For a job we don't track: the board's own 'first published' date is
    the one piece of history we still have."""
    age = (today - published).days
    if age < 45:
        return None
    pts = 30 if age >= 180 else 20 if age >= 90 else 10
    return Reason(pts, f"The board says this posting has been up for {age} days "
                       f"(since {published.strftime('%b %d, %Y')})", "history")


def score_pasted(analysis: Analysis, title: str, text: str, board: str | None = None, *,
                 published: date | None = None, today: date | None = None) -> dict:
    """Score a posting someone pasted in. If it matches a tracked posting,
    its whole lineage history counts; otherwise only text signals do (plus
    the posting's own age, when its publish date is known)."""
    sh = shingles(f"{title}\n{text}")
    sig = analysis.hasher.signature(sh)
    best, best_sim = None, 0.0
    for pid in analysis.index.query(sig):
        p = analysis.postings[pid]
        if board and p.board != board:
            continue
        if title and title_similarity(title, p.title) < TITLE_THRESHOLD:
            continue
        sim = jaccard(sh, analysis.shingle_sets[pid])
        if sim >= TEXT_THRESHOLD and sim > best_sim:
            best, best_sim = p, sim

    reasons = text_reasons(text_signals(text))
    match = None
    if best is not None:
        lin = analysis.lineages[analysis.lineage_of[best.id]]
        reasons += history_reasons(lin, analysis.as_of)
        match = {"lineage_id": lin.id, "similarity": round(best_sim, 3),
                 "company": analysis.board_names.get(best.board, best.board)}
    elif published is not None:
        age_reason = posting_age_reason(published, today or date.today())
        if age_reason:
            reasons.append(age_reason)
    result = _combine(reasons).to_dict()
    result["match"] = match
    if match is None:
        result["note"] = ("This job isn't in the tracked history, so reposts can't be detected: it was "
                          "scored on its text" + (" and its posting date." if published else " only."))
    return result

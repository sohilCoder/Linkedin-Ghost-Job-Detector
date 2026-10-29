"""Measure the detector against a simulated dataset's ground truth.

Two questions, each compared with simpler baselines:

1. Linking: are postings grouped into the right job histories?
     exact    identical text only (misses every edited repost)
     title    same title = same job (merges different jobs that share a title)
     minhash  near-duplicate detection (this project)

2. Detection: among jobs open on the last day, which are ghosts?
     posting age   flag if the *current* posting is 60+ days old. This is
                   all a job seeker can see, and a repost resets it.
     history only  lineage signals (reposts, true age), no text
     full score    history + text signals
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from .lineage import build
from .score import HIGH, score_lineage, score_naive
from .store import Store

NAIVE_DAYS = 60


def load_truth(data_dir: Path) -> dict[tuple[str, str], dict]:
    with open(data_dir / "ground_truth.csv", encoding="utf-8", newline="") as f:
        return {(r["board"], r["external_id"]): r for r in csv.DictReader(f)}


def pairwise(predicted: dict, truth: dict) -> dict:
    """Pairwise precision/recall of a grouping (see warm-intro-pathfinder)."""
    keys = [k for k in truth if k in predicted]
    joint = Counter((predicted[k], truth[k]) for k in keys)
    pred = Counter(predicted[k] for k in keys)
    true = Counter(truth[k] for k in keys)
    pairs = lambda n: n * (n - 1) // 2  # noqa: E731
    tp = sum(pairs(n) for n in joint.values())
    pp, tt = sum(pairs(n) for n in pred.values()), sum(pairs(n) for n in true.values())
    precision = tp / pp if pp else 1.0
    recall = tp / tt if tt else 1.0
    return {"precision": precision, "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0}


def _detection(flags: list[tuple[str, bool]]) -> dict:
    tp = sum(1 for t, f in flags if f and t == "ghost")
    fp = sum(1 for t, f in flags if f and t != "ghost")
    ghosts = sum(1 for t, _ in flags if t == "ghost")
    out = {
        "precision": tp / (tp + fp) if tp + fp else 1.0,
        "recall": tp / ghosts if ghosts else 1.0,
        "flagged": tp + fp,
    }
    for kind in ("genuine", "legit_repost", "bulk"):
        n = sum(1 for t, _ in flags if t == kind)
        out[f"false_alarm_{kind}"] = sum(1 for t, f in flags if f and t == kind) / n if n else 0.0
    return out


def evaluate(data_dir: str | Path) -> dict:
    data_dir = Path(data_dir)
    if not (data_dir / "ground_truth.csv").exists():
        raise FileNotFoundError(f"{data_dir} has no ground_truth.csv; evaluation needs simulated data")
    store = Store(data_dir / "ghostjobs.db")
    store.ingest_dir(data_dir)
    truth = load_truth(data_dir)
    report: dict = {"linking": {}, "detection": {}}

    analyses = {}
    for mode in ("exact", "title", "minhash"):
        a = build(store, link_mode=mode)
        analyses[mode] = a
        predicted = {(p.board, p.external_id): a.lineage_of[pid] for pid, p in a.postings.items()}
        true_groups = {k: v["lineage_uid"] for k, v in truth.items()}
        report["linking"][mode] = {"lineages": len(a.lineages), **pairwise(predicted, true_groups)}

    def open_lineages(a):
        for lin in a.lineages:
            if lin.is_open:
                kind = Counter(truth[(p.board, p.external_id)]["type"] for p in lin.postings).most_common(1)[0][0]
                yield lin, kind

    a = analyses["minhash"]
    report["open_jobs"] = dict(Counter(kind for _, kind in open_lineages(a)))
    report["detection"]["posting_age"] = _detection(
        [(k, score_naive(lin.latest, a.as_of) >= NAIVE_DAYS) for lin, k in open_lineages(a)])
    report["detection"]["history_only"] = _detection(
        [(k, score_lineage(lin, a.as_of, use_text=False).points >= HIGH) for lin, k in open_lineages(a)])
    report["detection"]["full_score"] = _detection(
        [(k, score_lineage(lin, a.as_of).points >= HIGH) for lin, k in open_lineages(a)])
    t = analyses["title"]
    report["detection"]["full_score_title_linking"] = _detection(
        [(k, score_lineage(lin, t.as_of).points >= HIGH) for lin, k in open_lineages(t)])
    store.close()
    return report


def format_report(r: dict) -> str:
    lines = ["Linking postings into job histories (pairwise)",
             f"  {'method':12}{'histories':>10}{'precision':>11}{'recall':>9}"]
    for mode, m in r["linking"].items():
        lines.append(f"  {mode:12}{m['lineages']:>10}{m['precision']:>11.1%}{m['recall']:>9.1%}")
    open_jobs = ", ".join(f"{v} {k}" for k, v in sorted(r["open_jobs"].items()))
    lines += ["", f"Detecting ghost jobs among open jobs ({open_jobs})",
              f"  {'method':26}{'precision':>10}{'recall':>8}  false alarms: genuine / legit repost / bulk"]
    labels = {"posting_age": f"posting age >= {NAIVE_DAYS} days", "history_only": "history only",
              "full_score": "full score", "full_score_title_linking": "full score, title linking"}
    for key, m in r["detection"].items():
        lines.append(f"  {labels[key]:26}{m['precision']:>10.1%}{m['recall']:>8.1%}  "
                     f"{m['false_alarm_genuine']:>6.1%} / {m['false_alarm_legit_repost']:.1%} / {m['false_alarm_bulk']:.1%}")
    return "\n".join(lines)

"""Read-side model shared by the CLI and web server: build once, answer queries."""

from __future__ import annotations

import json
import random
import threading
from datetime import date
from pathlib import Path
from typing import Callable

from .collect import FetchError, fetch_json
from .lineage import Analysis, Lineage, build
from .links import parse_job_link
from .score import Score, score_lineage, score_naive, score_pasted
from .sources import parse
from .store import Store


class LinkCheckError(Exception):
    """A link was valid but the job couldn't be fetched; `status` is the HTTP
    status the API should answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _fetch_live(url: str):
    # One quick retry: someone is waiting on this in the browser.
    return fetch_json(url, retries=1, backoff=1.0, timeout=10.0)


class App:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.store = Store(self.data_dir / "ghostjobs.db")
        self.ingest_stats = self.store.ingest_dir(self.data_dir)
        manifest = self.data_dir / "manifest.json"
        self.simulated = manifest.exists() and json.loads(manifest.read_text(encoding="utf-8")).get("simulated", False)
        self._lock = threading.Lock()
        self.analysis: Analysis = build(self.store)
        self.scores: dict[int, Score] = {
            lin.id: score_lineage(lin, self.analysis.as_of) for lin in self.analysis.lineages
        }
        self.by_external_id = {(p.board, p.external_id): pid for pid, p in self.analysis.postings.items()}
        self.fetch: Callable = _fetch_live

    def name(self, board: str) -> str:
        return self.analysis.board_names.get(board, board.split(":", 1)[-1])

    def _row(self, lin: Lineage) -> dict:
        a, s, latest = self.analysis, self.scores[lin.id], lin.latest
        return {
            "lineage_id": lin.id,
            "title": latest.title,
            "location": latest.location,
            "score": s.points,
            "risk": s.risk,
            "reposts": lin.reposts,
            "age_days": lin.age_days(a.as_of),
            "posting_age_days": score_naive(latest, a.as_of),
            "url": latest.url,
        }

    def overview(self) -> dict:
        a = self.analysis
        companies: dict[str, dict] = {}
        for lin in a.lineages:
            if not lin.is_open:
                continue
            c = companies.setdefault(lin.board, {"board": lin.board, "name": self.name(lin.board),
                                                 "open_jobs": 0, "high": 0, "medium": 0})
            c["open_jobs"] += 1
            risk = self.scores[lin.id].risk
            if risk in ("high", "medium"):
                c[risk] += 1
        for c in companies.values():
            c["high_share"] = round(c["high"] / c["open_jobs"], 3) if c["open_jobs"] else 0.0
        days = [v[2] for v in a.board_days.values()]
        return {
            "simulated": self.simulated,
            "as_of": a.as_of.isoformat(),
            "days_tracked": max(days, default=0),
            "stats": a.stats,
            "companies": sorted(companies.values(), key=lambda c: (-c["high_share"], -c["open_jobs"], c["name"])),
        }

    def company(self, board: str) -> dict:
        rows = [self._row(l) for l in self.analysis.lineages if l.board == board and l.is_open]
        if not rows and board not in self.analysis.board_days:
            raise KeyError(board)
        rows.sort(key=lambda r: (-r["score"], r["title"]))
        return {"board": board, "name": self.name(board), "jobs": rows}

    def lineage(self, lineage_id: int) -> dict:
        a = self.analysis
        if not 0 <= lineage_id < len(a.lineages):
            raise KeyError(lineage_id)
        lin = a.lineages[lineage_id]
        first_day = a.board_days.get(lin.board, (a.as_of.isoformat(),))[0]
        return {
            **self._row(lin),
            **self.scores[lin.id].to_dict(),
            "company": self.name(lin.board),
            "is_open": lin.is_open,
            "tracking_start": first_day,
            "as_of": a.as_of.isoformat(),
            "postings": [
                {"external_id": p.external_id, "title": p.title, "published": p.start.isoformat(),
                 "first_seen": p.first_seen.isoformat(), "last_seen": p.last_seen.isoformat(),
                 "is_open": p.is_open, "url": p.url}
                for p in lin.postings
            ],
            "text": lin.latest.text,
        }

    def check(self, title: str, text: str, board: str | None = None) -> dict:
        with self._lock:   # MinHasher/LSH are read-only, but keep scoring serialized
            return score_pasted(self.analysis, title, text, board or None)

    def check_url(self, url: str) -> dict:
        """Score the job behind a Greenhouse or Lever link.

        Raises links.UnsupportedLink for links we can't read (including
        LinkedIn) and LinkCheckError when the board can't be reached."""
        link = parse_job_link(url)
        a = self.analysis

        pid = self.by_external_id.get((link.board, link.job_id))
        if pid is not None:
            # Already tracked: use our copy, which comes with full history.
            p = a.postings[pid]
            lin = a.lineages[a.lineage_of[pid]]
            result = self.scores[lin.id].to_dict()
            result["match"] = {"lineage_id": lin.id, "similarity": 1.0, "company": self.name(lin.board)}
            result["source"] = "tracked"
            result["job"] = {"title": p.title, "company": self.name(p.board), "location": p.location,
                             "url": p.url, "published": p.start.isoformat()}
            return result

        try:
            payload = self.fetch(link.api_url)
        except FetchError as exc:
            if exc.status == 404:
                raise LinkCheckError(404, "That job wasn't found. It may have been taken down, "
                                          "or the link is incomplete.") from exc
            raise LinkCheckError(502, f"Couldn't reach {link.source.title()} right now. "
                                      "Try again in a minute, or paste the description instead.") from exc

        wrapped = {"jobs": [payload]} if link.source == "greenhouse" else [payload]
        postings = parse(link.source, wrapped) if isinstance(payload, dict) else []
        if not postings or not postings[0].text:
            raise LinkCheckError(502, "The job board answered, but without a job description to score.")
        p = postings[0]
        tracked_board = link.board if link.board in a.board_days else None
        with self._lock:
            result = score_pasted(a, p.title, p.text, tracked_board,
                                  published=p.first_published, today=date.today())
        company = payload.get("company_name") if isinstance(payload.get("company_name"), str) else None
        if not company:
            company = self.name(link.board) if tracked_board else link.token
        result["source"] = "live"
        result["job"] = {
            "title": p.title,
            "company": company,
            "location": p.location,
            "url": p.url or url,
            "published": p.first_published.isoformat() if p.first_published else None,
        }
        return result

    def example(self, seed: int | None = None) -> dict:
        """A tracked open posting to paste into the checker, for demos."""
        rng = random.Random(seed)
        pool = [l for l in self.analysis.lineages if l.is_open and self.scores[l.id].points >= 30] \
            or [l for l in self.analysis.lineages if l.is_open]
        if not pool:
            return {"title": "", "text": "", "board": "", "url": ""}
        lin = rng.choice(pool)
        return {"title": lin.latest.title, "text": lin.latest.text, "board": lin.board, "url": lin.latest.url}

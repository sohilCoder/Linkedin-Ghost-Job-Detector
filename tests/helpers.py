"""Build tiny hand-made job boards for tests."""

from __future__ import annotations

import html
import json
from datetime import date, timedelta
from pathlib import Path

from ghostjobs.collect import Board, snapshot_path, write_snapshot

BASE = date(2026, 6, 1)

SWE = [
    "Design and build payment services in Python and Go",
    "Own features end to end from design doc to rollout",
    "Improve reliability of our Kubernetes deployment pipeline",
    "Write well tested code and review pull requests",
    "Join the on-call rotation for the services you own",
    "Profile and optimize slow Postgres queries",
]
ANALYST = [
    "Build Tableau dashboards that leadership reads weekly",
    "Write SQL to answer questions about customer retention",
    "Design and analyze A/B tests with product managers",
    "Forecast demand for the next two quarters",
    "Maintain dbt models for company reporting",
    "Investigate anomalies in revenue data",
]


def content(title: str, duties: list[str], salary: str = "$120,000 - $150,000", extra: str = "") -> str:
    body = (f"<h2>About Acme</h2><p>Acme builds tools for small shops.</p>"
            f"<p>As a {html.escape(title)} you will:</p><ul>"
            + "".join(f"<li>{html.escape(d)}</li>" for d in duties)
            + f"</ul><p>{html.escape(extra)}</p><p>The base salary range is {salary}.</p>")
    return html.escape(body)


def job(job_id: int, title: str, duties: list[str], location: str = "Chicago, IL",
        published: date = BASE, **kw) -> dict:
    return {
        "id": job_id,
        "title": title,
        "updated_at": f"{published.isoformat()}T09:00:00Z",
        "first_published": f"{published.isoformat()}T09:00:00Z",
        "location": {"name": location},
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "content": content(title, duties, **kw),
    }


def write_board(data_dir: Path, schedule: list[tuple[dict, int, int]], *, n_days: int,
                token: str = "acme", skip_days: set[int] = frozenset()) -> None:
    """schedule: (job dict, first day index, last day index exclusive)."""
    board = Board("greenhouse", token, token.title())
    for d in range(n_days):
        if d in skip_days:
            continue
        jobs = [j for j, start, end in schedule if start <= d < end]
        write_snapshot(snapshot_path(data_dir, board, BASE + timedelta(days=d)),
                       {"jobs": jobs, "meta": {"total": len(jobs)}})
    names = data_dir / "boards.json"
    existing = json.loads(names.read_text(encoding="utf-8")) if names.exists() else {}
    existing[board.key] = board.name
    names.write_text(json.dumps(existing), encoding="utf-8")

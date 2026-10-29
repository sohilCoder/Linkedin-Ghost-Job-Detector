"""SQLite storage: snapshots in, posting histories out.

Tables
    boards      tracked boards and display names
    observed    which (board, day) snapshots exist; the source of truth for
                "was this board checked that day?"
    postings    one row per (board, external id): text, salary, first/last seen
    sightings   (posting, day) pairs
    signatures  MinHash cache keyed by text hash, so re-runs are incremental

Why `observed` matters: if the collector failed on a day, every posting is
missing from that day's data. Treating that as "closed" would invent fake
reposts. Openness is therefore judged against each board's own observed days.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

from .sources import SOURCES, parse

SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
    board TEXT PRIMARY KEY,          -- 'greenhouse:token'
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observed (
    board TEXT NOT NULL,
    day TEXT NOT NULL,
    n_jobs INTEGER NOT NULL,
    PRIMARY KEY (board, day)
);
CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY,
    board TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT NOT NULL,
    url TEXT NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    salary_low REAL,
    salary_high REAL,
    first_published TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    edits INTEGER NOT NULL DEFAULT 0,
    UNIQUE (board, external_id)
);
CREATE TABLE IF NOT EXISTS sightings (
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    day TEXT NOT NULL,
    PRIMARY KEY (posting_id, day)
);
CREATE TABLE IF NOT EXISTS signatures (
    text_hash TEXT PRIMARY KEY,
    sig BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_postings_board ON postings(board);
"""


def text_hash(title: str, text: str) -> str:
    return hashlib.sha1(f"{title}\n{text}".encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # --- ingestion ------------------------------------------------------------
    def ingest_dir(self, data_dir: str | Path) -> dict:
        """Ingest every snapshot not yet seen. Safe to run repeatedly."""
        data_dir = Path(data_dir)
        names_file = data_dir / "boards.json"
        names = json.loads(names_file.read_text(encoding="utf-8")) if names_file.exists() else {}
        done = {(r["board"], r["day"]) for r in self.db.execute("SELECT board, day FROM observed")}

        files = []
        for source in SOURCES:
            root = data_dir / "snapshots" / source
            if not root.exists():
                continue
            for board_dir in sorted(p for p in root.iterdir() if p.is_dir()):
                key = f"{source}:{board_dir.name}"
                for f in sorted(board_dir.glob("*.json.gz")):
                    day = f.name.split(".")[0]
                    if (key, day) not in done:
                        files.append((day, key, source, f))
        files.sort()   # chronological, so first_seen/last_seen grow correctly

        stats = {"files": 0, "new_postings": 0, "edited": 0, "errors": []}
        with self.db:
            for key, name in names.items():
                self.db.execute("INSERT INTO boards VALUES (?, ?) ON CONFLICT(board) DO UPDATE SET name=excluded.name",
                                (key, name))
            for day, key, source, f in files:
                try:
                    with gzip.open(f, "rt", encoding="utf-8") as fh:
                        payload = json.load(fh)
                except (OSError, json.JSONDecodeError) as exc:
                    stats["errors"].append(f"{f}: {exc}")
                    continue
                self.db.execute("INSERT OR IGNORE INTO boards VALUES (?, ?)",
                                (key, names.get(key, key.split(":", 1)[1])))
                postings = parse(source, payload)
                self.db.execute("INSERT INTO observed VALUES (?, ?, ?)", (key, day, len(postings)))
                for p in postings:
                    self._upsert(key, day, p, stats)
                stats["files"] += 1
        return stats

    def _upsert(self, board: str, day: str, p, stats: dict) -> None:
        h = text_hash(p.title, p.text)
        fp = p.first_published.isoformat() if p.first_published else None
        row = self.db.execute("SELECT id, text_hash FROM postings WHERE board=? AND external_id=?",
                              (board, p.external_id)).fetchone()
        if row is None:
            cur = self.db.execute(
                "INSERT INTO postings (board, external_id, title, location, url, text, text_hash,"
                " salary_low, salary_high, first_published, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (board, p.external_id, p.title, p.location, p.url, p.text, h,
                 p.salary_low, p.salary_high, fp, day, day))
            pid = cur.lastrowid
            stats["new_postings"] += 1
        else:
            pid = row["id"]
            if row["text_hash"] != h:   # edited in place: keep latest version
                stats["edited"] += 1
                self.db.execute(
                    "UPDATE postings SET title=?, location=?, text=?, text_hash=?, salary_low=?,"
                    " salary_high=?, edits=edits+1 WHERE id=?",
                    (p.title, p.location, p.text, h, p.salary_low, p.salary_high, pid))
            self.db.execute("UPDATE postings SET last_seen=max(last_seen, ?), first_seen=min(first_seen, ?)"
                            " WHERE id=?", (day, day, pid))
        self.db.execute("INSERT OR IGNORE INTO sightings VALUES (?, ?)", (pid, day))

    # --- reads ------------------------------------------------------------------
    def postings(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM postings ORDER BY board, first_seen, id").fetchall()

    def board_days(self) -> dict[str, tuple[str, str, int]]:
        """board -> (first observed day, last observed day, number of days)."""
        return {
            r["board"]: (r["first"], r["last"], r["n"])
            for r in self.db.execute("SELECT board, min(day) AS first, max(day) AS last, count(*) AS n"
                                     " FROM observed GROUP BY board")
        }

    def board_names(self) -> dict[str, str]:
        return {r["board"]: r["name"] for r in self.db.execute("SELECT board, name FROM boards")}

    def sightings(self, posting_ids: list[int]) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {pid: [] for pid in posting_ids}
        if not posting_ids:
            return out
        marks = ",".join("?" * len(posting_ids))
        for r in self.db.execute(f"SELECT posting_id, day FROM sightings WHERE posting_id IN ({marks})"
                                 " ORDER BY day", posting_ids):
            out[r["posting_id"]].append(r["day"])
        return out

    # --- signature cache --------------------------------------------------------
    def cached_signatures(self) -> dict[str, bytes]:
        return {r["text_hash"]: r["sig"] for r in self.db.execute("SELECT text_hash, sig FROM signatures")}

    def save_signatures(self, items: dict[str, bytes]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO signatures VALUES (?, ?)", items.items())


def as_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None

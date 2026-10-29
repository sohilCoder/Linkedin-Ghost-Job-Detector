"""Daily collector: fetch each tracked board once and save a raw snapshot.

Snapshots are stored exactly as the API returned them (gzipped JSON), one
file per board per day:

    <data>/snapshots/<source>/<token>/<YYYY-MM-DD>.json.gz

Keeping raw responses means parsing bugs can be fixed and everything
re-ingested later without losing history.

Boards file format (one per line, # for comments):
    greenhouse:<token>  Optional Display Name
    lever:<token>       Optional Display Name
The token is the part after boards.greenhouse.io/ or jobs.lever.co/ in a
company's job board URL.
"""

from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from .sources import SOURCES, board_url

USER_AGENT = "ghost-job-detector/0.1 (personal research project)"
RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Board:
    source: str
    token: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.source}:{self.token}"


def read_boards(path: str | Path) -> list[Board]:
    boards = []
    for line_no, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        spec, _, name = line.partition(" ")
        source, _, token = spec.partition(":")
        if source not in SOURCES or not token:
            raise ValueError(f"{path}:{line_no}: expected 'greenhouse:<token>' or 'lever:<token>'")
        boards.append(Board(source, token.strip(), name.strip() or token.strip().replace("-", " ").title()))
    return boards


class FetchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status   # HTTP status, when the server answered


def fetch_json(url: str, *, retries: int = 3, backoff: float = 2.0, timeout: float = 20.0,
               opener: Callable = urllib.request.urlopen, sleep: Callable = time.sleep):
    """GET a JSON document, retrying rate limits and server errors with
    exponential backoff and jitter. Client errors (404 etc.) fail fast."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with opener(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUS or attempt == retries:
                raise FetchError(f"HTTP {exc.code} for {url}", exc.code) from exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            wait = float(retry_after) if retry_after and retry_after.isdigit() else backoff * 2 ** attempt
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == retries:
                raise FetchError(f"{type(exc).__name__} for {url}: {exc}") from exc
            wait = backoff * 2 ** attempt
        sleep(wait + random.uniform(0, 0.5))
    raise FetchError(f"gave up on {url}")  # pragma: no cover


def snapshot_path(data_dir: Path, board: Board, day: date) -> Path:
    return data_dir / "snapshots" / board.source / board.token / f"{day.isoformat()}.json.gz"


def write_snapshot(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(payload, f)
    tmp.replace(path)   # atomic: never leave a half-written snapshot


def collect(boards: list[Board], data_dir: str | Path, *, day: date | None = None,
            delay: float = 1.0, fetch: Callable = fetch_json, sleep: Callable = time.sleep) -> dict:
    """Fetch every board once for `day`. A failed board is reported, not
    fatal: a missing day is handled downstream as 'not observed', so it
    never looks like every posting closed."""
    data_dir = Path(data_dir)
    day = day or date.today()
    report = {"day": day.isoformat(), "saved": [], "skipped": [], "failed": {}}
    names_file = data_dir / "boards.json"
    names = json.loads(names_file.read_text(encoding="utf-8")) if names_file.exists() else {}
    for i, board in enumerate(boards):
        names[board.key] = board.name
        path = snapshot_path(data_dir, board, day)
        if path.exists():
            report["skipped"].append(board.key)
            continue
        if i:
            sleep(delay)   # be polite between requests
        try:
            payload = fetch(board_url(board.source, board.token))
        except FetchError as exc:
            report["failed"][board.key] = str(exc)
            continue
        write_snapshot(path, payload)
        report["saved"].append(board.key)
    data_dir.mkdir(parents=True, exist_ok=True)
    names_file.write_text(json.dumps(names, indent=2), encoding="utf-8")
    return report

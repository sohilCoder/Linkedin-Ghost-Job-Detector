import gzip
import io
import json
import urllib.error
from datetime import date

import pytest

from ghostjobs.collect import Board, FetchError, collect, fetch_json, read_boards, snapshot_path
from ghostjobs.sources import board_url, parse_greenhouse, parse_lever


def test_parse_greenhouse_with_pay_ranges_and_missing_fields():
    payload = {"jobs": [
        {"id": 11, "title": "Data Analyst", "location": {"name": "Austin, TX"},
         "absolute_url": "https://x/11", "first_published": "2026-05-01T10:00:00-04:00",
         "content": "&lt;p&gt;Analyze retention.&lt;/p&gt;",
         "pay_input_ranges": [{"min_cents": 9_000_000, "max_cents": 12_000_000, "currency_type": "USD"}]},
        {"id": 12, "title": "Engineer", "content": None, "location": None},
        {"title": "no id, skipped"},
    ], "meta": {"total": 3}}
    a, b = parse_greenhouse(payload)
    assert a.external_id == "11" and a.text == "Analyze retention."
    assert (a.salary_low, a.salary_high) == (90_000, 120_000)
    assert a.first_published == date(2026, 5, 1)
    assert b.location == "" and b.text == "" and b.salary_low is None
    assert parse_greenhouse("garbage") == []


def test_parse_lever():
    payload = [{
        "id": "abc-123", "text": "Account Executive", "hostedUrl": "https://jobs.lever.co/x/abc-123",
        "categories": {"location": "Remote - US", "team": "Sales"},
        "descriptionPlain": "Sell software.", "lists": [{"text": "You will", "content": "<li>Run demos</li>"}],
        "additionalPlain": "Benefits included.", "createdAt": 1767225600000,
        "salaryRange": {"min": 30, "max": 40, "currency": "USD", "interval": "per-hour-wage"},
    }]
    (p,) = parse_lever(payload)
    assert p.title == "Account Executive" and p.location == "Remote - US"
    assert "Run demos" in p.text and "Benefits included." in p.text
    assert p.first_published == date(2026, 1, 1)
    assert (p.salary_low, p.salary_high) == (62_400, 83_200)


def test_board_urls():
    assert board_url("greenhouse", "acme").startswith("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
    assert board_url("lever", "acme") == "https://api.lever.co/v0/postings/acme?mode=json"
    with pytest.raises(ValueError):
        board_url("workday", "acme")


def test_read_boards(tmp_path):
    f = tmp_path / "boards.txt"
    f.write_text("# comment\ngreenhouse:acme  Acme Corp\n\nlever:globex\n", encoding="utf-8")
    boards = read_boards(f)
    assert boards == [Board("greenhouse", "acme", "Acme Corp"), Board("lever", "globex", "Globex")]
    f.write_text("workday:nope\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_boards(f)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, None)


def test_fetch_retries_rate_limits_then_succeeds():
    calls, sleeps = [], []

    def opener(req, timeout):
        calls.append(req.full_url)
        if len(calls) < 3:
            raise http_error(429)
        return FakeResponse(b'{"jobs": []}')

    assert fetch_json("https://x", opener=opener, sleep=sleeps.append) == {"jobs": []}
    assert len(calls) == 3 and len(sleeps) == 2 and sleeps[1] > sleeps[0]


def test_fetch_fails_fast_on_404():
    calls = []

    def opener(req, timeout):
        calls.append(1)
        raise http_error(404)

    with pytest.raises(FetchError):
        fetch_json("https://x", opener=opener, sleep=lambda s: None)
    assert len(calls) == 1


def test_collect_saves_skips_and_reports_failures(tmp_path):
    boards = [Board("greenhouse", "acme", "Acme"), Board("lever", "broken", "Broken")]

    def fetch(url):
        if "broken" in url:
            raise FetchError("HTTP 404")
        return {"jobs": [{"id": 1, "title": "Ünïcode Engineer"}]}

    day = date(2026, 10, 1)
    report = collect(boards, tmp_path, day=day, fetch=fetch, sleep=lambda s: None)
    assert report["saved"] == ["greenhouse:acme"] and "lever:broken" in report["failed"]
    with gzip.open(snapshot_path(tmp_path, boards[0], day), "rt", encoding="utf-8") as f:
        assert json.load(f)["jobs"][0]["title"] == "Ünïcode Engineer"
    again = collect(boards, tmp_path, day=day, fetch=fetch, sleep=lambda s: None)
    assert again["skipped"] == ["greenhouse:acme"]
    assert json.loads((tmp_path / "boards.json").read_text(encoding="utf-8"))["lever:broken"] == "Broken"

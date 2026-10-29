"""Parsers for public job board feeds, normalized to one Posting shape.

Supported:
  greenhouse  https://boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true
  lever       https://api.lever.co/v0/postings/<token>?mode=json

Parsers are defensive: they read the fields they need, tolerate missing or
extra ones, and never trust that optional fields (pay ranges) exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from .text import html_to_text, parse_salary

SOURCES = ("greenhouse", "lever")


@dataclass
class Posting:
    external_id: str
    title: str
    location: str
    url: str
    text: str
    salary_low: float | None = None
    salary_high: float | None = None
    first_published: date | None = None
    updated: date | None = None


def board_url(source: str, token: str) -> str:
    if source == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    if source == "lever":
        return f"https://api.lever.co/v0/postings/{token}?mode=json"
    raise ValueError(f"unknown source: {source}")


def _iso_date(value) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _epoch_ms_date(value) -> date | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()


def _salary(text: str, low=None, high=None) -> tuple[float | None, float | None]:
    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and 0 < low <= high:
        return float(low), float(high)
    found = parse_salary(text)
    return (found.low, found.high) if found else (None, None)


def parse_greenhouse(payload) -> list[Posting]:
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    out = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("id") is None:
            continue
        text = html_to_text(job.get("content") or "")
        low = high = None
        ranges = job.get("pay_input_ranges") or []
        if ranges and isinstance(ranges[0], dict):
            r = ranges[0]
            if isinstance(r.get("min_cents"), (int, float)) and isinstance(r.get("max_cents"), (int, float)):
                low, high = r["min_cents"] / 100, r["max_cents"] / 100
        low, high = _salary(text, low, high)
        location = job.get("location")
        out.append(Posting(
            external_id=str(job["id"]),
            title=str(job.get("title") or "").strip(),
            location=str(location.get("name") if isinstance(location, dict) else location or "").strip(),
            url=str(job.get("absolute_url") or ""),
            text=text,
            salary_low=low,
            salary_high=high,
            first_published=_iso_date(job.get("first_published")),
            updated=_iso_date(job.get("updated_at")),
        ))
    return out


def parse_lever(payload) -> list[Posting]:
    items = payload if isinstance(payload, list) else []
    out = []
    for job in items:
        if not isinstance(job, dict) or not job.get("id"):
            continue
        parts = [job.get("descriptionPlain") or html_to_text(job.get("description") or "")]
        for section in job.get("lists") or []:
            if isinstance(section, dict):
                parts.append(str(section.get("text") or ""))
                parts.append(html_to_text(str(section.get("content") or "")))
        parts.append(job.get("additionalPlain") or html_to_text(job.get("additional") or ""))
        text = "\n".join(p for p in parts if p)
        sr = job.get("salaryRange") if isinstance(job.get("salaryRange"), dict) else {}
        low, high = sr.get("min"), sr.get("max")
        if str(sr.get("interval", "")).lower().startswith("per-hour") and low and high:
            low, high = low * 2080, high * 2080
        low, high = _salary(text, low, high)
        cats = job.get("categories") if isinstance(job.get("categories"), dict) else {}
        created = _epoch_ms_date(job.get("createdAt"))
        out.append(Posting(
            external_id=str(job["id"]),
            title=str(job.get("text") or "").strip(),
            location=str(cats.get("location") or "").strip(),
            url=str(job.get("hostedUrl") or ""),
            text=text,
            salary_low=low,
            salary_high=high,
            first_published=created,
            updated=created,
        ))
    return out


def parse(source: str, payload) -> list[Posting]:
    if source == "greenhouse":
        return parse_greenhouse(payload)
    if source == "lever":
        return parse_lever(payload)
    raise ValueError(f"unknown source: {source}")

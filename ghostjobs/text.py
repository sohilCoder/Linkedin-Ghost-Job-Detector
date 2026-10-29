"""Turning raw job postings into comparable text and simple signals.

Everything here is deterministic and dependency-free: HTML to text, the
word shingles MinHash compares, salary extraction, and the "vagueness"
features the scorer uses.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

# --- HTML -> text -------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    """Greenhouse sends HTML that is itself entity-escaped ("&lt;p&gt;").
    Unescape once if needed, strip tags, and tidy whitespace."""
    raw = raw or ""
    if "&lt;" in raw and "<" not in raw:
        raw = html.unescape(raw)
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    text = "".join(parser.parts)
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


# --- normalization and shingles -----------------------------------------------

def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


_WORD = re.compile(r"[a-z0-9+#]+")


def words(text: str) -> list[str]:
    return _WORD.findall(strip_accents(text or "").lower())


def shingles(text: str, k: int = 3) -> set[str]:
    """Overlapping k-word windows over the whole text:
    'build data pipelines in python' -> {'build data pipelines',
    'data pipelines in', 'pipelines in python'}.

    Windows deliberately cross line breaks. Alternatives measured on
    simulated data (per-line windows, sorted lines, boilerplate removal) all
    separated reposts from different jobs worse; see README. Known weakness:
    in a very short posting, heavy bullet reordering breaks a large share of
    the cross-line windows.
    """
    toks = words(text)
    if len(toks) < k:
        return {" ".join(toks)} if toks else set()
    return {" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1)}


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


_TITLE_NOISE = {"remote", "hybrid", "onsite", "us", "usa", "new", "urgent", "hiring", "immediate", "start"}


def normalize_title(title: str) -> str:
    """'Senior Software Engineer, Payments (Remote - US)' -> 'senior software engineer payments'."""
    t = re.sub(r"\(.*?\)", " ", strip_accents(title or "").lower())
    toks = [w for w in _WORD.findall(t) if w not in _TITLE_NOISE]
    return " ".join(toks)


def title_similarity(a: str, b: str) -> float:
    return jaccard(set(normalize_title(a).split()), set(normalize_title(b).split()))


def normalize_location(loc: str) -> str:
    """'Remote (US)', 'Remote - US' and 'US Remote' all become 'remote us'."""
    toks = sorted(set(_WORD.findall(strip_accents(loc or "").lower())) - {"and", "or"})
    return " ".join(toks) or "unspecified"


# --- salary -------------------------------------------------------------------

_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kK])?"
_RANGE = re.compile(_MONEY + r"\s*(?:-|–|—|to)\s*" + _MONEY)
_HOURLY = re.compile(r"(?:per hour|/\s?hr|/\s?hour|hourly)", re.I)


@dataclass(frozen=True)
class Salary:
    low: float
    high: float

    @property
    def ratio(self) -> float:
        return self.high / self.low if self.low else float("inf")


def _money(number: str, k: str | None) -> float:
    value = float(number.replace(",", ""))
    return value * 1000 if k else value


def parse_salary(text: str) -> Salary | None:
    """Find a pay range like '$120,000 - $180,000' or '$95k to $140k'.

    Hourly ranges are annualized (x2080) so ratios stay comparable. Returns
    the first plausible range, or None.
    """
    for match in _RANGE.finditer(text or ""):
        low, high = _money(match.group(1), match.group(2)), _money(match.group(3), match.group(4))
        if low <= 0 or high < low:
            continue
        window = text[match.end():match.end() + 25]
        if _HOURLY.search(window) or high < 500:
            low, high = low * 2080, high * 2080
        if 15_000 <= low <= 2_000_000:
            return Salary(low, high)
    return None


# --- vagueness ----------------------------------------------------------------

# Phrases that say outright this is not a specific open role.
PIPELINE_PHRASES = [
    "talent pipeline", "future opportunities", "future openings", "always looking for",
    "always on the lookout", "evergreen", "talent community", "general application",
    "may not be an active", "not tied to a specific", "upcoming opportunities",
    "pool of candidates", "as roles become available", "when a role opens",
]

BUZZWORDS = [
    "fast-paced", "fast paced", "rockstar", "ninja", "self-starter", "self starter",
    "wear many hats", "dynamic environment", "results-driven", "go-getter",
    "synergy", "passionate", "hit the ground running", "various projects",
    "other duties as assigned", "competitive salary", "work hard play hard",
    "thrive in ambiguity", "world-class", "game-changing",
]


@dataclass
class TextSignals:
    word_count: int
    pipeline_phrases: list[str]
    buzzwords: list[str]
    specifics: int          # numbers, tools, team names: signs of a real role
    salary: Salary | None

    @property
    def vagueness(self) -> float:
        """0 (concrete) .. 1 (very vague)."""
        buzz = min(1.0, len(self.buzzwords) / 4)
        concrete = min(1.0, self.specifics / 6)
        return max(0.0, min(1.0, 0.6 * buzz + 0.4 * (1 - concrete)))


_SPECIFIC = re.compile(
    r"\b(python|java|go|rust|sql|kubernetes|aws|gcp|react|typescript|spark|kafka|"
    r"terraform|postgres|tableau|figma|c\+\+|team|reports? to|on-call|okrs?|"
    r"\d+\+? years?|\d+%|\d+ (?:engineers|people|customers|million|users))\b",
    re.I,
)


def text_signals(text: str) -> TextSignals:
    lower = strip_accents(text or "").lower()
    return TextSignals(
        word_count=len(words(text)),
        pipeline_phrases=[p for p in PIPELINE_PHRASES if p in lower],
        buzzwords=[b for b in BUZZWORDS if b in lower],
        specifics=len(_SPECIFIC.findall(text or "")),
        salary=parse_salary(text),
    )

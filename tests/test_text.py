import pytest

from ghostjobs.text import (
    html_to_text,
    jaccard,
    normalize_location,
    normalize_title,
    parse_salary,
    shingles,
    text_signals,
    title_similarity,
)


def test_html_to_text_unescapes_greenhouse_content():
    raw = "&lt;h2&gt;About&lt;/h2&gt;&lt;ul&gt;&lt;li&gt;Ship &amp;amp; learn&lt;/li&gt;&lt;/ul&gt;"
    assert html_to_text(raw) == "About\nShip & learn"


def test_html_to_text_drops_scripts():
    assert html_to_text("<p>Hi</p><script>alert(1)</script><p>there</p>") == "Hi\nthere"


def test_shingles_mostly_survive_reordering():
    a = "build data pipelines in python. review pull requests with the team every week"
    b = "review pull requests with the team every week. build data pipelines in python"
    assert jaccard(shingles(a), shingles(b)) > 0.5
    assert shingles("") == set()
    assert shingles("two words") == {"two words"}


@pytest.mark.parametrize("text, low, high", [
    ("The base salary range is $120,000 - $180,000.", 120_000, 180_000),
    ("Pay: $95k to $140k per year", 95_000, 140_000),
    ("$110,000–$150,000", 110_000, 150_000),
    ("Hourly: $25 - $35 per hour", 52_000, 72_800),
])
def test_parse_salary(text, low, high):
    s = parse_salary(text)
    assert s and s.low == pytest.approx(low) and s.high == pytest.approx(high)


@pytest.mark.parametrize("text", ["No pay info here", "We raised $5,000,000 - $9,000,000", "$200 - $100"])
def test_parse_salary_rejects_non_salaries(text):
    assert parse_salary(text) is None


def test_title_and_location_normalization():
    assert normalize_title("Senior Software Engineer, Payments (Remote - US)") == "senior software engineer payments"
    assert title_similarity("Software Engineer", "Software Engineer, Payments") >= 0.5
    assert title_similarity("Software Engineer", "Data Analyst") == 0
    assert normalize_location("Remote (US)") == normalize_location("US - Remote")
    assert normalize_location("") == "unspecified"


def test_text_signals():
    vague = ("We are always looking for passionate rockstars in a fast-paced environment. "
             "Join our talent community for future opportunities.")
    s = text_signals(vague)
    assert "talent community" in s.pipeline_phrases and "future opportunities" in s.pipeline_phrases
    assert {"rockstar", "fast-paced", "passionate"} <= set(s.buzzwords)
    assert s.vagueness >= 0.5
    concrete = text_signals("Join the Payments team. Build services in Python and Go on Kubernetes and AWS. "
                            "3+ years of experience. Write SQL against Postgres.")
    assert concrete.vagueness < 0.5 and not concrete.pipeline_phrases

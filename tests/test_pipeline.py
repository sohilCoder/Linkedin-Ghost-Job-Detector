"""End-to-end behaviour on small hand-made boards."""

from datetime import timedelta

from ghostjobs.app import App
from ghostjobs.lineage import build
from ghostjobs.score import score_lineage
from ghostjobs.store import Store
from helpers import ANALYST, BASE, SWE, job, write_board


def reworded(duties):
    """A typical repost edit: swap two bullets and reword one."""
    d = list(duties)
    d[1], d[2] = d[2], d[1]
    d[0] = d[0].replace("build", "develop")
    return d


def ghost_scenario(tmp_path, skip_days=frozenset()):
    schedule = [
        # The same Software Engineer job posted 3 times with edits (ghost-like)
        (job(1, "Software Engineer", SWE, published=BASE - timedelta(days=50)), 0, 30),
        (job(2, "Software Engineer", reworded(SWE), published=BASE + timedelta(days=31)), 31, 62),
        (job(3, "Software Engineer, Payments", SWE[1:] + SWE[:1], published=BASE + timedelta(days=62)), 62, 100),
        # A different job that happens to share the title
        (job(4, "Software Engineer", ANALYST, published=BASE + timedelta(days=70)), 70, 100),
        # One role posted in three cities at once
        (job(5, "Data Analyst", ANALYST, location="Chicago, IL"), 10, 40),
        (job(6, "Data Analyst", ANALYST, location="Austin, TX"), 10, 40),
        (job(7, "Data Analyst", ANALYST, location="Remote - US"), 11, 41),
    ]
    write_board(tmp_path, schedule, n_days=100, skip_days=skip_days)
    store = Store(tmp_path / "ghostjobs.db")
    store.ingest_dir(tmp_path)
    return store


def lineage_of(analysis, external_id):
    pid = next(p.id for p in analysis.postings.values() if p.external_id == str(external_id))
    return analysis.lineages[analysis.lineage_of[pid]]


def test_reposts_are_stitched_into_one_lineage(tmp_path):
    a = build(ghost_scenario(tmp_path))
    lin = lineage_of(a, 1)
    assert {p.external_id for p in lin.postings} == {"1", "2", "3"}
    assert lin.reposts == 2 and lin.is_open
    assert lin.age_days(a.as_of) == (a.as_of - (BASE - timedelta(days=50))).days


def test_same_title_different_job_stays_separate(tmp_path):
    a = build(ghost_scenario(tmp_path))
    assert lineage_of(a, 4) is not lineage_of(a, 1)
    naive = build(Store(tmp_path / "ghostjobs.db"), link_mode="title")
    assert lineage_of(naive, 4) is lineage_of(naive, 1)   # the baseline gets this wrong


def test_bulk_posting_is_not_a_repost(tmp_path):
    a = build(ghost_scenario(tmp_path))
    lins = {lineage_of(a, i).id for i in (5, 6, 7)}
    assert len(lins) == 3
    for i in (5, 6, 7):
        lin = lineage_of(a, i)
        assert lin.reposts == 0 and lin.concurrent_siblings == 2 and not lin.is_open


def test_collector_outages_do_not_create_fake_reposts(tmp_path):
    a = build(ghost_scenario(tmp_path, skip_days={15, 16, 45}))
    assert lineage_of(a, 1).reposts == 2
    assert lineage_of(a, 5).reposts == 0


def test_repost_history_drives_the_score(tmp_path):
    a = build(ghost_scenario(tmp_path))
    ghost = score_lineage(lineage_of(a, 1), a.as_of)
    fresh = score_lineage(lineage_of(a, 4), a.as_of)
    assert ghost.risk in ("medium", "high") and fresh.risk == "low"
    assert any("Reposted 2 times" in r.text for r in ghost.reasons)
    assert any("advertised for" in r.text for r in ghost.reasons)


def test_signatures_are_cached_between_runs(tmp_path):
    store = ghost_scenario(tmp_path)
    first = build(store).stats
    second = build(store).stats
    assert first["signatures_computed"] > 0 and second["signatures_computed"] == 0


def test_ingest_is_incremental(tmp_path):
    store = ghost_scenario(tmp_path)
    assert store.ingest_dir(tmp_path)["files"] == 0


def test_pasted_posting_matches_tracked_history(tmp_path):
    ghost_scenario(tmp_path)
    app = App(tmp_path)
    latest = lineage_of(app.analysis, 3).latest
    result = app.check("Software Engineer", latest.text)
    assert result["match"] and result["match"]["lineage_id"] == lineage_of(app.analysis, 3).id
    assert any(r["kind"] == "history" for r in result["reasons"])
    unknown = app.check("Barista", "Make espresso drinks and keep the cafe clean. " * 5)
    assert unknown["match"] is None and "note" in unknown

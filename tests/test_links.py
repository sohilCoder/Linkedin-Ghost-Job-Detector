import json
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone

import pytest

from ghostjobs.app import App, LinkCheckError
from ghostjobs.collect import FetchError
from ghostjobs.links import LINKEDIN_MESSAGE, UnsupportedLink, parse_job_link
from ghostjobs.server import make_server
from helpers import SWE, content, job
from test_pipeline import ghost_scenario, lineage_of

LEVER_ID = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"


@pytest.mark.parametrize("url, source, token, job_id", [
    ("https://boards.greenhouse.io/acme/jobs/4012345", "greenhouse", "acme", "4012345"),
    ("job-boards.greenhouse.io/acme/jobs/4012345?gh_src=abc#app", "greenhouse", "acme", "4012345"),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=4012345", "greenhouse", "acme", "4012345"),
    ("https://www.boards.greenhouse.io/acme/jobs/4012345", "greenhouse", "acme", "4012345"),
    (f"https://jobs.lever.co/acme/{LEVER_ID}", "lever", "acme", LEVER_ID),
    (f"https://jobs.lever.co/acme/{LEVER_ID.upper()}/apply", "lever", "acme", LEVER_ID),
    (f"https://jobs.eu.lever.co/acme/{LEVER_ID}", "lever", "acme", LEVER_ID),
])
def test_parses_supported_links(url, source, token, job_id):
    link = parse_job_link(url)
    assert (link.source, link.token, link.job_id) == (source, token, job_id)


def test_api_url_is_built_not_copied():
    gh = parse_job_link("https://boards.greenhouse.io/acme/jobs/42")
    assert gh.api_url.startswith("https://boards-api.greenhouse.io/v1/boards/acme/jobs/42")
    lv = parse_job_link(f"https://jobs.eu.lever.co/acme/{LEVER_ID}")
    assert lv.api_url == f"https://api.eu.lever.co/v0/postings/acme/{LEVER_ID}?mode=json"


@pytest.mark.parametrize("url, expected", [
    ("https://www.linkedin.com/jobs/view/3912345678/", "LinkedIn doesn't allow"),
    ("https://lnkd.in/abc123", "LinkedIn doesn't allow"),
    ("https://careers.acme.com/jobs?gh_jid=4012345", "hosts its Greenhouse jobs on its own site"),
    ("https://www.indeed.com/viewjob?jk=abc", "Only Greenhouse"),
    ("https://boards.greenhouse.io/acme", "not a single job"),
    ("https://jobs.lever.co/acme", "not a single job"),
    ("https://boards.greenhouse.io/acme/jobs/not-a-number", "valid company and job id"),
    ("https://jobs.lever.co/acme/1234", "valid company and job id"),
    ("https://boards.greenhouse.io/..%2F..%2Fadmin/jobs/1", "valid company and job id"),
    ("", "Paste a job link"),
])
def test_rejects_unsupported_links_with_helpful_messages(url, expected):
    with pytest.raises(UnsupportedLink, match=expected):
        parse_job_link(url)
    assert "Apply on company website" in LINKEDIN_MESSAGE


def greenhouse_job_payload(published: date, **kw) -> dict:
    payload = job(777, "Platform Engineer", SWE, published=published, **kw)
    payload["company_name"] = "Initech"
    return payload


@pytest.fixture()
def app(tmp_path):
    ghost_scenario(tmp_path)
    return App(tmp_path)


def test_tracked_job_uses_local_history_without_fetching(app):
    def no_fetch(url):
        raise AssertionError("should not fetch a job we already track")

    app.fetch = no_fetch
    lin = lineage_of(app.analysis, 3)
    r = app.check_url(lin.latest.url)
    assert r["source"] == "tracked" and r["match"]["lineage_id"] == lin.id
    assert any("Reposted" in reason["text"] for reason in r["reasons"])


def test_untracked_job_is_fetched_and_scored(app):
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return greenhouse_job_payload(date.today() - timedelta(days=120),
                                      salary="$90,000 - $210,000",
                                      extra="We are always looking for talented people for future opportunities.")

    app.fetch = fake_fetch
    r = app.check_url("https://boards.greenhouse.io/initech/jobs/777")
    assert calls == ["https://boards-api.greenhouse.io/v1/boards/initech/jobs/777?pay_transparency=true"]
    assert r["source"] == "live" and r["match"] is None
    assert r["job"]["company"] == "Initech" and r["job"]["title"] == "Platform Engineer"
    texts = " ".join(reason["text"] for reason in r["reasons"])
    assert "up for 120 days" in texts and "future opportunities" in texts and "2.3x" in texts
    assert r["risk"] in ("medium", "high")
    assert "reposts can't be detected" in r["note"]


def test_live_lever_job(app):
    app.fetch = lambda url: {
        "id": LEVER_ID, "text": "Account Executive", "categories": {"location": "Remote - US"},
        "hostedUrl": f"https://jobs.lever.co/initech/{LEVER_ID}",
        "descriptionPlain": "Sell our analytics product to mid-market retailers. " * 20,
        "createdAt": int(datetime.combine(date.today() - timedelta(days=10), time(12),
                                          tzinfo=timezone.utc).timestamp() * 1000),
        "salaryRange": {"min": 70000, "max": 95000, "currency": "USD", "interval": "per-year-salary"},
    }
    r = app.check_url(f"https://jobs.lever.co/initech/{LEVER_ID}")
    assert r["source"] == "live" and r["risk"] == "low" and r["job"]["location"] == "Remote - US"
    assert r["job"]["published"] == (date.today() - timedelta(days=10)).isoformat()


def test_fetch_failures_become_clear_errors(app):
    def gone(url):
        raise FetchError("HTTP 404", 404)

    app.fetch = gone
    with pytest.raises(LinkCheckError) as exc:
        app.check_url("https://boards.greenhouse.io/initech/jobs/1")
    assert exc.value.status == 404 and "taken down" in str(exc.value)

    def down(url):
        raise FetchError("URLError")

    app.fetch = down
    with pytest.raises(LinkCheckError) as exc:
        app.check_url("https://boards.greenhouse.io/initech/jobs/1")
    assert exc.value.status == 502

    app.fetch = lambda url: {"id": 1, "title": "Empty", "content": ""}
    with pytest.raises(LinkCheckError, match="without a job description"):
        app.check_url("https://boards.greenhouse.io/initech/jobs/1")


def test_check_url_endpoint(app):
    app.fetch = lambda url: greenhouse_job_payload(date.today())
    server = make_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(body):
        req = urllib.request.Request(base + "/api/check-url", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    try:
        status, r = post({"url": "https://boards.greenhouse.io/initech/jobs/777"})
        assert status == 200 and r["job"]["company"] == "Initech"
        status, r = post({"url": "https://www.linkedin.com/jobs/view/123"})
        assert status == 400 and "LinkedIn" in r["error"]
        assert post(["not", "an", "object"])[0] == 400
        app.fetch = lambda url: (_ for _ in ()).throw(FetchError("HTTP 404", 404))
        assert post({"url": "https://boards.greenhouse.io/initech/jobs/5"})[0] == 404
    finally:
        server.shutdown()
        server.server_close()

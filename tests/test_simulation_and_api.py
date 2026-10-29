import gzip
import json
import threading
import urllib.error
import urllib.request

import pytest

from ghostjobs.app import App
from ghostjobs.evaluate import evaluate
from ghostjobs.server import make_server
from ghostjobs.simulate import generate


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    out = tmp_path_factory.mktemp("sim")
    generate(out, days=100, seed=3)
    return out


def test_simulation_is_reproducible(tmp_path):
    generate(tmp_path / "a", days=30, seed=9)
    generate(tmp_path / "b", days=30, seed=9)
    files_a = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*.gz"))
    assert files_a == sorted(p.relative_to(tmp_path / "b") for p in (tmp_path / "b").rglob("*.gz"))
    for rel in files_a[:50]:
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes()


def test_simulated_files_are_utf8_greenhouse_format(sim):
    f = next((sim / "snapshots" / "greenhouse").rglob("*.json.gz"))
    with gzip.open(f, "rb") as fh:
        payload = json.loads(fh.read().decode("utf-8"))
    job = payload["jobs"][0]
    assert {"id", "title", "location", "content", "first_published", "absolute_url"} <= set(job)
    assert "&lt;" in job["content"]   # entity-escaped HTML, like Greenhouse


def test_detector_beats_baselines(sim):
    r = evaluate(sim)
    link = r["linking"]
    assert link["minhash"]["precision"] >= 0.97 and link["minhash"]["recall"] >= 0.97
    assert link["exact"]["recall"] < 0.2
    assert link["title"]["precision"] < link["minhash"]["precision"]
    det = r["detection"]
    assert det["full_score"]["recall"] > det["posting_age"]["recall"] + 0.3
    assert det["full_score"]["precision"] >= 0.85
    assert det["full_score"]["false_alarm_genuine"] <= 0.05


def test_http_api(sim):
    app = App(sim)
    server = make_server(app, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    try:
        with urllib.request.urlopen(base + "/") as res:
            assert b"Ghost Job Detector" in res.read()
        status, overview = call("/api/overview")
        assert status == 200 and overview["simulated"] and overview["companies"]
        board = overview["companies"][0]["board"]
        status, company = call(f"/api/company?board={board}")
        jobs = company["jobs"]
        assert status == 200 and jobs == sorted(jobs, key=lambda j: (-j["score"], j["title"]))
        status, detail = call(f"/api/lineage?id={jobs[0]['lineage_id']}")
        assert status == 200 and detail["postings"] and "reasons" in detail
        assert call("/api/lineage?id=999999")[0] == 404
        assert call("/api/lineage?id=abc")[0] == 400
        assert call("/api/company?board=nope:nope")[0] == 404
        example = call("/api/example")[1]
        status, checked = call("/api/check", {"title": example["title"], "text": example["text"]})
        assert status == 200 and checked["match"] is not None
        assert call("/api/check", {"title": "x", "text": "short"})[0] == 400
    finally:
        server.shutdown()
        server.server_close()

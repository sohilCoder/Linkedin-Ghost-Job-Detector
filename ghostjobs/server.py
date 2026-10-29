"""JSON API + web page on the standard library's http.server.

    GET  /                      web UI
    GET  /api/overview          totals and company leaderboard
    GET  /api/company?board=    open jobs at one company, riskiest first
    GET  /api/lineage?id=       one job's full history and score breakdown
    GET  /api/example           a tracked posting to try in the checker
    POST /api/check             {"title", "text", "board"?} -> score
    POST /api/check-url         {"url"} -> score of a Greenhouse / Lever job
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from .app import App, LinkCheckError
from .links import UnsupportedLink

MAX_BODY = 200_000


def make_handler(app: App):
    page = resources.files("ghostjobs").joinpath("static/index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "GhostJobs/1.0"

        def log_message(self, fmt, *args):  # noqa: N802
            pass

        def _send(self, status, body: bytes, ctype: str):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, data):
            self._send(status, json.dumps(data).encode("utf-8"), "application/json")

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    return self._send(200, page, "text/html; charset=utf-8")
                if url.path == "/api/overview":
                    return self._json(200, app.overview())
                if url.path == "/api/company":
                    return self._json(200, app.company(q.get("board", "")))
                if url.path == "/api/lineage":
                    return self._json(200, app.lineage(int(q.get("id", "-1"))))
                if url.path == "/api/example":
                    return self._json(200, app.example())
            except KeyError:
                return self._json(404, {"error": "not found"})
            except ValueError:
                return self._json(400, {"error": "bad parameter"})
            return self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            if path not in ("/api/check", "/api/check-url"):
                return self._json(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                return self._json(413, {"error": "posting must be under 200 KB"})
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return self._json(400, {"error": "bad request"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "bad request"})
            if path == "/api/check-url":
                try:
                    return self._json(200, app.check_url(str(body.get("url") or "")))
                except UnsupportedLink as exc:
                    return self._json(400, {"error": str(exc)})
                except LinkCheckError as exc:
                    return self._json(exc.status, {"error": str(exc)})
            try:
                text = str(body.get("text") or "").strip()
                if len(text) < 20:
                    return self._json(400, {"error": "paste the full job description"})
                result = app.check(str(body.get("title") or ""), text, str(body.get("board") or ""))
            except AttributeError:
                return self._json(400, {"error": "bad request"})
            return self._json(200, result)

    return Handler


def make_server(app: App, host="127.0.0.1", port=8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(app))


def serve(app: App, host="127.0.0.1", port=8000) -> None:
    server = make_server(app, host, port)
    print(f"Ghost Job Detector running at http://{host}:{server.server_port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

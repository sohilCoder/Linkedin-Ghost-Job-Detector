"""Turn a pasted job link into a request to that board's public feed.

Supported links
    https://boards.greenhouse.io/<company>/jobs/<id>
    https://job-boards.greenhouse.io/<company>/jobs/<id>
    https://boards.greenhouse.io/embed/job_app?for=<company>&token=<id>
    https://jobs.lever.co/<company>/<id>          (also jobs.eu.lever.co, /apply)

Security: the pasted URL is never fetched. We extract the company token and
job id, validate both against strict patterns, and build the official API
URL ourselves on a fixed host. So a malicious link can't make the server
request an arbitrary address (server-side request forgery).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
GREENHOUSE_ID = re.compile(r"^\d{1,20}$")
LEVER_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io",
                    "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}
LEVER_HOSTS = {"jobs.lever.co": "api.lever.co", "jobs.eu.lever.co": "api.eu.lever.co"}

LINKEDIN_MESSAGE = (
    "LinkedIn doesn't allow automated access to its pages, so this app doesn't read LinkedIn links. "
    "Paste the job description instead, or click \"Apply on company website\" on LinkedIn and "
    "paste that link if it goes to a Greenhouse or Lever page."
)


class UnsupportedLink(ValueError):
    """The link can't be checked; the message says why and what to do."""


@dataclass(frozen=True)
class JobLink:
    source: str          # "greenhouse" | "lever"
    token: str           # company's board token
    job_id: str
    api_url: str

    @property
    def board(self) -> str:
        return f"{self.source}:{self.token}"


def parse_job_link(raw: str) -> JobLink:
    url = (raw or "").strip()
    if not url:
        raise UnsupportedLink("Paste a job link first.")
    if "://" not in url:
        url = "https://" + url
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    segments = [s for s in parts.path.split("/") if s]
    query = parse_qs(parts.query)

    if host == "linkedin.com" or host.endswith(".linkedin.com") or host == "lnkd.in":
        raise UnsupportedLink(LINKEDIN_MESSAGE)

    if host in GREENHOUSE_HOSTS:
        if segments[:2] == ["embed", "job_app"]:
            token, job_id = query.get("for", [""])[0], query.get("token", [""])[0]
        elif len(segments) >= 3 and segments[1] == "jobs":
            token, job_id = segments[0], segments[2]
        else:
            raise UnsupportedLink("That looks like a Greenhouse board page, not a single job. "
                                  "Open a specific job and paste its link.")
        if not TOKEN.match(token) or not GREENHOUSE_ID.match(job_id):
            raise UnsupportedLink("That Greenhouse link doesn't have a valid company and job id.")
        return JobLink("greenhouse", token, job_id,
                       f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?pay_transparency=true")

    if host in LEVER_HOSTS:
        if len(segments) < 2:
            raise UnsupportedLink("That looks like a Lever board page, not a single job. "
                                  "Open a specific job and paste its link.")
        token, job_id = segments[0], segments[1]
        if not TOKEN.match(token) or not LEVER_ID.match(job_id):
            raise UnsupportedLink("That Lever link doesn't have a valid company and job id.")
        return JobLink("lever", token, job_id.lower(),
                       f"https://{LEVER_HOSTS[host]}/v0/postings/{token}/{job_id.lower()}?mode=json")

    if "gh_jid" in query:
        raise UnsupportedLink(
            "This company hosts its Greenhouse jobs on its own site, so the link doesn't say which "
            "board to ask. Paste the job description instead.")
    raise UnsupportedLink(
        "Only Greenhouse (boards.greenhouse.io) and Lever (jobs.lever.co) job links are supported. "
        "For other sites, paste the job description instead.")

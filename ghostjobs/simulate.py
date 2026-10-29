"""Simulate months of daily job board snapshots, with ground truth.

Output is byte-for-byte the layout the real collector writes (gzipped
Greenhouse-format JSON, one file per board per day), so ingestion, matching
and scoring run on exactly the same code path as real data.

Every role has a hidden type:
  genuine        open 2-8 weeks, then closes for good
  ghost          stays up indefinitely; most are closed and reposted every
                 3-6 weeks with small edits (new id, fresh "posted" date),
                 some just sit there for months
  legit_repost   a real role that didn't fill: closed, then reposted once or
                 twice, then filled (the tricky case: looks ghost-ish)
  bulk           one real role posted in several cities at once (also tricky)

To keep the test honest, the cheap signals overlap between classes: some
genuine roles have wide salary ranges or buzzwords, and many ghosts look
perfectly concrete, so text alone can't separate them.

Companies are fictional. Real companies are never labeled as posting ghost
jobs, even in a demo.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import html
import json
import random
import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

AS_OF = date(2026, 10, 1)

COMPANIES = [
    ("Pinecrest Data", "pinecrestdata"), ("Lanternfish AI", "lanternfishai"),
    ("Mapleforge", "mapleforge"), ("Quillstone Software", "quillstone"),
    ("Brightwater Bio", "brightwaterbio"), ("Cobaltline Systems", "cobaltline"),
    ("Fernhollow Games", "fernhollow"), ("Gridwise Energy", "gridwise"),
    ("Kettlebrook Health", "kettlebrook"), ("Oakmoss Retail", "oakmoss"),
    ("Saltmarsh Logistics", "saltmarsh"), ("Thimbleworks", "thimbleworks"),
    ("Zephyrine Labs", "zephyrine"), ("Orbitly", "orbitly"),
    ("Driftwood Insurance", "driftwoodins"), ("Juniperline Pay", "juniperline"),
    ("Hollowbrook Media", "hollowbrook"), ("Vantablue Robotics", "vantablue"),
]

MISSIONS = [
    "helps small businesses understand their customers", "builds tools that make clinical trials faster",
    "is modernizing how freight moves across North America", "makes payroll simple for growing teams",
    "builds robots that sort recycling", "helps families plan their finances",
    "makes multiplayer games for mobile", "brings clean energy data to utilities",
    "powers checkout for independent retailers", "helps insurers price risk fairly",
]

LOCATIONS = ["New York, NY", "San Francisco, CA", "Chicago, IL", "Austin, TX", "Seattle, WA",
             "Remote - US", "Denver, CO", "Boston, MA", "Atlanta, GA"]

FAMILIES = {
    "software": {
        "titles": ["Software Engineer", "Senior Software Engineer", "Backend Engineer", "Software Engineer II"],
        "teams": ["Payments", "Platform", "Search", "Growth", "Infrastructure", "Mobile", "Data Platform"],
        "salary": (120_000, 185_000),
        "duties": [
            "Design, build and operate services in Python and Go that handle millions of requests a day",
            "Own features end to end, from design doc to production rollout",
            "Improve the reliability of our Kubernetes-based deployment pipeline",
            "Partner with product and design to scope new features",
            "Write well-tested code and review pull requests from teammates",
            "Join a shared on-call rotation for the services your team owns",
            "Profile and optimize slow database queries in Postgres",
            "Build internal tooling that makes other engineers faster",
            "Migrate legacy endpoints to our new event-driven architecture with Kafka",
            "Mentor newer engineers through pairing and code review",
            "Instrument services with metrics and alerts so problems surface early",
            "Contribute to architecture decisions for the {team} team roadmap",
            "Ship improvements to our public API used by thousands of customers",
            "Reduce infrastructure costs on AWS without hurting latency",
        ],
        "reqs": [
            "3+ years of professional software engineering experience",
            "Strong skills in at least one of Python, Go, Java or TypeScript",
            "Experience with SQL databases such as Postgres or MySQL",
            "Familiarity with cloud platforms such as AWS or GCP",
            "Clear written communication and a habit of documenting decisions",
            "Experience operating production systems and debugging incidents",
            "A degree in computer science or equivalent practical experience",
            "Comfort working in a codebase with automated tests and CI",
        ],
    },
    "data": {
        "titles": ["Data Analyst", "Senior Data Analyst", "Data Scientist", "Analytics Engineer"],
        "teams": ["Growth", "Finance", "Marketing", "Product Analytics", "Operations"],
        "salary": (85_000, 150_000),
        "duties": [
            "Build dashboards in Tableau that leadership uses every week",
            "Write SQL to answer questions about customer behavior",
            "Design and analyze A/B tests with product managers",
            "Model the drivers of retention and present findings to the {team} team",
            "Maintain dbt models that power company-wide reporting",
            "Partner with engineering to improve event tracking quality",
            "Forecast demand for the next two quarters",
            "Automate recurring reports with Python",
            "Define and document core business metrics",
            "Investigate anomalies in revenue data and explain them",
            "Build pricing experiments and measure their impact",
            "Turn ambiguous questions into clear analyses with recommendations",
        ],
        "reqs": [
            "2+ years of experience in analytics or data science",
            "Fluency in SQL and comfort with Python or R",
            "Experience with experimentation and statistics",
            "Experience with a BI tool such as Tableau or Looker",
            "Ability to explain technical results to non-technical audiences",
            "Familiarity with dbt or similar data modeling tools",
        ],
    },
    "product": {
        "titles": ["Product Manager", "Senior Product Manager", "Associate Product Manager"],
        "teams": ["Onboarding", "Billing", "Mobile", "Integrations", "Core Experience"],
        "salary": (110_000, 175_000),
        "duties": [
            "Own the roadmap for the {team} area and set quarterly OKRs",
            "Talk to customers every week to understand their problems",
            "Write clear specs and work closely with 6 engineers and a designer",
            "Define success metrics and track them after launch",
            "Prioritize ruthlessly across competing requests",
            "Run discovery for new opportunities with design and research",
            "Coordinate launches with marketing, sales and support",
            "Analyze funnel data to find the biggest drop-off points",
            "Present product strategy to leadership",
            "Work with sales to understand why deals are won and lost",
        ],
        "reqs": [
            "3+ years of product management experience",
            "Experience shipping B2B or consumer software products",
            "Comfort with data: you can write basic SQL or work closely with analysts",
            "Excellent written and verbal communication",
            "A track record of working well with engineers and designers",
        ],
    },
    "design": {
        "titles": ["Product Designer", "Senior Product Designer", "UX Researcher"],
        "teams": ["Core Experience", "Mobile", "Design Systems", "Growth"],
        "salary": (100_000, 165_000),
        "duties": [
            "Design end-to-end flows in Figma from early sketches to final specs",
            "Run usability tests and turn findings into improvements",
            "Contribute components to our design system",
            "Partner with product managers and engineers on the {team} team",
            "Present design work and rationale in critiques",
            "Prototype interactions to test ideas quickly",
            "Plan and run interviews with 8 to 10 customers per study",
            "Improve accessibility across the product",
        ],
        "reqs": [
            "3+ years of product design or research experience",
            "A portfolio showing your process and shipped work",
            "Strong skills in Figma",
            "Experience working closely with engineering teams",
            "Familiarity with accessibility standards",
        ],
    },
    "gtm": {
        "titles": ["Account Executive", "Customer Success Manager", "Sales Development Representative"],
        "teams": ["Mid-Market", "Enterprise", "SMB", "Partnerships"],
        "salary": (65_000, 130_000),
        "duties": [
            "Manage a book of 40 customers and help them get value from the product",
            "Run discovery calls and product demos",
            "Build pipeline through outbound prospecting",
            "Negotiate and close new contracts in the {team} segment",
            "Partner with product to share customer feedback",
            "Keep the CRM up to date and forecast accurately",
            "Lead quarterly business reviews with key accounts",
            "Identify expansion opportunities within existing accounts",
        ],
        "reqs": [
            "2+ years of experience in sales or customer success",
            "Experience with Salesforce or a similar CRM",
            "Strong presentation and negotiation skills",
            "Experience selling software to businesses",
        ],
    },
}

VAGUE_DUTIES = [
    "Contribute to various projects across the business",
    "Collaborate with stakeholders in a fast-paced, dynamic environment",
    "Wear many hats and take on other duties as assigned",
    "Help drive results-driven initiatives across teams",
    "Support the team with whatever is needed to succeed",
]
BUZZ_SENTENCES = [
    "We move fast and expect you to hit the ground running.",
    "You are a self-starter who can thrive in ambiguity.",
    "We are looking for passionate rockstars who love a fast-paced environment.",
    "This is a world-class team doing game-changing work.",
]
PIPELINE_SENTENCES = [
    "We are always looking for talented people, and this posting helps us build our talent pipeline for future opportunities.",
    "This posting may not be tied to a specific opening; we review applications as roles become available.",
    "Join our talent community and we will reach out about upcoming opportunities.",
]
BENEFITS = [
    "Benefits include medical, dental and vision coverage, a 401(k) match, and 20 days of paid time off.",
    "We offer comprehensive health coverage, paid parental leave, and a yearly learning budget.",
    "Perks include flexible hours, a home office stipend, and company-wide recharge weeks.",
]

# Small rewordings a recruiter makes when reposting a job.
SYNONYMS = {
    "build": "develop", "Build": "Develop", "help": "assist", "customers": "users",
    "improve": "enhance", "Improve": "Enhance", "with": "alongside", "clear": "concise",
    "Own": "Lead", "experience": "background", "Partner": "Collaborate", "new": "upcoming",
    "every": "each", "work": "collaborate", "strong": "solid", "Strong": "Solid",
    "team": "group", "quickly": "rapidly", "find": "identify",
}

TYPE_LEGIT, TYPE_GHOST, TYPE_GENUINE, TYPE_BULK = "legit_repost", "ghost", "genuine", "bulk"


@dataclass
class Company:
    name: str
    token: str
    mission: str
    ghost_rate: float
    n_roles: int


@dataclass
class Role:
    uid: str
    company: Company
    kind: str
    family: str
    title: str
    team: str
    duties: list[str]
    reqs: list[str]
    salary: tuple[int, int] | None
    extras: list[str]
    benefit: str
    locations: list[str]
    postings: list["SimPosting"] = field(default_factory=list)


@dataclass
class SimPosting:
    external_id: int
    role: Role
    location: str
    title: str
    content_html: str
    published: date
    start_day: int
    end_day: int            # exclusive


def _render(role: Role, title: str, location: str, duties: list[str], salary, rng) -> str:
    c = role.company
    blocks = [
        f"<h2>About {c.name}</h2>",
        f"<p>{c.name} {c.mission}. We are a team of builders who care about our customers and each other.</p>",
        "<h2>About the role</h2>",
        f"<p>Location: {html.escape(location)}. As a {html.escape(title)} on the {role.team} team, you will:</p>",
        "<ul>" + "".join(f"<li>{html.escape(d.format(team=role.team))}</li>" for d in duties) + "</ul>",
    ]
    blocks += [f"<p>{html.escape(s)}</p>" for s in role.extras]
    blocks.append("<h2>What we're looking for</h2>")
    blocks.append("<ul>" + "".join(f"<li>{html.escape(r)}</li>" for r in role.reqs) + "</ul>")
    if salary:
        blocks.append(f"<h2>Compensation</h2><p>The base salary range for this role is "
                      f"${salary[0]:,} - ${salary[1]:,}.</p>")
    blocks.append(f"<p>{role.benefit}</p>")
    return html.escape("".join(blocks))   # Greenhouse sends entity-escaped HTML


def _edit(role: Role, title: str, duties: list[str], salary, rng: random.Random):
    """A repost: same job, lightly reworded."""
    duties = list(duties)
    if rng.random() < 0.6:
        rng.shuffle(duties)
    if rng.random() < 0.3 and len(duties) > 3:
        duties.pop(rng.randrange(len(duties)))
    if rng.random() < 0.3:
        pool = [d for d in FAMILIES[role.family]["duties"] if d not in duties]
        if pool:
            duties.insert(rng.randrange(len(duties) + 1), rng.choice(pool))
    swaps = rng.sample(sorted(SYNONYMS), k=rng.randint(2, 5))
    duties = [" ".join(SYNONYMS[w] if w in swaps and w in SYNONYMS else w for w in d.split()) for d in duties]
    if salary and rng.random() < 0.3:
        bump = rng.choice([-5000, 5000, 10000])
        salary = (salary[0] + bump, salary[1] + bump)
    if rng.random() < 0.15:
        title = title.replace("Senior", "Sr.") if "Senior" in title else \
            title if "," in title else f"{title}, {role.team}"
    return title, duties, salary


def _make_role(uid: str, company: Company, kind: str, rng: random.Random) -> Role:
    family = rng.choice(list(FAMILIES))
    spec = FAMILIES[family]
    title = rng.choice(spec["titles"])
    team = rng.choice(spec["teams"])
    duties = rng.sample(spec["duties"], k=rng.randint(5, 7))
    reqs = rng.sample(spec["reqs"], k=min(len(spec["reqs"]), rng.randint(3, 5)))
    lo, hi = spec["salary"]
    base = rng.randrange(lo, hi, 5000)
    ratio = rng.uniform(1.15, 1.45)
    extras: list[str] = []
    salary: tuple[int, int] | None = (base, int(round(base * ratio, -3)))

    if kind == TYPE_GHOST:
        # Ghost text is a mix: some vague, many perfectly concrete.
        if rng.random() < 0.35:
            extras.append(rng.choice(PIPELINE_SENTENCES))
        if rng.random() < 0.40:
            extras += rng.sample(BUZZ_SENTENCES, k=rng.randint(1, 2))
            duties = duties[:3] + rng.sample(VAGUE_DUTIES, k=2)
        if rng.random() < 0.45:
            salary = (base, int(round(base * rng.uniform(1.8, 2.8), -3)))
        elif rng.random() < 0.3:
            salary = None
    else:
        # Noise so text signals are not a giveaway for real jobs either.
        if rng.random() < 0.12:
            extras.append(rng.choice(BUZZ_SENTENCES))
        if rng.random() < 0.08:
            salary = (base, int(round(base * rng.uniform(1.8, 2.3), -3)))
        elif rng.random() < 0.10:
            salary = None
    locations = rng.sample(LOCATIONS, k=rng.randint(3, 6)) if kind == TYPE_BULK else [rng.choice(LOCATIONS)]
    return Role(uid, company, kind, family, title, team, duties, reqs, salary, extras,
                rng.choice(BENEFITS), locations)


def _schedule(role: Role, days: int, rng: random.Random, next_id) -> None:
    """Lay out when each posting of a role is live, in day indexes relative to
    the first tracked day (negative = before tracking started)."""

    def add(start: int, length: int, location: str, title: str, duties, salary):
        p = SimPosting(next_id(), role, location, title, _render(role, title, location, duties, salary, rng),
                       AS_OF - timedelta(days=days - 1 - start), start, start + length)
        role.postings.append(p)

    title, duties, salary = role.title, role.duties, role.salary
    loc = role.locations[0]
    if role.kind == TYPE_GENUINE:
        add(rng.randint(-45, max(-44, days - 5)), rng.randint(14, 55), loc, title, duties, salary)
    elif role.kind == TYPE_BULK:
        start = rng.randint(-20, max(-19, days - 10))
        for location in role.locations:
            add(start + rng.randint(0, 3), rng.randint(20, 50), location, title, duties, salary)
    elif role.kind == TYPE_LEGIT:
        start = rng.randint(-20, max(-19, days - 70))
        n_reposts = 2 if rng.random() < 0.25 else 1
        for i in range(n_reposts + 1):
            length = rng.randint(20, 40)
            add(start, length, loc, title, duties, salary)
            start += length + rng.randint(2, 20)
            title, duties, salary = _edit(role, title, duties, salary, rng)
    else:  # ghost
        if rng.random() < 0.2:
            # never reposted: just sits there for months
            add(rng.randint(-220, -60), days + 400, loc, title, duties, salary)
        else:
            start = rng.randint(-150, max(-149, days - 45))
            while start < days:
                length = rng.randint(21, 45)
                add(start, length, loc, title, duties, salary)
                start += length + rng.randint(-2, 4)   # sometimes overlaps the old one
                title, duties, salary = _edit(role, title, duties, salary, rng)


def generate(out_dir: str | Path, *, days: int = 120, seed: int = 42, outage_rate: float = 0.02) -> dict:
    rng = random.Random(seed)
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    counter = iter(range(4_100_000 + rng.randrange(100_000), 10**9, 7))
    next_id = lambda: next(counter)  # noqa: E731

    companies = []
    for name, token in COMPANIES:
        ghost_rate = rng.choice([0.02, 0.05, 0.08, 0.12, 0.2, 0.3])
        companies.append(Company(name, token, rng.choice(MISSIONS), ghost_rate, rng.randint(18, 40)))

    roles: list[Role] = []
    for c in companies:
        for i in range(c.n_roles):
            r = rng.random()
            if r < c.ghost_rate:
                kind = TYPE_GHOST
            elif r < c.ghost_rate + 0.12:
                kind = TYPE_LEGIT
            elif r < c.ghost_rate + 0.20:
                kind = TYPE_BULK
            else:
                kind = TYPE_GENUINE
            role = _make_role(f"{c.token}-{i:03d}", c, kind, rng)
            _schedule(role, days, rng, next_id)
            roles.append(role)

    first_day = AS_OF - timedelta(days=days - 1)
    snap_root = out / "snapshots" / "greenhouse"
    by_company: dict[str, list[SimPosting]] = {}
    for role in roles:
        by_company.setdefault(role.company.token, []).extend(role.postings)

    for c in companies:
        postings = by_company.get(c.token, [])
        folder = snap_root / c.token
        folder.mkdir(parents=True)
        for d in range(days):
            if d < days - 1 and rng.random() < outage_rate:
                continue   # the collector failed that day
            day = first_day + timedelta(days=d)
            jobs = [
                {
                    "id": p.external_id,
                    "internal_job_id": int(hashlib.sha1(p.role.uid.encode()).hexdigest()[:7], 16),
                    "title": p.title,
                    "updated_at": f"{p.published.isoformat()}T09:00:00-05:00",
                    "first_published": f"{p.published.isoformat()}T09:00:00-05:00",
                    "requisition_id": None,
                    "location": {"name": p.location},
                    "absolute_url": f"https://boards.greenhouse.io/{c.token}/jobs/{p.external_id}",
                    "content": p.content_html,
                    "metadata": None,
                }
                for p in postings if p.start_day <= d < p.end_day
            ]
            payload = {"jobs": jobs, "meta": {"total": len(jobs)}}
            # mtime=0 keeps the files byte-identical for the same seed
            with open(folder / f"{day.isoformat()}.json.gz", "wb") as raw, \
                    gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                gz.write(json.dumps(payload).encode("utf-8"))

    (out / "boards.json").write_text(
        json.dumps({f"greenhouse:{c.token}": c.name for c in companies}, indent=2), encoding="utf-8")

    with open(out / "ground_truth.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["board", "external_id", "role_uid", "lineage_uid", "type"])
        for role in roles:
            for p in role.postings:
                if p.end_day <= 0 or p.start_day >= days:
                    continue   # never visible while tracking
                lineage = f"{role.uid}@{p.location}" if role.kind == TYPE_BULK else role.uid
                w.writerow([f"greenhouse:{role.company.token}", p.external_id, role.uid, lineage, role.kind])

    counts: dict[str, int] = {}
    for role in roles:
        counts[role.kind] = counts.get(role.kind, 0) + 1
    manifest = {
        "simulated": True,
        "seed": seed,
        "days": days,
        "first_day": first_day.isoformat(),
        "as_of": AS_OF.isoformat(),
        "companies": len(companies),
        "roles_by_type": counts,
        "postings": sum(len(r.postings) for r in roles),
        "note": "All companies are fictional.",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest

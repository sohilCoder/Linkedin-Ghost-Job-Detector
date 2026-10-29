"""Command line: python -m ghostjobs <command>

    simulate   write months of fake job board snapshots
    collect    fetch today's snapshot of real boards (Greenhouse / Lever)
    report     list the riskiest open jobs
    check      score a job description from a text file
    eval       measure accuracy on simulated data
    serve      run the web app
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ghostjobs", description="Ghost Job Detector")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("simulate", help="generate simulated snapshots")
    p.add_argument("--out", default="data/demo")
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--seed", type=int, default=42)

    p = sub.add_parser("collect", help="fetch today's snapshot of real boards")
    p.add_argument("--boards", default="boards.txt")
    p.add_argument("--data", default="data/real")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")

    p = sub.add_parser("report", help="list the riskiest open jobs")
    p.add_argument("--data", default="data/demo")
    p.add_argument("--limit", type=int, default=10)

    p = sub.add_parser("check", help="score a Greenhouse/Lever job link, or a description saved as a text file")
    p.add_argument("target", help="job link or path to a text file")
    p.add_argument("--title", default="")
    p.add_argument("--data", default="data/demo")

    p = sub.add_parser("eval", help="measure accuracy against ground truth")
    p.add_argument("--data", default="data/demo")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--data", default="data/demo")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    if args.command == "simulate":
        from .simulate import generate
        m = generate(args.out, days=args.days, seed=args.seed)
        kinds = ", ".join(f"{v} {k}" for k, v in sorted(m["roles_by_type"].items()))
        print(f"Wrote {m['days']} days of snapshots for {m['companies']} fictional companies to {args.out}")
        print(f"Roles: {kinds}")
        return 0

    if args.command == "collect":
        from .collect import collect, read_boards
        if not Path(args.boards).exists():
            print(f"No boards file at {args.boards}. Copy boards.example.txt to boards.txt and add boards.")
            return 1
        report = collect(read_boards(args.boards), args.data, delay=args.delay)
        print(f"{report['day']}: saved {len(report['saved'])}, already had {len(report['skipped'])}, "
              f"failed {len(report['failed'])}")
        for board, err in report["failed"].items():
            print(f"  {board}: {err}")
        return 0 if not report["failed"] else 2

    if args.command == "eval":
        from .evaluate import evaluate, format_report
        r = evaluate(args.data)
        print(json.dumps(r, indent=2) if args.json else format_report(r))
        return 0

    from .app import App
    if not Path(args.data).exists():
        print(f"No data at {args.data}. Run `python -m ghostjobs simulate` first.")
        return 1
    app = App(args.data)

    if args.command == "report":
        a = app.analysis
        print(f"{a.stats['open_lineages']} open jobs across {len(a.board_days)} companies, as of {a.as_of}\n")
        rows = sorted((l for l in a.lineages if l.is_open), key=lambda l: -app.scores[l.id].points)
        for lin in rows[:args.limit]:
            s = app.scores[lin.id]
            print(f"{s.points:>3}  {s.risk.upper():6}  {lin.latest.title}, {app.name(lin.board)} ({lin.latest.location})")
            for r in s.reasons:
                print(f"           +{r.points:<3} {r.text}")
        return 0

    if args.command == "check":
        if args.target.startswith(("http://", "https://")) or "greenhouse.io" in args.target \
                or "lever.co" in args.target or "linkedin.com" in args.target:
            from .app import LinkCheckError
            from .links import UnsupportedLink
            try:
                r = app.check_url(args.target)
            except (UnsupportedLink, LinkCheckError) as exc:
                print(exc)
                return 1
            job = r["job"]
            print(f"{job['title']} · {job['company']} · {job['location']}")
            print("(from tracked history)" if r["source"] == "tracked" else "(fetched live from the job board)")
        else:
            text = Path(args.target).read_text(encoding="utf-8", errors="replace")
            r = app.check(args.title, text)
        print(f"Ghost risk: {r['score']}/100 ({r['risk']})")
        for reason in r["reasons"]:
            print(f"  +{reason['points']:<3} {reason['text']}")
        if r.get("note"):
            print(f"\n{r['note']}")
        return 0

    if args.command == "serve":
        from .server import serve
        serve(app, args.host, args.port)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

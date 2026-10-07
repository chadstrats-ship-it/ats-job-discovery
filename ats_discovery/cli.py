"""Command line interface: python -m ats_discovery --company stripe --ats greenhouse"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import __version__
from .fetchers import fetch_jobs
from .models import ATS_NAMES, Job
from .scoring import DEFAULT_CONFIG_PATH, ConfigError, ScoredJob, evaluate_all, load_config, sort_scored
from .transport import FetchError, FixtureHttp, UrllibHttp


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ats_discovery",
        description="Fetch jobs from a public ATS board, filter them and print them ranked by score.",
    )
    src = p.add_argument_group("what to fetch")
    src.add_argument("--company", metavar="SLUG", help="board slug, e.g. stripe (the id in the company's careers URL)")
    src.add_argument("--ats", choices=ATS_NAMES, help="which ATS hosts the board")
    src.add_argument("--name", help="display name for --company (default: the name the API reports, else the slug)")
    src.add_argument("--all-companies", metavar="FILE", help="YAML list of boards (see companies.example.yaml); --ats narrows it")
    src.add_argument("--fixture", metavar="PATH", help="read a saved API payload from PATH instead of the network (needs --company/--ats)")
    out = p.add_argument_group("scoring and output")
    out.add_argument("--config", metavar="YAML", help="scoring YAML (default: the packaged scoring.example.yaml)")
    out.add_argument("--limit", type=int, metavar="N", help="print at most N jobs")
    out.add_argument("--min-score", type=float, default=0, metavar="S", help="hide jobs scoring below S")
    out.add_argument("--json", action="store_true", help="emit JSON (with the score breakdown) instead of a table")
    out.add_argument("--no-filters", action="store_true", help="score everything; skip the remote/US/degree/clearance/age rules")
    out.add_argument("--as-of", metavar="ISO_DATE", help="evaluate age and recency as of this date (reproducible output)")
    out.add_argument("--print-config", action="store_true", help="print the packaged example scoring YAML and exit")
    net = p.add_argument_group("network politeness")
    net.add_argument("--delay", type=float, default=0.5, metavar="SEC", help="minimum seconds between HTTP requests (default 0.5)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _load_companies(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    rows = data.get("companies") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("companies file must contain a 'companies:' list")
    for r in rows:
        if not isinstance(r, dict) or r.get("ats") not in ATS_NAMES or not r.get("slug"):
            raise ValueError(f"bad companies entry (needs ats + slug): {r!r}")
    return rows


def _parse_as_of(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 3] + "..."


def format_table(rows: list[ScoredJob]) -> str:
    """Fixed-width table: score, company, title, location, url."""
    headers = ("SCORE", "COMPANY", "TITLE", "LOCATION", "URL")
    body = [
        (str(r.score), _clip(r.job.company, 18), _clip(r.job.title, 52), _clip(r.job.location, 34), r.job.url) for r in rows
    ]
    widths = [max(len(h), *(len(b[i]) for b in body)) if body else len(h) for i, h in enumerate(headers[:-1])]
    line = lambda cells: "  ".join(c.ljust(w) if i < 4 else c for i, (c, w) in enumerate(zip(cells, widths + [0])))  # noqa: E731
    return "\n".join([line(headers), line(tuple("-" * w for w in widths) + ("---",))] + [line(b) for b in body])


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)

    if args.print_config:
        sys.stdout.write(Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
        return 0
    if args.all_companies and (args.company or args.fixture):
        print("error: --all-companies cannot be combined with --company or --fixture", file=sys.stderr)
        return 2
    if not args.all_companies and not (args.company and args.ats):
        print("error: give --company SLUG --ats ATS, or --all-companies FILE", file=sys.stderr)
        return 2
    if args.fixture and not (args.company and args.ats):
        print("error: --fixture needs --company and --ats (they label the jobs)", file=sys.stderr)
        return 2

    try:
        cfg = load_config(args.config)
        now = _parse_as_of(args.as_of)
    except (OSError, ConfigError, yaml.YAMLError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    jobs: list[Job] = []
    failures = 0
    try:
        if args.all_companies:
            boards = [b for b in _load_companies(args.all_companies) if not args.ats or b["ats"] == args.ats]
        else:
            boards = [{"ats": args.ats, "slug": args.company, "name": args.name}]
        http = FixtureHttp.from_file(args.fixture) if args.fixture else UrllibHttp(min_interval=args.delay)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for b in boards:
        try:
            got = fetch_jobs(b["ats"], str(b["slug"]), http, company=b.get("name"))
            print(f"fetched {len(got):>4} postings from {b['ats']}/{b['slug']}", file=sys.stderr)
            jobs += got
        except FetchError as e:
            failures += 1
            print(f"warning: {b['ats']}/{b['slug']}: {e}", file=sys.stderr)
    if failures and failures == len(boards):
        return 2

    scored = evaluate_all(jobs, cfg, apply_filters=not args.no_filters, now=now)
    ranked = [s for s in sort_scored(scored) if s.score >= args.min_score]
    dropped = sum(1 for s in scored if s.skip_reason)
    shown = ranked[: args.limit] if args.limit else ranked
    print(f"{len(jobs)} fetched, {dropped} filtered out, {len(ranked)} scored >= {args.min_score:g}, showing {len(shown)}", file=sys.stderr)

    if args.json:
        json.dump([s.to_dict() for s in shown], sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(format_table(shown))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

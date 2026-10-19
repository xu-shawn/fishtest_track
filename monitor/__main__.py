"""Command line: python -m monitor {collect,charts,readme}."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from .charts import render_all
from .collect import backfill_finished, collect
from .readme import build_readme
from .store import load


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m monitor", description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data"), help="history directory")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("collect", help="take one snapshot and update the history")

    backfill = commands.add_parser("backfill", help="fetch older finished tests (one-off)")
    backfill.add_argument("--days", type=int, default=365)

    charts = commands.add_parser("charts", help="draw the SVG charts")
    charts.add_argument("--out", type=Path, default=Path("build/charts"))

    readme = commands.add_parser("readme", help="rewrite README.md from the history")
    readme.add_argument("--readme", type=Path, default=Path("README.md"))
    readme.add_argument("--charts-url", required=True,
                        help="where the chart SVGs are served from (URL or relative path)")

    args = parser.parse_args()
    now = datetime.now(UTC).replace(microsecond=0)

    if args.command == "collect":
        collect(args.data)
        return
    if args.command == "backfill":
        print(f"{backfill_finished(args.data, args.days)} finished tests added")
        return

    history = load(args.data)
    if args.command == "charts":
        args.out.mkdir(parents=True, exist_ok=True)
        for name, svg in render_all(history, now).items():
            (args.out / name).write_text(svg)
        print(f"charts written to {args.out}")
    elif args.command == "readme":
        text = build_readme(history, now, args.charts_url.rstrip("/"))
        args.readme.write_text(text)
        print(f"{args.readme} written")


if __name__ == "__main__":
    main()

"""Build the app's reference cache from Statcast.

  python -m pitchportal_ingest.pipeline                 # both, default window
  python -m pitchportal_ingest.pipeline --metrics-only
  python -m pitchportal_ingest.pipeline --start 2024-04-01 --end 2024-09-30
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import config
from .arsenals import build_arsenals
from .league_metrics import build_metrics


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main() -> None:
    ap = argparse.ArgumentParser(description="Pull Statcast via pybaseball and cache app reference JSON.")
    ap.add_argument("--start", default=config.DEFAULT_START)
    ap.add_argument("--end", default=config.DEFAULT_END)
    ap.add_argument("--out", default=str(config.CACHE_DIR), help="cache output dir")
    ap.add_argument("--metrics-only", action="store_true")
    ap.add_argument("--arsenals-only", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)

    if not args.arsenals_only:
        print(f"Building league metrics {args.start}..{args.end}")
        metrics = build_metrics(args.start, args.end)
        write_json(out / "metrics.json", metrics)
        print(f"  → wrote {out / 'metrics.json'} ({len(metrics)} pitch types)")

    if not args.metrics_only:
        print("Building pro arsenals")
        arsenals = build_arsenals(args.start, args.end)
        write_json(out / "arsenals.json", arsenals)
        print(f"  → wrote {out / 'arsenals.json'} ({len(arsenals)} pitchers)")

    print("Done. Start the server with: uvicorn pitchportal_ingest.server:app --host 0.0.0.0 --port 8000")


if __name__ == "__main__":
    main()

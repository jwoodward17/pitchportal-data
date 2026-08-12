"""Build the league reference table (METRICS) from a Statcast pull.

Output shape (per app pitch key):
  { "velo": {avg,lo,hi}, "ivb": {avg,lo,hi}, "hb": {avg,lo,hi}, "spin": {avg,lo,hi} }
lo/hi are the 10th/90th percentiles (the "normal range" the app draws).
"""

from __future__ import annotations

import pandas as pd

from . import config


def _stat(series: pd.Series) -> dict:
    s = series.dropna()
    return {
        "avg": int(round(s.mean())),
        "lo": int(round(s.quantile(0.10))),
        "hi": int(round(s.quantile(0.90))),
    }


def build_metrics(start: str = config.DEFAULT_START, end: str = config.DEFAULT_END) -> dict:
    from pybaseball import statcast

    print(f"  pulling statcast {start}..{end} (this can take a few minutes)...")
    df = statcast(start_dt=start, end_dt=end)
    if df is None or df.empty:
        raise RuntimeError("statcast() returned no rows for that window")

    df = df[df["pitch_type"].notna()].copy()
    df["appkey"] = df["pitch_type"].map(config.PITCH_TYPE_MAP)
    df = df.dropna(subset=["appkey", "release_speed", "pfx_x", "pfx_z"])

    # §6 mapping: pfx_* are feet from the catcher's view -> inches.
    df["ivb"] = df["pfx_z"] * 12.0  # signed (ride +, drop −)
    df["hb"] = df["pfx_x"].abs() * 12.0  # positive magnitude (app convention)

    out: dict = {}
    for key in config.APP_PITCH_KEYS:
        sub = df[df["appkey"] == key]
        if sub.empty:
            continue
        out[key] = {
            "velo": _stat(sub["release_speed"]),
            "ivb": _stat(sub["ivb"]),
            "hb": _stat(sub["hb"]),
            "spin": _stat(sub["release_spin_rate"]),
        }
    return out

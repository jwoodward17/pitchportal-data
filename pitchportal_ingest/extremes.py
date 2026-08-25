"""Single-pitch extremes — the fastest pitch of the year, the most ride on one
four-seam, the most depth on one curveball, the most sweep on one slider.

── WHY THIS IS INCREMENTAL ───────────────────────────────────────────────────
Savant's pitch-level export has no server-side filters beyond date, and a full
season is ~430 MB across ~150 requests. Pulling that every six hours is rude to
Savant and slow on a CI runner. So this keeps its own state file
(cache/extremes.json), pulls only the days since the last run, and merges. The
first run of a season (file missing or `year` differs) backfills from
mid-March — a one-time ~15-minute job. After that each run is a few MB.

── WHAT COUNTS ──────────────────────────────────────────────────────────────
Regular season only (hfGT=R), and a pitch must be a real pitch type in
PITCH_TYPES. Movement uses Savant's pfx_x / pfx_z (feet → inches), which are
already gravity-removed: pfx_z IS induced vertical break. Horizontal break is
reported as magnitude so a lefty's run and a righty's run rank on one board.
"""

from __future__ import annotations

import csv
import io
import json
import urllib.request
from datetime import date, datetime, timedelta, timezone

from . import config

UA = "PitchPortal/1.0 (+extremes ingest)"
SEARCH = (
    "https://baseballsavant.mlb.com/statcast_search/csv?all=true&hfPT=&hfSea={year}%7C"
    "&hfGT=R%7C&player_type=pitcher&game_date_gt={day}&game_date_lt={day}"
    "&min_pitches=0&type=details"
)

PITCH_TYPES = {
    "FF": "fourseam", "SI": "sinker", "FC": "cutter", "SL": "slider",
    "ST": "sweeper", "CU": "curve", "KC": "curve", "CH": "change", "FS": "split",
}
PITCH_KEYS = list(dict.fromkeys(PITCH_TYPES.values()))

# Board definitions: key -> (source field, transform, higher-is-better, which pitch
# keys it applies to). "ivb" reads pfx_z*12; "hb" reads |pfx_x|*12.
BOARDS = {
    "velo":  ("release_speed",     lambda v: v,            True,  PITCH_KEYS),
    "ride":  ("pfx_z",             lambda v: v * 12,       True,  ["fourseam", "sinker", "cutter"]),
    "depth": ("pfx_z",             lambda v: v * 12,       False, ["slider", "sweeper", "curve", "change", "split"]),
    "sweep": ("pfx_x",             lambda v: abs(v) * 12,  True,  PITCH_KEYS),
    "spin":  ("release_spin_rate", lambda v: v,            True,  PITCH_KEYS),
}
TOP_N = 30

# Season start for backfill. Spring training pitches are excluded by hfGT=R
# anyway, so starting a little early costs nothing.
SEASON_START_MD = (3, 15)


def _get(url: str, timeout: int = 90) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8-sig", "ignore")


def _num(row: dict, key: str):
    v = (row.get(key) or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _name(row: dict) -> str:
    raw = (row.get("player_name") or "").strip()
    if ", " in raw:
        last, first = raw.split(", ", 1)
        return f"{first} {last}"
    return raw


def _entry(row: dict, value: float, key: str, code: str) -> dict:
    # Opponent is whichever side the pitcher isn't on.
    home, away = row.get("home_team") or "", row.get("away_team") or ""
    top = (row.get("inning_topbot") or "").lower() == "top"
    pteam = home if top else away
    opp = away if top else home
    return {
        "playerId": (row.get("pitcher") or "").strip(),
        "name": _name(row),
        "hand": (row.get("p_throws") or "").strip(),
        "team": pteam,
        "opp": opp,
        "pitch": key,
        "pitchCode": code,
        "value": round(value, 1),
        "velo": _num(row, "release_speed"),
        "ivb": round((_num(row, "pfx_z") or 0) * 12, 1),
        "hb": round((_num(row, "pfx_x") or 0) * 12, 1),
        "spin": _num(row, "release_spin_rate"),
        "date": (row.get("game_date") or "")[:10],
        "result": (row.get("description") or "").replace("_", " "),
        "event": (row.get("events") or "").replace("_", " ") or None,
    }


def _empty(year: int) -> dict:
    return {
        "year": year,
        "generatedAt": None,
        "throughDate": None,
        "boards": {b: {k: [] for k in keys} for b, (_, _, _, keys) in BOARDS.items()},
        "maxVelo": {},  # playerId -> best single pitch
    }


def _merge_list(existing: list[dict], new: list[dict], higher: bool) -> list[dict]:
    seen = set()
    out = []
    for e in sorted(existing + new, key=lambda e: e["value"], reverse=higher):
        sig = (e["playerId"], e["date"], e["value"], e["pitchCode"])
        if sig in seen:
            continue
        seen.add(sig)
        out.append(e)
        if len(out) >= TOP_N:
            break
    return out


def ingest_day(state: dict, day: date) -> int:
    """Pull one day's pitches and fold them into state. Returns rows seen."""
    try:
        rows = list(csv.DictReader(io.StringIO(_get(SEARCH.format(year=state["year"], day=day.isoformat())))))
    except Exception:
        return 0
    candidates: dict[str, dict[str, list[dict]]] = {b: {k: [] for k in keys} for b, (_, _, _, keys) in BOARDS.items()}
    for r in rows:
        code = (r.get("pitch_type") or "").strip()
        key = PITCH_TYPES.get(code)
        if not key:
            continue
        for b, (field, fn, higher, keys) in BOARDS.items():
            if key not in keys:
                continue
            raw = _num(r, field)
            if raw is None:
                continue
            candidates[b][key].append(_entry(r, fn(raw), key, code))
        v = _num(r, "release_speed")
        pid = (r.get("pitcher") or "").strip()
        if v is not None and pid:
            cur = state["maxVelo"].get(pid)
            if cur is None or v > cur["value"]:
                state["maxVelo"][pid] = _entry(r, v, key, code)
    for b, (_, _, higher, keys) in BOARDS.items():
        for k in keys:
            if candidates[b][k]:
                state["boards"][b][k] = _merge_list(state["boards"][b][k], candidates[b][k], higher)
    return len(rows)


def refresh(path=None, today: date | None = None) -> dict:
    today = today or date.today()
    year = config.SEASON
    target = path or (config.CACHE_DIR / "extremes.json")

    state = None
    if target.exists():
        try:
            state = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            state = None
    if not state or state.get("year") != year:
        state = _empty(year)

    # Re-pull the last two already-seen days too: Savant back-fills corrections
    # and late games for a day or so after the fact.
    if state["throughDate"]:
        start = date.fromisoformat(state["throughDate"]) - timedelta(days=2)
    else:
        start = date(year, *SEASON_START_MD)
    start = max(start, date(year, *SEASON_START_MD))
    end = min(today, date(year, 11, 15))

    day = start
    total = 0
    while day <= end:
        total += ingest_day(state, day)
        day += timedelta(days=1)

    state["throughDate"] = end.isoformat()
    state["generatedAt"] = datetime.now(timezone.utc).isoformat()
    state["counts"] = {"pitchers": len(state["maxVelo"]), "rowsThisRun": total}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state), encoding="utf-8")
    return state


if __name__ == "__main__":
    import sys, time
    t0 = time.time()
    s = refresh()
    fast = s["boards"]["velo"]["fourseam"][:3]
    print(
        f"extremes.json ({s['year']}): through {s['throughDate']}, "
        f"{s['counts']['pitchers']} pitchers, {s['counts']['rowsThisRun']} rows this run, "
        f"{time.time()-t0:.0f}s | top FF: " + ", ".join(f"{e['name']} {e['value']}" for e in fast)
    )

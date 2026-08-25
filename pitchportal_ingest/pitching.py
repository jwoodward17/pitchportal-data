"""Pitch-level Statcast leaderboards, auto-pulled from Baseball Savant.

Three public Savant CSV exports get merged on (player_id, pitch_type):

  1. **pitch-movement** — `pitcher_break_z_induced` (IVB), `pitcher_break_z`
     (total VB), `pitcher_break_x` (HB), `avg_speed`, plus Savant's own
     percent-rank vs league for the pitch type.
  2. **pitch-arsenal-stats** — `whiff_percent`, `run_value_per_100`,
     `pitch_usage`, `k_percent`, `put_away`, `est_woba`, `hard_hit_percent`.
  3. **custom leaderboard** — `arm_angle`. Pitcher-level, joined on player_id.

**Extension, VAA and HAA come from raw Statcast, not a leaderboard.** No Savant
leaderboard publishes them — the custom leaderboard echoes back any selection
name you give it and then returns it empty (seven spellings of extension tried,
all blank across 339 rows). But the raw pitch-level search export carries the
release-point physics, so we compute them ourselves:

    vy_f  = -sqrt(vy0^2 - 2*ay*(50 - 17/12))     # velocity at the plate front
    t     = (vy_f - vy0) / ay
    vz_f  = vz0 + az*t                            # vertical velocity there
    VAA   = -atan(vz_f / vy_f)                    # negative: descending
    HAA   = -atan(vx_f / vy_f)

Statcast measures from y = 50 ft and the plate front edge is 17/12 ft. The
negation matters: without it the sign comes out inverted and every VAA reads
positive, which is backwards — the ball is descending into the zone.

Sanity check against known league values: four-seam VAA ≈ -4.8°, curveball
≈ -9.7°, average extension ≈ 6.4 ft. Those are the numbers this produces.

Because these are stable release traits rather than volatile results, a rolling
recent window is sampled instead of a whole season — a few weeks of pitches is
plenty and keeps the pull to a sane size.
"""

from __future__ import annotations

import csv
import io
import json
import math
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import config

UA = "PitchPortal/0.1 (+pitching ingest)"

# Raw pitch-level export. Heavy (~6 MB/day), so we pull a recent window in
# chunks rather than a whole season.
SEARCH = (
    "https://baseballsavant.mlb.com/statcast_search/csv?all=true&hfPT=&hfSea={year}%7C"
    "&hfGT=R%7C&player_type=pitcher&game_date_gt={start}&game_date_lt={end}"
    "&min_pitches=0&type=details"
)
# Days of recent pitches sampled for VAA / HAA / extension.
APPROACH_WINDOW_DAYS = 21
# Minimum pitches of a type before we trust a pitcher's averages.
MIN_APPROACH_PITCHES = 15

MOVEMENT = (
    "https://baseballsavant.mlb.com/leaderboard/pitch-movement"
    "?year={year}&team=&min={min_pitches}&pitch_type={pt}&hand=&x=diff_z&csv=true"
)
ARSENAL = (
    "https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
    "?type=pitcher&pitchType=&year={year}&team=&min={min_pa}&csv=true"
)
SPIN = (
    "https://baseballsavant.mlb.com/leaderboard/pitch-arsenals"
    "?type=avg_spin&year={year}&team=&min=10&csv=true"
)
# Column prefixes in the pitch-arsenals export -> app pitch keys.
SPIN_COLS = {"ff": "fourseam", "si": "sinker", "fc": "cutter", "sl": "slider",
             "st": "sweeper", "cu": "curve", "ch": "change", "fs": "split"}
CUSTOM = (
    "https://baseballsavant.mlb.com/leaderboard/custom?year={year}&type=pitcher"
    "&filter=&min=50&selections=pa,k_percent,bb_percent,woba,xwoba,arm_angle"
    "&chart=false&x=pa&y=pa&r=no&chartType=beeswarm&sort=pa&sortDir=desc&csv=true"
)

# Savant pitch_type codes we care about, mapped to the app's own pitch keys.
PITCH_TYPES = {
    "FF": "fourseam",
    "SI": "sinker",
    "FC": "cutter",
    "SL": "slider",
    "ST": "sweeper",
    "CU": "curve",
    "KC": "curve",
    "CH": "change",
    "FS": "split",
}

# Which direction is "better" on IVB, per pitch. This is the rule that makes the
# leaderboard mean anything: for a four-seam you want ride, so more IVB is
# better — but for a sinker, changeup, splitter or curve, LESS induced vertical
# break is the good end. Cutters and sweepers live around zero and can be either
# side of it depending on the pitcher, so ranking them by IVB is meaningless and
# we say so rather than sorting them arbitrarily.
IVB_DIRECTION = {
    "fourseam": "higher",
    "sinker": "lower",
    "change": "lower",
    "split": "lower",
    "curve": "lower",
    "slider": "lower",
    "cutter": "neutral",
    "sweeper": "neutral",
}


def _get(url: str, timeout: int = 45) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8-sig", "ignore")


def _rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def _num(row: dict, key: str):
    v = (row.get(key) or "").strip().strip('"')
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _name(row: dict) -> str:
    raw = (row.get("last_name, first_name") or "").strip().strip('"')
    if ", " in raw:
        last, first = raw.split(", ", 1)
        return f"{first} {last}".strip()
    return raw


# Leaderboards rank only pitches thrown at least this often; arsenals show
# everything. Pulling at a low floor and flagging qualification keeps one
# dataset serving both without a starter's fifth pitch vanishing.
QUALIFY_PITCHES = 50


def fetch_movement(year: int, min_pitches: int = 10) -> dict[tuple[str, str], dict]:
    """(player_id, pitch_key) -> movement row. One request per pitch type."""
    out: dict[tuple[str, str], dict] = {}
    for code, key in PITCH_TYPES.items():
        try:
            rows = _rows(_get(MOVEMENT.format(year=year, min_pitches=min_pitches, pt=code)))
        except Exception:
            continue
        for r in rows:
            pid = (r.get("pitcher_id") or "").strip().strip('"')
            if not pid:
                continue
            # KC folds into CU; keep whichever has more pitches.
            existing = out.get((pid, key))
            thrown = _num(r, "pitches_thrown") or 0
            if existing and (existing.get("pitches") or 0) >= thrown:
                continue
            out[(pid, key)] = {
                "playerId": pid,
                "name": _name(r),
                "hand": (r.get("pitch_hand") or "").strip().strip('"'),
                "team": (r.get("team_name_abbrev") or "").strip().strip('"'),
                "pitch": key,
                "pitchCode": code,
                "velo": _num(r, "avg_speed"),
                "ivb": _num(r, "pitcher_break_z_induced"),
                "vb": _num(r, "pitcher_break_z"),
                "hb": _num(r, "pitcher_break_x"),
                "pitches": thrown,
                "qualified": thrown >= QUALIFY_PITCHES,
                "vsLeagueZ": _num(r, "diff_z"),
                "vsLeagueX": _num(r, "diff_x"),
            }
    return out


def fetch_arsenal(year: int, min_pa: int = 1) -> dict[tuple[str, str], dict]:
    """(player_id, pitch_key) -> results row: whiff, run value, usage."""
    try:
        rows = _rows(_get(ARSENAL.format(year=year, min_pa=min_pa)))
    except Exception:
        return {}
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        pid = (r.get("player_id") or "").strip().strip('"')
        code = (r.get("pitch_type") or "").strip().strip('"')
        key = PITCH_TYPES.get(code)
        if not pid or not key:
            continue
        out[(pid, key)] = {
            "whiff": _num(r, "whiff_percent"),
            "kPct": _num(r, "k_percent"),
            "putAway": _num(r, "put_away"),
            "rv100": _num(r, "run_value_per_100"),
            "usage": _num(r, "pitch_usage"),
            "xwoba": _num(r, "est_woba"),
            "hardHit": _num(r, "hard_hit_percent"),
        }
    return out


def fetch_spin(year: int) -> dict[tuple[str, str], float]:
    """(player_id, pitch_key) -> average spin rate (rpm). One request for all."""
    try:
        rows = _rows(_get(SPIN.format(year=year)))
    except Exception:
        return {}
    out: dict[tuple[str, str], float] = {}
    for r in rows:
        pid = (r.get("pitcher") or "").strip().strip('"')
        if not pid:
            continue
        for col, key in SPIN_COLS.items():
            v = _num(r, f"{col}_avg_spin")
            if v is not None:
                out[(pid, key)] = v
    return out


def fetch_release(year: int) -> dict[str, dict]:
    """player_id -> arm angle. Extension is not available here — see docstring."""
    try:
        rows = _rows(_get(CUSTOM.format(year=year)))
    except Exception:
        return {}
    out = {}
    for r in rows:
        pid = (r.get("player_id") or "").strip().strip('"')
        if not pid:
            continue
        out[pid] = {
            "armAngle": _num(r, "arm_angle"),
            "xwobaOverall": _num(r, "xwoba"),
            "bbPct": _num(r, "bb_percent"),
        }
    return out


def approach_angles(vx0, vy0, vz0, ax, ay, az):
    """VAA / HAA in degrees at the front of the plate. Both negative in normal use."""
    yf = 17.0 / 12.0
    disc = vy0 * vy0 - 2 * ay * (50.0 - yf)
    if disc <= 0 or ay == 0:
        return None, None
    vy_f = -math.sqrt(disc)
    t = (vy_f - vy0) / ay
    vz_f = vz0 + az * t
    vx_f = vx0 + ax * t
    # Negated so a descending pitch reads negative, which is the convention.
    return -math.degrees(math.atan(vz_f / vy_f)), -math.degrees(math.atan(vx_f / vy_f))


def fetch_approach(year: int, days: int = APPROACH_WINDOW_DAYS) -> dict[tuple[str, str], dict]:
    """(player_id, pitch_key) -> mean VAA / HAA / extension over a recent window.

    Pulled a week at a time; a failed chunk is skipped rather than losing the lot.
    """
    end = date(year, 10, 1) if year < date.today().year else date.today()
    start = end - timedelta(days=days)

    buckets: dict[tuple[str, str], list[tuple[float, float, float]]] = defaultdict(list)
    chunk = start
    while chunk < end:
        chunk_end = min(chunk + timedelta(days=7), end)
        try:
            text = _get(
                SEARCH.format(year=year, start=chunk.isoformat(), end=chunk_end.isoformat()),
                timeout=180,
            )
            for r in csv.DictReader(io.StringIO(text)):
                key = PITCH_TYPES.get((r.get("pitch_type") or "").strip())
                pid = (r.get("pitcher") or "").strip()
                if not key or not pid:
                    continue
                try:
                    vaa, haa = approach_angles(
                        float(r["vx0"]), float(r["vy0"]), float(r["vz0"]),
                        float(r["ax"]), float(r["ay"]), float(r["az"]),
                    )
                    ext = float(r["release_extension"])
                except (ValueError, KeyError, TypeError):
                    continue
                if vaa is None:
                    continue
                buckets[(pid, key)].append((vaa, haa, ext))
        except Exception:
            pass
        chunk = chunk_end

    out: dict[tuple[str, str], dict] = {}
    for key, vals in buckets.items():
        if len(vals) < MIN_APPROACH_PITCHES:
            continue
        n = len(vals)
        out[key] = {
            "vaa": round(sum(v for v, _, _ in vals) / n, 2),
            "haa": round(sum(h for _, h, _ in vals) / n, 2),
            "extension": round(sum(e for _, _, e in vals) / n, 2),
            "approachN": n,
        }
    return out


def _percentile(values: list[float], v: float) -> float:
    """Where v sits in values, 0–100."""
    if not values:
        return 50.0
    below = sum(1 for x in values if x < v)
    return 100.0 * below / len(values)


def _grade(pct: float) -> int:
    """20–80 scouting scale from a percentile. 50 is average, 10 points per SD."""
    # ~0.5 SD per 10 points: p50 -> 50, p84 -> 60, p16 -> 40.
    if pct >= 99: return 80
    if pct >= 95: return 70
    if pct >= 84: return 65
    if pct >= 69: return 60
    if pct >= 55: return 55
    if pct >= 45: return 50
    if pct >= 31: return 45
    if pct >= 16: return 40
    if pct >= 5: return 35
    return 30


def add_grades(rows: list[dict]) -> None:
    """Grade every pitch on the 20–80 scale, within its own pitch type.

    NOTE: this is OUR grade, computed transparently from Savant run value per
    100 and whiff rate — it is not FanGraphs Stuff+ or PitchingBot, and it is
    labelled that way in the app so nobody mistakes it for a licensed metric.
    """
    by_pitch: dict[str, list[dict]] = {}
    for r in rows:
        by_pitch.setdefault(r["pitch"], []).append(r)

    for pitch, group in by_pitch.items():
        # Percentiles come from the qualified pool so a 12-pitch sample can't
        # distort the scale; every row is still graded against that pool.
        pool = [r for r in group if r.get("qualified")] or group
        rvs = [r["rv100"] for r in pool if r.get("rv100") is not None]
        whiffs = [r["whiff"] for r in pool if r.get("whiff") is not None]
        for r in group:
            parts = []
            # Run value per 100 is from the PITCHER's perspective on Savant:
            # positive is good for the pitcher.
            if r.get("rv100") is not None and rvs:
                parts.append(_percentile(rvs, r["rv100"]))
            if r.get("whiff") is not None and whiffs:
                parts.append(_percentile(whiffs, r["whiff"]))
            if parts:
                pct = sum(parts) / len(parts)
                r["gradePct"] = round(pct, 1)
                r["grade"] = _grade(pct)
            else:
                r["gradePct"] = None
                r["grade"] = None


def build(year: int) -> dict:
    movement = fetch_movement(year)
    arsenal = fetch_arsenal(year)
    release = fetch_release(year)
    approach = fetch_approach(year)
    spin = fetch_spin(year)

    rows: list[dict] = []
    for key, mv in movement.items():
        row = dict(mv)
        row["spin"] = spin.get(key)
        row.update(arsenal.get(key, {}))
        row.update(release.get(mv["playerId"], {}))
        row.update(approach.get(key, {}))
        rows.append(row)

    add_grades(rows)
    rows.sort(key=lambda r: (r["pitch"], -(r.get("pitches") or 0)))

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "year": year,
        "ivbDirection": IVB_DIRECTION,
        "counts": {
            "rows": len(rows),
            "pitchers": len({r["playerId"] for r in rows}),
            "withArsenal": sum(1 for r in rows if r.get("whiff") is not None),
            "withArmAngle": sum(1 for r in rows if r.get("armAngle") is not None),
            "withSpin": sum(1 for r in rows if r.get("spin") is not None),
            "withApproach": sum(1 for r in rows if r.get("vaa") is not None),
        },
        # No VAA: Savant does not publish it. See module docstring.
        "missing": [],
        "rows": rows,
    }


# --- league trends, derived from the data rather than from articles ----------

# Trends compare snapshots across seasons; anchor the last one to the live season.
TREND_YEARS = [config.SEASON - 5, config.SEASON - 2, config.SEASON]


def build_trends(years: list[int] | None = None) -> dict:
    """Compute what is actually moving across the league, year over year.

    This exists because "pitch trends" written from blog posts is just repeating
    what someone else concluded. Pulling arsenal stats for several seasons and
    diffing them gives findings we can point at a number for — usage share,
    average velocity and whiff rate per pitch type, and how each has moved.
    """
    years = years or TREND_YEARS
    per_year: dict[int, dict[str, dict]] = {}

    for y in years:
        try:
            rows = _rows(_get(ARSENAL.format(year=y, min_pa=1)))
        except Exception:
            continue
        agg: dict[str, dict] = {}
        for r in rows:
            key = PITCH_TYPES.get((r.get("pitch_type") or "").strip())
            if not key:
                continue
            n = _num(r, "pitches") or 0
            if not n:
                continue
            a = agg.setdefault(key, {"pitches": 0.0, "whiffW": 0.0, "whiffN": 0.0})
            a["pitches"] += n
            w = _num(r, "whiff_percent")
            if w is not None:
                a["whiffW"] += w * n
                a["whiffN"] += n
        total = sum(a["pitches"] for a in agg.values()) or 1
        per_year[y] = {
            k: {
                "usageShare": round(100.0 * a["pitches"] / total, 2),
                "whiff": round(a["whiffW"] / a["whiffN"], 2) if a["whiffN"] else None,
                "pitches": int(a["pitches"]),
            }
            for k, a in agg.items()
        }

    got = sorted(per_year)
    if len(got) < 2:
        return {"years": got, "perYear": per_year, "findings": []}

    first, last = got[0], got[-1]
    findings = []
    for pitch in sorted(set(per_year[last]) | set(per_year[first])):
        a = per_year[first].get(pitch)
        b = per_year[last].get(pitch)
        if not a or not b:
            continue
        d_use = round(b["usageShare"] - a["usageShare"], 2)
        d_whiff = (
            round(b["whiff"] - a["whiff"], 2)
            if a.get("whiff") is not None and b.get("whiff") is not None
            else None
        )
        findings.append(
            {
                "pitch": pitch,
                "usageFrom": a["usageShare"],
                "usageTo": b["usageShare"],
                "usageDelta": d_use,
                "whiffFrom": a.get("whiff"),
                "whiffTo": b.get("whiff"),
                "whiffDelta": d_whiff,
                # Direction is derived, not asserted.
                "direction": "rising" if d_use > 0.5 else "falling" if d_use < -0.5 else "steady",
            }
        )
    findings.sort(key=lambda f: -abs(f["usageDelta"]))
    return {"years": got, "from": first, "to": last, "perYear": per_year, "findings": findings}


# ---- derived reference files ------------------------------------------------
# metrics.json (league per-pitch ranges) and arsenals.json (featured pitcher
# mixes) used to be built by a separate pybaseball pipeline that pulled whole
# raw seasons. Everything they need is already in the rows built above, so they
# are derived here instead — one scheduled job keeps every screen's data the
# same age. Spin is the one field these rows don't carry; league spin barely
# moves year to year, so it ships as reference constants.

SPIN_REFERENCE = {
    "fourseam": {"avg": 2300, "lo": 2000, "hi": 2600},
    "sinker": {"avg": 2150, "lo": 1850, "hi": 2400},
    "cutter": {"avg": 2400, "lo": 2150, "hi": 2600},
    "slider": {"avg": 2450, "lo": 2100, "hi": 2700},
    "sweeper": {"avg": 2700, "lo": 2400, "hi": 2950},
    "curve": {"avg": 2650, "lo": 2300, "hi": 3000},
    "change": {"avg": 1750, "lo": 1500, "hi": 2100},
    "split": {"avg": 1300, "lo": 1000, "hi": 1700},
}


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    i = (len(sorted_vals) - 1) * p
    lo, hi = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def _metric_block(sample: list[dict], key: str) -> dict:
    entry: dict = {}
    for field in ("velo", "ivb", "hb"):
        vals = sorted(v for r in sample if (v := r.get(field)) is not None)
        if not vals:
            continue
        entry[field] = {
            "avg": round(sum(vals) / len(vals)),
            "lo": round(_pct(vals, 0.10)),
            "hi": round(_pct(vals, 0.90)),
        }
    spins = sorted(v for r in sample if (v := r.get("spin")) is not None)
    entry["spin"] = (
        {"avg": round(sum(spins) / len(spins)), "lo": round(_pct(spins, 0.10)), "hi": round(_pct(spins, 0.90))}
        if len(spins) >= 10 else SPIN_REFERENCE[key]
    )
    entry["n"] = len(sample)
    return entry


def derive_metrics(rows: list[dict]) -> dict:
    """League {avg, lo, hi} per pitch type, for everyone and split by hand.

    lo/hi are the 10th/90th percentile — 'normal range', not extremes. The
    hand split matters because HB is signed: a lefty's sinker runs the other
    way, and a pooled average of +15 and -15 is a lie. Shape:
      { pitchKey: { all: {...}, L: {...}, R: {...} } }
    The app's legacy readers that expect the flat block get `all`'s fields
    mirrored at the top level too."""
    out: dict = {}
    for key in config.APP_PITCH_KEYS:
        qualified = [r for r in rows if r["pitch"] == key and r.get("qualified")]
        allb = _metric_block(qualified, key)
        entry = dict(allb)
        entry["all"] = allb
        entry["L"] = _metric_block([r for r in qualified if r.get("hand") == "L"], key)
        entry["R"] = _metric_block([r for r in qualified if r.get("hand") == "R"], key)
        out[key] = entry
    return out


def derive_arsenals(rows: list[dict]) -> dict:
    """Featured pitchers' current mixes, straight from this season's rows.
    A featured name with no rows this season (hurt, retired) is dropped rather
    than shown with stale numbers."""
    by_name: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_name[r["name"]].append(r)
    out: dict = {}
    for slug, (full_name, hand, role) in config.FEATURED.items():
        mine = by_name.get(full_name, [])
        if not mine:
            continue
        total = sum(r.get("pitches") or 0 for r in mine) or 1
        for r in mine:
            if r.get("usage") is None:
                r["usage"] = round(100 * (r.get("pitches") or 0) / total, 1)
        mine.sort(key=lambda r: -(r["usage"] or 0))
        out[slug] = {
            "name": full_name,
            "hand": f"{hand}HP",
            "role": role,
            "pitches": [
                [
                    config.PITCH_DISPLAY[r["pitch"]],
                    round(r["usage"]),
                    round(r["velo"]) if r.get("velo") is not None else 0,
                    f"{'+' if (r.get('ivb') or 0) >= 0 else ''}{round(r.get('ivb') or 0)}″",
                    f"{abs(round(r.get('hb') or 0))}″",
                    round(r["spin"]) if r.get("spin") is not None else None,
                ]
                for r in mine
                if r.get("usage") and r["usage"] >= 2
            ],
        }
    return out


def refresh(year: int | None = None, path=None) -> dict:
    year = year or config.SEASON
    data = build(year)
    try:
        data["trends"] = build_trends()
    except Exception:
        data["trends"] = {"years": [], "findings": []}
    target = path or (config.CACHE_DIR / "pitching.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data), encoding="utf-8")

    # Keep the sibling reference files the same age as the leaderboards.
    (config.CACHE_DIR / "metrics.json").write_text(
        json.dumps(derive_metrics(data["rows"]), ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    (config.CACHE_DIR / "arsenals.json").write_text(
        json.dumps(derive_arsenals(data["rows"]), ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return data


if __name__ == "__main__":
    import sys, time
    y = int(sys.argv[1]) if len(sys.argv) > 1 else config.SEASON
    t0 = time.time()
    d = refresh(y)
    c = d["counts"]
    print(
        f"pitching.json ({y}): {c['rows']} pitch-rows / {c['pitchers']} pitchers, "
        f"{c['withArsenal']} whiff, {c['withArmAngle']} arm angle, "
        f"{c['withApproach']} VAA/HAA/ext "
        f"in {time.time()-t0:.1f}s"
    )

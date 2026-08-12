"""Build featured pro arsenals (ARSENALS) from per-pitcher Statcast pulls.

Output shape (per pitcher key):
  { "name", "hand": "LHP"|"RHP", "role",
    "pitches": [[name, usage%, velo, ivb_str, hb_str], ...] }  # sorted by usage
The app derives the movement-leaders board (s-leaders) from this same object.
"""

from __future__ import annotations

from . import config, convert


def _resolve_id(key: str) -> int | None:
    name = config.FEATURED[key][0]
    parts = name.split()
    first, last = parts[0], " ".join(parts[1:])
    try:
        from pybaseball import playerid_lookup

        res = playerid_lookup(last, first)
        ids = res["key_mlbam"].dropna()
        if len(ids):
            return int(ids.iloc[0])
    except Exception:
        pass
    return config.FALLBACK_IDS.get(key)


def build_arsenal(key: str, start: str, end: str) -> dict | None:
    from pybaseball import statcast_pitcher

    name, hand, role = config.FEATURED[key]
    pid = _resolve_id(key)
    if pid is None:
        print(f"    ! could not resolve an MLBAM id for {key}; skipping")
        return None

    df = statcast_pitcher(start, end, pid)
    if df is None or df.empty:
        print(f"    ! no statcast rows for {name}; skipping")
        return None

    df = df[df["pitch_type"].notna()].copy()
    df["appkey"] = df["pitch_type"].map(config.PITCH_TYPE_MAP)
    df = df.dropna(subset=["appkey"])
    total = len(df)
    if total == 0:
        return None

    rows: list[list] = []
    for appkey, sub in df.groupby("appkey"):
        usage = int(round(100 * len(sub) / total))
        if usage < 1:
            continue
        velo = int(round(sub["release_speed"].dropna().mean()))
        ivb = sub["pfx_z"].dropna().mean() * 12.0
        hb = sub["pfx_x"].dropna().abs().mean() * 12.0
        rows.append(
            [config.PITCH_DISPLAY.get(appkey, appkey), usage, velo, convert.fmt_ivb(ivb), convert.fmt_hb(hb)]
        )

    rows.sort(key=lambda r: r[1], reverse=True)
    return {
        "name": name,
        "hand": "LHP" if hand == "L" else "RHP",
        "role": role,
        "pitches": rows,
    }


def build_arsenals(start: str = config.DEFAULT_START, end: str = config.DEFAULT_END) -> dict:
    out: dict = {}
    for key in config.FEATURED:
        print(f"  arsenal: {key}")
        arsenal = build_arsenal(key, start, end)
        if arsenal:
            out[key] = arsenal
    return out

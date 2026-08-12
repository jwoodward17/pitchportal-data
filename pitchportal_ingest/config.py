from __future__ import annotations

from pathlib import Path

# Cache output dir — served by server.py. Mirror metrics.json/arsenals.json into
# app/src/data/seed/ if you want to refresh the app's offline fallback too.
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"

# The season is derived from today, never hardcoded — a hardcoded year is how
# the app shipped showing last season. Before April there is no meaningful
# current-season sample, so January–March serves the completed prior season.
from datetime import date as _date


def current_season(today: _date | None = None) -> int:
    t = today or _date.today()
    return t.year if t.month >= 4 else t.year - 1


SEASON = current_season()
DEFAULT_START = f"{SEASON}-03-15"
DEFAULT_END = f"{SEASON}-11-15"

# Statcast `pitch_type` code -> app pitch key (the 8 keys the app uses).
PITCH_TYPE_MAP = {
    "FF": "fourseam",
    "SI": "sinker",
    "FT": "sinker",  # legacy two-seam
    "FC": "cutter",
    "SL": "slider",
    "ST": "sweeper",  # sweeper (codified 2023+)
    "SV": "slider",  # slurve -> slider family
    "CU": "curve",
    "KC": "curve",  # knuckle-curve
    "CS": "curve",  # slow curve
    "CH": "change",
    "FS": "split",  # splitter
    "FO": "split",  # forkball -> split family
}

APP_PITCH_KEYS = [
    "fourseam",
    "sinker",
    "cutter",
    "slider",
    "sweeper",
    "curve",
    "change",
    "split",
]

# Display names used in ARSENALS pitch rows.
PITCH_DISPLAY = {
    "fourseam": "4-Seam",
    "sinker": "Sinker",
    "cutter": "Cutter",
    "slider": "Slider",
    "sweeper": "Sweeper",
    "curve": "Curveball",
    "change": "Changeup",
    "split": "Splitter",
}

# Featured pitchers for ARSENALS: key -> (full name, throws L/R, role blurb).
FEATURED = {
    "sale": ("Chris Sale", "L", "Low-slot lefty — east-west fastball/slider picture."),
    "skubal": ("Tarik Skubal", "L", "Power lefty — elite fastball/changeup combo."),
    "snell": ("Blake Snell", "L", "High-ride heater up, big breaking balls down."),
    "valdez": (
        "Framber Valdez",
        "L",
        "Ground-ball model — heavy sinker, big curve. A great template for your arsenal.",
    ),
    "cole": ("Gerrit Cole", "R", "Prototype power righty — ride up, breaking down."),
    "burnes": ("Corbin Burnes", "R", "Cutter-led — builds everything off a plus cutter."),
    "strider": ("Spencer Strider", "R", "Two-pitch power — huge ride plus a wipeout slider."),
    "webb": ("Logan Webb", "R", "Sinker / sweeper ground-ball machine."),
    "verlander": ("Justin Verlander", "R", "High-IVB ride leader — the prototype rising four-seam."),
    "gausman": ("Kevin Gausman", "R", "Fastball / splitter — the model modern splitter."),
    "holmes": ("Clay Holmes", "R", "Sinker / sweeper — huge sweep off heavy sink."),
}

# Known MLBAM ids — fallback when playerid_lookup is offline/ambiguous.
FALLBACK_IDS = {
    "sale": 519242,
    "skubal": 669373,
    "snell": 605483,
    "valdez": 664285,
    "cole": 543037,
    "burnes": 669203,
    "strider": 675911,
    "webb": 657277,
    "verlander": 434378,
    "gausman": 592332,
    "holmes": 593974,
}

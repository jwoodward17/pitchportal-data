"""Statcast movement -> app IVB / HB convention.

Statcast `pfx_x` / `pfx_z` are in FEET, from the CATCHER's perspective (§6
mapping note). We convert to inches and to the app's conventions:

  * IVB: signed inches (positive = ride, negative = drop)  ->  pfx_z * 12
  * HB:  positive magnitude in the pitch's natural direction (run/sweep/cut),
         which is how the app stores it for every pitch       ->  |pfx_x| * 12

`hb_inches_armside` is provided for when you want a *signed* arm-side value
instead — it flips sign by handedness so arm-side reads positive for both
LHP and RHP.
"""

from __future__ import annotations

# Unicode glyphs used by the app's string fields.
INCH = "″"  # ″ double prime
MINUS = "−"  # − minus sign


def ivb_inches(pfx_z: float) -> float:
    return pfx_z * 12.0


def hb_inches_magnitude(pfx_x: float) -> float:
    return abs(pfx_x) * 12.0


def hb_inches_armside(pfx_x: float, p_throws: str) -> float:
    inches = pfx_x * 12.0
    return inches if str(p_throws).upper().startswith("L") else -inches


def fmt_ivb(value_inches: float) -> str:
    """e.g. +17″ / −9″ (whole inches, app-style)."""
    r = int(round(value_inches))
    sign = "+" if r >= 0 else MINUS
    return f"{sign}{abs(r)}{INCH}"


def fmt_hb(value_inches: float) -> str:
    """e.g. 16″ (whole-inch positive magnitude)."""
    return f"{int(round(abs(value_inches)))}{INCH}"

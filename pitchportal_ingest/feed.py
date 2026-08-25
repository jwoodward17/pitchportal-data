"""Auto-pulling community feed.

Polls the **public YouTube RSS feed** of each tracked channel and normalises the
result into the shape `app/src/data/community.ts` already uses.

Why RSS rather than an API:

  * No API key, no OAuth, no quota. `https://www.youtube.com/feeds/videos.xml`
    is a public, documented endpoint that updates the moment a channel posts.
  * The YouTube Data API would need a key and burns quota on every poll.
  * Scraping the site HTML would violate YouTube's terms; this does not.

Why YouTube only: X, Instagram and TikTok have no equivalent open feed. Their
APIs are gated and scraping them breaks their terms, so the app treats those as
link-out cards rather than pretending to embed them. If a creator you care about
cross-posts to YouTube, track their channel here and their clips flow in.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from . import config

RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
UA = "PitchPortal/0.1 (+feed ingest)"

# Atom + YouTube + Media RSS namespaces used by the YouTube feed.
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


@dataclass(frozen=True)
class Channel:
    id: str          # account id used by the app
    channel_id: str  # YouTube UC… id
    name: str
    handle: str
    topic: str       # default topic; per-post keywords can override
    blurb: str


# Verified 2026-08-10: every one of these resolves and returns entries.
CHANNELS: list[Channel] = [
    Channel(
        id="driveline",
        channel_id="UC2SXF3PBsnStkiqbtZI0k9w",
        name="Driveline Baseball",
        handle="@drivelinebaseball",
        topic="drills",
        blurb="Pitch design, biomechanics and training research.",
    ),
    Channel(
        id="tread",
        channel_id="UCANTSXGjfXdmXzlAbELdWXw",
        name="Tread Athletics",
        handle="@TreadAthletics",
        topic="drills",
        blurb="Velo development and remote pitching training.",
    ),
    Channel(
        id="mlb",
        channel_id="UCoLrcjPV5PbUrUyXq5mjc_A",
        name="MLB",
        handle="@MLB",
        topic="news",
        blurb="Official Major League Baseball highlights.",
    ),
    Channel(
        id="flatground",
        channel_id="UCBc5KijdW47nMWbvI6Ygz8g",
        name="Flatground Baseball",
        handle="@FlatgroundApp",
        topic="nasty",
        blurb="College and pro scouting looks.",
    ),
    Channel(
        id="topvelocity",
        channel_id="UC-PnNghNYDbOmQ2Fkkj0sqQ",
        name="TopVelocity",
        handle="@TopVelocity",
        topic="drills",
        blurb="Velocity mechanics breakdowns.",
    ),
]

# Rough topic routing from the video title, so the feed's filter chips do
# something useful instead of every post landing in one bucket.
TOPIC_RULES: list[tuple[str, str]] = [
    (r"\b(grip|pitch design|sweeper|slider|cutter|changeup|kick change|splitter|curve|sinker)\b", "shop"),
    (r"\b(drill|routine|program|lift|mobility|arm care|plyo|training|workout)\b", "drills"),
    (r"\b(nasty|filthy|disgusting|unhittable|k'?s|strikeout|punchout)\b", "nasty"),
    (r"\b(highlight|walk-?off|recap|debut|signs|traded|injur)\b", "news"),
]


def _topic_for(title: str, default: str) -> str:
    low = title.lower()
    for pattern, topic in TOPIC_RULES:
        if re.search(pattern, low):
            return topic
    return default


def _time_ago(published: str) -> str:
    """'2026-08-09T18:04:00+00:00' -> '18h'. The app shows this verbatim."""
    try:
        then = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return "recent"
    delta = datetime.now(timezone.utc) - then
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 7:
        return f"{days}d"
    return f"{days // 7}w"


def _fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


OEMBED = "https://www.youtube.com/oembed?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D{vid}&format=json"


def _embeddable(vid: str) -> bool:
    """True if YouTube will play this video inside a third-party embed.

    Uploaders can disable embedding per video; such videos show "Video
    unavailable" in our player. YouTube's oEmbed endpoint returns 401 for
    exactly those (400/404 for removed, private or malformed), and 200 otherwise — a keyless,
    reliable pre-check that costs one small request per clip at ingest time,
    so the app never shows a card that can't play."""
    req = urllib.request.Request(OEMBED.format(vid=vid), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return e.code not in (400, 401, 403, 404)
    except Exception:
        # Network hiccup: keep the clip rather than silently thinning the feed.
        return True


def fetch_channel(ch: Channel, limit: int = 8) -> list[dict]:
    """Pull one channel's recent uploads. Returns [] on any failure — one dead
    channel must not take the whole feed down."""
    try:
        raw = _fetch(RSS.format(cid=ch.channel_id))
        root = ET.fromstring(raw)
    except Exception:
        return []

    posts: list[dict] = []
    for entry in root.findall("atom:entry", NS)[:limit]:
        vid_el = entry.find("yt:videoId", NS)
        title_el = entry.find("atom:title", NS)
        pub_el = entry.find("atom:published", NS)
        if vid_el is None or title_el is None or not vid_el.text:
            continue

        vid = vid_el.text
        title = (title_el.text or "").strip()
        published = (pub_el.text or "") if pub_el is not None else ""
        if not _embeddable(vid):
            continue

        group = entry.find("media:group", NS)
        views = 0
        if group is not None:
            comm = group.find("media:community", NS)
            if comm is not None:
                stats = comm.find("media:statistics", NS)
                if stats is not None:
                    views = int(stats.get("views") or 0)

        posts.append(
            {
                "id": f"yt-{vid}",
                "accountId": ch.id,
                "platform": "youtube",
                "topic": _topic_for(title, ch.topic),
                "text": title,
                "media": "clip",
                "youtubeId": vid,
                "likes": views // 100,  # views/100 keeps the number card-sized
                "comments": 0,
                "timeAgo": _time_ago(published),
                "publishedAt": published,
                "url": f"https://www.youtube.com/watch?v={vid}",
            }
        )
    return posts


# --- X / Twitter -------------------------------------------------------------
#
# X has no open feed. Their API returns 401 without auth, and the endpoints their
# embed widgets use are undocumented — pulling from those in bulk would be
# scraping, which their terms prohibit. So native X pull is gated on a real API
# token, and without one the app falls back to X's official embedded timeline
# widget (see xTimelineHtml), which auto-updates on its own but arrives as X's
# card rather than native posts.
#
# To enable native pull: X API v2 Basic tier, then
#   export X_BEARER_TOKEN="..."
X_BEARER = os.environ.get("X_BEARER_TOKEN", "").strip()

X_ACCOUNTS = [
    ("pitchingninja", "PitchingNinja", "nasty"),
    ("treadhq", "TreadHQ", "drills"),
    ("flatground", "FlatgroundApp", "nasty"),
]


def _x_get(url: str) -> dict | None:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {X_BEARER}", "User-Agent": UA}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fetch_x(handle: str, account_id: str, topic: str, limit: int = 5) -> list[dict]:
    """Recent posts for one X account. Returns [] unless a bearer token is set."""
    if not X_BEARER:
        return []

    user = _x_get(f"https://api.twitter.com/2/users/by/username/{handle}")
    uid = (user or {}).get("data", {}).get("id")
    if not uid:
        return []

    fields = "tweet.fields=created_at,public_metrics,attachments&expansions=attachments.media_keys&media.fields=type,preview_image_url"
    tl = _x_get(
        f"https://api.twitter.com/2/users/{uid}/tweets?max_results={max(5, limit)}&{fields}"
    )
    posts = []
    for t in (tl or {}).get("data", [])[:limit]:
        metrics = t.get("public_metrics", {}) or {}
        posts.append(
            {
                "id": f"x-{t['id']}",
                "accountId": account_id,
                "platform": "x",
                "topic": topic,
                "text": t.get("text", ""),
                "media": "clip",
                "tweetId": t["id"],
                "likes": metrics.get("like_count", 0),
                "comments": metrics.get("reply_count", 0),
                "timeAgo": _time_ago(t.get("created_at", "")),
                "publishedAt": t.get("created_at", ""),
                "url": f"https://x.com/i/status/{t['id']}",
            }
        )
    return posts


def build_feed(per_channel: int = 8, cap: int = 60) -> dict:
    """Merge every channel, newest first."""
    accounts = [
        {
            "id": c.id,
            "name": c.name,
            "handle": c.handle,
            "platform": "youtube",
            "blurb": c.blurb,
            "followers": "",
            "topics": [c.topic],
            "url": f"https://www.youtube.com/{c.handle}",
        }
        for c in CHANNELS
    ]

    posts: list[dict] = []
    ok_channels = 0
    for ch in CHANNELS:
        got = fetch_channel(ch, limit=per_channel)
        if got:
            ok_channels += 1
        posts.extend(got)

    # Native X posts, only when a bearer token is configured.
    x_posts = 0
    for account_id, handle, topic in X_ACCOUNTS:
        got = fetch_x(handle, account_id, topic)
        x_posts += len(got)
        posts.extend(got)

    posts.sort(key=lambda p: p.get("publishedAt") or "", reverse=True)

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "channelsOk": ok_channels,
        "channelsTotal": len(CHANNELS),
        # Lets the app show an honest "X is / isn't auto-pulling" state.
        "xNative": bool(X_BEARER),
        "xPosts": x_posts,
        "accounts": accounts,
        "posts": posts[:cap],
    }


def refresh(path=None) -> dict:
    """Build and write the cache. Returns the payload."""
    data = build_feed()
    target = path or (config.CACHE_DIR / "feed.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


if __name__ == "__main__":
    t0 = time.time()
    d = refresh()
    print(
        f"feed.json: {len(d['posts'])} posts from "
        f"{d['channelsOk']}/{d['channelsTotal']} channels in {time.time()-t0:.1f}s"
    )

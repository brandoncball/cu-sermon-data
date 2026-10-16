#!/usr/bin/env python3
"""
Church Unlimited - what counts as a full Sunday message
=======================================================
Shared by build-sermons.py (latest-sermon.json) and build-watch.py
(watch-videos.json), so every surface that lists messages applies one rule:

  latest-sermon.json -> /sermons "Latest Message" hero, the homepage cards, and
                        the /sermons "More Messages" grid (served by the
                        latest-sermons Supabase edge function, which reads
                        this file)
  watch-videos.json  -> the /watch grid

RULE (Brandon, 2026-10-15): YouTube Shorts never go on the sermons pages. Only
full Sunday messages do, and the first card is always the latest one.

Why this exists: the channel RSS feed lists every upload. From 2026-10 the CU
Social Publisher (Make) also posts sermon-clip reels and promos to the channel
as Shorts, and on 2026-10-15 they had taken the Latest Message hero, two of the
three homepage cards and 5 of the 9 /watch cards.

A feed entry is a full message only if ALL of these hold:
  1. Its link is not a youtube.com/shorts/ URL.
  2. Neither its title nor its description carries a #shorts tag.
  3. Its title has the Sunday upload format: three or more parts separated by
     "|", the last one "Church Unlimited". For example:
        The Sound of the Law | Pastor Brandon Ball | Church Unlimited
        The Last Piece of Wood | Easter 2026 | Pastor Brandon Ball | Church Unlimited
        Secure in Heaven, Strong on Earth | Cody Prickett | Church Unlimited
     Every full message on the channel is titled this way (checked against all
     33 streams and uploads, March to October 2026) and no Short is. Rule 3 is
     a positive test, so a Short that drops the hashtag still stays off.
     A message uploaded with a different title is skipped and the run log says
     why. Fix the title on YouTube and the next run picks it up.

Messages uploaded as ordinary videos instead of livestreams count too (The End
of Trying, 2026-08-30, was an upload, not a stream). There is deliberately no
"must be a livestream" rule.

CARRY-FORWARD: the RSS feed holds only the channel's 15 newest uploads, and
Shorts now outnumber messages, so the feed alone may carry just two or three
messages. Each build therefore merges the messages already published in the
JSON with the fresh ones from the feed, de-duplicated by videoId, newest first.
A week of Shorts can never push the messages off the page. Entries carried
forward are re-checked, so anything published before this rule is purged.

Stdlib only.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

CHANNEL_ID = "UCKRjDX5HMyO4Xls_t4xqC_w"
FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
BRAND = "church unlimited"  # last "|" part of every Sunday upload title

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

SHORTS_TAG_RE = re.compile(r"#shorts\b", re.IGNORECASE)

# A YouTube chapter line in a description: "38:04 Guarding Grace", "1:12:49 ...".
CHAPTER_LINE_RE = re.compile(r"^\s*((?:\d{1,2}:)?[0-5]?\d:[0-5]\d)\s+(.+?)\s*$")


# -- Feed --

def fetch_feed(url: str = FEED_URL, user_agent: str = "cu-sermon-updater/1.0 (+https://mychurchunlimited.com)",
               attempts: int = 3) -> bytes:
    """Return the feed bytes. CU_FEED_FILE (path to a local XML file) replaces
    the network, for tests. Transient failures are retried; the last error is
    raised if every attempt fails."""
    local = os.environ.get("CU_FEED_FILE")
    if local:
        return Path(local).read_bytes()
    last_error = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_error = e
            if attempt + 1 < attempts:
                time.sleep(5 * (attempt + 1))
    raise last_error


def _text(el) -> str:
    return (el.text or "") if el is not None else ""


def parse_entries(xml_bytes: bytes) -> list:
    """Every entry in the feed, newest first: videoId, raw title, published
    timestamp, alternate link and FULL description (never truncated, so a
    #shorts tag at the very end is still seen)."""
    root = ET.fromstring(xml_bytes)
    entries = []
    for entry in root.findall("atom:entry", NS):
        video_id = _text(entry.find("yt:videoId", NS)).strip()
        title = _text(entry.find("atom:title", NS)).strip()
        published = _text(entry.find("atom:published", NS)).strip()
        if not (video_id and title and published):
            continue
        link = ""
        for link_el in entry.findall("atom:link", NS):
            if link_el.get("rel", "alternate") == "alternate":
                link = link_el.get("href", "")
                break
        description = ""
        group = entry.find("media:group", NS)
        if group is not None:
            description = _text(group.find("media:description", NS)).strip()
        entries.append({
            "videoId": video_id,
            "title": title,
            "published": published,
            "link": link,
            "description": description,
        })
    return entries


# -- The rule --

def title_parts(title: str) -> list:
    return [p.strip() for p in (title or "").split("|") if p.strip()]


def skip_reason(entry: dict):
    """None if the feed entry is a full Sunday message, else why it is left off."""
    if "/shorts/" in (entry.get("link") or ""):
        return "Short (shorts link)"
    if SHORTS_TAG_RE.search(entry.get("title") or "") or SHORTS_TAG_RE.search(entry.get("description") or ""):
        return "Short (#shorts tag)"
    parts = title_parts(entry.get("title", ""))
    if len(parts) < 3 or parts[-1].lower() != BRAND:
        return "title is not 'Message | Speaker | Church Unlimited'"
    return None


def carried_skip_reason(video: dict):
    """The same rule for an entry already in the published JSON. Those keep
    only the parsed title and speaker, so a title that had no speaker part
    shows up as speaker "Church Unlimited" or a missing speaker."""
    if not video.get("videoId") or not video.get("date"):
        return "incomplete entry"
    blob = " ".join(str(video.get(k) or "") for k in ("title", "speaker", "description"))
    if SHORTS_TAG_RE.search(blob):
        return "Short (#shorts tag)"
    speaker = (video.get("speaker") or "").strip()
    if not speaker or speaker.lower() == BRAND:
        return "no speaker in the title, so not a message"
    return None


# -- Helpers shared by both builders --

def strip_chapters(description: str) -> str:
    """Description text with the YouTube chapter list removed."""
    keep = [line for line in (description or "").splitlines() if not CHAPTER_LINE_RE.match(line)]
    return "\n".join(keep).strip()


def load_existing(path) -> list:
    """Videos already published in the JSON at path; [] if missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    videos = data.get("videos", []) if isinstance(data, dict) else []
    return [v for v in videos if isinstance(v, dict)] if isinstance(videos, list) else []


def merge_messages(fresh: list, existing: list, count: int) -> list:
    """Fresh feed messages, then the previously published ones they do not
    already cover, de-duplicated by videoId and sorted newest date first
    (stable, so a fresh entry wins a tie). Returns at most `count`."""
    merged, seen = [], set()
    for video in fresh:
        if video["videoId"] not in seen:
            seen.add(video["videoId"])
            merged.append(video)
    for video in existing:
        video_id = video.get("videoId")
        if not video_id or video_id in seen:
            continue
        reason = carried_skip_reason(video)
        if reason:
            print(f"  drop {video_id} from the published list ({reason}): {str(video.get('title', ''))[:70]}")
            continue
        seen.add(video_id)
        merged.append(video)
    merged.sort(key=lambda v: v.get("date", ""), reverse=True)
    return merged[:count]

#!/usr/bin/env python3
"""
Church Unlimited - Watch Grid JSON Builder
==========================================
Fetches the Church Unlimited YouTube channel RSS feed and writes
watch-videos.json for the Watch-page grid.

This grid lists the 9 most recent services, newest first. Box 1 is always
the latest Sunday message; every card shifts one slot right when the next
service is uploaded.

START TIME / SKIPPING WORSHIP:
If a video's description contains a YouTube chapter labeled "Message"
(e.g. a line like "12:34 Message"), that timestamp is parsed into a
"start" value (seconds). The Watch-page player opens at that point so
on-site viewers skip straight to the preaching. The full video on YouTube
is never touched. If no Message chapter is found, start = 0 (plays from
the beginning).

Run locally:
    python build-watch.py

Run in GitHub Actions:
    See .github/workflows/update-watch.yml

No third-party dependencies. Stdlib only (urllib + xml.etree + re).
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# -- CONFIG --
CHANNEL_ID = "UCKRjDX5HMyO4Xls_t4xqC_w"
SKIP_LATEST = 0          # box 1 is always the latest Sunday message
DISPLAY_COUNT = 9        # 3 cols x 3 rows
MESSAGE_LABEL = "message"  # chapter label that marks where preaching starts
OUTPUT_FILENAME = "watch-videos.json"

FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

# Matches H:MM:SS or M:SS / MM:SS timestamps.
TS_RE = re.compile(r"(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)")


def fetch_feed(url: str) -> bytes:
    local = os.environ.get("CU_FEED_FILE")
    if local:
        return Path(local).read_bytes()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "cu-watch-updater/1.0 (+https://mychurchunlimited.com)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_speaker(title: str) -> tuple[str, str]:
    parts = [p.strip() for p in title.split("|") if p.strip()]
    if len(parts) == 1:
        return parts[0], "Church Unlimited"
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], parts[-2]


def parse_iso_date(iso_str: str) -> str:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%d")


def parse_message_start(description: str, label: str) -> int:
    """Find a chapter line labeled <label> (e.g. 'Message') and return its
    timestamp in seconds. Returns 0 if not found. Scans the FULL description,
    so call this before truncating the display text."""
    if not description:
        return 0
    label = label.lower()
    for line in description.splitlines():
        if label in line.lower():
            m = TS_RE.search(line)
            if m:
                h = int(m.group(1)) if m.group(1) else 0
                mn = int(m.group(2))
                s = int(m.group(3))
                return h * 3600 + mn * 60 + s
    return 0


def build_videos(xml_bytes: bytes, skip: int, count: int) -> list:
    root = ET.fromstring(xml_bytes)
    entries = root.findall("atom:entry", NS)
    selected = entries[skip:skip + count]
    videos = []
    for entry in selected:
        title_el = entry.find("atom:title", NS)
        video_id_el = entry.find("yt:videoId", NS)
        published_el = entry.find("atom:published", NS)
        if title_el is None or video_id_el is None or published_el is None:
            continue
        raw_title = title_el.text or ""
        video_id = video_id_el.text or ""
        published = published_el.text or ""
        sermon_title, speaker = parse_speaker(raw_title)

        description = ""
        media_group = entry.find("media:group", NS)
        if media_group is not None:
            desc_el = media_group.find("media:description", NS)
            if desc_el is not None and desc_el.text:
                description = desc_el.text.strip()

        start = parse_message_start(description, MESSAGE_LABEL)

        videos.append({
            "title": sermon_title,
            "speaker": speaker,
            "date": parse_iso_date(published),
            "description": description[:300],
            "start": start,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
            "videoId": video_id,
        })
    return videos


def build_payload(videos: list) -> dict:
    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "channel": "https://www.youtube.com/@Mychurchunlimited",
        "videos": videos,
    }


def write_if_changed(path: Path, payload: dict) -> bool:
    new_videos_json = json.dumps(payload["videos"], sort_keys=True)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if json.dumps(existing.get("videos", []), sort_keys=True) == new_videos_json:
                print("No change in videos. Skipping write.")
                return False
        except (json.JSONDecodeError, KeyError):
            pass
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path} ({len(payload['videos'])} videos)")
    return True


def main() -> int:
    output_path = Path(__file__).resolve().parent.parent / OUTPUT_FILENAME
    override = os.environ.get("CU_WATCH_OUTPUT")
    if override:
        output_path = Path(override)

    print(f"Fetching: {FEED_URL}")
    try:
        xml_bytes = fetch_feed(FEED_URL)
    except urllib.error.URLError as e:
        print(f"ERROR fetching feed: {e}", file=sys.stderr)
        return 1

    videos = build_videos(xml_bytes, SKIP_LATEST, DISPLAY_COUNT)
    if not videos:
        print("ERROR: no videos parsed from feed", file=sys.stderr)
        return 1

    payload = build_payload(videos)
    print(f"Box 1: {videos[0]['title']} ({videos[0]['date']}) start={videos[0]['start']}s")
    write_if_changed(output_path, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())

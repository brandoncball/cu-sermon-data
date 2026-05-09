#!/usr/bin/env python3
"""
Church Unlimited - Sermon JSON Builder
=======================================
Fetches the latest videos from the Church Unlimited YouTube channel RSS feed
and writes latest-sermon.json in the shape the website expects.

Run locally:
    python build-sermons.py

Run in GitHub Actions:
    See .github/workflows/update-sermons.yml

No third-party dependencies. Stdlib only (urllib + xml.etree).
"""

import json
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# ── CONFIG ──
CHANNEL_ID = "UCKRjDX5HMyO4Xls_t4xqC_w"
VIDEO_COUNT = 6
OUTPUT_FILENAME = "latest-sermon.json"

FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

# Atom + YouTube + Media RSS namespaces
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def fetch_feed(url: str) -> bytes:
    """Fetch the RSS feed bytes. Raises on HTTP error."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "cu-sermon-updater/1.0 (+https://mychurchunlimited.com)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_speaker(title: str) -> tuple[str, str]:
    """
    Parse a YouTube title like "X | Pastor Brandon Ball | Church Unlimited"
    into (sermon_title, speaker).

    Handles:
      - "Title | Speaker | Church Unlimited"               (3 parts)
      - "Title | Series | Speaker | Church Unlimited"      (4 parts)
      - "Title"                                            (no separator)
      - Defaults speaker to "Church Unlimited" if unknown
    """
    parts = [p.strip() for p in title.split("|") if p.strip()]
    if len(parts) == 1:
        return parts[0], "Church Unlimited"
    if len(parts) == 2:
        # "Title | Something" — assume Something is speaker
        return parts[0], parts[1]
    # 3+ parts: last part is the channel/brand, second-to-last is the speaker
    sermon_title = parts[0]
    speaker = parts[-2]
    return sermon_title, speaker


def parse_iso_date(iso_str: str) -> str:
    """Convert YouTube's ISO timestamp to a YYYY-MM-DD date string."""
    # Format: 2026-05-04T04:44:03+00:00
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%d")


def build_videos(xml_bytes: bytes, count: int) -> list[dict]:
    """Parse the RSS XML and build the list of video dicts."""
    root = ET.fromstring(xml_bytes)
    entries = root.findall("atom:entry", NS)
    videos = []
    for entry in entries[:count]:
        title_el = entry.find("atom:title", NS)
        video_id_el = entry.find("yt:videoId", NS)
        published_el = entry.find("atom:published", NS)

        if title_el is None or video_id_el is None or published_el is None:
            continue

        raw_title = title_el.text or ""
        video_id = video_id_el.text or ""
        published = published_el.text or ""

        sermon_title, speaker = parse_speaker(raw_title)

        # media:group/media:description
        description = ""
        media_group = entry.find("media:group", NS)
        if media_group is not None:
            desc_el = media_group.find("media:description", NS)
            if desc_el is not None and desc_el.text:
                description = desc_el.text.strip()

        videos.append({
            "title": sermon_title,
            "speaker": speaker,
            "date": parse_iso_date(published),
            "description": description[:300],
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
            "videoId": video_id,
        })
    return videos


def build_payload(videos: list[dict]) -> dict:
    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "channel": "https://www.youtube.com/@Mychurchunlimited",
        "videos": videos,
    }


def write_if_changed(path: Path, payload: dict) -> bool:
    """Write the JSON file only if the videos array has actually changed.
    Returns True if a write happened, False if no change."""
    new_videos_json = json.dumps(payload["videos"], sort_keys=True)

    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            existing_videos_json = json.dumps(existing.get("videos", []), sort_keys=True)
            if existing_videos_json == new_videos_json:
                print(f"No change in videos. Skipping write.")
                return False
        except (json.JSONDecodeError, KeyError):
            # Existing file is malformed — overwrite.
            pass

    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path} ({len(payload['videos'])} videos)")
    return True


def main() -> int:
    output_path = Path(__file__).resolve().parent.parent / OUTPUT_FILENAME
    # If running inside the cu-sermon-data repo, the script lives in scripts/
    # and the JSON sits at the repo root — that's what parent.parent gives us.
    # If overridden via env, respect it.
    import os
    override = os.environ.get("CU_SERMON_OUTPUT")
    if override:
        output_path = Path(override)

    print(f"Fetching: {FEED_URL}")
    try:
        xml_bytes = fetch_feed(FEED_URL)
    except urllib.error.URLError as e:
        print(f"ERROR fetching feed: {e}", file=sys.stderr)
        return 1

    videos = build_videos(xml_bytes, VIDEO_COUNT)
    if not videos:
        print("ERROR: no videos parsed from feed", file=sys.stderr)
        return 1

    payload = build_payload(videos)
    print(f"Latest video: {videos[0]['title']} ({videos[0]['date']})")

    write_if_changed(output_path, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Church Unlimited - Sermon JSON Builder
=======================================
Fetches the latest videos from the Church Unlimited YouTube channel RSS feed
and writes latest-sermon.json in the shape the website expects.

FULL SUNDAY MESSAGES ONLY (2026-10-15). The channel also carries YouTube
Shorts (sermon-clip reels, promos), and those must never reach the site. Which
uploads count as messages, and why the published list is carried forward from
one build to the next, is decided in sermon_feed.py, shared with
build-watch.py. videos[0] is always the latest message.

Consumers: the /sermons "Latest Message" hero (videos[0]), the homepage cards
(first 3) and the /sermons "More Messages" grid (items 2-10, through the
latest-sermons edge function). Hence VIDEO_COUNT = 12.

Run locally:
    python build-sermons.py

Run in GitHub Actions:
    See .github/workflows/update-sermons.yml

No third-party dependencies. Stdlib only (urllib + xml.etree).
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sermon_feed  # noqa: E402  (shared message rules, same folder)

# ── CONFIG ──
CHANNEL_ID = sermon_feed.CHANNEL_ID
VIDEO_COUNT = 12
OUTPUT_FILENAME = "latest-sermon.json"

FEED_URL = sermon_feed.FEED_URL


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


def build_videos(xml_bytes: bytes, count: int, existing: list = ()) -> list[dict]:
    """Full messages from the feed, merged with the messages already published
    (existing), newest first. Shorts and other non-message uploads are skipped
    and logged."""
    fresh = []
    for entry in sermon_feed.parse_entries(xml_bytes):
        reason = sermon_feed.skip_reason(entry)
        if reason:
            print(f"  skip {entry['videoId']} ({reason}): {entry['title'][:70]}")
            continue
        sermon_title, speaker = parse_speaker(entry["title"])
        video_id = entry["videoId"]
        fresh.append({
            "title": sermon_title,
            "speaker": speaker,
            "date": parse_iso_date(entry["published"]),
            "description": sermon_feed.strip_chapters(entry["description"])[:300],
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
            "videoId": video_id,
        })
    return sermon_feed.merge_messages(fresh, list(existing), count)


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
        except (json.JSONDecodeError, KeyError, AttributeError):
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
    override = os.environ.get("CU_SERMON_OUTPUT")
    if override:
        output_path = Path(override)

    print(f"Fetching: {FEED_URL}")
    try:
        xml_bytes = sermon_feed.fetch_feed(FEED_URL)
    except Exception as e:
        print(f"ERROR fetching feed (published JSON left as is): {e}", file=sys.stderr)
        return 1

    existing = sermon_feed.load_existing(output_path)
    try:
        videos = build_videos(xml_bytes, VIDEO_COUNT, existing)
    except Exception as e:
        print(f"ERROR parsing feed (published JSON left as is): {e}", file=sys.stderr)
        return 1
    if not videos:
        print("ERROR: no full messages found; published JSON left as is", file=sys.stderr)
        return 1

    payload = build_payload(videos)
    print(f"Latest message: {videos[0]['title']} ({videos[0]['date']})")

    write_if_changed(output_path, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())

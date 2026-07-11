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
The Watch-page player can open at the preaching instead of the worship set.
The "start" value (seconds) for each video is resolved in this order:

  1. MANUAL CHAPTER (authoritative). If the video description contains a
     YouTube chapter labeled "Message" (a line like "12:34 Message"), that
     timestamp wins. Nothing else is consulted.

  2. AUTO-DETECT FROM CAPTIONS. Otherwise we pull the video's auto-captions
     with yt-dlp and look for the pastor's standard opening cue -- "jump in
     the word with me", "the title of our message today", and friends (see
     OPENING_CUES). The earliest cue inside a plausible window becomes the
     start, minus SAFETY_BUFFER seconds so the viewer lands just before the
     first sentence rather than just after it.

  3. FALL BACK TO 0. If no chapter and no cue is found (guest speaker, an
     unusual run of show, or captions YouTube has not generated yet), start
     stays 0 and the video simply plays from the beginning -- the same
     behavior as before this feature existed. Nothing breaks.

Measured on 10 services (Apr-Jul 2026), the message begins anywhere from
32:30 to 45:20 into the video -- a ~13 minute spread. That is exactly why a
single fixed offset for every video does not work, and why this is per-video.

Detection is CACHED: a video that already has a nonzero start in the existing
watch-videos.json is not re-examined. A video sitting at 0 IS retried on every
run, so a week where captions were not ready yet heals itself on the next build.

The full video on YouTube is never modified.

Run locally:
    python build-watch.py          (add --no-captions to skip detection)

Run in GitHub Actions:
    See .github/workflows/update-watch.yml

Dependencies: stdlib (urllib + xml.etree + re) for the feed. Caption detection
additionally shells out to yt-dlp if it is on PATH; if yt-dlp is missing,
detection is skipped and every video falls back to rule 3.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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

# -- CAPTION AUTO-DETECTION --
# Phrases the pastor uses to open the message. Matched against caption text
# with punctuation stripped, so write them lowercase and unpunctuated. All are
# searched and the earliest plausible hit wins. Add to this list freely.
OPENING_CUES = [
    "jump in the word with me",
    "in the word with me",
    "get in the word with me",
    "get in the word",
    "jump in the word",
    "the title of our message today",
    "title of our message",
    "our message today is called",
    "message today is called",
    "open your bible",
    "open your bibles",
]

# A detected cue is only trusted inside this window. Real message starts have
# ranged 32-45 min; we allow margin for guest speakers and short services but
# reject anything absurd (a stray phrase in announcements, or a callback near
# the end of the sermon).
MIN_START = 12 * 60        # 12:00 -- nothing legitimate starts before this
MAX_START_FRACTION = 0.70  # and never past 70% of the video's length

# Land the viewer just BEFORE the first sentence, not just after it. Clipping
# the opening line is far worse than serving a few extra seconds of lead-in.
SAFETY_BUFFER = 20         # seconds

CAPTION_TIMEOUT = 90       # per-video yt-dlp timeout, seconds

FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

# Matches H:MM:SS or M:SS / MM:SS timestamps.
TS_RE = re.compile(r"(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)")

# Word-level tokens out of a VTT, carrying the cue start time.
VTT_CUE_RE = re.compile(r"(\d\d):(\d\d):(\d\d)\.(\d\d\d)\s+-->")
WORD_RE = re.compile(r"[a-z']+")


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


def parse_speaker(title: str) -> tuple:
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


def have_ytdlp() -> bool:
    return shutil.which("yt-dlp") is not None


def fetch_captions(video_id: str) -> str:
    """Download auto-captions for a video and return the raw VTT text.
    Returns "" if captions are unavailable or yt-dlp fails -- never raises."""
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "yt-dlp", "--skip-download",
            "--write-auto-subs", "--write-subs",
            "--sub-lang", "en.*", "--sub-format", "vtt",
            "--no-warnings", "--ignore-no-formats-error",
            "-o", str(Path(tmp) / "%(id)s.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=CAPTION_TIMEOUT, check=False)
        except (subprocess.TimeoutExpired, OSError):
            return ""
        vtts = sorted(Path(tmp).glob("*.vtt"),
                      key=lambda p: (".en." not in p.name, len(p.name)))
        for v in vtts:
            try:
                return v.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
    return ""


def vtt_to_word_stream(vtt: str) -> list:
    """Return [(seconds, word), ...] with YouTube's rolling-caption duplication
    removed. Auto-captions repeat the tail of the previous cue at the head of
    the next one; naive parsing doubles most of the transcript and smears the
    timing."""
    words = []
    prev = []
    for block in re.split(r"\n\n+", vtt):
        m = VTT_CUE_RE.search(block)
        if not m:
            continue
        h, mi, s, ms = (int(g) for g in m.groups())
        t0 = h * 3600 + mi * 60 + s + ms / 1000.0
        body = "\n".join(l for l in block.split("\n") if "-->" not in l)
        body = re.sub(r"<[^>]+>", "", body)
        toks = WORD_RE.findall(body.lower())
        overlap = 0
        for k in range(min(len(prev), len(toks)), 0, -1):
            if prev[-k:] == toks[:k]:
                overlap = k
                break
        for w in toks[overlap:]:
            words.append((t0, w))
        prev = toks
    return words


def detect_message_start(vtt: str) -> int:
    """Find where the preaching begins by locating the pastor's opening cue.
    Returns 0 when nothing trustworthy is found."""
    words = vtt_to_word_stream(vtt)
    if not words:
        return 0
    duration = words[-1][0]
    ceiling = duration * MAX_START_FRACTION

    text_parts, offsets, pos = [], [], 0
    for t, w in words:
        offsets.append((pos, t))
        text_parts.append(w)
        pos += len(w) + 1
    text = " ".join(text_parts)

    def time_at(char_index: int) -> float:
        found = 0.0
        for p, t in offsets:
            if p <= char_index:
                found = t
            else:
                break
        return found

    hits = []
    for cue in OPENING_CUES:
        needle = " ".join(WORD_RE.findall(cue.lower()))
        if not needle:
            continue
        for m in re.finditer(re.escape(needle), text):
            t = time_at(m.start())
            if MIN_START <= t <= ceiling:
                hits.append(t)

    if not hits:
        return 0
    return max(0, int(min(hits) - SAFETY_BUFFER))


def resolve_start(video: dict, cached: dict, use_captions: bool) -> int:
    """Precedence: manual chapter > cached value > caption detection > 0."""
    if video["start"]:
        print(f"    {video['videoId']}: Message chapter in description -- "
              f"{video['start'] // 60}:{video['start'] % 60:02d}")
        return video["start"]

    prior = cached.get(video["videoId"], 0)
    if prior:
        return prior

    if not use_captions:
        return 0

    vtt = fetch_captions(video["videoId"])
    if not vtt:
        print(f"    {video['videoId']}: no captions yet -- start=0, will retry next run")
        return 0

    start = detect_message_start(vtt)
    if start:
        print(f"    {video['videoId']}: message detected at {start // 60}:{start % 60:02d}")
    else:
        print(f"    {video['videoId']}: no opening cue found -- start=0 (plays from beginning)")
    return start


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

    # Reuse start times already resolved, so each video is only detected once.
    # Videos still sitting at 0 get retried (self-healing).
    cached = {}
    if output_path.exists():
        try:
            old = json.loads(output_path.read_text(encoding="utf-8"))
            cached = {v["videoId"]: v.get("start", 0) for v in old.get("videos", [])}
        except (json.JSONDecodeError, KeyError, OSError):
            cached = {}

    use_captions = "--no-captions" not in sys.argv and have_ytdlp()
    if "--no-captions" in sys.argv:
        print("Caption detection disabled (--no-captions).")
    elif not use_captions:
        print("WARNING: yt-dlp not found on PATH -- skipping message detection.")
    else:
        print("Resolving message start times...")

    for v in videos:
        v["start"] = resolve_start(v, cached, use_captions)

    payload = build_payload(videos)
    detected = sum(1 for v in videos if v["start"])
    b = videos[0]
    print(f"Box 1: {b['title']} ({b['date']}) "
          f"start={b['start']}s ({b['start'] // 60}:{b['start'] % 60:02d})")
    print(f"Message start resolved for {detected}/{len(videos)} videos.")
    write_if_changed(output_path, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())

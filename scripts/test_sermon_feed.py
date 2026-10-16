#!/usr/bin/env python3
"""
Regression tests for the full-messages-only rule (stdlib unittest).

Run:  python -m unittest discover -s scripts -p "test_*.py" -v
Both workflows run this before building, so a broken rule never publishes.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sermon_feed  # noqa: E402


def load_script(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_sermons = load_script("build-sermons")
build_watch = load_script("build-watch")


def entry(title, description="", link=None, video_id="abcdefghijk", published="2026-10-12T04:40:48+00:00"):
    return {"videoId": video_id, "title": title, "published": published,
            "link": link or f"https://www.youtube.com/watch?v={video_id}", "description": description}


def feed_xml(entries):
    """A YouTube-style Atom feed for the given entries (newest first)."""
    from xml.sax.saxutils import escape
    rows = []
    for e in entries:
        rows.append(
            "<entry>"
            f"<id>yt:video:{e['videoId']}</id>"
            f"<yt:videoId>{e['videoId']}</yt:videoId>"
            f"<yt:channelId>{sermon_feed.CHANNEL_ID}</yt:channelId>"
            f"<title>{escape(e['title'])}</title>"
            f"<link rel=\"alternate\" href=\"{escape(e['link'])}\"/>"
            f"<published>{e['published']}</published>"
            f"<media:group><media:title>{escape(e['title'])}</media:title>"
            f"<media:description>{escape(e['description'])}</media:description></media:group>"
            "</entry>"
        )
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
            'xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">'
            + "".join(rows) + "</feed>").encode("utf-8")


# Real titles from the channel (2026-03 to 2026-10).
MESSAGES = [
    "The Sound of the Law | Pastor Brandon Ball | Church Unlimited",
    "The Unequal Exchange | Pastor Adam Newell | Church Unlimited",
    "The Last Piece of Wood | Easter 2026 | Pastor Brandon Ball | Church Unlimited",
    "Secure in Heaven, Strong on Earth | Cody Prickett | Church Unlimited",
    "You Can't Follow Yourself | Pastor Brandon Ball | Church Unlimited",
]
SHORTS = [
    ("God isn't inspecting you.",
     "God isn't inspecting you. He's inspecting your Lamb.\n\n#ChurchUnlimitedBHM #TheFoolishGospel #Shorts"),
    ("Faith Was Never a Tool | The Faith We Lost #Shorts", "For decades, faith has been taught as a force."),
    ("The law keeps a scorecard, and even on your best day it finds you short.", "Come rest in it with us."),
    ("Christianity was never about moving God higher on your priority list.", ""),
]

CHAPTERS = "0:00 Worship\n28:09 Welcome and giving\n31:42 Pastor Adam Newell: welcome and announcements\n35:58 The Unequal Exchange"


class RuleTests(unittest.TestCase):
    def test_real_message_titles_are_kept(self):
        for title in MESSAGES:
            self.assertIsNone(sermon_feed.skip_reason(entry(title)), title)

    def test_real_shorts_are_skipped_with_or_without_the_hashtag(self):
        for title, description in SHORTS:
            self.assertIsNotNone(sermon_feed.skip_reason(entry(title, description)), title)

    def test_shorts_tag_in_full_description_skips_a_message_titled_upload(self):
        long_body = "x" * 400 + " #Shorts"  # tag past the 300-char display cut
        reason = sermon_feed.skip_reason(entry(MESSAGES[0], long_body))
        self.assertEqual(reason, "Short (#shorts tag)")

    def test_shorts_link_skips_even_a_message_titled_upload(self):
        reason = sermon_feed.skip_reason(entry(MESSAGES[0], link="https://www.youtube.com/shorts/abcdefghijk"))
        self.assertEqual(reason, "Short (shorts link)")

    def test_title_must_end_with_church_unlimited(self):
        self.assertIsNotNone(sermon_feed.skip_reason(entry("Hook | The Sound of the Law | Sermon Clip")))
        self.assertIsNotNone(sermon_feed.skip_reason(entry("Christmas Eve | Church Unlimited")))

    def test_hashtag_word_boundary(self):
        self.assertIsNone(sermon_feed.skip_reason(entry(MESSAGES[0], "#shortstop is not a tag we use")))

    def test_carried_entries_rechecked(self):
        ok = {"videoId": "TMGzpyOlnRs", "date": "2026-10-12", "title": "The Sound of the Law",
              "speaker": "Pastor Brandon Ball", "description": "Paul warned the church"}
        self.assertIsNone(sermon_feed.carried_skip_reason(ok))
        self.assertIsNotNone(sermon_feed.carried_skip_reason(dict(ok, speaker="Church Unlimited")))
        self.assertIsNotNone(sermon_feed.carried_skip_reason(dict(ok, speaker="The Faith We Lost #Shorts")))
        self.assertIsNotNone(sermon_feed.carried_skip_reason(dict(ok, speaker="")))
        self.assertIsNotNone(sermon_feed.carried_skip_reason(dict(ok, date="")))

    def test_merge_dedupes_sorts_and_limits(self):
        fresh = [{"videoId": "A", "date": "2026-10-12", "speaker": "Pastor X", "title": "new"}]
        existing = [
            {"videoId": "A", "date": "2026-10-12", "speaker": "Pastor X", "title": "old copy"},
            {"videoId": "S", "date": "2026-10-13", "speaker": "Church Unlimited", "title": "a Short"},
            {"videoId": "B", "date": "2026-10-05", "speaker": "Pastor X", "title": "b"},
            {"videoId": "C", "date": "2026-09-28", "speaker": "Pastor X", "title": "c"},
        ]
        merged = sermon_feed.merge_messages(fresh, existing, 2)
        self.assertEqual([v["videoId"] for v in merged], ["A", "B"])
        self.assertEqual(merged[0]["title"], "new")

    def test_strip_chapters(self):
        self.assertEqual(sermon_feed.strip_chapters(CHAPTERS + "\nGrace is a gift."), "Grace is a gift.")


def channel_feed():
    """The channel as it looked on 2026-10-15: Shorts interleaved with messages."""
    rows = [
        ("JG2Jhtc-5eU", SHORTS[2][0], "Come rest in it with us this Sunday. #ChurchUnlimitedBHM #Shorts", "2026-10-14T20:00:00+00:00"),
        ("8R1ChdNL8mU", "Your good behavior was never the reason God chose to love you.", "#Shorts", "2026-10-13T20:00:00+00:00"),
        ("EU5P6PYfFIw", SHORTS[1][0], SHORTS[1][1], "2026-10-12T15:00:00+00:00"),
        ("TMGzpyOlnRs", MESSAGES[0], "Paul warned the church to beware of the dogs.\n\n0:00 Worship\n34:30 The Sound of the Law", "2026-10-12T04:40:48+00:00"),
        ("Mw6VwJ7gEBI", SHORTS[0][0], SHORTS[0][1], "2026-10-09T20:00:00+00:00"),
        ("J1kqqqPW-WU", SHORTS[3][0], "", "2026-10-05T20:00:00+00:00"),
        ("PMjlcQPbjgA", "Uncomplicated | Pastor Brandon Ball | Church Unlimited", "Under the Old Covenant.", "2026-10-05T04:40:00+00:00"),
    ]
    return feed_xml([entry(t, d, video_id=v, published=p) for v, t, d, p in rows])


EXISTING = [  # what the published JSON held: older messages, plus a Short from before the rule
    {"title": "Faith Was Never a Tool", "speaker": "The Faith We Lost #Shorts", "date": "2026-10-12",
     "description": "For decades", "start": 0, "url": "u", "thumbnail": "t", "videoId": "EU5P6PYfFIw"},
    {"title": "Uncomplicated", "speaker": "Pastor Brandon Ball", "date": "2026-10-05", "description": "d",
     "start": 2186, "url": "u", "thumbnail": "t", "videoId": "PMjlcQPbjgA"},
    {"title": "Preposterous", "speaker": "Pastor Brandon Ball", "date": "2026-09-28", "description": "d",
     "start": 1854, "url": "u", "thumbnail": "t", "videoId": "w__VPv0kL9c"},
    {"title": "Guarding Grace", "speaker": "Pastor Brandon Ball", "date": "2026-09-20", "description": "d",
     "start": 2264, "url": "u", "thumbnail": "t", "videoId": "ToNnJ8LNW5s"},
]


class BuilderTests(unittest.TestCase):
    def run_builder(self, module, out_env, existing, argv=("x",)):
        with tempfile.TemporaryDirectory() as tmp:
            feed = Path(tmp) / "feed.xml"
            feed.write_bytes(channel_feed())
            out = Path(tmp) / "out.json"
            out.write_text(json.dumps({"videos": existing}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"CU_FEED_FILE": str(feed), out_env: str(out)}), \
                    mock.patch.object(sys, "argv", list(argv)):
                code = module.main()
                first = json.loads(out.read_text(encoding="utf-8"))
                code_again = module.main()  # idempotent: nothing new, no rewrite
                second = json.loads(out.read_text(encoding="utf-8"))
            return code, first, code_again, second

    def test_build_sermons_latest_is_the_sunday_message_and_no_shorts(self):
        code, data, code_again, again = self.run_builder(build_sermons, "CU_SERMON_OUTPUT", EXISTING)
        self.assertEqual(code, 0)
        ids = [v["videoId"] for v in data["videos"]]
        self.assertEqual(ids, ["TMGzpyOlnRs", "PMjlcQPbjgA", "w__VPv0kL9c", "ToNnJ8LNW5s"])
        self.assertEqual(data["videos"][0]["title"], "The Sound of the Law")
        self.assertEqual(data["videos"][0]["speaker"], "Pastor Brandon Ball")
        self.assertNotIn("0:00", data["videos"][0]["description"])  # chapters stripped
        self.assertEqual(code_again, 0)
        self.assertEqual(again["updated"], data["updated"])  # second run wrote nothing

    def test_build_watch_keeps_start_times_and_no_shorts(self):
        code, data, code_again, again = self.run_builder(build_watch, "CU_WATCH_OUTPUT", EXISTING,
                                                         argv=("build-watch.py", "--no-captions"))
        self.assertEqual(code, 0)
        by_id = {v["videoId"]: v for v in data["videos"]}
        self.assertEqual(list(by_id), ["TMGzpyOlnRs", "PMjlcQPbjgA", "w__VPv0kL9c", "ToNnJ8LNW5s"])
        self.assertEqual(by_id["PMjlcQPbjgA"]["start"], 2186)   # carried, still in feed
        self.assertEqual(by_id["w__VPv0kL9c"]["start"], 1854)   # carried, no longer in feed
        self.assertEqual(again["updated"], data["updated"])

    def test_feed_failure_leaves_published_json_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "latest-sermon.json"
            out.write_text('{"keep": true}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"CU_SERMON_OUTPUT": str(out)}), \
                    mock.patch.object(build_sermons.sermon_feed, "fetch_feed", side_effect=OSError("HTTP 500")):
                self.assertEqual(build_sermons.main(), 1)
            self.assertEqual(out.read_text(encoding="utf-8"), '{"keep": true}')


if __name__ == "__main__":
    unittest.main()

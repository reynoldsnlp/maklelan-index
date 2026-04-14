#!/usr/bin/env python3
"""Scraper for Dan McClellan's (@maklelan) YouTube channel.

Uses only the Python standard library plus *requests* for HTTP and *html.parser*
(stdlib) for HTML entity decoding – no Selenium, Playwright, or official API key
required.

Workflow
--------
1. Fetch the channel /videos page and extract the embedded ``ytInitialData``
   JSON blob.
2. Walk the video grid to collect video IDs and metadata.  Follow continuation
   tokens to page through the full catalogue.
3. For each previously-unseen video fetch the watch page, locate the caption
   track URL embedded in ``ytInitialPlayerResponse``, and download the XML
   transcript.
4. Run the scripture-reference regex over the full concatenated transcript text
   to find Bible references with timestamps.
5. Merge results into ``docs/data/index.json`` and ``docs/data/videos.json`` and
   commit the changes (CI handles the actual ``git push``).
"""

from __future__ import annotations

import json
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Import the scripture-reference parser from the sibling module
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))
from bible_books import find_scripture_refs  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CHANNEL_HANDLE = "@maklelan"
CHANNEL_URL = f"https://www.youtube.com/{CHANNEL_HANDLE}/videos"
BROWSE_API = "https://www.youtube.com/youtubei/v1/browse"
YT_CLIENT_VERSION = "2.20240101.00.00"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Paths
_REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = _REPO_ROOT / "docs" / "data"

# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


# ---------------------------------------------------------------------------
# YouTube page extraction helpers
# ---------------------------------------------------------------------------


def _raw_decode_at(text: str, marker: str) -> dict | None:
    """Find *marker* in *text* then decode the JSON object that follows."""
    idx = text.find(marker)
    if idx == -1:
        return None
    obj_start = text.find("{", idx + len(marker))
    if obj_start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, obj_start)
        return obj
    except (json.JSONDecodeError, ValueError):
        return None


def extract_yt_initial_data(html: str) -> dict | None:
    """Extract the ``ytInitialData`` JSON object embedded in a YouTube page."""
    for marker in ("var ytInitialData = ", 'window["ytInitialData"] = ', "ytInitialData = "):
        obj = _raw_decode_at(html, marker)
        if obj:
            return obj
    return None


def extract_innertube_api_key(html: str) -> str:
    """Extract the InnerTube API key from a YouTube page (falls back to a known value)."""
    import re

    m = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', html)
    return m.group(1) if m else "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"


# ---------------------------------------------------------------------------
# Video list extraction
# ---------------------------------------------------------------------------


def _video_from_renderer(renderer: dict) -> dict | None:
    """Convert a ``videoRenderer`` dict into a simplified video metadata dict."""
    video_id = renderer.get("videoId")
    if not video_id:
        return None

    title = ""
    try:
        title = renderer["title"]["runs"][0]["text"]
    except (KeyError, IndexError, TypeError):
        try:
            title = renderer["title"]["simpleText"]
        except (KeyError, TypeError):
            pass

    published = ""
    try:
        published = renderer["publishedTimeText"]["simpleText"]
    except (KeyError, TypeError):
        pass

    thumbnail = ""
    try:
        thumbnail = renderer["thumbnail"]["thumbnails"][-1]["url"]
    except (KeyError, IndexError, TypeError):
        pass

    duration = ""
    try:
        duration = renderer["thumbnailOverlayTimeStatusRenderer"]["text"]["simpleText"]
    except (KeyError, TypeError):
        pass

    return {
        "id": video_id,
        "title": title,
        "published": published,
        "thumbnail": thumbnail,
        "duration": duration,
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def _extract_from_item(item: dict) -> tuple[dict | None, str | None]:
    """Return (video_dict | None, continuation_token | None) from a grid item."""
    video = None
    token = None

    renderer = (
        item.get("richItemRenderer", {}).get("content", {}).get("videoRenderer")
        or item.get("videoRenderer")
    )
    if renderer:
        video = _video_from_renderer(renderer)

    cont = item.get("continuationItemRenderer", {})
    if cont:
        try:
            token = (
                cont["continuationEndpoint"]["continuationCommand"]["token"]
            )
        except (KeyError, TypeError):
            pass

    return video, token


def parse_initial_video_grid(data: dict) -> tuple[list[dict], str | None]:
    """Walk ``ytInitialData`` and return (video_list, continuation_token)."""
    videos: list[dict] = []
    continuation: str | None = None

    try:
        tabs = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"]
    except (KeyError, TypeError):
        return videos, continuation

    for tab in tabs:
        tab_renderer = tab.get("tabRenderer", {})
        content = tab_renderer.get("content", {})
        grid = content.get("richGridRenderer") or {}
        if not grid:
            # Try older sectionListRenderer layout
            try:
                grid = (
                    content["sectionListRenderer"]["contents"][0]
                    ["itemSectionRenderer"]["contents"][0]
                    ["gridRenderer"]
                )
            except (KeyError, IndexError, TypeError):
                grid = {}

        items = grid.get("contents", [])
        if not items:
            continue

        for item in items:
            video, token = _extract_from_item(item)
            if video:
                videos.append(video)
            if token:
                continuation = token
        break  # We found the active tab

    return videos, continuation


def fetch_continuation_videos(
    session: requests.Session,
    token: str,
    api_key: str,
) -> tuple[list[dict], str | None]:
    """Fetch the next page of videos using a continuation token."""
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": YT_CLIENT_VERSION,
            }
        },
        "continuation": token,
    }
    try:
        resp = session.post(
            f"{BROWSE_API}?key={api_key}",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"  Continuation request failed: {exc}", flush=True)
        return [], None

    videos: list[dict] = []
    next_token: str | None = None

    try:
        items = (
            data["continuationContents"]["richGridContinuation"]["contents"]
        )
    except (KeyError, TypeError):
        items = []

    for item in items:
        video, token = _extract_from_item(item)
        if video:
            videos.append(video)
        if token:
            next_token = token

    return videos, next_token


# ---------------------------------------------------------------------------
# Transcript / closed-caption fetching
# ---------------------------------------------------------------------------


def _find_caption_track_url(html: str) -> str | None:
    """Extract the English caption track URL from a YouTube watch-page HTML."""
    idx = html.find('"captionTracks":[')
    if idx == -1:
        return None
    array_start = html.find("[", idx + len('"captionTracks":'))
    if array_start == -1:
        return None
    try:
        tracks, _ = json.JSONDecoder().raw_decode(html, array_start)
    except (json.JSONDecodeError, ValueError):
        return None

    preferred: str | None = None
    for track in tracks:
        lang = track.get("languageCode", "")
        url = track.get("baseUrl", "")
        if not url or not lang.startswith("en"):
            continue
        # Prefer manually-authored CC over auto-generated ("asr")
        if preferred is None or track.get("kind") != "asr":
            preferred = url

    return preferred


def _unescape_html(text: str) -> str:
    """Unescape HTML entities in transcript text using stdlib html.parser."""

    class _Collector(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self._buf: list[str] = []

        def handle_data(self, data: str) -> None:
            self._buf.append(data)

        def result(self) -> str:
            return "".join(self._buf)

    p = _Collector()
    p.feed(text)
    return p.result()


def parse_transcript_xml(xml_text: str) -> list[dict]:
    """Parse YouTube transcript XML into a list of timed segments.

    Returns ``[{"start": float, "dur": float, "text": str}, ...]``.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    segments: list[dict] = []
    for elem in root.findall("text"):
        start = float(elem.get("start", 0))
        dur = float(elem.get("dur", 0))
        raw = elem.text or ""
        text = _unescape_html(raw).strip()
        if text:
            segments.append({"start": start, "dur": dur, "text": text})
    return segments


def fetch_transcript(session: requests.Session, video_id: str) -> list[dict]:
    """Fetch the English transcript for *video_id*.  Returns [] if unavailable."""
    try:
        resp = session.get(
            f"https://www.youtube.com/watch?v={video_id}",
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"  Could not fetch watch page: {exc}", flush=True)
        return []

    url = _find_caption_track_url(resp.text)
    if not url:
        return []

    try:
        xml_resp = session.get(url, timeout=30)
        xml_resp.raise_for_status()
        return parse_transcript_xml(xml_resp.text)
    except Exception as exc:
        print(f"  Could not fetch transcript XML: {exc}", flush=True)
        return []


# ---------------------------------------------------------------------------
# Scripture reference extraction from transcript
# ---------------------------------------------------------------------------


def _timestamp_for_position(char_pos: int, position_map: list[tuple[int, float]]) -> float:
    """Binary-search *position_map* to find the segment timestamp for *char_pos*."""
    lo, hi = 0, len(position_map) - 1
    ts = 0.0
    while lo <= hi:
        mid = (lo + hi) // 2
        if position_map[mid][0] <= char_pos:
            ts = position_map[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return ts


def refs_from_transcript(transcript: list[dict]) -> list[dict]:
    """Find scripture references in *transcript*, deduplicated by (ref, second).

    Returns ``[{"canonical": str, "timestamp": int, "snippet": str}, ...]``.
    """
    if not transcript:
        return []

    # Build a single string of all transcript text and a position→timestamp map
    parts: list[str] = []
    position_map: list[tuple[int, float]] = []  # (char_start_of_segment, timestamp)
    pos = 0
    for seg in transcript:
        position_map.append((pos, seg["start"]))
        chunk = seg["text"] + " "
        parts.append(chunk)
        pos += len(chunk)

    full_text = "".join(parts)
    refs = find_scripture_refs(full_text)

    results: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for ref_info in refs:
        canonical = ref_info["canonical"]
        ts = int(_timestamp_for_position(ref_info["start"], position_map))
        key = (canonical, ts)
        if key in seen:
            continue
        seen.add(key)

        # Build a readable snippet around the match
        snip_start = max(0, ref_info["start"] - 60)
        snip_end = min(len(full_text), ref_info["end"] + 60)
        snippet = full_text[snip_start:snip_end].strip()

        results.append({"canonical": canonical, "timestamp": ts, "snippet": snippet})

    return results


# ---------------------------------------------------------------------------
# Data persistence
# ---------------------------------------------------------------------------


def load_data() -> tuple[dict, dict]:
    """Load (or initialise) index.json and videos.json from ``docs/data/``."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    index_file = DATA_DIR / "index.json"
    videos_file = DATA_DIR / "videos.json"

    if index_file.exists():
        with open(index_file, encoding="utf-8") as f:
            index = json.load(f)
    else:
        index = {"updated": None, "references": {}}

    if videos_file.exists():
        with open(videos_file, encoding="utf-8") as f:
            videos_data = json.load(f)
    else:
        videos_data = {"videos": {}, "processed": []}

    return index, videos_data


def save_data(index: dict, videos_data: dict) -> None:
    """Write index.json and videos.json to ``docs/data/``."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    index["updated"] = datetime.now(timezone.utc).isoformat()

    with open(DATA_DIR / "index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    with open(DATA_DIR / "videos.json", "w", encoding="utf-8") as f:
        json.dump(videos_data, f, indent=2, ensure_ascii=False)

    print(
        f"Saved {len(index['references'])} references "
        f"from {len(videos_data['videos'])} videos.",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print(f"Scraping {CHANNEL_URL}", flush=True)
    session = _make_session()
    index, videos_data = load_data()
    processed_ids: set[str] = set(videos_data.get("processed", []))

    # ── 1. Fetch channel page ────────────────────────────────────────────────
    print("Fetching channel page …", flush=True)
    try:
        resp = session.get(CHANNEL_URL, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"ERROR: Could not fetch channel page: {exc}", file=sys.stderr)
        sys.exit(1)

    api_key = extract_innertube_api_key(resp.text)
    yt_data = extract_yt_initial_data(resp.text)
    if not yt_data:
        print("ERROR: Could not extract ytInitialData from channel page.", file=sys.stderr)
        sys.exit(1)

    all_videos, continuation = parse_initial_video_grid(yt_data)
    print(f"  {len(all_videos)} videos on first page.", flush=True)

    # ── 2. Paginate through all videos ──────────────────────────────────────
    page = 1
    while continuation and page < 30:  # safety cap: 30 pages × ~30 videos/page ≈ 900 videos
        time.sleep(1.0)
        print(f"  Fetching page {page + 1} …", flush=True)
        more, continuation = fetch_continuation_videos(session, continuation, api_key)
        all_videos.extend(more)
        print(f"  Total so far: {len(all_videos)}", flush=True)
        page += 1

    print(f"Total videos found: {len(all_videos)}", flush=True)

    # ── 3. Update video metadata ─────────────────────────────────────────────
    new_videos: list[dict] = []
    for v in all_videos:
        vid_id = v["id"]
        videos_data.setdefault("videos", {})[vid_id] = {
            "id": vid_id,
            "title": v["title"],
            "published": v["published"],
            "thumbnail": v["thumbnail"],
            "duration": v["duration"],
            "url": v["url"],
        }
        if vid_id not in processed_ids:
            new_videos.append(v)

    print(f"New / unprocessed videos: {len(new_videos)}", flush=True)

    # ── 4. Process each new video ────────────────────────────────────────────
    for i, video in enumerate(new_videos, 1):
        vid_id = video["id"]
        print(
            f"[{i}/{len(new_videos)}] {video['title'][:70]}",
            flush=True,
        )

        transcript = fetch_transcript(session, vid_id)
        if not transcript:
            print("  No transcript – skipping.", flush=True)
            processed_ids.add(vid_id)
            continue

        print(f"  Transcript: {len(transcript)} segments", flush=True)
        refs = refs_from_transcript(transcript)
        print(f"  Found {len(refs)} scripture reference occurrences", flush=True)

        for ref in refs:
            canonical = ref["canonical"]
            entry = {
                "video_id": vid_id,
                "timestamp": ref["timestamp"],
                "snippet": ref["snippet"],
            }
            bucket = index["references"].setdefault(canonical, [])
            # Avoid duplicate entries
            already = any(
                e["video_id"] == vid_id and e["timestamp"] == ref["timestamp"]
                for e in bucket
            )
            if not already:
                bucket.append(entry)

        processed_ids.add(vid_id)
        time.sleep(0.75)  # polite delay between video requests

    # ── 5. Persist ───────────────────────────────────────────────────────────
    videos_data["processed"] = sorted(processed_ids)
    save_data(index, videos_data)
    print("Done.", flush=True)


if __name__ == "__main__":
    main()

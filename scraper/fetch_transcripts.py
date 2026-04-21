#!/usr/bin/env python3
"""Fetch transcripts for unprocessed videos.

Reads ``docs/data/videos.json`` to find videos that still need transcripts,
fetches them, and saves each transcript to ``data/transcripts/<video_id>.json``.
Updates per-video ``processed`` status in ``videos.json`` (to ``"failed"`` on
failure; transcript-fetched videos keep their status for ``build_index.py``
to finalise).

Usage::

    python scraper/fetch_transcripts.py [--max N]

The default limit is 49 transcripts per run.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import requests
from innertube import InnerTube
from youtube_transcript_api import YouTubeTranscriptApi

sys.path.insert(0, str(Path(__file__).parent))
import build_index  # noqa: E402
from common import (  # noqa: E402
    PLAYER_CLIENT_TYPES,
    STATUS_FAILED,
    STATUS_NO_REFS,
    STATUS_NOT_ATTEMPTED,
    STATUS_YES,
    TRANSCRIPT_DIR,
    extract_publish_date,
    extract_yt_initial_player_response,
    load_data,
    make_session,
    polite_sleep,
    save_videos,
    _DELAY_AFTER_FAILURE,
    _DELAY_BETWEEN_FALLBACKS,
    _DELAY_BETWEEN_VIDEOS,
)

# Default maximum number of transcripts to fetch in a single run
DEFAULT_MAX_TRANSCRIPTS = 49

# Stop processing after this many consecutive failures (likely IP ban)
_MAX_CONSECUTIVE_FAILURES = 5


# ---------------------------------------------------------------------------
# Caption URL helpers
# ---------------------------------------------------------------------------


def _pick_english_caption_url(tracks: list[dict]) -> str | None:
    """Select the best English caption track URL from a list of track dicts."""
    preferred: str | None = None
    for track in tracks:
        lang = track.get("languageCode", "")
        url = track.get("baseUrl", "")
        if not url or not lang.startswith("en"):
            continue
        if preferred is None or track.get("kind") != "asr":
            preferred = url
    return preferred


def _caption_url_from_player_response(player_data: dict) -> str | None:
    try:
        tracks = (
            player_data["captions"]["playerCaptionsTracklistRenderer"]["captionTracks"]
        )
        return _pick_english_caption_url(tracks)
    except (KeyError, TypeError):
        return None


def _caption_url_from_watch_page(html: str) -> str | None:
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
    return _pick_english_caption_url(tracks)


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def _unescape_html(text: str) -> str:
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


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _load_cached_transcript(video_id: str) -> list[dict] | None:
    path = TRANSCRIPT_DIR / f"{video_id}.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            print(f"  Warning: could not read cached transcript for {video_id}: {exc}", flush=True)
    return None


def _save_transcript(video_id: str, transcript: list[dict]) -> None:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"{video_id}.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(transcript, f, ensure_ascii=False)
    except Exception as exc:
        print(f"  Warning: could not cache transcript: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Transcript fetching
# ---------------------------------------------------------------------------


def fetch_transcript(
    session: requests.Session,
    innertube_clients: list[tuple[str, InnerTube]],
    video_id: str,
    *,
    yt_api: YouTubeTranscriptApi,
) -> tuple[list[dict], str]:
    """Fetch the English transcript for *video_id*.

    Returns ``(transcript_segments, publish_date)``.  *publish_date* is an
    ISO date string (``"2024-03-15"``) extracted from the player response,
    or ``""`` if unavailable.

    1. Return from local cache if available.
    2. Try ``youtube-transcript-api``.
    3. Try each InnerTube client type.
    4. Fall back to scraping the watch-page HTML.
    """
    publish_date = ""

    cached = _load_cached_transcript(video_id)
    if cached is not None:
        return cached, publish_date

    caption_url: str | None = None

    # youtube-transcript-api
    try:
        fetched = yt_api.fetch(video_id, languages=("en", "en-US", "en-GB"))
        transcript = [{"start": seg.start, "dur": seg.duration, "text": seg.text} for seg in fetched]
        if transcript:
            _save_transcript(video_id, transcript)
            return transcript, publish_date
    except Exception as exc:
        print(f"  youtube-transcript-api failed: {exc}", flush=True)
        polite_sleep(_DELAY_BETWEEN_FALLBACKS)

    # InnerTube clients
    for client_type, client in innertube_clients:
        try:
            player_data = client.player(video_id)
            if not publish_date:
                publish_date = extract_publish_date(player_data)
            caption_url = _caption_url_from_player_response(player_data)
            if caption_url:
                print(f"  Caption URL found via InnerTube({client_type})", flush=True)
                break
        except Exception as exc:
            print(f"  innertube player({client_type}) failed: {exc}", flush=True)
        polite_sleep(_DELAY_BETWEEN_FALLBACKS)

    # Watch-page fallback
    if not caption_url:
        polite_sleep(_DELAY_BETWEEN_FALLBACKS)
        try:
            resp = session.get(
                f"https://www.youtube.com/watch?v={video_id}",
                timeout=30,
            )
            resp.raise_for_status()
            player_response = extract_yt_initial_player_response(resp.text)
            if player_response:
                if not publish_date:
                    publish_date = extract_publish_date(player_response)
                caption_url = _caption_url_from_player_response(player_response)
            if not caption_url:
                caption_url = _caption_url_from_watch_page(resp.text)
            if caption_url:
                print("  Caption URL found via watch-page fallback", flush=True)
        except Exception as exc:
            print(f"  Could not fetch watch page: {exc}", flush=True)

    if not caption_url:
        return [], publish_date

    polite_sleep(_DELAY_BETWEEN_FALLBACKS)
    try:
        xml_resp = session.get(caption_url, timeout=30)
        xml_resp.raise_for_status()
        transcript = parse_transcript_xml(xml_resp.text)
        if transcript:
            _save_transcript(video_id, transcript)
        return transcript, publish_date
    except Exception as exc:
        print(f"  Could not fetch transcript XML: {exc}", flush=True)
        return [], publish_date


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _fetch_publish_date(
    innertube_clients: list[tuple[str, InnerTube]],
    video_id: str,
) -> str:
    """Make a lightweight player request solely to get the publish date."""
    for client_type, client in innertube_clients:
        try:
            player_data = client.player(video_id)
            date = extract_publish_date(player_data)
            if date:
                return date
        except Exception:
            pass
        polite_sleep(_DELAY_BETWEEN_FALLBACKS)
    return ""


def _backfill_dates(videos_data: dict, max_count: int) -> None:
    """Fetch real publish dates for videos that are missing them."""
    innertube_clients: list[tuple[str, InnerTube]] = [
        (t, InnerTube(t)) for t in PLAYER_CLIENT_TYPES
    ]

    missing = [
        rec for rec in videos_data.get("videos", {}).values()
        if not rec.get("published_date")
        and (TRANSCRIPT_DIR / f"{rec['id']}.json").exists()
    ]
    if not missing:
        print(
            "No videos with transcripts are missing publish dates – "
            "skipping backfill.",
            flush=True,
        )
        return

    if len(missing) > max_count:
        print(
            f"Capping date backfill to {max_count} of {len(missing)} videos.",
            flush=True,
        )
        missing = missing[:max_count]

    print(f"Backfilling publish dates for {len(missing)} videos …", flush=True)
    filled = 0
    for i, rec in enumerate(missing, 1):
        vid_id = rec["id"]
        print(f"  [{i}/{len(missing)}] {rec.get('title', vid_id)[:60]}", end="", flush=True)
        date = _fetch_publish_date(innertube_clients, vid_id)
        if date:
            videos_data["videos"][vid_id]["published_date"] = date
            filled += 1
            print(f" → {date}", flush=True)
            save_videos(videos_data)
        else:
            print(" → no date found", flush=True)
        polite_sleep(_DELAY_BETWEEN_VIDEOS)

    print(f"Backfilled {filled} publish dates.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch transcripts for unprocessed videos.")
    parser.add_argument(
        "--max", type=int, default=DEFAULT_MAX_TRANSCRIPTS,
        help=f"Maximum number of transcripts to fetch (default: {DEFAULT_MAX_TRANSCRIPTS})",
    )
    parser.add_argument(
        "--no-backfill-dates", action="store_true",
        help="Skip backfilling publish dates for videos that are missing them.",
    )
    args = parser.parse_args()
    max_transcripts: int = args.max

    _, videos_data = load_data()

    if not args.no_backfill_dates:
        _backfill_dates(videos_data, max_transcripts)

    session = make_session()
    innertube_clients: list[tuple[str, InnerTube]] = [
        (t, InnerTube(t)) for t in PLAYER_CLIENT_TYPES
    ]
    yt_api = YouTubeTranscriptApi()

    to_process = [
        rec for rec in videos_data.get("videos", {}).values()
        if rec.get("processed") in (STATUS_NOT_ATTEMPTED, STATUS_FAILED)
        and not (TRANSCRIPT_DIR / f"{rec['id']}.json").exists()
    ]

    if len(to_process) > max_transcripts:
        print(
            f"Capping to {max_transcripts} videos this run "
            f"({len(to_process)} pending).",
            flush=True,
        )
        to_process = to_process[:max_transcripts]

    print(f"Videos to fetch transcripts for: {len(to_process)}", flush=True)

    consecutive_failures = 0
    fetched_count = 0
    for i, video in enumerate(to_process, 1):
        vid_id = video["id"]
        vid_record = videos_data["videos"][vid_id]
        print(f"[{i}/{len(to_process)}] {video.get('title', vid_id)[:70]}", flush=True)

        transcript, publish_date = fetch_transcript(session, innertube_clients, vid_id, yt_api=yt_api)

        # Save publish date if we got one and the record doesn't have one yet
        if publish_date and not vid_record.get("published_date"):
            vid_record["published_date"] = publish_date
            print(f"  Published: {publish_date}", flush=True)

        if not transcript:
            consecutive_failures += 1
            print(
                f"  No transcript – marking failed "
                f"({consecutive_failures}/{_MAX_CONSECUTIVE_FAILURES} consecutive).",
                flush=True,
            )
            vid_record["processed"] = STATUS_FAILED
            save_videos(videos_data)
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                print(
                    f"\n⚠ {_MAX_CONSECUTIVE_FAILURES} consecutive failures – "
                    f"likely IP-blocked. Stopping early to preserve progress.",
                    flush=True,
                )
                break
            polite_sleep(_DELAY_AFTER_FAILURE, consecutive_failures=consecutive_failures)
            continue

        consecutive_failures = 0
        fetched_count += 1
        print(f"  Transcript: {len(transcript)} segments", flush=True)
        save_videos(videos_data)
        polite_sleep(_DELAY_BETWEEN_VIDEOS)

    print(f"Done. Fetched {fetched_count} transcripts.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.", flush=True)
    build_index.main()

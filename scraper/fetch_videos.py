#!/usr/bin/env python3
"""Fetch the full video catalogue from Dan McClellan's YouTube channel.

Updates ``docs/data/videos.json`` with metadata for every video on the
channel, preserving existing per-video ``processed`` status.

Usage::

    python scraper/fetch_videos.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from innertube import InnerTube

sys.path.insert(0, str(Path(__file__).parent))
import build_index  # noqa: E402
from common import (  # noqa: E402
    CHANNEL_URL,
    STATUS_NOT_ATTEMPTED,
    STATUS_YES,
    STATUS_NO_REFS,
    extract_yt_initial_data,
    load_data,
    make_session,
    polite_sleep,
    save_videos,
    _DELAY_BETWEEN_PAGES,
)


# ---------------------------------------------------------------------------
# Video list extraction
# ---------------------------------------------------------------------------


def _parse_short_view_count(text: str) -> int:
    """Parse short view counts like '123K views' or '1.2M views' into integers."""
    m = re.match(r"([\d.]+)\s*([KMB]?)", text, re.IGNORECASE)
    if not m:
        return 0
    num = float(m.group(1))
    suffix = m.group(2).upper()
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return int(num * multiplier)


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

    view_count = 0
    try:
        vct = renderer["viewCountText"]["simpleText"]  # e.g. "123,456 views"
        view_count = int(re.sub(r"[^\d]", "", vct))
    except (KeyError, TypeError, ValueError):
        try:
            vct = renderer["shortViewCountText"]["simpleText"]  # e.g. "123K views"
            view_count = _parse_short_view_count(vct)
        except (KeyError, TypeError, ValueError):
            pass

    return {
        "id": video_id,
        "title": title,
        "published": published,
        "thumbnail": thumbnail,
        "duration": duration,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "view_count": view_count,
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
            token = cont["continuationEndpoint"]["continuationCommand"]["token"]
        except (KeyError, TypeError):
            pass

    return video, token


def _items_from_continuation_response(data: dict) -> tuple[list[dict], str | None]:
    """Extract (items, next_continuation_token) from a browse continuation response."""
    items: list[dict] = []

    try:
        items = data["continuationContents"]["richGridContinuation"]["contents"]
    except (KeyError, TypeError):
        pass

    if not items:
        for action in data.get("onResponseReceivedActions", []):
            action_items = (
                action.get("appendContinuationItemsAction", {}).get("continuationItems", [])
            )
            if action_items:
                items = action_items
                break

    videos: list[dict] = []
    next_token: str | None = None
    for item in items:
        video, token = _extract_from_item(item)
        if video:
            videos.append(video)
        if token:
            next_token = token

    return videos, next_token


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
        break

    return videos, continuation


def fetch_all_videos(session, innertube_client) -> list[dict]:
    """Fetch every video from the channel by paginating through all pages."""
    print("Fetching channel page …", flush=True)
    try:
        resp = session.get(CHANNEL_URL, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"ERROR: Could not fetch channel page: {exc}", file=sys.stderr)
        sys.exit(1)

    yt_data = extract_yt_initial_data(resp.text)
    if not yt_data:
        print("ERROR: Could not extract ytInitialData from channel page.", file=sys.stderr)
        sys.exit(1)

    all_videos, continuation = parse_initial_video_grid(yt_data)
    print(f"  {len(all_videos)} videos on first page.", flush=True)

    page = 1
    page_failures = 0
    while continuation:
        polite_sleep(_DELAY_BETWEEN_PAGES, consecutive_failures=page_failures)
        print(f"  Fetching page {page + 1} …", flush=True)
        try:
            data = innertube_client.browse(continuation=continuation)
            page_failures = 0
        except Exception as exc:
            page_failures += 1
            print(f"  Continuation request failed: {exc}", flush=True)
            if page_failures >= 3:
                print("  Too many consecutive page failures – stopping pagination.", flush=True)
                break
            continue

        more, continuation = _items_from_continuation_response(data)
        if not more and not continuation:
            print("  No more videos found in continuation response.", flush=True)
            break
        all_videos.extend(more)
        print(f"  Total so far: {len(all_videos)}", flush=True)
        page += 1

    print(f"Total videos found: {len(all_videos)}", flush=True)
    return all_videos


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print(f"Scraping {CHANNEL_URL}", flush=True)
    session = make_session()
    innertube_client = InnerTube("WEB")
    _, videos_data = load_data()

    all_videos = fetch_all_videos(session, innertube_client)

    for v in all_videos:
        vid_id = v["id"]
        existing = videos_data.setdefault("videos", {}).get(vid_id, {})
        videos_data["videos"][vid_id] = {
            "id": vid_id,
            "title": v["title"],
            "published": v["published"],
            "published_date": existing.get("published_date", ""),
            "thumbnail": v["thumbnail"],
            "duration": v["duration"],
            "url": v["url"],
            "view_count": v.get("view_count", existing.get("view_count", 0)),
            "processed": existing.get("processed", STATUS_NOT_ATTEMPTED),
        }

    save_videos(videos_data)

    n_total = len(videos_data["videos"])
    n_pending = sum(
        1 for r in videos_data["videos"].values()
        if r.get("processed") not in (STATUS_YES, STATUS_NO_REFS)
    )
    print(f"Saved {n_total} videos ({n_pending} pending processing).", flush=True)


if __name__ == "__main__":
    main()
    build_index.main()

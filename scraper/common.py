"""Shared constants, helpers, and HTTP session setup for the scraper modules."""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CHANNEL_HANDLE = "@maklelan"
CHANNEL_URL = f"https://www.youtube.com/{CHANNEL_HANDLE}/videos"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Per-video processed status constants
STATUS_NOT_ATTEMPTED = "not_attempted"
STATUS_YES = "yes"
STATUS_NO_REFS = "no_refs"
STATUS_FAILED = "failed"

# Paths
_REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = _REPO_ROOT / "docs" / "data"
TRANSCRIPT_DIR = _REPO_ROOT / "data" / "transcripts"

# Rate-limiting defaults (seconds)
_DELAY_BETWEEN_PAGES = 2.0
_DELAY_BETWEEN_VIDEOS = 5.0
_DELAY_BETWEEN_FALLBACKS = 2.0
_DELAY_AFTER_FAILURE = 10.0
_JITTER_FRACTION = 0.5

# InnerTube client types to try in order when fetching player data
PLAYER_CLIENT_TYPES = ["WEB", "ANDROID", "TVHTML5"]


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


# ---------------------------------------------------------------------------
# Rate-limiting helper
# ---------------------------------------------------------------------------


def polite_sleep(base: float, *, consecutive_failures: int = 0) -> None:
    """Sleep for *base* seconds ± jitter, with exponential back-off on failures."""
    delay = base * (2 ** min(consecutive_failures, 5))
    delay = min(delay, 120.0)
    jitter = delay * _JITTER_FRACTION
    actual = delay + random.uniform(-jitter, jitter)
    actual = max(0.5, actual)
    time.sleep(actual)


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


def extract_yt_initial_player_response(html: str) -> dict | None:
    """Extract the ``ytInitialPlayerResponse`` JSON object embedded in a YouTube watch page."""
    for marker in (
        "var ytInitialPlayerResponse = ",
        'window["ytInitialPlayerResponse"] = ',
        "ytInitialPlayerResponse = ",
    ):
        obj = _raw_decode_at(html, marker)
        if obj:
            return obj
    return None


# ---------------------------------------------------------------------------
# Publish date extraction
# ---------------------------------------------------------------------------


def extract_publish_date(player_data: dict) -> str:
    """Extract the ISO 8601 publish date from an InnerTube player response.

    Looks in ``microformat.playerMicroformatRenderer.publishDate``.
    Returns a date string like ``"2024-03-15"`` or ``""`` if not found.
    """
    try:
        raw = player_data["microformat"]["playerMicroformatRenderer"]["publishDate"]
        # raw is like "2024-03-15T08:52:53-07:00"; keep just the date part
        return raw[:10]
    except (KeyError, TypeError, IndexError):
        return ""


# ---------------------------------------------------------------------------
# Data persistence
# ---------------------------------------------------------------------------


def load_data() -> tuple[dict, dict]:
    """Load (or initialise) index.json and videos.json from ``docs/data/``.

    Migrates legacy ``videos_data["processed"]`` list to per-video keys.
    """
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
        videos_data = {"videos": {}}

    # Migrate legacy top-level "processed" list → per-video status field.
    videos_data.pop("processed", None)
    for rec in videos_data.get("videos", {}).values():
        if "processed" not in rec:
            rec["processed"] = STATUS_NOT_ATTEMPTED

    return index, videos_data


def save_videos(videos_data: dict) -> None:
    """Write videos.json to ``docs/data/``."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "videos.json", "w", encoding="utf-8") as f:
        json.dump(videos_data, f, indent=2, ensure_ascii=False)


def save_index(index: dict) -> None:
    """Write index.json to ``docs/data/``."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    index["updated"] = datetime.now(timezone.utc).isoformat()
    with open(DATA_DIR / "index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

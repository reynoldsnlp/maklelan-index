#!/usr/bin/env python3
"""Fetch transcripts via headless Playwright with anti-detection measures.

Functionally equivalent to ``fetch_transcripts.py`` — same inputs
(``docs/data/videos.json``), same outputs (``data/transcripts/<id>.json``
and updated per-video ``processed`` status), same CLI flags, same
``_MAX_CONSECUTIVE_FAILURES`` stop condition, same tail call to
``build_index.main()``.

The important difference is *how* the transcript is obtained.  Rather
than calling YouTube's JSON APIs directly, this script drives a real
Chromium instance and interacts like a person:

* Lands on https://www.youtube.com first (persistent profile keeps
  cookies across runs).
* Accepts the consent banner if present.
* Clicks the search box and types ``"maklelan <title words>"`` one
  character at a time with realistic per-keystroke jitter and the
  occasional longer pause.
* Scrolls down through results, hovers the matching thumbnail, then
  clicks it.
* Spends ~10-20 seconds on the watch page (scrolls, lets the video
  play, wiggles the mouse) before pulling the transcript.
* Extracts the caption XML URL from ``window.ytInitialPlayerResponse``
  and fetches it using the browser's own request context so cookies
  and Client Hints headers match what the watch page used.  Falls back
  to scraping the on-page "Show transcript" panel.

Budget ~30 seconds per video; that is the point, not a bug.  The goal
is sustainable scraping that does not trip bot detection.

Install once::

    pip install playwright
    playwright install chromium

Usage::

    python scraper/fetch_transcripts_playwright.py [--max N]
                                                    [--no-backfill-dates]
                                                    [--head]
                                                    [--profile-dir DIR]

By default the script runs until stopped (Ctrl+C) or an unrecoverable
error occurs — at which point the indexer is run over whatever
transcripts have been saved so far, then the script exits.  Pass
``--max N`` to cap a run at a specific count.  Run with ``--head``
to watch the browser (useful for debugging selector drift after a
YouTube UI change).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PwTimeoutError,
    sync_playwright,
)

sys.path.insert(0, str(Path(__file__).parent))
import build_index  # noqa: E402
from common import (  # noqa: E402
    STATUS_FAILED,
    STATUS_NOT_ATTEMPTED,
    TRANSCRIPT_DIR,
    extract_publish_date,
    load_data,
    save_videos,
)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Default on-disk location for the Chromium user-data-dir.  A persistent
# profile is a strong anti-detection signal: real users have cookies,
# history, cached JS, a consent decision, etc.
_DEFAULT_PROFILE_DIR = (
    Path(__file__).parent.parent / "data" / "playwright-profile"
)

# Recent stable Chrome on macOS Intel.  Keep this in rough sync with
# the --user-agent reality of real browsers; an old UA is itself a tell.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_CH_UA = (
    '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'
)

VIEWPORT = {"width": 1366, "height": 768}

# Stealth init script — runs before any page script on every navigation.
# Patches the most commonly-checked "is this headless" tells.  This is
# not a silver bullet; it is the baseline that stops the trivial checks.
_STEALTH_JS = r"""
// Drop the navigator.webdriver === true giveaway.
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// Headless Chromium exposes an empty plugins array and [] languages.
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
});
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        {name: 'PDF Viewer'},
        {name: 'Chrome PDF Viewer'},
        {name: 'Chromium PDF Viewer'},
        {name: 'Microsoft Edge PDF Viewer'},
        {name: 'WebKit built-in PDF'},
    ],
});
Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

// Restore a plausible window.chrome object.
window.chrome = window.chrome || {
    runtime: {},
    loadTimes: function () {},
    csi: function () {},
    app: {isInstalled: false},
};

// Normalise the permissions.query() behaviour that headless leaks.
const _origQuery = window.navigator.permissions &&
                   window.navigator.permissions.query;
if (_origQuery) {
    window.navigator.permissions.query = (p) => (
        p.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : _origQuery(p)
    );
}

// WebGL vendor/renderer strings; default headless strings are a tell.
const _getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (p) {
    if (p === 37445) return 'Intel Inc.';                 // UNMASKED_VENDOR_WEBGL
    if (p === 37446) return 'Intel Iris OpenGL Engine';   // UNMASKED_RENDERER_WEBGL
    return _getParameter.call(this, p);
};
"""


# ---------------------------------------------------------------------------
# Human-timing helpers
# ---------------------------------------------------------------------------


def _sleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def _human_type(page: Page, text: str) -> None:
    """Type *text* into the currently-focused element with human jitter."""
    for ch in text:
        page.keyboard.type(ch)
        r = random.random()
        if r < 0.03:
            _sleep(0.45, 1.1)          # occasional "thinking" pause
        elif r < 0.07:
            _sleep(0.22, 0.40)         # small hesitation
        else:
            _sleep(0.055, 0.18)        # normal keystroke cadence


def _wiggle_mouse(page: Page, moves: int | None = None) -> None:
    """Move the cursor around a few times at organic speeds."""
    vp = page.viewport_size or VIEWPORT
    for _ in range(moves if moves is not None else random.randint(2, 5)):
        x = random.randint(40, vp["width"] - 40)
        y = random.randint(80, vp["height"] - 80)
        page.mouse.move(x, y, steps=random.randint(12, 32))
        _sleep(0.05, 0.25)


def _scroll(page: Page, total_px: int) -> None:
    """Scroll vertically by *total_px* pixels in small, irregular steps."""
    direction = 1 if total_px >= 0 else -1
    remaining = abs(total_px)
    while remaining > 0:
        step = min(random.randint(70, 260), remaining)
        page.mouse.wheel(0, step * direction)
        remaining -= step
        _sleep(0.12, 0.4)


def _hover_and_click(page: Page, locator) -> None:
    """Scroll into view, wait, hover, wait, then click — never teleport-click."""
    locator.scroll_into_view_if_needed()
    _sleep(0.35, 0.85)
    try:
        locator.hover()
    except Exception:
        pass
    _sleep(0.18, 0.55)
    locator.click()


# ---------------------------------------------------------------------------
# Browser setup
# ---------------------------------------------------------------------------


def _launch_context(
    pw: Playwright,
    profile_dir: Path,
    *,
    headless: bool,
) -> BrowserContext:
    profile_dir.mkdir(parents=True, exist_ok=True)
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        user_agent=USER_AGENT,
        viewport=VIEWPORT,
        screen=VIEWPORT,
        locale="en-US",
        timezone_id="America/Denver",
        color_scheme="light",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-default-browser-check",
            "--no-first-run",
            "--password-store=basic",
            f"--window-size={VIEWPORT['width']},{VIEWPORT['height']}",
        ],
        # --enable-automation is what Playwright adds by default; stripping
        # it removes the "Chrome is being controlled by automated test
        # software" banner flag that sites can read.
        ignore_default_args=["--enable-automation"],
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": _CH_UA,
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
        },
    )
    context.add_init_script(_STEALTH_JS)
    return context


# ---------------------------------------------------------------------------
# YouTube navigation
# ---------------------------------------------------------------------------


def _accept_cookies_if_needed(page: Page) -> None:
    """Click the EU consent banner button if present."""
    labels = re.compile(
        r"(Accept all|I agree|Reject all|Accept the use of cookies)", re.I
    )
    try:
        btn = page.get_by_role("button", name=labels).first
        if btn.count() > 0:
            _hover_and_click(page, btn)
            _sleep(1.0, 2.0)
    except Exception:
        pass


def _go_home(page: Page) -> None:
    """Navigate to the YouTube homepage and idle briefly."""
    page.goto("https://www.youtube.com/", wait_until="domcontentloaded")
    _accept_cookies_if_needed(page)
    _sleep(1.5, 3.0)
    _wiggle_mouse(page)
    _scroll(page, random.randint(150, 450))
    _sleep(0.6, 1.4)
    _scroll(page, -random.randint(100, 300))
    _sleep(0.4, 1.0)


def _focus_searchbox(page: Page) -> None:
    # Several selectors covering historical and current markup.
    selectors = [
        'input#search',
        'input[name="search_query"]',
        'ytd-searchbox input',
        'yt-searchbox input',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click()
                return
        except Exception:
            continue
    raise RuntimeError("Could not locate YouTube search box.")


def _do_search(page: Page, query: str) -> None:
    _focus_searchbox(page)
    _sleep(0.25, 0.6)
    # Clear any prior query.
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    _sleep(0.12, 0.3)
    _human_type(page, query)
    _sleep(0.45, 1.05)
    page.keyboard.press("Enter")
    try:
        page.wait_for_url(re.compile(r"/results\?"), timeout=15_000)
    except PwTimeoutError:
        pass
    page.wait_for_load_state("domcontentloaded")
    _sleep(1.2, 2.6)


def _click_search_result(page: Page, video_id: str) -> bool:
    """Click the search result whose href matches *video_id*.

    Behaviour: check the viewport once.  If the target isn't in view,
    scroll down a single human-sized step and re-check.  Repeat until
    either the target is in the viewport (then click it, without any
    re-centering scroll), a shelf boundary appears in the viewport
    (signalling we've left the original result set), or ``scrollY``
    stops advancing (bottom of page).  No upward scrolling ever.
    """
    # Prefer the title link — clicking the thumbnail is blocked by
    # YouTube's hover video preview, which inserts a <video> element
    # over the thumbnail and intercepts pointer events.
    anchor_sel = (
        f'a#video-title[href*="/watch?v={video_id}"], '
        f'a#video-title-link[href*="/watch?v={video_id}"], '
        f'h3 a[href*="/watch?v={video_id}"]'
    )
    # Shelves / card lists are inserted after the original video
    # result list on search pages; once one is in view we've scrolled
    # past the primary results.
    boundary_sel = (
        "ytd-shelf-renderer, "
        "ytd-horizontal-card-list-renderer, "
        "ytd-reel-shelf-renderer, "
        "grid-shelf-view-model"
    )

    _IN_VIEWPORT_JS = """(sel) => {
        const vh = window.innerHeight;
        for (const el of document.querySelectorAll(sel)) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0 &&
                r.top >= 0 && r.bottom <= vh) {
                return true;
            }
        }
        return false;
    }"""
    _OVERLAPS_VIEWPORT_JS = """(sel) => {
        const vh = window.innerHeight;
        for (const el of document.querySelectorAll(sel)) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0 &&
                r.top < vh && r.bottom > 0) {
                return true;
            }
        }
        return false;
    }"""

    def _target_in_viewport() -> bool:
        try:
            return bool(page.evaluate(_IN_VIEWPORT_JS, anchor_sel))
        except Exception:
            return False

    def _boundary_in_view() -> bool:
        try:
            return bool(page.evaluate(_OVERLAPS_VIEWPORT_JS, boundary_sel))
        except Exception:
            return False

    def _click_target() -> bool:
        link = page.locator(anchor_sel).first
        try:
            # Target is already fully in the viewport; hover and click
            # directly — no scroll_into_view_if_needed, which would
            # otherwise animate a re-centering scroll and look robotic.
            link.hover(timeout=3_000)
            _sleep(0.2, 0.55)
            link.click(timeout=5_000)
        except Exception as exc:
            print(f"  Click on result failed: {exc}", flush=True)
            return False
        try:
            page.wait_for_url(re.compile(r"/watch\?"), timeout=15_000)
        except PwTimeoutError:
            return False
        page.wait_for_load_state("domcontentloaded")
        return True

    if _target_in_viewport():
        return _click_target()

    last_scroll_y = -1
    # Safety cap — a real results page is at most a couple dozen
    # scrolls from top to shelf boundary.
    for _ in range(40):
        if _boundary_in_view():
            return False
        try:
            scroll_y = page.evaluate("() => window.scrollY")
        except Exception:
            scroll_y = None
        if scroll_y is not None and scroll_y == last_scroll_y:
            return False
        last_scroll_y = scroll_y

        _scroll(page, random.randint(300, 650))
        _sleep(0.6, 1.8)

        if _target_in_viewport():
            return _click_target()

    return False


def _navigate_to_video(page: Page, video_id: str, title: str) -> bool:
    """Search for the video and click through.  Returns True on success."""
    _go_home(page)
    words = re.findall(r"\w+", title)[:6]
    query = "maklelan " + " ".join(words) if words else "maklelan"
    _do_search(page, query)
    if _click_search_result(page, video_id):
        return True

    # Fallback: direct navigation.  Still goes through the browser, so
    # cookies and headers match; we just skip the search UI.
    print("  Target not in results; using direct watch URL.", flush=True)
    try:
        page.goto(
            f"https://www.youtube.com/watch?v={video_id}",
            wait_until="domcontentloaded",
        )
        _accept_cookies_if_needed(page)
        _sleep(1.5, 3.0)
        return True
    except Exception as exc:
        print(f"  Direct navigation failed: {exc}", flush=True)
        return False


def _idle_on_watch_page(page: Page) -> None:
    """Spend a believable chunk of time on the watch page."""
    _sleep(2.2, 4.5)
    _wiggle_mouse(page)
    _scroll(page, random.randint(250, 600))        # skim description
    _sleep(1.0, 2.5)
    # Occasionally scrub the video a bit (clicks inside the player area).
    if random.random() < 0.35:
        try:
            player = page.locator("#movie_player, video").first
            if player.count() > 0:
                box = player.bounding_box()
                if box:
                    x = box["x"] + box["width"] * random.uniform(0.2, 0.8)
                    y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                    page.mouse.move(x, y, steps=random.randint(10, 25))
                    _sleep(0.4, 1.0)
        except Exception:
            pass
    _scroll(page, -random.randint(120, 320))       # scroll back up
    _sleep(0.6, 1.6)


# ---------------------------------------------------------------------------
# Caption URL path (preferred: produces identical JSON to fetch_transcripts.py)
# ---------------------------------------------------------------------------


def _pick_english_caption_url(tracks: list[dict]) -> str | None:
    preferred: str | None = None
    for track in tracks:
        lang = track.get("languageCode", "")
        url = track.get("baseUrl", "")
        if not url or not lang.startswith("en"):
            continue
        # Prefer human-authored over ASR when both exist.
        if preferred is None or track.get("kind") != "asr":
            preferred = url
    return preferred


def _caption_url_from_player_response(player_data: dict) -> str | None:
    try:
        tracks = (
            player_data["captions"]["playerCaptionsTracklistRenderer"]
            ["captionTracks"]
        )
    except (KeyError, TypeError):
        return None
    return _pick_english_caption_url(tracks)


def _extract_player_response(page: Page) -> dict | None:
    try:
        page.wait_for_function(
            "() => !!window.ytInitialPlayerResponse", timeout=10_000
        )
    except PwTimeoutError:
        return None
    try:
        return page.evaluate("() => window.ytInitialPlayerResponse")
    except Exception:
        return None


def _fetch_caption_xml(context: BrowserContext, url: str) -> str | None:
    try:
        resp = context.request.get(url, timeout=30_000)
        if resp.ok:
            return resp.text()
        print(f"  Caption URL returned HTTP {resp.status}", flush=True)
    except Exception as exc:
        print(f"  Caption URL fetch failed: {exc}", flush=True)
    return None


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


def _parse_transcript_xml(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    segments: list[dict] = []
    for elem in root.findall("text"):
        start = float(elem.get("start", 0))
        dur = float(elem.get("dur", 0))
        text = _unescape_html(elem.text or "").strip()
        if text:
            segments.append({"start": start, "dur": dur, "text": text})
    return segments


# ---------------------------------------------------------------------------
# DOM transcript panel (fallback)
# ---------------------------------------------------------------------------


_TS_RE = re.compile(r"^\s*(?:(\d+):)?(\d+):(\d{2})\s*$")


def _parse_timestamp(s: str) -> float | None:
    m = _TS_RE.match(s)
    if not m:
        return None
    h, mm, ss = m.groups()
    return float((int(h) if h else 0) * 3600 + int(mm) * 60 + int(ss))


def _expand_description(page: Page) -> None:
    """Click the ``...more`` expander so the in-description transcript
    button becomes visible.  Best-effort — silently does nothing if
    selectors miss."""
    # Scroll the description area into view first.
    _scroll(page, random.randint(300, 550))
    _sleep(0.5, 1.1)
    for sel in [
        "tp-yt-paper-button#expand",
        "#description-inline-expander #expand",
        "ytd-text-inline-expander #expand",
        "#expand",
    ]:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                _hover_and_click(page, btn)
                _sleep(0.6, 1.3)
                return
        except Exception:
            continue
    # Text-based fallback: click any visible "...more" / "Show more".
    try:
        btn = page.get_by_text(
            re.compile(r"^\s*(\.{3}more|…more|Show more)\s*$", re.I)
        ).first
        if btn.count() > 0 and btn.is_visible():
            _hover_and_click(page, btn)
            _sleep(0.6, 1.3)
    except Exception:
        pass


def _click_show_transcript_button(page: Page) -> bool:
    """Locate and click a "Show transcript" button in the page.

    Tries the expanded-description location first, then falls back to
    the "..." overflow menu next to the like / share buttons.
    """
    _expand_description(page)

    # Prefer the accessible-name lookup — survives markup churn better
    # than ytd-* selectors.
    by_role = page.get_by_role(
        "button", name=re.compile(r"show transcript", re.I)
    )
    try:
        if by_role.count() > 0 and by_role.first.is_visible():
            _hover_and_click(page, by_role.first)
            return True
    except Exception:
        pass

    # Fallback selectors inside the description section.
    for sel in [
        'ytd-video-description-transcript-section-renderer button',
        'ytd-button-renderer:has-text("Show transcript") button',
        'yt-button-shape:has-text("Show transcript") button',
        'button[aria-label*="transcript" i]',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                _hover_and_click(page, btn)
                return True
        except Exception:
            continue

    # Last resort: the "..." overflow menu → "Show transcript".
    try:
        more = page.locator(
            'button[aria-label="More actions"], '
            'ytd-menu-renderer yt-icon-button button'
        ).first
        if more.count() > 0 and more.is_visible():
            _hover_and_click(page, more)
            _sleep(0.6, 1.2)
            item = page.get_by_role(
                "menuitem", name=re.compile(r"transcript", re.I)
            ).first
            if item.count() > 0:
                _hover_and_click(page, item)
                return True
    except Exception:
        pass

    return False


# ---- XHR capture of /youtubei/v1/get_transcript ---------------------------


def _runs_text(snippet: dict) -> str:
    if not isinstance(snippet, dict):
        return ""
    if "simpleText" in snippet:
        return str(snippet["simpleText"]).strip()
    runs = snippet.get("runs") or []
    return "".join(r.get("text", "") for r in runs).strip()


def _parse_youtubei_get_transcript(data: dict) -> list[dict]:
    """Walk the /youtubei/v1/get_transcript response for segment renderers.

    The response nests a ``transcriptSegmentRenderer`` under several
    layers that YouTube has rearranged over time; a generic walker is
    more durable than a fixed path.
    """
    segments: list[dict] = []

    def walk(obj):
        if isinstance(obj, dict):
            seg = obj.get("transcriptSegmentRenderer")
            if isinstance(seg, dict):
                try:
                    start_ms = int(seg.get("startMs", 0))
                    end_ms = int(seg.get("endMs", 0))
                    text = _runs_text(seg.get("snippet") or {})
                    if text:
                        segments.append(
                            {
                                "start": start_ms / 1000.0,
                                "dur": max(0, end_ms - start_ms) / 1000.0,
                                "text": text,
                            }
                        )
                except (TypeError, ValueError):
                    pass
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return segments


def _scrape_transcript_dom(page: Page) -> list[dict]:
    """Final fallback: read timestamp+text pairs from the open panel.

    Handles both the current Lit-based markup
    (``<transcript-segment-view-model>`` with ``ytwTranscriptSegment*``
    classes) and the older polymer one
    (``ytd-transcript-segment-renderer`` with ``.segment-timestamp`` /
    ``.segment-text``).
    """
    segment_sel = (
        "transcript-segment-view-model, ytd-transcript-segment-renderer"
    )
    try:
        page.wait_for_selector(segment_sel, timeout=10_000)
    except PwTimeoutError:
        print("  DOM scrape: no segment elements appeared.", flush=True)
        return []

    # Best-effort scroll the panel's scrollable container to load any
    # virtualised segments.
    try:
        container = page.locator(
            "ytd-transcript-segment-list-renderer #segments-container, "
            "ytd-transcript-segment-list-renderer, "
            "[target-id='engagement-panel-searchable-transcript']"
        ).first
        if container.count() > 0:
            h = container.element_handle()
            if h:
                for _ in range(10):
                    page.evaluate(
                        "(el) => { el.scrollTop = el.scrollHeight; }", h
                    )
                    _sleep(0.25, 0.55)
    except Exception:
        pass

    raw = page.evaluate(
        """
        () => {
            const rows = [];
            const nodes = document.querySelectorAll(
                'transcript-segment-view-model, '
                + 'ytd-transcript-segment-renderer'
            );
            for (const node of nodes) {
                const ts = node.querySelector(
                    '.ytwTranscriptSegmentViewModelTimestamp, '
                    + '.segment-timestamp, '
                    + '[class*="Timestamp"]:not([class*="A11y"])'
                );
                const tx = node.querySelector(
                    '.ytAttributedStringHost, '
                    + '.segment-text, '
                    + 'yt-formatted-string.segment-text, '
                    + 'span[role="text"]'
                );
                const tsText = ts ? ts.innerText.trim() : '';
                const txText = tx ? tx.innerText.trim() : '';
                if (tsText || txText) rows.push({ts: tsText, text: txText});
            }
            return rows;
        }
        """
    )

    segments: list[dict] = []
    starts: list[float] = []
    for row in raw:
        start = _parse_timestamp(row.get("ts", ""))
        text = row.get("text", "")
        if start is None or not text:
            continue
        starts.append(start)
        segments.append({"start": start, "dur": 0.0, "text": text})
    for i in range(len(segments) - 1):
        segments[i]["dur"] = max(0.0, starts[i + 1] - starts[i])
    if segments:
        segments[-1]["dur"] = 3.0
    return segments


def _transcript_from_panel(page: Page) -> list[dict]:
    """Open the on-page transcript panel and return its segments.

    Registers a response listener *before* clicking so we can capture
    the ``/youtubei/v1/get_transcript`` XHR (which carries exact
    millisecond timestamps).  If that response never arrives (or
    parsing it yields nothing), fall back to scraping the rendered
    DOM panel.
    """
    captured: list[dict] = []

    def _on_response(resp):
        try:
            if "/youtubei/v1/get_transcript" in resp.url and resp.ok:
                captured.append(resp.json())
        except Exception:
            pass

    page.on("response", _on_response)
    try:
        if not _click_show_transcript_button(page):
            print("  Could not find a 'Show transcript' button.", flush=True)
            return []
        # Wait for the response to arrive (up to ~8s) or the panel DOM
        # to populate, whichever comes first.
        for _ in range(40):
            if captured:
                break
            _sleep(0.2, 0.25)
        _sleep(0.8, 1.6)
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    if captured:
        for data in captured:
            segs = _parse_youtubei_get_transcript(data)
            if segs:
                return segs
        print(
            "  Captured get_transcript XHR but parsed 0 segments; "
            "falling through to DOM scrape.",
            flush=True,
        )
    else:
        print(
            "  No get_transcript XHR captured; attempting DOM scrape.",
            flush=True,
        )

    return _scrape_transcript_dom(page)


# ---------------------------------------------------------------------------
# Cache helpers (identical on-disk format to fetch_transcripts.py)
# ---------------------------------------------------------------------------


def _load_cached_transcript(video_id: str) -> list[dict] | None:
    path = TRANSCRIPT_DIR / f"{video_id}.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            print(
                f"  Warning: could not read cached transcript for "
                f"{video_id}: {exc}",
                flush=True,
            )
    return None


def _save_transcript(video_id: str, transcript: list[dict]) -> None:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"{video_id}.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(transcript, f, ensure_ascii=False)
    except Exception as exc:
        print(f"  Warning: could not cache transcript: {exc}", flush=True)


# Single well-known path so the user always knows where to look after
# a failure; each new failure overwrites the previous dump.
_FAILED_DUMP_PATH = Path(__file__).parent.parent / "failed-scrape.html"


def _save_failed_scrape(page: Page, video_id: str, reason: str = "") -> None:
    """Write the current page DOM to ``failed-scrape.html`` for inspection."""
    try:
        html = page.content()
    except Exception as exc:
        print(
            f"  Could not capture page HTML for {video_id}: {exc}", flush=True
        )
        return
    try:
        url = page.url
    except Exception:
        url = ""
    try:
        with open(_FAILED_DUMP_PATH, "w", encoding="utf-8") as f:
            f.write(f"<!-- video_id: {video_id} -->\n")
            f.write(f"<!-- url: {url} -->\n")
            if reason:
                f.write(f"<!-- reason: {reason} -->\n")
            f.write(html)
        print(
            f"  Saved failed scrape DOM → {_FAILED_DUMP_PATH}", flush=True
        )
    except Exception as exc:
        print(f"  Could not save failed scrape dump: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Direct call to /youtubei/v1/get_transcript from inside the page context
# ---------------------------------------------------------------------------
#
# When a user clicks "Show transcript" on a watch page, YouTube's
# JS issues a POST to /youtubei/v1/get_transcript?prettyPrint=false
# with a ``params`` token that identifies the transcript engagement
# panel for this video, plus the page's INNERTUBE_CONTEXT.  Both of
# those live on ``window`` already, so we can replicate the request
# from inside the page — no UI clicks, no scraping, and cookies /
# Client-Hints / x-goog-visitor-id are applied by the browser exactly
# as they are for the real call.
#
# The ``params`` token can be found by walking ``ytInitialData`` for
# any ``getTranscriptEndpoint.params`` leaf.  It appears as the
# ``commandExecutorCommand`` payload behind the transcript button.

_GET_TRANSCRIPT_JS = r"""
async () => {
    const findParams = (root) => {
        const stack = [root];
        while (stack.length) {
            const obj = stack.pop();
            if (!obj || typeof obj !== 'object') continue;
            const ep = obj.getTranscriptEndpoint;
            if (ep && typeof ep.params === 'string' && ep.params) {
                return ep.params;
            }
            for (const k in obj) {
                const v = obj[k];
                if (v && typeof v === 'object') stack.push(v);
            }
        }
        return null;
    };

    const params =
        (window.ytInitialData && findParams(window.ytInitialData)) || null;

    let ctx = null;
    try {
        if (window.ytcfg && typeof window.ytcfg.get === 'function') {
            ctx = window.ytcfg.get('INNERTUBE_CONTEXT');
        }
    } catch (e) {}
    if (!ctx && window.ytcfg && window.ytcfg.data_) {
        ctx = window.ytcfg.data_.INNERTUBE_CONTEXT;
    }

    let clientVersion = '';
    try {
        clientVersion = (window.ytcfg && window.ytcfg.get &&
            window.ytcfg.get('INNERTUBE_CLIENT_VERSION')) || '';
    } catch (e) {}

    let publishDate = '';
    try {
        const raw = window.ytInitialPlayerResponse
            && window.ytInitialPlayerResponse.microformat
            && window.ytInitialPlayerResponse.microformat.playerMicroformatRenderer
            && window.ytInitialPlayerResponse.microformat.playerMicroformatRenderer.publishDate;
        if (raw) publishDate = String(raw).substring(0, 10);
    } catch (e) {}

    if (!params) return {error: 'no_params', publishDate};
    if (!ctx)    return {error: 'no_context', publishDate};

    try {
        const resp = await fetch(
            '/youtubei/v1/get_transcript?prettyPrint=false',
            {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'X-YouTube-Client-Name': '1',
                    'X-YouTube-Client-Version': clientVersion || '2.20260421.00.00',
                },
                body: JSON.stringify({context: ctx, params: params}),
            }
        );
        if (!resp.ok) return {error: 'http_' + resp.status, publishDate};
        const data = await resp.json();
        return {data, publishDate};
    } catch (e) {
        return {error: 'fetch_failed: ' + (e && e.message), publishDate};
    }
}
"""


def _get_transcript_via_api(page: Page) -> tuple[list[dict], str]:
    """Call ``/youtubei/v1/get_transcript`` from inside the watch page.

    Returns ``(segments, publish_date)``.  Uses the page's own
    INNERTUBE_CONTEXT so the request is indistinguishable from what
    clicking the transcript button would produce.
    """
    try:
        result = page.evaluate(_GET_TRANSCRIPT_JS)
    except Exception as exc:
        print(f"  get_transcript eval failed: {exc}", flush=True)
        return [], ""

    if not isinstance(result, dict):
        return [], ""

    publish_date = result.get("publishDate") or ""

    if result.get("error"):
        print(f"  get_transcript API: {result['error']}", flush=True)
        return [], publish_date

    data = result.get("data")
    if not data:
        return [], publish_date

    segments = _parse_youtubei_get_transcript(data)
    if not segments:
        print(
            "  get_transcript API returned 0 parseable segments.", flush=True
        )
    return segments, publish_date


# ---------------------------------------------------------------------------
# Top-level per-video
# ---------------------------------------------------------------------------


def fetch_transcript(
    context: BrowserContext,
    page: Page,
    video_id: str,
    title: str,
) -> tuple[list[dict], str]:
    """Fetch the English transcript for *video_id*, returning (segments, date).

    1. Return from local cache if available.
    2. Navigate via search → click, idle on page.
    3. Click the on-page "Show transcript" panel and scrape segments
       from the rendered DOM — this is the primary path because it
       works on current YouTube markup.  Timestamps are integer
       seconds instead of sub-second, but the text is complete.
    4. Fall back to POSTing ``/youtubei/v1/get_transcript`` from the
       page context (yields sub-second timestamps when the ``params``
       token is discoverable).
    5. Fall back to the caption-XML URL from ``ytInitialPlayerResponse``.
    """
    cached = _load_cached_transcript(video_id)
    if cached is not None:
        return cached, ""

    if not _navigate_to_video(page, video_id, title):
        return [], ""

    _idle_on_watch_page(page)

    publish_date = ""

    # Primary path: the on-page transcript panel.
    segments = _transcript_from_panel(page)
    if segments:
        # Grab the publish date opportunistically from the player
        # response while the page is still open.
        try:
            player_data = _extract_player_response(page)
            if player_data:
                publish_date = extract_publish_date(player_data)
        except Exception:
            pass
        _save_transcript(video_id, segments)
        return segments, publish_date

    # Fallback 1: direct API call from the page context.
    segments, publish_date = _get_transcript_via_api(page)
    if segments:
        _save_transcript(video_id, segments)
        return segments, publish_date

    # Fallback 2: caption XML URL from ytInitialPlayerResponse.
    player_data = _extract_player_response(page)
    if player_data:
        if not publish_date:
            publish_date = extract_publish_date(player_data)
        caption_url = _caption_url_from_player_response(player_data)
        if caption_url:
            xml_text = _fetch_caption_xml(context, caption_url)
            if xml_text:
                segments = _parse_transcript_xml(xml_text)
                if segments:
                    _save_transcript(video_id, segments)
                    return segments, publish_date

    _save_failed_scrape(
        page, video_id, reason="all transcript paths returned 0 segments"
    )
    return segments, publish_date


# ---------------------------------------------------------------------------
# Date backfill
# ---------------------------------------------------------------------------


def _fetch_publish_date(page: Page, video_id: str, title: str) -> str:
    if not _navigate_to_video(page, video_id, title):
        return ""
    _sleep(1.5, 3.5)
    player_data = _extract_player_response(page)
    return extract_publish_date(player_data) if player_data else ""


def _backfill_dates(
    page: Page,
    videos_data: dict,
    max_count: int | None,
) -> None:
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

    if max_count is not None and len(missing) > max_count:
        print(
            f"Capping date backfill to {max_count} of {len(missing)} videos.",
            flush=True,
        )
        missing = missing[:max_count]

    print(f"Backfilling publish dates for {len(missing)} videos …", flush=True)
    filled = 0
    for i, rec in enumerate(missing, 1):
        vid_id = rec["id"]
        title = rec.get("title", vid_id)
        print(f"  [{i}/{len(missing)}] {title[:60]}", end="", flush=True)
        try:
            date = _fetch_publish_date(page, vid_id, title)
        except Exception as exc:
            print(f" → error: {exc}", flush=True)
            date = ""
        if date:
            videos_data["videos"][vid_id]["published_date"] = date
            filled += 1
            print(f" → {date}", flush=True)
            save_videos(videos_data)
        else:
            print(" → no date found", flush=True)
        _sleep(5.0, 12.0)
    print(f"Backfilled {filled} publish dates.", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> None:
    _, videos_data = load_data()

    profile_dir = Path(args.profile_dir) if args.profile_dir \
        else _DEFAULT_PROFILE_DIR

    with sync_playwright() as pw:
        context = _launch_context(pw, profile_dir, headless=not args.head)
        try:
            page = context.new_page()
            # Prime the session: visit the homepage once up front so that
            # consent cookies and SID tokens are set before any video.
            _go_home(page)

            if not args.no_backfill_dates:
                _backfill_dates(page, videos_data, args.max)

            to_process = [
                rec for rec in videos_data.get("videos", {}).values()
                if rec.get("processed") in (STATUS_NOT_ATTEMPTED, STATUS_FAILED)
                and not (TRANSCRIPT_DIR / f"{rec['id']}.json").exists()
            ]
            if args.max is not None and len(to_process) > args.max:
                print(
                    f"Capping to {args.max} videos this run "
                    f"({len(to_process)} pending).",
                    flush=True,
                )
                to_process = to_process[:args.max]

            print(
                f"Videos to fetch transcripts for: {len(to_process)}",
                flush=True,
            )

            fetched_count = 0
            for i, video in enumerate(to_process, 1):
                vid_id = video["id"]
                vid_record = videos_data["videos"][vid_id]
                print(
                    f"[{i}/{len(to_process)}] "
                    f"{video.get('title', vid_id)[:70]}",
                    flush=True,
                )

                transcript, publish_date = fetch_transcript(
                    context, page, vid_id, video.get("title", "")
                )

                if publish_date and not vid_record.get("published_date"):
                    vid_record["published_date"] = publish_date
                    print(f"  Published: {publish_date}", flush=True)

                if not transcript:
                    print("  No transcript – marking failed.", flush=True)
                    vid_record["processed"] = STATUS_FAILED
                    save_videos(videos_data)
                    # Failure back-off — longer than the between-videos
                    # wait so we do not hammer an unhappy YouTube.
                    _sleep(22.0, 45.0)
                    continue

                fetched_count += 1
                print(f"  Transcript: {len(transcript)} segments", flush=True)
                save_videos(videos_data)
                _sleep(8.0, 18.0)

            print(f"Done. Fetched {fetched_count} transcripts.", flush=True)
        finally:
            try:
                context.close()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch transcripts via headless Playwright with anti-bot "
            "measures.  Functionally equivalent to fetch_transcripts.py."
        )
    )
    parser.add_argument(
        "--max", type=int, default=None,
        help=(
            "Optional cap on number of transcripts fetched in this run. "
            "Default: no cap — run until stopped (Ctrl+C) or an "
            "unrecoverable error occurs."
        ),
    )
    parser.add_argument(
        "--no-backfill-dates", action="store_true",
        help="Skip backfilling publish dates for videos that are missing them.",
    )
    parser.add_argument(
        "--head", action="store_true",
        help="Run with a visible browser window (useful for debugging).",
    )
    parser.add_argument(
        "--profile-dir", default=None,
        help=(
            "Chromium user-data-dir for cookie/history persistence "
            f"(default: {_DEFAULT_PROFILE_DIR})."
        ),
    )
    args = parser.parse_args()
    _run(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.", flush=True)
    except Exception as exc:
        # Any uncaught error (browser crash, YouTube markup change,
        # network outage, …) should still flush accumulated progress
        # into the index before exiting.
        import traceback
        print("\nFatal error — running indexer before exit:", flush=True)
        traceback.print_exc()
    build_index.main()

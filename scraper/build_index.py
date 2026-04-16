#!/usr/bin/env python3
"""Build the scripture-reference index from cached transcripts.

Reads ``docs/data/videos.json`` and ``data/transcripts/*.json``, runs the
scripture-reference regex over each transcript, and writes the results to
``docs/data/index.json``.  Also updates the per-video ``processed`` status
in ``videos.json``.

Usage::

    python scraper/build_index.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bible_books import find_scripture_refs  # noqa: E402
from common import (
    STATUS_FAILED,
    STATUS_NO_REFS,
    STATUS_NOT_ATTEMPTED,
    STATUS_YES,
    TRANSCRIPT_DIR,
    load_data,
    save_index,
    save_videos,
)


# ---------------------------------------------------------------------------
# Scripture reference extraction
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

    parts: list[str] = []
    position_map: list[tuple[int, float]] = []
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

        snip_start = max(0, ref_info["start"] - 60)
        snip_end = min(len(full_text), ref_info["end"] + 60)
        snippet = full_text[snip_start:snip_end].strip()

        results.append({"canonical": canonical, "timestamp": ts, "snippet": snippet})

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    index, videos_data = load_data()

    # Rebuild references from scratch so stale entries are removed
    index["references"] = {}

    videos = videos_data.get("videos", {})
    processed_count = 0
    ref_count = 0

    for vid_id, vid_record in videos.items():
        transcript_path = TRANSCRIPT_DIR / f"{vid_id}.json"
        if not transcript_path.exists():
            continue

        try:
            with open(transcript_path, encoding="utf-8") as f:
                transcript = json.load(f)
        except Exception as exc:
            print(f"Warning: could not read transcript for {vid_id}: {exc}", flush=True)
            continue

        refs = refs_from_transcript(transcript)

        for ref in refs:
            canonical = ref["canonical"]
            entry = {
                "video_id": vid_id,
                "timestamp": ref["timestamp"],
                "snippet": ref["snippet"],
            }
            bucket = index["references"].setdefault(canonical, [])
            already = any(
                e["video_id"] == vid_id and e["timestamp"] == ref["timestamp"]
                for e in bucket
            )
            if not already:
                bucket.append(entry)

        if refs:
            vid_record["processed"] = STATUS_YES
            ref_count += len(refs)
        else:
            vid_record["processed"] = STATUS_NO_REFS

        processed_count += 1

    save_index(index)
    save_videos(videos_data)

    print(
        f"Built index: {len(index['references'])} references "
        f"({ref_count} occurrences) from {processed_count} transcripts.",
        flush=True,
    )


if __name__ == "__main__":
    main()

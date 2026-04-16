/**
 * McClellan Scripture Index – frontend app
 *
 * Loads docs/data/index.json and docs/data/videos.json then provides
 * a live-search interface keyed by scripture reference.
 * Results are video-centric: each video appears once with all matching
 * scripture hits listed inside it.
 */

/* ── State ─────────────────────────────────────────────────────── */

/** @type {{ updated: string|null, references: Record<string, Array<{video_id:string, timestamp:number, snippet:string}>> } | null} */
let indexData = null;

/** @type {{ videos: Record<string, {id:string, title:string, published:string, published_date:string, thumbnail:string, duration:string, url:string, view_count:number}> } | null} */
let videosData = null;

/** Sorted list of all canonical references (for quick filtering) */
let allRefs = [];

/** Current sort mode */
let currentSort = "relevance";

/** Last query string (for re-sorting without re-filtering) */
let lastQuery = "";

/* ── Bootstrap ─────────────────────────────────────────────────── */

async function init() {
  const statsEl = document.getElementById("stats");
  statsEl.textContent = "Loading index …";

  try {
    const [idxResp, vidResp] = await Promise.all([
      fetch("data/index.json"),
      fetch("data/videos.json"),
    ]);

    if (!idxResp.ok) throw new Error(`index.json ${idxResp.status}`);
    if (!vidResp.ok) throw new Error(`videos.json ${vidResp.status}`);

    indexData = await idxResp.json();
    videosData = await vidResp.json();
  } catch (err) {
    document.getElementById("results-list").innerHTML =
      `<p class="error">⚠ Could not load index data (${esc(String(err))}).<br>
       The scraper may not have run yet – check the GitHub Actions tab.</p>`;
    statsEl.textContent = "";
    return;
  }

  allRefs = Object.keys(indexData.references).sort(compareScriptureRefs);

  const refCount = allRefs.length;
  const vidCount = Object.keys(videosData.videos).length;
  const updated = indexData.updated
    ? new Date(indexData.updated).toLocaleDateString(undefined, {
        year: "numeric",
        month: "long",
        day: "numeric",
      })
    : "never";
  document.getElementById("updated").textContent = updated;

  if (refCount === 0) {
    statsEl.textContent = "Index is empty – the scraper has not run yet.";
    document.getElementById("results-list").innerHTML =
      `<p class="hint">Trigger the <em>Scrape and Update Index</em> GitHub Actions workflow
       to populate the index, then refresh this page.</p>`;
    return;
  }

  statsEl.textContent =
    `${refCount.toLocaleString()} scripture references indexed across ${vidCount.toLocaleString()} videos`;

  // Attach search listener
  const input = document.getElementById("search-input");
  input.addEventListener("input", () => render(input.value));

  // Attach sort listener
  const sortSelect = document.getElementById("sort-select");
  sortSelect.addEventListener("change", () => {
    currentSort = sortSelect.value;
    render(lastQuery);
  });

  input.focus();

  // Show hint on empty state
  render("");
}

/* ── Search / filter ───────────────────────────────────────────── */

/**
 * Parse a scripture reference string into its components.
 * Handles: "Book", "Book Ch", "Book Ch:V", "Book Ch:V-V2"
 * @param {string} str
 * @returns {{ book: string, chapter: number|null, verseStart: number|null, verseEnd: number|null } | null}
 */
function parseScriptureQuery(str) {
  const m = str.match(/^(.+?)\s+(\d+)(?::(\d+)(?:\s*-\s*(\d+))?)?$/);
  if (!m) {
    // Could be just a book name (e.g. "Genesis", "1 Cor")
    if (/[a-z]/i.test(str)) return { book: str.trim(), chapter: null, verseStart: null, verseEnd: null };
    return null;
  }
  return {
    book: m[1].trim(),
    chapter: parseInt(m[2], 10),
    verseStart: m[3] ? parseInt(m[3], 10) : null,
    verseEnd: m[4] ? parseInt(m[4], 10) : m[3] ? parseInt(m[3], 10) : null,
  };
}

/**
 * Test whether a canonical reference matches a parsed query, with verse-range
 * awareness.  For example, query "Genesis 1:8" matches ref "Genesis 1:1-23"
 * because verse 8 falls within 1–23.
 * @param {string} ref  canonical reference from the index
 * @param {{ book: string, chapter: number|null, verseStart: number|null, verseEnd: number|null }} q  parsed query
 * @returns {boolean}
 */
function refMatchesQuery(ref, q) {
  const r = parseScriptureQuery(ref);
  if (!r) return false;

  // Book must match (case-insensitive prefix/substring)
  if (!r.book.toLowerCase().startsWith(q.book.toLowerCase())
      && !r.book.toLowerCase().includes(q.book.toLowerCase())) {
    return false;
  }

  // If query has no chapter, book match is enough
  if (q.chapter === null) return true;

  // Chapter must match exactly
  if (r.chapter !== q.chapter) return false;

  // If query has no verse, chapter match is enough
  if (q.verseStart === null) return true;

  // If the indexed ref has no verse, chapter match is enough
  if (r.verseStart === null) return true;

  // Verse-range overlap: query range and ref range must intersect
  return q.verseStart <= r.verseEnd && q.verseEnd >= r.verseStart;
}

/**
 * Collect matching refs, group all hits by video, sort, and render.
 * @param {string} query
 */
function render(query) {
  lastQuery = query;
  const q = query.trim().toLowerCase();
  const container = document.getElementById("results-list");
  const sortControls = document.getElementById("sort-controls");

  if (!q) {
    container.innerHTML = `<p class="hint">Start typing a book name, chapter, or verse above.</p>`;
    sortControls.classList.add("hidden");
    return;
  }

  const parsed = parseScriptureQuery(query.trim());
  const matched = parsed
    ? allRefs.filter((ref) => refMatchesQuery(ref, parsed))
    : allRefs.filter((ref) => ref.toLowerCase().includes(q));

  if (matched.length === 0) {
    container.innerHTML = `<p class="no-results">No results for <strong>${esc(query)}</strong>.</p>`;
    sortControls.classList.add("hidden");
    return;
  }

  // Build video-centric data: Map<videoId, { ref, timestamp, snippet }[]>
  const videoHits = new Map();
  for (const ref of matched) {
    const entries = indexData.references[ref];
    if (!entries) continue;
    for (const entry of entries) {
      if (!videoHits.has(entry.video_id)) videoHits.set(entry.video_id, []);
      videoHits.get(entry.video_id).push({
        ref,
        timestamp: entry.timestamp,
        snippet: entry.snippet,
      });
    }
  }

  // Sort hits within each video by timestamp
  for (const hits of videoHits.values()) {
    hits.sort((a, b) => a.timestamp - b.timestamp);
  }

  // Convert to array for sorting
  let videoList = Array.from(videoHits.entries()).map(([videoId, hits]) => ({
    videoId,
    hits,
    video: videosData.videos[videoId],
  }));

  // Remove entries with no video metadata
  videoList = videoList.filter((v) => v.video);

  // Sort videos
  videoList = sortVideos(videoList, currentSort);

  // Show sort controls
  sortControls.classList.remove("hidden");

  const limit = 100;
  const shown = videoList.slice(0, limit);
  const html = shown.map(buildVideoCard).join("");

  container.innerHTML =
    html +
    (videoList.length > limit
      ? `<p class="hint">Showing first ${limit} of ${videoList.length} videos – refine your search to narrow results.</p>`
      : "");
}

/**
 * Sort the video list according to the selected mode.
 */
function sortVideos(videoList, mode) {
  switch (mode) {
    case "relevance":
      return videoList.sort((a, b) => b.hits.length - a.hits.length);
    case "date-desc":
      return videoList.sort((a, b) =>
        (b.video.published_date || "").localeCompare(a.video.published_date || "")
      );
    case "date-asc":
      return videoList.sort((a, b) =>
        (a.video.published_date || "").localeCompare(b.video.published_date || "")
      );
    case "views":
      return videoList.sort((a, b) => (b.video.view_count || 0) - (a.video.view_count || 0));
    default:
      return videoList;
  }
}

/* ── Card builder ───────────────────────────────────────────────── */

/**
 * Build HTML for a single video card with all its scripture hits.
 * @param {{ videoId: string, hits: Array<{ref:string, timestamp:number, snippet:string}>, video: object }} item
 * @returns {string}
 */
function buildVideoCard(item) {
  const { videoId, hits, video } = item;
  const videoUrl = `https://www.youtube.com/watch?v=${esc(videoId)}`;
  const thumbHtml = video.thumbnail
    ? `<img src="${esc(video.thumbnail)}" alt="" class="thumbnail" loading="lazy">`
    : "";

  const metaParts = [];
  if (video.published_date) {
    metaParts.push(esc(video.published_date));
  } else if (video.published) {
    metaParts.push(esc(video.published));
  }
  if (video.view_count) metaParts.push(`${video.view_count.toLocaleString()} views`);
  const metaHtml = metaParts.length
    ? `<span class="video-meta">${metaParts.join(" · ")}</span>`
    : "";

  const hitsHtml = hits
    .map((hit) => {
      const tsUrl = `${videoUrl}&t=${hit.timestamp}`;
      const timeStr = formatTime(hit.timestamp);
      const snippetHtml = hit.snippet
        ? `<span class="snippet">&ldquo;${boldRef(esc(hit.snippet), hit.ref)}&rdquo;</span>`
        : "";
      return `<a href="${tsUrl}" target="_blank" rel="noopener noreferrer" class="hit-line">
        <span class="hit-ref">${esc(hit.ref)}</span>
        <span class="timestamp">⏱ ${timeStr}</span>
        ${snippetHtml}
      </a>`;
    })
    .join("");

  return `<div class="video-card">
  <a href="${videoUrl}" target="_blank" rel="noopener noreferrer" class="video-header">
    ${thumbHtml}
    <div class="video-header-info">
      <span class="video-title">${esc(video.title)}</span>
      ${metaHtml}
      <span class="hit-count">${hits.length} match${hits.length === 1 ? "" : "es"}</span>
    </div>
  </a>
  <div class="hit-lines">${hitsHtml}</div>
</div>`;
}

/**
 * Bold the scripture reference within an already-escaped snippet.
 * @param {string} escapedSnippet  HTML-escaped snippet text
 * @param {string} ref  canonical reference (e.g. "Genesis 21:17")
 */
function boldRef(escapedSnippet, ref) {
  const escapedRef = esc(ref);
  return escapedSnippet.replace(escapedRef, `<strong>${escapedRef}</strong>`);
}

/* ── Utilities ──────────────────────────────────────────────────── */

/**
 * Format seconds as H:MM:SS or M:SS.
 * @param {number} seconds
 */
function formatTime(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) {
    return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  return `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * Escape a string for safe insertion into HTML.
 * @param {string} str
 */
function esc(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Comparison function for sorting canonical scripture references in Bible order. */
function compareScriptureRefs(a, b) {
  const BOOK_ORDER = [
    "Genesis","Exodus","Leviticus","Numbers","Deuteronomy",
    "Joshua","Judges","Ruth",
    "1 Samuel","2 Samuel","1 Kings","2 Kings",
    "1 Chronicles","2 Chronicles",
    "Ezra","Nehemiah","Esther","Job","Psalms","Proverbs",
    "Ecclesiastes","Song of Solomon",
    "Isaiah","Jeremiah","Lamentations","Ezekiel","Daniel",
    "Hosea","Joel","Amos","Obadiah","Jonah","Micah",
    "Nahum","Habakkuk","Zephaniah","Haggai","Zechariah","Malachi",
    "Matthew","Mark","Luke","John","Acts","Romans",
    "1 Corinthians","2 Corinthians","Galatians","Ephesians",
    "Philippians","Colossians",
    "1 Thessalonians","2 Thessalonians",
    "1 Timothy","2 Timothy","Titus","Philemon",
    "Hebrews","James",
    "1 Peter","2 Peter",
    "1 John","2 John","3 John","Jude","Revelation",
  ];
  // e.g. "Romans 8:28" → book="Romans", rest="8:28"
  const parse = (r) => {
    const m = r.match(/^(.+?)\s+(\d+:\d+(?:-\d+)?)$/);
    if (!m) return { bookIdx: 999, rest: r };
    const bookIdx = BOOK_ORDER.indexOf(m[1]);
    return { bookIdx: bookIdx === -1 ? 999 : bookIdx, rest: m[2] };
  };
  const pa = parse(a);
  const pb = parse(b);
  if (pa.bookIdx !== pb.bookIdx) return pa.bookIdx - pb.bookIdx;
  // Compare "8:28" vs "8:3" numerically
  const nums = (s) => s.split(/[:-]/).map(Number);
  const na = nums(pa.rest);
  const nb = nums(pb.rest);
  for (let i = 0; i < Math.max(na.length, nb.length); i++) {
    const d = (na[i] || 0) - (nb[i] || 0);
    if (d !== 0) return d;
  }
  return 0;
}

/* ── Entry point ────────────────────────────────────────────────── */
init();

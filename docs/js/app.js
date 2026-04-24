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

/** Current grouping mode when sorting by reference: "video" | "reference" */
let currentGrouping = "video";

/** Last query string (for re-sorting without re-filtering) */
let lastQuery = "";

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
    updateGroupToggleVisibility();
    render(lastQuery);
  });

  // Attach group-by-ref toggle listener
  const groupCheckbox = document.getElementById("group-by-ref");
  groupCheckbox.addEventListener("change", () => {
    currentGrouping = groupCheckbox.checked ? "reference" : "video";
    render(lastQuery);
  });

  input.focus();

  // Show hint on empty state
  render("");
}

/* ── Search / filter ───────────────────────────────────────────── */

/**
 * Parse a scripture reference string into an inclusive (chapter, verse) span.
 * Handles: "Book", "Book Ch", "Book Ch-Ch2", "Book Ch:V",
 * "Book Ch:V-V2", "Book Ch:V-Ch2:V2".
 * A null verse in a span endpoint means "any verse in that chapter".
 * @param {string} str
 * @returns {{ book: string, chapterStart: number|null, verseStart: number|null, chapterEnd: number|null, verseEnd: number|null } | null}
 */
function parseScriptureQuery(str) {
  str = str.trim();
  let m;

  // Book Ch:V-Ch2:V2
  if ((m = str.match(/^(.+?)\s+(\d+):(\d+)\s*-\s*(\d+):(\d+)$/))) {
    return { book: m[1].trim(), chapterStart: +m[2], verseStart: +m[3], chapterEnd: +m[4], verseEnd: +m[5] };
  }
  // Book Ch:V-V2
  if ((m = str.match(/^(.+?)\s+(\d+):(\d+)\s*-\s*(\d+)$/))) {
    return { book: m[1].trim(), chapterStart: +m[2], verseStart: +m[3], chapterEnd: +m[2], verseEnd: +m[4] };
  }
  // Book Ch:V
  if ((m = str.match(/^(.+?)\s+(\d+):(\d+)$/))) {
    return { book: m[1].trim(), chapterStart: +m[2], verseStart: +m[3], chapterEnd: +m[2], verseEnd: +m[3] };
  }
  // Book Ch-Ch2
  if ((m = str.match(/^(.+?)\s+(\d+)\s*-\s*(\d+)$/))) {
    return { book: m[1].trim(), chapterStart: +m[2], verseStart: null, chapterEnd: +m[3], verseEnd: null };
  }
  // Book Ch
  if ((m = str.match(/^(.+?)\s+(\d+)$/))) {
    return { book: m[1].trim(), chapterStart: +m[2], verseStart: null, chapterEnd: +m[2], verseEnd: null };
  }
  // Bare book name (e.g. "Genesis", "1 Cor")
  if (/[a-z]/i.test(str)) {
    return { book: str, chapterStart: null, verseStart: null, chapterEnd: null, verseEnd: null };
  }
  return null;
}

// Max verses-per-chapter slot used to linearize (chapter, verse) pairs so that
// range overlap reduces to a simple interval intersection. Any value larger
// than the longest chapter in scripture (Psalm 119, 176 verses) works.
const VERSES_PER_CHAPTER = 10000;

/** Linear scalar for the start of a (chapter, verse) endpoint; null verse → 0. */
function spanStart(chapter, verse) {
  return chapter * VERSES_PER_CHAPTER + (verse === null ? 0 : verse);
}

/** Linear scalar for the end of a (chapter, verse) endpoint; null verse → end of chapter. */
function spanEnd(chapter, verse) {
  return chapter * VERSES_PER_CHAPTER + (verse === null ? VERSES_PER_CHAPTER - 1 : verse);
}

/**
 * Test whether a canonical reference matches a parsed query. Both sides are
 * treated as inclusive spans over (chapter, verse); they match when the spans
 * intersect. So "Genesis 1-2" matches "Gen 1" and "Gen 2"; "Gen 1:4-8" matches
 * each of 1:4…1:8; and "Gen 1:10-11" matches a ref to "Gen 1:11-12".
 * @param {string} ref  canonical reference from the index
 * @param {ReturnType<typeof parseScriptureQuery>} q  parsed query
 * @returns {boolean}
 */
function refMatchesQuery(ref, q) {
  const r = parseScriptureQuery(ref);
  if (!r) return false;

  // Book must match (case-insensitive prefix/substring)
  const rb = r.book.toLowerCase();
  const qb = q.book.toLowerCase();
  if (!rb.startsWith(qb) && !rb.includes(qb)) return false;

  // If query has no chapter, book match is enough
  if (q.chapterStart === null) return true;
  // If ref has no chapter (bare book name), book match is enough
  if (r.chapterStart === null) return true;

  const qS = spanStart(q.chapterStart, q.verseStart);
  const qE = spanEnd(q.chapterEnd, q.verseEnd);
  const rS = spanStart(r.chapterStart, r.verseStart);
  const rE = spanEnd(r.chapterEnd, r.verseEnd);
  return qS <= rE && qE >= rS;
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

  // Show sort controls + group toggle (visibility of the toggle itself depends on sort mode)
  sortControls.classList.remove("hidden");
  updateGroupToggleVisibility();

  // Reference-sorted + grouped-by-reference: render a reference-centric view
  if (currentSort === "reference" && currentGrouping === "reference") {
    renderByReference(matched, container);
    return;
  }

  // Otherwise render video-centric
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

  const limit = 100;
  const shown = videoList.slice(0, limit);
  const html = shown.map(buildVideoCard).join("");

  container.innerHTML =
    html +
    (videoList.length > limit
      ? `<p class="hint">Showing first ${limit} of ${videoList.length} videos – refine your search to narrow results.</p>`
      : "");
}

/** Show the group-by toggle only when sorting by Bible reference. */
function updateGroupToggleVisibility() {
  const toggle = document.getElementById("group-toggle");
  if (currentSort === "reference") {
    toggle.classList.remove("hidden");
  } else {
    toggle.classList.add("hidden");
  }
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
    case "reference":
      // Sort videos by the earliest Bible reference among their hits.
      return videoList.sort((a, b) => {
        const ka = earliestRefKey(a.hits);
        const kb = earliestRefKey(b.hits);
        return compareKeys(ka, kb);
      });
    default:
      return videoList;
  }
}

/**
 * Parse a canonical scripture reference string into structured fields, including
 * a book-order index and whether the reference spans more than a single verse/chapter.
 */
function parseRef(ref) {
  const p = parseScriptureQuery(ref);
  if (!p) return null;
  const bookIdx = BOOK_ORDER.indexOf(p.book);
  return {
    book: p.book,
    bookIdx: bookIdx === -1 ? 999 : bookIdx,
    chapterStart: p.chapterStart,
    verseStart: p.verseStart,
    chapterEnd: p.chapterEnd,
    verseEnd: p.verseEnd,
    isSpan: !(p.chapterStart === p.chapterEnd && p.verseStart === p.verseEnd),
  };
}

/**
 * Build an orderable sort key for a reference. Ordering rules:
 *   - book in canonical order
 *   - then starting chapter
 *   - then starting verse; a missing verse sorts before verse 1 (Gen 1 before Gen 1:1)
 *   - spans sort before individual items that share the same starting point
 *   - then ending chapter, then ending verse (tiebreaker for differing spans)
 */
function refSortKey(ref) {
  const p = parseRef(ref);
  if (!p) return [999, 0, -1, 1, 0, -1];
  const chapStart = p.chapterStart ?? 0;
  // Missing verse sorts before any numbered verse
  const verseStart = p.verseStart === null ? -1 : p.verseStart;
  // Spans first within the same start key
  const spanBucket = p.isSpan ? 0 : 1;
  const chapEnd = p.chapterEnd ?? chapStart;
  const verseEnd = p.verseEnd === null ? -1 : p.verseEnd;
  return [p.bookIdx, chapStart, verseStart, spanBucket, chapEnd, verseEnd];
}

/** Lexicographic compare for numeric-array keys. */
function compareKeys(a, b) {
  const n = Math.max(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const d = (a[i] ?? 0) - (b[i] ?? 0);
    if (d !== 0) return d;
  }
  return 0;
}

/** Minimum sort key across a list of hits (each hit has a `.ref`). */
function earliestRefKey(hits) {
  let best = null;
  for (const h of hits) {
    const k = refSortKey(h.ref);
    if (best === null || compareKeys(k, best) < 0) best = k;
  }
  return best ?? [999, 0, -1, 1, 0, -1];
}

/**
 * Group key for a reference when grouping by reference: uses the starting
 * point only, so that spans (e.g. "Gen 2:3-7") group with their first item ("Gen 2:3").
 */
function refGroupKey(ref) {
  const p = parseRef(ref);
  if (!p) return `~${ref}`;
  const v = p.verseStart === null ? "" : p.verseStart;
  return `${p.bookIdx}|${p.chapterStart ?? 0}|${v}`;
}

/** Human-readable header for a group, derived from the starting reference. */
function refGroupLabel(ref) {
  const p = parseRef(ref);
  if (!p) return ref;
  if (p.chapterStart === null) return p.book;
  if (p.verseStart === null) return `${p.book} ${p.chapterStart}`;
  return `${p.book} ${p.chapterStart}:${p.verseStart}`;
}

/**
 * Render a reference-centric view: groups of references (keyed by starting
 * point), each containing spans first, then individual refs; under each ref
 * is a list of video hits (timestamp + snippet).
 */
function renderByReference(matchedRefs, container) {
  // Organize by group key → ordered list of refs → list of hits
  const groups = new Map();
  for (const ref of matchedRefs) {
    const entries = indexData.references[ref];
    if (!entries || entries.length === 0) continue;
    const key = refGroupKey(ref);
    if (!groups.has(key)) groups.set(key, { label: refGroupLabel(ref), refs: [] });
    groups.get(key).refs.push({
      ref,
      key: refSortKey(ref),
      hits: entries.slice().sort((a, b) => {
        const va = (videosData.videos[a.video_id]?.published_date) || "";
        const vb = (videosData.videos[b.video_id]?.published_date) || "";
        if (va !== vb) return vb.localeCompare(va); // newest first within a ref
        return a.timestamp - b.timestamp;
      }),
    });
  }

  // Sort groups by their minimum ref key (which is also the group's starting point)
  const groupList = Array.from(groups.values()).map((g) => {
    g.refs.sort((a, b) => compareKeys(a.key, b.key));
    return { ...g, key: g.refs[0].key };
  });
  groupList.sort((a, b) => compareKeys(a.key, b.key));

  const totalHits = groupList.reduce(
    (n, g) => n + g.refs.reduce((m, r) => m + r.hits.length, 0),
    0,
  );
  const limit = 100;
  const shown = groupList.slice(0, limit);

  const html = shown.map(buildRefGroupCard).join("");
  container.innerHTML =
    html +
    (groupList.length > limit
      ? `<p class="hint">Showing first ${limit} of ${groupList.length} reference groups (${totalHits.toLocaleString()} total hits) – refine your search to narrow results.</p>`
      : "");
}

/** Build HTML for a reference group (header + nested refs + hits). */
function buildRefGroupCard(group) {
  const refsHtml = group.refs.map((r) => {
    const hitsHtml = r.hits.map((h) => {
      const video = videosData.videos[h.video_id];
      if (!video) return "";
      const videoUrl = `https://www.youtube.com/watch?v=${esc(h.video_id)}`;
      const tsUrl = `${videoUrl}&t=${h.timestamp}`;
      const timeStr = formatTime(h.timestamp);
      const title = esc(video.title || h.video_id);
      const snippetHtml = h.snippet
        ? `<span class="snippet">&ldquo;${boldRef(esc(h.snippet), r.ref)}&rdquo;</span>`
        : "";
      return `<a href="${tsUrl}" target="_blank" rel="noopener noreferrer" class="hit-line ref-hit-line">
        <span class="ref-hit-title">${title}</span>
        <span class="timestamp">⏱ ${timeStr}</span>
        ${snippetHtml}
      </a>`;
    }).join("");
    return `<div class="ref-entry">
      <div class="ref-entry-header"><span class="hit-ref">${esc(r.ref)}</span>
        <span class="hit-count">${r.hits.length} match${r.hits.length === 1 ? "" : "es"}</span>
      </div>
      <div class="hit-lines">${hitsHtml}</div>
    </div>`;
  }).join("");

  return `<div class="ref-group-card">
    <div class="ref-group-header">${esc(group.label)}</div>
    ${refsHtml}
  </div>`;
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

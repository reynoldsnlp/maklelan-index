/**
 * McClellan Scripture Index – frontend app
 *
 * Loads docs/data/index.json and docs/data/videos.json then provides
 * a live-search interface keyed by scripture reference.
 */

/* ── State ─────────────────────────────────────────────────────── */

/** @type {{ updated: string|null, references: Record<string, Array<{video_id:string, timestamp:number, snippet:string}>> } | null} */
let indexData = null;

/** @type {{ videos: Record<string, {id:string, title:string, published:string, thumbnail:string, duration:string, url:string}> } | null} */
let videosData = null;

/** Sorted list of all canonical references (for quick filtering) */
let allRefs = [];

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
  input.focus();

  // Show hint on empty state
  render("");
}

/* ── Search / filter ───────────────────────────────────────────── */

/**
 * Filter allRefs to those matching *query* and render results.
 * @param {string} query
 */
function render(query) {
  const q = query.trim().toLowerCase();
  const container = document.getElementById("results-list");

  if (!q) {
    container.innerHTML = `<p class="hint">Start typing a book name, chapter, or verse above.</p>`;
    return;
  }

  const matched = allRefs.filter((ref) => ref.toLowerCase().includes(q));

  if (matched.length === 0) {
    container.innerHTML = `<p class="no-results">No results for <strong>${esc(query)}</strong>.</p>`;
    return;
  }

  const limit = 200; // avoid painting thousands of cards at once
  const shown = matched.slice(0, limit);
  const html = shown.map(buildRefCard).join("");

  container.innerHTML =
    html +
    (matched.length > limit
      ? `<p class="hint">Showing first ${limit} of ${matched.length} matches – refine your search to narrow results.</p>`
      : "");
}

/* ── Card builder ───────────────────────────────────────────────── */

/**
 * @param {string} ref  canonical scripture reference
 * @returns {string} HTML for one reference card
 */
function buildRefCard(ref) {
  const entries = indexData.references[ref];
  if (!entries || entries.length === 0) return "";

  const entriesHtml = entries
    .map((entry) => {
      const video = videosData.videos[entry.video_id];
      if (!video) return "";

      const ts = entry.timestamp;
      const timeStr = formatTime(ts);
      const ytUrl = `https://www.youtube.com/watch?v=${esc(entry.video_id)}&t=${ts}`;
      const thumbHtml = video.thumbnail
        ? `<img src="${esc(video.thumbnail)}" alt="" class="thumbnail" loading="lazy">`
        : "";
      const snippetHtml = entry.snippet
        ? `<span class="snippet">&ldquo;${esc(entry.snippet)}&rdquo;</span>`
        : "";

      return `<div class="entry">
  <a href="${ytUrl}" target="_blank" rel="noopener noreferrer" class="video-link">
    ${thumbHtml}
    <div class="entry-info">
      <span class="video-title">${esc(video.title)}</span>
      <span class="timestamp">⏱ ${timeStr}</span>
      ${snippetHtml}
    </div>
  </a>
</div>`;
    })
    .join("");

  return `<div class="ref-group">
  <h2 class="ref-title">${esc(ref)}</h2>
  <div class="ref-entries">${entriesHtml}</div>
</div>`;
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

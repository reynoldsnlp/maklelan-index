# maklelan-index

A scripture reference index of Dan McClellan's ([@maklelan](https://www.youtube.com/@maklelan)) YouTube videos.

## What is this?

This project scrapes Dan McClellan's YouTube channel, extracts scripture references from video
transcripts (closed captions), and builds a searchable index hosted on GitHub Pages.

## Usage

Visit the **[GitHub Pages site](https://reynoldsnlp.github.io/maklelan-index/)** and type any
scripture reference (e.g. `Genesis 1:1`, `John 3:16`, `1 Cor 15`) into the search box.  Results
show every video and the exact timestamp where Dan discusses that passage—click a result to jump
straight there on YouTube.

## How it works

1. `scraper/scrape.py` fetches the channel's video list and downloads transcripts (closed captions)
   from YouTube using the `innertube` library for reliable API access and `requests` for caption XML.
2. `scraper/bible_books.py` parses transcripts for scripture references using regex.
3. The index is written to `docs/data/` as JSON files and committed to the repository.
4. GitHub Actions runs the scraper every Sunday and on manual dispatch.
5. The `docs/` directory is served via GitHub Pages.

## Setup (for forks / fresh deployment)

1. **Enable GitHub Pages**: Settings → Pages → Source → *Deploy from a branch* →
   branch `main` (or your default), folder `/docs`.
2. The scraper runs automatically every Sunday at 06:00 UTC.
3. To trigger a manual run: Actions → *Scrape and Update Index* → *Run workflow*.

## Local development

```bash
uv sync
uv run python scraper/scrape.py
```

The generated data lands in `docs/data/`.

## License

See [LICENSE](LICENSE) for details.


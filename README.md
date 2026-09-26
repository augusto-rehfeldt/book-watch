# Calibre Book Recommender

Calibre Book Recommender builds a local HTML report of recently released English-language books that match your Calibre library and the genres you want right now.

It reads Calibre through `calibredb` and never changes the library. Book data comes from Google Books, Open Library, Hardcover, and Reactor. OpenAI OAuth can optionally rank releases using your ChatGPT account, chosen genres, and aggregate signals from your library, and detect translated or retitled books you already own.

## Requirements

- Python 3.11 or newer
- [Calibre](https://calibre-ebook.com/) with `calibredb` on `PATH`
- Node.js with `npx` (only for OpenAI OAuth)
- No third-party Python packages

## Quick start

1. Edit `config.toml` and set `[library].path` to your Calibre library.
2. Check the installation and generate a report:

```powershell
python book_watch.py self-test
python book_watch.py run
```

When AI enrichment is needed, Book Watch starts the loopback-only OpenAI OAuth
proxy automatically. On first use, `npx` may ask to download it and a browser
may open for sign-in. This happens only when OpenAI OAuth is the provider actually
in use — the report's model picker loads a gateway's model list when you select it,
not on every page load. The configured model is checked against the proxy's live
text-model list; context and output limits are not currently reported.

Reports are written to `reports/`. `run` writes the report as soon as the candidates
are scored, then serves it on `127.0.0.1:8787` and opens a browser tab: the page you
are reading fills itself in as AI ranking, cover lookups and Mobilism links finish,
and reloads every 8 seconds until the run is done. It keeps serving (so the Download
buttons work) until Ctrl-C. Use `run --no-serve` to write the report and exit, and
`--port`/`--no-open` to change the port or keep the browser shut.

## Configuration

`config.toml` controls watched authors and series, completed or ignored series, genre filters, publication dates, sources, caching, and how many library authors are checked per run.

API keys are optional. Put any you use in a local `.env` file:

```dotenv
GOOGLE_BOOKS_API_KEY=...
HARDCOVER_API_TOKEN=...
MOBILISM_USERNAME=...
MOBILISM_PASSWORD=...
```

`.env` is ignored by Git. OpenAI OAuth needs no API key; it reuses the local
Codex credentials in `~/.codex`. Without a Hardcover token, Hardcover is skipped.

`[mobilism]` adds a link to the forum topic for a release, so a report card can
point at one. The step is skipped without credentials, because the forum refuses
searches from logged-out visitors.

With `[mobilism] discover = true` the forum is also searched for the run's owned
series and authors, and its release topics become candidates. It indexes the indie
serials that Google Books and Open Library barely carry, which is where most missed
series continuations were hiding. Bundle topics ("… Series", box sets, omnibuses) and
audiobook or comic formats are dropped as candidates in their own right — but a series'
bundle topic is opened and read, because it lists every volume of that series and is kept
current. Anything numbered past the newest volume in your library is reported as a
continuation, provided the topic is by the same authors you own.

Each run also rotates through the library's own series (`[run] series_per_run`, least
recently checked first, newest volume first), not just the ones hand-listed in
`[watch] ongoing_series`.

With `[mobilism] download = true`, `download` also falls back to that topic's
file-host mirrors when Library Genesis has no usable copy. Those mirrors are
gated by a Cloudflare Turnstile widget that only a real, headed Chrome gets a
token for, so this needs `pip install playwright` and Google Chrome installed:
a Chrome window opens off-screen for up to a couple of minutes per book, and
only the direct file link comes back into the script. Without either of them the
fallback reports itself as skipped and nothing else changes.

## Commands

`python book_watch.py --config <path> <command>` uses a `config.toml` other than the default (the flag goes before the command).

```powershell
# Normal report
python book_watch.py run

# Steer discovery and AI ranking toward one or more genres
python book_watch.py run --genre "cozy fantasy"
python book_watch.py run --genre "historical fiction" --genre mystery

# Check particular authors or series without general genre searches
python book_watch.py run --focused --author "Adrian Tchaikovsky" --series "Children of Time"

# Disable AI, or run entirely from cached/local data
python book_watch.py run --no-ai
python book_watch.py run --no-network --no-ai

# Pick the AI provider for ranking, or widen the rotation of owned authors/series
python book_watch.py run --ai claude
python book_watch.py run --max-authors 40 --max-series 20

# Ignore the HTTP cache for this run
python book_watch.py run --refresh

# Write the report and exit instead of serving it (scheduled runs)
python book_watch.py run --no-serve

# Keep or dismiss a candidate from future reports
python book_watch.py decide work:0123456789abcdef keep
python book_watch.py decide work:0123456789abcdef dismiss

# Rebuild existing reports with the current layout
python book_watch.py update-reports

# Open the latest report in a browser with working Download buttons
python book_watch.py serve

# Download a candidate and add it to Calibre with its metadata
python book_watch.py download work:0123456789abcdef

# Download everything marked keep
python book_watch.py download --all-keeps

# List what has already been downloaded
python book_watch.py download --list --limit 100

# Re-run the Calibre import for downloads whose file was saved but not imported
python book_watch.py retry-imports
```

## Downloading

Two ways in. A served report (`run`, or `serve` for an existing one) gives every card a
**Download to Calibre** button; the button reports what happened and stays disabled for
books already fetched. `download` does the same from the command line, taking the
candidate ids shown at the bottom of each card (the same ids `decide` uses).

For more than one book at a time, tick the checkbox on each card and press **Download
selected** — shift-click a second checkbox to tick everything between it and the last one
you clicked, skipping cards the current filter hides — or narrow the report with the search box and category chips and press
**Download shown** with nothing ticked. Books are fetched one after another, with
progress next to the button. The **Downloaded** chip filters the report down to what has
already been added to Calibre.

The **Downloads** panel above the book sections lists what the server is fetching right
now — including requests still queued behind the one in progress — and what it has
already filed, refreshing every few seconds while anything is running. `download --list`
prints the same history from the command line.

While a run is still filling the report in, the page reloads itself every few seconds,
but never while a book is open or a download is in flight.

When Library Genesis matches nothing, the configured AI provider is asked for other
titles the same book is published under (retitle, original language, with or without a
subtitle) and each is searched before the Mobilism fallback. Turn it off with
`download --no-ai` or `[download] ai_assist = false`.

Opening `reports/latest.html` straight from disk still works — the buttons are then
disabled and say so, because a `file://` page has no server to run `calibredb` for it.
Reports written before this feature get their buttons with `update-reports`.

Both routes search Library Genesis and add what they find to your Calibre library.

A libgen result is accepted only if its ISBN matches the candidate's, or its title
carries at least `min_title_match` of the wanted title's words *and* one of the
candidate's surnames appears in the author column. Formats are tried in the order set
in `[download].formats`, smallest file first, and a body whose magic bytes contradict
its extension is discarded as an error page. Files are kept in `downloads/`.

Calibre receives the title, authors, ISBN, series and index, tags, language and cover
on the `calibredb add` call, and publisher, publication date and description through a
follow-up `set_metadata`. Adding runs without `--duplicates`, so a book Calibre already
has under the same title and author is reported as a duplicate and nothing in the
library changes. Every import is recorded, so `--all-keeps` never downloads the same
book twice; pass `--force` to repeat one.

The server binds to loopback only and mints a random session token per run, injected
into the page it serves. `POST /download` is refused without that token, so another
site open in the same browser cannot make your machine fetch books.

Run `python book_watch.py --help` or `python book_watch.py run --help` for every option.

## Data and privacy

Generated reports, the cached Calibre snapshot, HTTP responses, decisions, and run history stay under `reports/` and `data/`; downloaded book files stay under `downloads/`. All three directories are ignored by Git. If Calibre is open or unavailable, the recommender uses the last successful snapshot.

The unofficial OpenAI OAuth proxy sends candidate metadata, publisher descriptions, aggregate library counts, top tags and authors, and relevant same-author title names using your ChatGPT account. It does not receive book files, Calibre paths, or personal notes. Treat the OAuth credentials in `~/.codex` like a password.

## Tests

```powershell
python -m unittest -v
```

## Missing lists and library browsing

```powershell
python book_watch.py import-missing missing.csv
python book_watch.py lookup --help
python book_watch.py export-atlas --output data/atlas/library.csv
python book_watch.py run --like 123
```

Import requires Title and Author columns (Series/Index optional); CSV uses the stdlib,
and XLSX input reuses the finder reader. Imported requests join report history even
when no publication date is known. Import itself never downloads or changes Calibre.
Use a report button or the existing `download` command to fetch/import a candidate.

The [Anna lookup](FINDER.md) implementation moved here from book_finder. It keeps its
existing CLI and optional `requests` dependency (`requirements-finder.txt`), while
downloads use book-watch's parser, validation and history. Files saved by lookup can
later be imported into Calibre without another fetch. Different existing files are
never overwritten; numbered filenames preserve them.

`export-atlas` reads the configured Calibre library directly, supports normalized
text and comments-style custom columns, and writes stable IDs, summary/comment text,
authors, series and cover paths. Override `--summary-column '#your_column'` if needed.
From Shelfscape, run `python backend/app.py --stories ../book-watch/data/atlas`.
The export is local and read-only with respect to Calibre. `run --like ID` focuses
release discovery on the authors/series of that Calibre book.

Checks: `python -B -m unittest -q test_book_watch test_library_exchange` and
`python book_watch.py self-test`; legacy parser checks remain in book_finder.

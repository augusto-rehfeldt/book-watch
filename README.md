# Calibre Book Recommender

Calibre Book Recommender builds a local HTML report of recently released English-language books that match your Calibre library and the genres you want right now.

It reads Calibre through `calibredb` and never changes the library. Book data comes from Google Books, Open Library, Hardcover, and Reactor. OpenRouter can optionally rank releases using your chosen genres plus aggregate signals from your library's tags and authors, and detect translated or retitled books you already own.

## Requirements

- Python 3.11 or newer
- [Calibre](https://calibre-ebook.com/) with `calibredb` on `PATH`
- No third-party Python packages

## Quick start

1. Edit `config.toml` and set `[library].path` to your Calibre library.
2. Check the installation and generate a report:

```powershell
python book_watch.py self-test
python book_watch.py run
```

Reports are written to `reports/`; open `reports/latest.html` in a browser.

## Configuration

`config.toml` controls watched authors and series, completed or ignored series, genre filters, publication dates, sources, caching, and how many library authors are checked per run.

API keys are optional. Put any you use in a local `.env` file:

```dotenv
GOOGLE_BOOKS_API_KEY=...
HARDCOVER_API_TOKEN=...
OPENROUTER_API_KEY=...
```

`.env` is ignored by Git. Without an OpenRouter key, AI ranking is skipped; without a Hardcover token, Hardcover is skipped.

## Commands

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

# Ignore the HTTP cache for this run
python book_watch.py run --refresh

# Keep or dismiss a candidate from future reports
python book_watch.py decide work:0123456789abcdef keep
python book_watch.py decide work:0123456789abcdef dismiss

# Rebuild existing reports with the current layout
python book_watch.py update-reports
```

Run `python book_watch.py --help` or `python book_watch.py run --help` for every option.

## Data and privacy

Generated reports, the cached Calibre snapshot, HTTP responses, decisions, and run history stay under `reports/` and `data/`; both directories are ignored by Git. If Calibre is open or unavailable, the recommender uses the last successful snapshot.

OpenRouter receives candidate metadata, publisher descriptions, aggregate library counts, top tags and authors, and relevant same-author title names. It does not receive book files, Calibre paths, or personal notes.

## Tests

```powershell
python -m unittest -v
```

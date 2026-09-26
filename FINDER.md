# Anna lookup (formerly book_finder)

A small book lookup pipeline for matching a CSV of missing books against Anna's Archive and validating the best candidate with book-watch's configured AI provider
(ai-suite's shared AIService, like every AI script in the workspace).

## What it does

- Reads books from a gitignored path file (`.missing_books_path`) that points to your missing-books CSV
- Searches Anna's Archive for each title/author pair
- Filters likely matches
- Validates candidates with book-watch's AI provider (`[ai] provider` in its config)
- Writes confirmed matches to `found_books_validated.txt`

## Requirements

- Python 3.10+
- `requests`
- book-watch's AI provider configured (default Command Code; see book-watch's README)
- The sibling `ai-suite` checkout (or `AI_SUITE_DIR`; a clone falls back to its vendored `ai_suite/`)

Install dependencies:

```bash
pip install -r requirements-finder.txt
```

## Input

The default input is taken from a gitignored file named `.missing_books_path` in the repo root. Put the full path to your CSV in that file, for example:

```text
.local/missing_books.csv
```

You can still override it explicitly with `--input-file`.

- `Title`
- `Author`
- `Series`
- `Index`

Example:

```csv
Title,Author,Series,Index
The Lost Battleship,Vaughn Heppner,Lost Starship,24
```

## Usage

Run with the default files:

```bash
python finder.py
```

Or pass explicit paths:

```bash
python finder.py \
  --input-file /path/to/missing_books.csv \
  --output-file found_books_validated.txt \
  --model <model id for the configured provider>   # optional
```

Verbose mode shows per-book progress:

```bash
python finder.py --verbose
```

## Downloading the books

`--download-dir` fetches the file itself from Library Genesis for every book that gets
a result, preferring `epub`, then `azw3`/`mobi`/`fb2`, with `pdf` as a last resort:

```bash
python finder.py --download-dir ~/books
```

Files are saved as `<Author> - <Title>.<ext>`; an existing file of that name is never
overwritten. Candidates are gated on title and author separately, so a different book
by the same author is not downloaded. Anna's Archive is unreachable on some networks
(ISP DNS blocks, Cloudflare), and this route works anyway: with the flag set, a book
with no Anna's Archive result is searched on Library Genesis directly. Without the
flag nothing is downloaded and the behaviour is unchanged.

## Output

The output file contains one validated match per line in a pipe-separated format:

```text
Title | Author | Link | Match title (FORMAT) | Reason: AI Validation (Confidence: 91) | Saved: /path/to/Author - Title.epub
```

The `Saved:` field appears only with `--download-dir`. A book found on Library Genesis
alone reads `Reason: direct Library Genesis download, <why Anna's Archive gave nothing>`.

## Notes

- No key file: keys come from book-watch's provider configuration (`.env`, Crush or
  opencode logins). An old `api_key.txt` is no longer read.
- The CLI is quiet by default and only prints a final summary unless `--verbose` is set.
- Search retries are built in for transient request failures.
- If a book can't be matched, the CLI prints a hint pointing to the `annas-mcp` project (`book-search` / `book-download`) so you can try a download workflow for permitted copies.


## Shared ownership and history

`finder.py` and `finder_ai.py` are owned here. `book_watch.py lookup` exposes the same
CLI. The old `../book_finder/anna_scraper.py` forwards here. Existing local credential
and missing-list path files are read in place; no credentials were moved. Prefer
`OPENAI_API_KEY` in the environment. `--config` selects the book-watch configuration
for downloads. The common parser enforces separate title and author gates, checks
file signatures and sizes, and never overwrites a different existing file.

Downloads enter book-watch's SQLite history as file-only records. A later
`book_watch.py download <candidate_id>` imports the existing file without fetching
it again. Lookup without `--download-dir` does not import into Calibre.

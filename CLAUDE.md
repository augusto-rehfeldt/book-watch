# Calibre Book Recommender (book-watch)

## What This Is
Builds a local HTML report of new releases matching your Calibre library + chosen genres; Google Books/Open Library/Hardcover/Reactor + AI ranking via commandcode (default), Hyper/OpenCode Zen/Claude/OpenRouter/OpenAI OAuth.

## Non-Negotiables
- Read-only access to Calibre everywhere except `download`, which adds new books
  through `calibredb add`. Nothing ever edits or deletes a book already in the library:
  `add_to_calibre()` runs without `--duplicates`, so a title/author Calibre already has
  is reported as a duplicate and the library is untouched.
- Never log or commit secrets from `.env` or `~/.codex`.

## Commands
- Self-test: `python book_watch.py self-test`
- Report (serves it too, blocks until Ctrl-C): `python book_watch.py run [--ai commandcode|hyper|opencode|claude|openrouter|openai_oauth|none] [--no-serve] [--port 8787] [--no-open]`
- Download + import: `python book_watch.py download <candidate_id> | --all-keeps [--no-ai]`
- Serve an existing report: `python book_watch.py serve [--port 8787] [--no-open]`
- Help: `python book_watch.py --help`

## Gotchas
- **The AI default is `commandcode`, a CLI provider, not an HTTP gateway.** It runs
  `cmdc -p --output-format text --model <model>` through **book writer's own adapter**
  (`commandcode_adapter()` loads `book writer/ai_book_creator/services/ai_service.py` by
  file path — the same module music-writer and mathforge use — plus its
  `ai_config_commandcode.json` for the model list). Auth is the user's Command Code
  login; `ai_key()` returns a sentinel for `cli:` providers so `resolve_ai_provider`
  treats it as usable. Two hard rules: **CLI providers are never a silent fallback** —
  the `resolve_ai_provider` scan skips them, so a dead hyper key falls back to
  opencode/OAuth exactly as before and never starts shelling out to `cmdc`; and in
  tests the adapter must be patched (`commandcode_adapter`), or a test will invoke the
  real CLI and hang for tens of seconds. The report's provider picker defaults to it
  and its model list comes from book writer's config, not a `/models` call.
- **Shared AI plumbing comes from book writer.** `book_writer_ai()` loads book writer's
  `ai_service.py` once by file path (`BOOK_WATCH_BOOK_WRITER` overrides the location);
  `commandcode_adapter()`, `ensure_openai_oauth_proxy()` and `opencode_auth_key()` delegate
  to it instead of keeping copies. Provider choice, key discovery (Crush, `AW_API_KEY`),
  fallback order, model validation and the HTTP ranking/assist calls stay here: they are
  book-watch's own settings, with fail-fast timeouts a report needs, whereas book
  writer's `generate_content` waits out usage limits for hours.
- **The sources run concurrently; the throttles are still per host.** `fetch_sources`
  submits one `fetch_source` task per enabled source to a thread pool — each task opens
  its **own** `connect_state` connection (pool threads must never share one) and Mobilism
  keeps its whole loop inside a single task because its forum login is shared. Inside a
  source, `google_candidates`/`open_library_candidates`/`fill_missing_covers` run their
  per-query/per-candidate pools (`[sources] query_workers`, default 4) over a
  `LockedConn` proxy around the one connection — `cached_request` only touches it for the
  http_cache rows, never during the network call, and the `_throttle_lock` keeps the
  per-host rate (`rate_seconds`) atomic under threads. `connect_state` is created with
  `check_same_thread=False` for this. Results are flattened in submission order, so
  candidate order and error labels stay deterministic no matter who finishes first.
- **AI providers**: `enrich_with_openrouter()` picks a provider from `--ai`, else
  `[ai] provider` (shipped as `commandcode` — see the CLI gotcha above),
  else the `[openai_oauth]`/`[openrouter]` sections, else Hyper again.
  `resolve_ai_provider()` falls back when the chosen provider has no usable key (missing
  env var, expired Crush token): other configured keyed providers first, then
  `[openai_oauth]` last, because its npx proxy may need a browser sign-in. CLI providers
  are skipped by that scan (see above). This is what
  keeps categorization and ranking alive when hyper's Crush JWT expires — without the
  fallback every AI stage silently no-ops and `ai_categories` stays empty.
  Keys come from each section's `api_key_env` in `.env`; hyper also accepts article-writer's
  `AW_API_KEY` and then the key Crush's CLI stored at login (`crush.json` under
  `%LOCALAPPDATA%`, `~/.local/share` or `~/.config`), and opencode Zen falls back to the
  key opencode's CLI already stores at `~/.local/share/opencode/auth.json`. The served report's provider/model picker POSTs
  `/rerank`, which re-ranks the cards in `latest.html` (undoing the old fit bonus first)
  and writes a fresh report. Model ids are validated against the gateway's `/models`
  where one is public; Anthropic's is not, so a bad `[claude] model` fails per batch.
- **The report updates in place; it must never reload itself.** While stages are
  pending the page fetches its own file and merges it (`refreshReport()`) instead of
  `location.reload()`: search text, the active kind/category chips, ticked checkboxes
  (re-checked by `data-key` after the swap), the download queue and the downloads panel
  all live in JS state or elements the merge never touches. Chip clicks and checkbox
  shift-click are **document-level delegated** listeners — per-element bindings die with
  the first merge. The merge replaces `.summary`, each `.book-section`'s cards (matched
  by `h2` heading text), the run-details `<details>`, and the `.filter-buttons` chips
  (aria-pressed re-applied from `cats`/`kind`). A missed tick (busy, open dialog, hidden
  tab) just waits for the next one; the run's final write has no `BW_REFRESH` script and
  stops the timer. `location.reload()` survives only as the `file://` fallback, and a
  successful `/rerank` merges the same way.
- **The downloads panel is grouped and color-coded** (`renderDownloads()`): In progress /
  Failed / Added to Calibre / Saved, not imported, with `dl-queued`/`dl-running`/
  `dl-failed`/`dl-done`/`dl-saved` row classes and status dots. `--ok` is the success
  color. Cards and picks are always re-read from the DOM (`allCards()`/`allPicks()`) —
  never snapshot them at load.
- **A download always gets a cover if one can be found.** `fetch_cover(candidate,
  config, conn)` tries the candidate's URLs (5), then `cover_lookup()` — the same
  Google Books/Open Library gates as the report's cover stage (exact normalized title,
  overlapping authors; `use_isbn=True` adds an `isbn:` query) — and merges the found
  URLs back into the stored payload so the search runs once ever. The cover only ever
  attaches to the book being added now (`calibredb add --cover`); a duplicate add
  returns None and nothing already in Calibre is touched.
- **`calibredb add` cannot set publisher, pubdate or comments.** `add_to_calibre()`
  applies title, authors, ISBN, series, series index, tags, language and cover on the
  `add` call, then runs `set_metadata --field` for the other three against the book id
  parsed out of «Added book ids: N». No id in the output means Calibre treated it as a
  duplicate, which is the only "already owned" check the command needs.
- **Library Genesis serves the search page only to a browser-looking client**
  (`BROWSER_UA`), and its `/ads.php` download key is minted per page view — that page is
  fetched with `cache_hours=0` so a cached key is never replayed. Search pages go
  through the normal HTTP cache.
- **`parse_libgen_rows()` uses quote-aware cell text and the edition link's title.**
  Tooltip HTML cannot contaminate the title. It accepts compact mirror rows and
  swapped title/author columns; separate matching gates reject same-author sequels.
- **A download is verified before it is filed** (`looks_like_book`): under 20 KB, or a
  body whose magic bytes contradict the extension, is a libgen error page.
- **The failure message only names sources that answered.** `libgen_fetch(errors=[…])`
  records a mirror that timed out, and `download_candidate()` drops it from
  «no usable copy found on …» and appends the reason instead; same for a Mobilism
  attempt that threw. A source that was never reached must never be reported as one
  that had nothing — libgen times out often enough that this is the common case.
- **Mobilism needs a login.** `search.php` answers logged-out visitors with "not
  permitted to use the search system", so `mobilism_session()` posts the phpBB login
  form (`sid` from the form page, `autologin=on`) and keeps the cookie jar for the whole
  run — one login, not one per candidate. `cached_request(opener=...)` then reuses the
  normal HTTP cache and throttle. Use `forum.mobilism.org`: the `.me` domain sits behind
  an interactive Cloudflare challenge that no HTTP client gets through.
- **The Mobilism mirrors need a real, headed Chrome** (`mirror_direct_link()`, the
  fallback `download_candidate()` tries after libgen). The topic links file hosts running
  the same XFileSharing script, whose free page is gated by a Cloudflare Turnstile
  widget. Nothing else works: plain HTTP, cloudscraper and curl_cffi never get a token,
  and neither do headless Chrome or a Playwright-launched browser — the widget just sits
  at an empty `cf-turnstile-response` forever. What does work is launching the user's own
  Chrome with `--remote-debugging-port` (so it carries no automation switches) and
  attaching over CDP; the widget then solves itself in ~10s. The window is parked at
  `--window-position=-32000,-32000` instead of being made headless, and popups are killed
  with `--block-new-web-contents` because the mirrors hijack or close the tab otherwise.
  Do not add request blocking — blocking ad hosts stalls the widget. `headless_first`
  (default on) tries `--headless=new` for `headless_seconds` before that, in its own
  profile directory, so a working headless run costs no window at all; every headless
  attempt so far has come back empty, which is why the headed retry stays.
- **A download reuses the topic the run already found.** Mobilism-sourced candidates keep
  their topic URL in `evidence`, `persist_candidates()` stores it and
  `candidate_from_payload()` restores it, so `mobilism_fetch()` skips the search — which
  otherwise might match a different topic, or none.
- **An expired Crush token is not a key.** `crush_auth_key()` drops a stored JWT whose
  `exp` has passed (`jwt_expired()`), because handing it over earns an HTTP 401 on every
  AI batch and every fetch assist instead of one clear "not configured" message.
- **Everything on a mirror page is clicked through the DOM**, never with a real click:
  ad layers cover the buttons and eat pointer events. The final `download2` POST is sent
  with `fetch()` from inside the page rather than by submitting the form, because a
  navigation hands the tab to whichever ad script wins the race. Only the direct link
  comes back to Python, where `http_bytes()`/`looks_like_book()` verify it like a libgen
  file. A mirror with no `F1` form (a dead file, e.g. every cloudfam.io link so far) is
  skipped after 30s.
- **Owned series are rotated like authors** (`select_series()`, `series_checks` table,
  `[run] series_per_run`). Before that only hand-listed `[watch] ongoing_series` were ever
  queried, so a continuation surfaced only when an author or subject query happened to
  return it. Order: least recently checked, then newest owned volume (`series["latest"]`
  from `pubdate`), then most volumes owned; `complete_series`/`ignore_series` and
  one-book series are skipped.
- **Mobilism is also a candidate source** (`mobilism_candidates()`, `[mobilism] discover`),
  because Google Books and Open Library barely index the indie serials this library is
  full of. It searches each rotated series and author with `sf=titleonly`, and
  `parse_mobilism_release()` turns release topics into candidates: format tag must be an
  ebook one, "… Series/box set/omnibus" bundles are dropped, the date is the newest of the
  row's topic and last-post dates, and an author query keeps only rows whose authors pass
  `author_matches()` (a bare surname search lands on every other Roberts). A `series:`
  query stamps its series on what it finds, so an unowned volume reports as a continuation.
  Topic titles put the author after the last " by ", never the first — "Bound by Trust by
  Y.V. Larson" is one title.
- **A series' own forum topic answers "is there a book after the one I own?"**
  (`mobilism_series_continuations()`, `[mobilism] series_topics`). The bundle topic is kept
  current with the series and opens with a numbered volume list, so
  `parse_mobilism_volumes()` reads it and anything past `max_index` becomes a candidate.
  Three gates keep it honest: the bundle's authors must overlap the owned series' authors
  («First Contact» as a name matches half the forum), the topic is chosen by *reverse*
  title overlap so "Backyard Starship Series" wins over "Backyard Starship: Origins srs",
  and only volumes past the newest owned one are emitted, because the only date available
  is the topic's latest post — right for a new release, wrong for a gap further down.
  That last-post date is often written "Today"/"Yesterday", which the row parser converts.
  `parse_mobilism_volumes()` splits on the numbering instead of matching whole entries,
  because a volume's blurb runs for thousands of characters ("1. Call Me Ares - Long live
  the soldiers!…"); the title is what precedes the dash, capped at ten words for the posts
  that run the blurb on with no separator. A number more than 10 past the newest owned
  volume came out of a blurb, not the list, and is dropped.
- **`fid[]=106` does not mean "ebooks only".** It searches the whole eBooks category,
  whose children include Audiobooks (124), Comics (311) and Magazines (123). The gate
  that keeps an `.M4B` out of the report is the format tag every release title ends in
  (`Title by Author (.ePUB)+`), matched against `download.formats`. `sc=0` would confine
  the search to forum 106 itself, which holds no topics at all.
- **`download_candidate()` is the single path**; the CLI loop and the report's Download
  button both call it. Anything added to one route belongs there, not in `download()`.
- **The report is written before it is filled.** `run_report()`'s `publish()` renders to
  the same file after scoring and again after each slow stage (AI, cover fallback,
  Mobilism), so `latest.html` exists within seconds. Faster still: the report is *seeded*
  from the previous run — candidate_history rows since the last completed run are
  re-scored and published before any network source runs, then a background worker
  thread (its own `connect_state` connection) refreshes Google Books/Open
  Library/Reactor/Hardcover/Mobilism in order and each source's finds are re-merged
  (`rebuild()` builds fresh Candidate objects from payloads so re-scoring never
  double-counts) and republished as they land. Every source emit is wrapped in its own
  try/except and leftover source labels are cleared from `pending` when the worker
  finishes, so a crashed source can never leave `window.BW_REFRESH` in the final write
  (whose timer reloads only when the page is idle
  (`!busy && !dialog.open && !document.hidden`)); do not go back to
  `<meta http-equiv="refresh">`: it closed the open book card and aborted the download
  or rerank request that was in flight. Report files are written atomically
  (`.html.tmp` + replace) because the served page re-reads them while the run rewrites
  them, and `state.sqlite` runs WAL with a 15 s busy timeout because the run, its
  worker thread and the report server each hold a connection.
- **`publish()` stores candidates before it writes.** The page is live while the run is
  still going, so a card whose row is not in `candidate_history` yet answers its Download
  button with «Unknown candidate id» — which is exactly what the early report did until
  `persist_candidates()` moved inside `publish()`.
- **Only `.filter[data-filter]` is a category chip.** `#bulk-download` and `#ai-rerank`
  borrow the `.filter` class for its styling; binding every `.filter` to the category
  handler set `kind` to `undefined` and hid every card as soon as either was clicked.
- **`run` serves by default.** It starts `report_httpd()` in a daemon thread right after
  the first write, opens a browser and blocks on the server when the pipeline ends.
  `--no-serve` restores the old write-and-exit behaviour (use it for scheduled runs).
- **The report's buttons need a served page.** The HTML is static, so a `file://` page has
  no way to run `calibredb`; the JS disables the button unless `window.BW_TOKEN` was
  injected, which only `report_server()` does. It binds 127.0.0.1 only and mints a
  per-run token — `POST /download` without it is a 403, which is what stops any other
  page in the browser from driving the local server. Do not widen the bind address.
- **Downloads are queued by the page, not by the modal.** `enqueue()`/`drain()` own the
  work: the card button and the bulk button both only enqueue, one `/download` POST is in
  flight at a time, and closing the book card cannot abort it (the fetch never belonged to
  the dialog). `state[key]` is `queued`/`running`/`failed: …`, `syncModal()` re-reads it
  whenever a card is opened, and the Downloads panel renders that queue plus `/downloads`
  (other tabs' work and the history). With nothing ticked the bulk button takes every card
  the search/filter leaves visible. There is no batch endpoint — the server still does one
  book at a time. The `Downloaded` filter chip reads `window.BW_STATUS`.
- **A failed Calibre import keeps the file — and is retried automatically.**
  `download_candidate()` records the download row with `imported=0` when
  `add_to_calibre()` throws, so the next attempt reuses the saved file and only re-runs
  the import instead of re-fetching from libgen. When the failure is calibre's own
  «Another calibre program … is running» lock (the GUI or a content server holds the
  library), the message says to close Calibre and retry — and three paths now do it for
  you: `run`/`serve` run `retry_pending_imports()` once at startup and, while the report
  is served, `import_retry_worker` retries every two minutes (a pass stops at the first
  lock message — every remaining row would fail identically); the Downloads panel's
  Failed/Saved rows carry a **Retry** button that enqueues the key through the normal
  one-at-a-time queue; and `python book_watch.py retry-imports` runs one pass on demand.
  `run_calibredb()` also strips calibre's own `SyntaxWarning` stderr noise («"\d" is an
  invalid escape sequence…» — emitted by calibredb.exe's Python, not by this code) so the
  reported cause is the real one.
- **`[library] path` must be the folder holding `metadata.db`.** Pointed anywhere else,
  `calibredb list` succeeds against a brand-new empty library it creates there, so the run
  reports 0 owned books and silently produces no series continuations and no author
  matches — everything lands in discovery. The config shipped `".."`, which had created an
  empty library at `D:\python`; the real one is `C:\Users\Augusto\Calibre Library`.
- **`calibredb list` exits 1 whenever the calibre GUI or content server is running**,
  no matter which library you point it at (the check is global, so copying `metadata.db`
  to a scratch folder does not dodge it). `load_calibre()` then falls back to
  `calibre_books_from_db()`, which reads `metadata.db` over a `mode=ro` sqlite URI and
  rebuilds the exact `list --for-machine` shape — authors joined with " & " and `|`
  unescaped back to a comma, fractional seconds stripped off the dates. `rating` is the
  one field it does not reproduce; nothing reads it. Only if that also fails does the run
  fall back to the stale `data/library.json` snapshot.
- **AI fetch assistance is on by default** (`[download] ai_assist`, off per command with
  `download --no-ai`): when libgen matches nothing, `ai_title_variants()` asks the ranking
  provider for other titles the same work is filed under and each one is searched as a
  `replace(candidate, title=…)` copy, so the usual title/author gate still applies. The
  prompt returns `{"titles": []}` for a book with no alternative title, which is normal.
- **One download at a time** (`report_server`'s `lock`): concurrent clicks would race
  the same libgen host past its throttle and the same Calibre database. `active` records
  each key as `queued` then `running` around that lock, which is what `GET /downloads`
  reports alongside `download_log()`'s history; `download --list` prints the same history
  from the CLI. Both `/downloads` and `/models` need the session token.
- **Model lists are fetched on demand.** `ai_providers_status()` (injected as `BW_AI` on
  every page load) is offline — just each provider's configured model. The live list comes
  from `GET /models?provider=…` → `ai_provider_models()`, called when the picker changes
  or the model select is focused, because listing openai_oauth's models is what starts the
  npx proxy and its browser sign-in. Do not put that back in the page-load path.
- **Candidate categories are cached, keyed by title+author.** `ai_categories` in
  `state.sqlite` stores `categorize_candidates()`'s verdicts (`category_key()` =
  `normalize_title(title)|normalize(first author)`), so a title is classified once ever:
  `apply_cached_categories()` fills candidates before `match_and_score()` and the AI pass
  only asks about the rest. Categories live on `Candidate.categories`, never inside
  `candidate.ai` — `apply_ai_results()` reassigns that dict wholesale during ranking.
  The taxonomy is the Calibre library's own tags (`category_taxonomy(catalog)`, most-used
  first; the generic `DEFAULT_CATEGORY_TAXONOMY` only covers a tag-less library), so the
  AI classifies into the vocabulary the library already uses. Cached names are validated
  against the current taxonomy when applied — entries from an older vocabulary are
  dropped, which is what sends those titles back through the AI instead of showing
  categories the report can no longer filter by. Because scoring has already run for
  uncached candidates, the AI stage re-applies taste via `apply_category_taste_delta()`, which
  scores the *difference* between the base haystack and the categorized one — a plain
  second `apply_taste_preference()` would double every hit the description already
  matched. In the report, cards and chips show `display_categories()`: the AI's verdict
  when present, otherwise source subjects mapped onto the library's tags
  (`SUBJECT_CATEGORY_SYNONYMS` bridges "Science Fiction" → "sci-fi" and friends), so
  filtering survives a down AI provider. Category chips (`data-filter="cat:…"`) are a
  second filter dimension beside the section chips: **every** category with a visible
  book gets a chip (no top-N cut), several can be active at once (a card matches when it
  carries any selected category), and the JS stores the bare name (`value.slice(4)`)
  while both sides lowercase the same way (`toLowerCase()`), so "historical" chips don't
  leak into "history" via substring matching.

## Consolidated book workflow

- This project owns `finder.py`, `finder_ai.py`, the shared download parser and history.
  `../book_finder` contains compatibility entry points and local credentials only.
- `library_exchange.py` owns missing-list import and read-only Calibre export. Preserve
  stable UUID IDs and support both normalized and direct custom-column layouts.
- `downloads.imported` distinguishes file-only saves from completed Calibre imports.
  `connect_state` migrates old databases; later imports reuse saved files.
- Check changes with `python -B -m unittest -q test_book_watch test_library_exchange`
  and `python ../book_finder/test_libgen_download.py`.

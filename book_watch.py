from __future__ import annotations

import argparse
import base64
import calendar
import csv
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import html
import http.server
import importlib.util
import json
import os
import queue
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import tempfile
import time
import tomllib
import unicodedata
import webbrowser
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, unquote, urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


VERSION = "0.2.0"
ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.toml"
OPENAI_OAUTH_PORT = 10531
STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    cache_key TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status INTEGER NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS author_checks (
    author_key TEXT PRIMARY KEY,
    author_name TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS series_checks (
    series_key TEXT PRIMARY KEY,
    series_name TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_history (
    candidate_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    authors TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    candidate_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('keep', 'dismiss')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    report_path TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS downloads (
    candidate_key TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    calibre_id INTEGER,
    file_path TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_categories (
    category_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    authors TEXT NOT NULL,
    categories TEXT NOT NULL,
    model TEXT NOT NULL,
    categorized_at TEXT NOT NULL
);
"""


def category_key(title: str, authors: Iterable[str]) -> str:
    first_author = next(iter(authors), "")
    return f"{normalize_title(title)}|{normalize(first_author)}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="seconds")


def progress(message: str) -> None:
    # One write for the whole line: source pools print from several threads and
    # print()'s two writes (text, then newline) can interleave mid-line.
    sys.stdout.write(f"[{datetime.now():%H:%M:%S}] {message}\n")
    sys.stdout.flush()


def redact_secrets(value: Any) -> str:
    return re.sub(r"([?&](?:key|api_key|token)=)[^&\s<\"]+", r"\1[redacted]", str(value), flags=re.I)


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def normalize_title(value: str) -> str:
    value = re.sub(r"\s*\([^()]*(?:(?:book|volume|series)\s*)?#?\d+(?:\.\d+)?[^()]*\)\s*$", "", value, flags=re.I)
    return normalize(value)


def split_authors(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = [str(item) for item in value]
    else:
        raw = re.split(r"\s+(?:&|and)\s+|\s*;\s*", str(value or ""))
    return [item.strip() for item in raw if item.strip()]


def clean_isbn(value: str) -> str:
    digits = re.sub(r"[^0-9Xx]", "", value or "")
    return digits.upper() if len(digits) in (10, 13) else ""


def isbn10_from_13(value: str) -> str:
    isbn = clean_isbn(value)
    if len(isbn) != 13 or not isbn.startswith("978") or not isbn.isdigit():
        return ""
    stem = isbn[3:12]
    check = (-sum((10 - index) * int(digit) for index, digit in enumerate(stem))) % 11
    return stem + ("X" if check == 10 else str(check))


def parse_identifiers(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, dict):
        pairs = value.items()
    else:
        pairs = re.findall(r"([a-zA-Z0-9_-]+):([^,]+)", str(value or ""))
    for key, item in pairs:
        if str(key).casefold() == "isbn":
            isbn = clean_isbn(str(item))
            if isbn:
                out.add(isbn)
    return out


def safe_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def candidate_key(title: str, authors: Iterable[str]) -> str:
    first_author = next(iter(authors), "")
    signature = f"{normalize_title(title)}|{normalize(first_author)}"
    return "work:" + hashlib.sha1(signature.encode("utf-8")).hexdigest()[:16]


def parse_dateish(value: str) -> date | None:
    value = str(value or "").strip()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        pass
    month_match = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if month_match:
        year, month = map(int, month_match.groups())
        try:
            return date(year, month, calendar.monthrange(year, month)[1])
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}", value):
        return date(int(value), 12, 31)
    match = re.search(r"\b(19|20)\d{2}\b", value)
    return date(int(match.group()), 12, 31) if match else None


def add_months(day: date, offset: int) -> date:
    month_index = day.year * 12 + day.month - 1 + offset
    year, month_zero = divmod(month_index, 12)
    return date(year, month_zero + 1, 1)


def load_env_file(path: str | Path) -> bool:
    path = Path(path).expanduser()
    if not path.exists():
        return False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)
    return True


def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    base = path.resolve().parent
    library = config.setdefault("library", {})
    for key in ("path", "snapshot"):
        value = Path(str(library[key])).expanduser()
        library[key] = str(value if value.is_absolute() else (base / value).resolve())
    config["state_path"] = str((base / "data" / "state.sqlite").resolve())
    config["report_dir"] = str((base / "reports").resolve())
    return config


def connect_state(config: dict[str, Any]) -> sqlite3.Connection:
    path = Path(config["state_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    # The Python-level thread check is off because query pools share one
    # connection through LockedConn; sqlite's own serialized mode makes that safe.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # The report run, its background source thread and the report server each hold a
    # connection; a reader no longer blocks the writer and a busy writer waits
    # instead of failing after the default 5 s.
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(STATE_SCHEMA)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if 'imported' not in {row[1] for row in conn.execute('PRAGMA table_info(downloads)')}:
            conn.execute('ALTER TABLE downloads ADD COLUMN imported INTEGER NOT NULL DEFAULT 1')
    return conn


def parse_noisy_json(text: str, expected: type = list) -> Any:
    decoder = json.JSONDecoder()
    starts = [i for i, char in enumerate(text) if char in "[{"]
    for start in starts:
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, expected):
            return value
    raise ValueError("No JSON payload found in command output")


def calibre_books_from_db(library: Path) -> list[dict[str, Any]]:
    """`calibredb` exits 1 while the calibre GUI (or content server) holds the library,
    whatever library you point it at, so read metadata.db directly instead. Read-only,
    and shaped exactly like `calibredb list --for-machine`."""
    conn = sqlite3.connect(f"file:{(library / 'metadata.db').as_posix()}?mode=ro", uri=True)
    try:
        books = {
            row[0]: {
                "id": row[0],
                "title": row[1],
                "authors": "",
                "series": "",
                "series_index": row[2],
                "tags": [],
                "isbn": "",
                "identifiers": {},
                # calibredb prints these without the fractional seconds the column keeps.
                "pubdate": re.sub(r"\.\d+", "", str(row[3] or "")).replace(" ", "T"),
                "last_modified": re.sub(r"\.\d+", "", str(row[4] or "")).replace(" ", "T"),
            }
            for row in conn.execute("SELECT id, title, series_index, pubdate, last_modified FROM books")
        }
        authors = "SELECT l.book, a.name FROM books_authors_link l JOIN authors a ON a.id = l.author ORDER BY l.id"
        for book, name in conn.execute(authors):
            row = books.get(book)
            if row is not None:  # calibre escapes a comma in an author name as "|"
                name = name.replace("|", ",")
                row["authors"] = f"{row['authors']} & {name}" if row["authors"] else name
        tags = "SELECT l.book, t.name FROM books_tags_link l JOIN tags t ON t.id = l.tag ORDER BY l.id"
        for book, name in conn.execute(tags):
            if book in books:
                books[book]["tags"].append(name)
        series = "SELECT l.book, s.name FROM books_series_link l JOIN series s ON s.id = l.series"
        for book, name in conn.execute(series):
            if book in books:
                books[book]["series"] = name
        for book, scheme, value in conn.execute("SELECT book, type, val FROM identifiers"):
            if book in books:
                books[book]["identifiers"][scheme] = value
                if scheme == "isbn":
                    books[book]["isbn"] = value
    finally:
        conn.close()
    return list(books.values())


def load_calibre(config: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    lib = config["library"]
    snapshot = Path(lib["snapshot"])
    command = [
        str(lib.get("calibredb", "calibredb")),
        "--with-library",
        str(lib["path"]),
        "list",
        "--fields",
        "id,title,authors,series,series_index,tags,rating,isbn,identifiers,pubdate,last_modified",
        "--for-machine",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip()[-1000:])
        books = parse_noisy_json(result.stdout, list)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps(books, ensure_ascii=False), encoding="utf-8")
        return books, "fresh Calibre export"
    except Exception as exc:
        try:
            books = calibre_books_from_db(Path(lib["path"]))
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text(json.dumps(books, ensure_ascii=False), encoding="utf-8")
            return books, "fresh read of metadata.db (calibredb unavailable)"
        except Exception as db_exc:
            exc = RuntimeError(f"{exc} / metadata.db: {db_exc}")
        if not snapshot.exists():
            raise RuntimeError(f"Calibre export failed and no snapshot exists: {exc}") from exc
        books = json.loads(snapshot.read_text(encoding="utf-8"))
        stamp = datetime.fromtimestamp(snapshot.stat().st_mtime).isoformat(timespec="minutes")
        return books, f"cached Calibre snapshot from {stamp}; export failed: {exc}"


@dataclass
class Candidate:
    title: str
    authors: list[str]
    key: str = ""
    isbns: set[str] = field(default_factory=set)
    cover_url: str = ""
    cover_urls: list[str] = field(default_factory=list)
    language: str = ""
    series: str = ""
    series_index: float | None = None
    published_date: str = ""
    publisher: str = ""
    description: str = ""
    subjects: set[str] = field(default_factory=set)
    categories: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    owned: bool = False
    matched_author: str = ""
    matched_series: str = ""
    series_alert: bool = False
    reissue: bool = False
    score: float = 0
    reasons: list[str] = field(default_factory=list)
    ai: dict[str, Any] = field(default_factory=dict)
    decision: str = ""

    def __post_init__(self) -> None:
        self.title = self.title.strip()
        self.authors = [item.strip() for item in self.authors if item.strip()]
        self.cover_urls = list(dict.fromkeys(value for value in [self.cover_url, *self.cover_urls] if value))
        self.cover_url = self.cover_urls[0] if self.cover_urls else ""
        self.key = self.key or candidate_key(self.title, self.authors)

    def merge(self, other: "Candidate") -> None:
        for author in other.authors:
            if normalize(author) not in {normalize(item) for item in self.authors}:
                self.authors.append(author)
        self.isbns.update(other.isbns)
        self.subjects.update(other.subjects)
        self.cover_urls = list(dict.fromkeys([*self.cover_urls, *other.cover_urls]))
        self.cover_url = self.cover_urls[0] if self.cover_urls else ""
        existing_links = {(item["source"], item["url"]) for item in self.evidence}
        self.evidence.extend(item for item in other.evidence if (item["source"], item["url"]) not in existing_links)
        for attr in ("language", "series", "published_date", "publisher", "description"):
            current = getattr(self, attr)
            incoming = getattr(other, attr)
            if not current or (attr == "description" and len(incoming) > len(current)):
                setattr(self, attr, incoming)
        if self.series_index is None:
            self.series_index = other.series_index


def build_catalog(raw_books: list[dict[str, Any]]) -> dict[str, Any]:
    books: list[dict[str, Any]] = []
    isbns: set[str] = set()
    signatures: set[tuple[str, str]] = set()
    author_counts: dict[str, list[Any]] = {}
    author_titles: dict[str, set[str]] = {}
    tag_counts: dict[str, list[Any]] = {}
    series: dict[str, dict[str, Any]] = {}
    for raw in raw_books:
        authors = split_authors(raw.get("authors"))
        book_isbns = parse_identifiers(raw.get("identifiers"))
        direct_isbn = clean_isbn(str(raw.get("isbn") or ""))
        if direct_isbn:
            book_isbns.add(direct_isbn)
        title = str(raw.get("title") or "").strip()
        title_key = normalize_title(title)
        series_name = str(raw.get("series") or "").strip()
        series_index = safe_float(raw.get("series_index"))
        book = {**raw, "authors_list": authors, "isbns_set": book_isbns}
        books.append(book)
        isbns.update(book_isbns)
        raw_tags = raw.get("tags") or []
        for tag in raw_tags if isinstance(raw_tags, list) else str(raw_tags).split(","):
            tag = str(tag).strip()
            if tag:
                tag_counts.setdefault(normalize(tag), [tag, 0])[1] += 1
        for author in authors:
            key = normalize(author)
            if not key:
                continue
            author_counts.setdefault(key, [author, 0])[1] += 1
            author_titles.setdefault(key, set()).add(title_key)
            signatures.add((title_key, key))
        if series_name:
            key = normalize(series_name)
            entry = series.setdefault(
                key, {"name": series_name, "max_index": 0.0, "titles": set(), "authors": set(), "latest": ""}
            )
            entry["titles"].add(title_key)
            entry["authors"].update(authors)
            if series_index is not None:
                entry["max_index"] = max(entry["max_index"], series_index)
            # Newest volume owned: a series still being published is the one worth
            # asking the sources about again.
            entry["latest"] = max(entry["latest"], str(raw.get("pubdate") or "")[:10])
    return {
        "books": books,
        "isbns": isbns,
        "signatures": signatures,
        "author_counts": author_counts,
        "author_titles": author_titles,
        "tag_counts": tag_counts,
        "series": series,
    }


_last_request: dict[str, float] = {}
# Query pools hit the same hosts from several threads; the throttle read/sleep/
# write must be atomic or a burst slips past the per-host rate.
_throttle_lock = threading.Lock()


class LockedConn:
    """One sqlite connection shared by query-pool threads.

    cached_request only touches the connection for the http_cache read and
    write, never during the network call, so a lock around each statement keeps
    pool threads from interleaving. Rows are fetched inside the lock: a cursor
    must not outlive it."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    class _Rows:
        def __init__(self, cursor: sqlite3.Cursor, lock: threading.Lock) -> None:
            self._cursor = cursor
            self._lock = lock

        def fetchone(self) -> Any:
            with self._lock:
                return self._cursor.fetchone()

    def execute(self, sql: str, parameters: tuple = ()) -> "LockedConn._Rows":
        with self._lock:
            return LockedConn._Rows(self._conn.execute(sql, parameters), self._lock)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()


def query_pool_size(config: dict[str, Any], count: int) -> int:
    """How many of a source's queries may run at once ([sources] query_workers)."""
    try:
        workers = int(config.get("sources", {}).get("query_workers", 4))
    except (TypeError, ValueError):
        workers = 4
    return max(1, min(workers, count))


def cached_request(
    conn: sqlite3.Connection,
    url: str,
    cache_hours: int,
    *,
    headers: dict[str, str] | None = None,
    refresh: bool = False,
    rate_seconds: float = 0,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    opener: Any = None,
) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) if payload is not None else ""
    cache_key = hashlib.sha256(f"{method}|{url}|{encoded}".encode()).hexdigest()
    now = utcnow()
    if not refresh:
        row = conn.execute("SELECT expires_at, status, body FROM http_cache WHERE cache_key=?", (cache_key,)).fetchone()
        if row and datetime.fromisoformat(row["expires_at"]) > now:
            if row["status"] >= 400:
                raise RuntimeError(f"Cached HTTP {row['status']} for {url}")
            return row["body"]
    host = re.match(r"https?://([^/]+)", url)
    host_key = host.group(1) if host else url
    with _throttle_lock:
        wait = rate_seconds - (time.monotonic() - _last_request.get(host_key, 0))
        if wait > 0:
            time.sleep(wait)
        # Spacing is between request starts; the network call itself never holds
        # the lock, or parallel queries would queue behind a slow response.
        _last_request[host_key] = time.monotonic()
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"User-Agent": f"CalibreBookRecommender/{VERSION} (personal library report)"}
    request_headers.update(headers or {})
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers, method=method)
    status = 599
    response_text = ""
    try:
        with (opener.open if opener is not None else urlopen)(request, timeout=90) as response:
            status = response.status
            response_text = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        status = exc.code
        response_text = exc.read().decode("utf-8", errors="replace")
    expires = now + timedelta(hours=cache_hours if status < 400 else min(cache_hours, 1))
    conn.execute(
        "INSERT OR REPLACE INTO http_cache(cache_key,fetched_at,expires_at,status,body) VALUES(?,?,?,?,?)",
        (cache_key, now.isoformat(), expires.isoformat(), status, response_text),
    )
    conn.commit()
    if status >= 400:
        raise RuntimeError(f"HTTP {status} for {url}: {response_text[:300]}")
    return response_text


SERIES_PATTERNS = (
    re.compile(r"^(?P<title>.+?)\s*\((?P<series>[^()#]{2,100}?)\s+#(?P<index>\d+(?:\.\d+)?)\)\s*$", re.I),
    re.compile(r"^(?P<title>.+?)[,:]\s*(?:book|volume)\s+(?P<index>\d+(?:\.\d+)?)\s+(?:of|in)\s+(?:the\s+)?(?P<series>.+)$", re.I),
    # "Dune (Dune Chronicles 01)" — no #, the way release titles and many editions
    # write it. The series must carry a letter, so "(2026)" is not read as one.
    re.compile(
        r"^(?P<title>.+?)\s*\((?P<series>[^()#,]*[A-Za-z][^()#,]*?)(?:,?\s+(?:book|bk\.?|vol\.?|volume|no\.?)\s*|\s+)(?P<index>\d{1,3}(?:\.\d+)?)\)\s*$",
        re.I,
    ),
)


def parse_series(title: str, subtitle: str = "", description: str = "") -> tuple[str, str, float | None]:
    for text in (title, f"{title}: {subtitle}" if subtitle else ""):
        for pattern in SERIES_PATTERNS:
            match = pattern.match(text.strip())
            if match:
                return match.group("title").strip(), match.group("series").strip(), safe_float(match.group("index"))
    desc_patterns = (
        re.compile(r"(?:book|volume)\s+(?P<index>\d+(?:\.\d+)?)\s+(?:in|of)\s+(?:the\s+)?(?P<series>[A-Z][A-Za-z0-9 '&:-]{2,80}?)(?:\s+series)?[.,]"),
        re.compile(r"(?P<index>\d+(?:st|nd|rd|th))\s+(?:book|novel)\s+in\s+(?:the\s+)?(?P<series>[A-Z][A-Za-z0-9 '&:-]{2,80}?)(?:\s+series)?[.,]", re.I),
    )
    for pattern in desc_patterns:
        match = pattern.search(description or "")
        if match:
            number = re.sub(r"\D", "", match.group("index"))
            return title, match.group("series").strip(), safe_float(number)
    return title, "", None


def google_candidates(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    queries: list[tuple[str, str]],
    refresh: bool,
) -> tuple[list[Candidate], list[str]]:
    cache_hours = int(config["sources"].get("cache_hours", 24))
    key = os.getenv(config.get("google_books", {}).get("api_key_env", "GOOGLE_BOOKS_API_KEY"), "")

    def run_one(conn_q: Any, label: str, query: str) -> tuple[list[Candidate], list[str]]:
        found: list[Candidate] = []
        errors: list[str] = []
        params = {"q": query, "orderBy": "newest", "maxResults": 40, "printType": "books", "langRestrict": "en"}
        if key:
            params["key"] = key
        url = "https://www.googleapis.com/books/v1/volumes?" + urlencode(params)
        try:
            data = json.loads(cached_request(conn_q, url, cache_hours, refresh=refresh))
        except RuntimeError as exc:
            if not key:
                errors.append(f"Google Books {label} error: {redact_secrets(exc)}")
                return found, errors
            params.pop("key")
            url = "https://www.googleapis.com/books/v1/volumes?" + urlencode(params)
            try:
                data = json.loads(cached_request(conn_q, url, cache_hours, refresh=refresh))
            except (RuntimeError, URLError, TimeoutError, json.JSONDecodeError) as retry_exc:
                errors.append(f"Google Books {label} error: {redact_secrets(retry_exc)}")
                return found, errors
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            errors.append(f"Google Books {label} error: {redact_secrets(exc)}")
            return found, errors
        for item in data.get("items", []):
            info = item.get("volumeInfo", {})
            title = str(info.get("title") or "").strip()
            authors = split_authors(info.get("authors"))
            if not title or not authors or info.get("language") != "en":
                continue
            description = re.sub(r"<[^>]+>", " ", str(info.get("description") or ""))
            clean_title, series, index = parse_series(title, str(info.get("subtitle") or ""), description)
            image_links = info.get("imageLinks") or {}
            covers = list(
                dict.fromkeys(
                    str(image_links.get(size) or "").replace("http://", "https://", 1)
                    for size in ("medium", "small", "thumbnail", "smallThumbnail")
                    if image_links.get(size)
                )
            )
            isbns = {
                clean_isbn(str(value.get("identifier") or ""))
                for value in info.get("industryIdentifiers", [])
                if clean_isbn(str(value.get("identifier") or ""))
            }
            found.append(
                Candidate(
                    title=clean_title,
                    authors=authors,
                    isbns=isbns,
                    cover_url=covers[0] if covers else "",
                    cover_urls=covers,
                    language="en",
                    series=series,
                    series_index=index,
                    published_date=str(info.get("publishedDate") or ""),
                    publisher=str(info.get("publisher") or ""),
                    description=" ".join(description.split())[:4000],
                    subjects={str(value) for value in info.get("categories", [])},
                    evidence=[{"source": "Google Books", "url": str(info.get("infoLink") or f"https://books.google.com/books?id={item.get('id','')}"), "detail": label}],
                )
            )
        return found, errors

    # Queries are independent, so a small pool turns ~latency each into ~latency
    # total. The connection is shared through LockedConn: cached_request touches
    # it only for the http_cache rows, never during the network call.
    workers = query_pool_size(config, len(queries))
    if workers > 1:
        shared = LockedConn(conn) if conn is not None else None
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = [future.result() for future in [pool.submit(run_one, shared, label, query) for label, query in queries]]
    else:
        outcomes = [run_one(conn, label, query) for label, query in queries]
    results: list[Candidate] = []
    errors: list[str] = []
    for found, query_errors in outcomes:
        results.extend(found)
        errors.extend(query_errors)
    return results, errors


def cover_lookup(conn: Any, config: dict[str, Any], candidate: Candidate, refresh: bool = False, use_isbn: bool = False) -> tuple[Candidate | None, int]:
    """Search Google Books/Open Library for this exact book and return a candidate
    carrying its cover URLs, or (None, error count).

    The gates are the report's cover-fallback gates — exact normalized title and
    overlapping authors — so a same-named different book never donates its cover.
    Shared by the report's cover stage and the download path."""
    title = candidate.title.replace('"', " ")
    author = candidate.authors[0].replace('"', " ") if candidate.authors else ""
    author_keys = {normalize(value) for value in candidate.authors}
    acceptable = lambda item: (  # noqa: E731 - small gate, kept beside its use
        item.cover_urls
        and normalize_title(item.title) == normalize_title(candidate.title)
        and author_keys & {normalize(value) for value in item.authors}
    )
    queries = []
    if use_isbn and candidate.isbns:
        queries.append((f"cover: {candidate.title}", f"isbn:{sorted(candidate.isbns)[0]}"))
    queries.append((f"cover: {candidate.title}", f'intitle:"{title}" inauthor:"{author}"'))
    errors = 0
    if config.get("sources", {}).get("google_books", True):
        found, failures = google_candidates(conn, config, queries, refresh)
        errors += len(failures)
        match = next((item for item in found if acceptable(item)), None)
        if match:
            return match, errors
    if config.get("sources", {}).get("open_library", True):
        try:
            found = open_library_candidates(conn, config, [(f"cover: {candidate.title}", "title", candidate.title)], refresh)
        except (RuntimeError, URLError, TimeoutError, json.JSONDecodeError):
            return None, errors + 1
        return next((item for item in found if acceptable(item)), None), errors
    return None, errors


def fill_missing_covers(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    candidates: list[Candidate],
    refresh: bool,
    *,
    retry_with_isbn: bool = False,
) -> tuple[int, int, int]:
    targets = [
        candidate
        for candidate in candidates
        if not candidate.cover_urls
        and (retry_with_isbn or not candidate.isbns)
        and not candidate.owned
        and candidate.decision != "dismiss"
        and candidate.score > -10
        and (published := parse_dateish(candidate.published_date)) is not None
        and published <= date.today()
    ]
    matched = errors = 0

    def run_one(conn_q: Any, candidate: Candidate) -> tuple[bool, int]:
        match, task_errors = cover_lookup(conn_q, config, candidate, refresh)
        if match:
            candidate.merge(match)
        return bool(match), task_errors

    # Coverless candidates are independent lookups; a pool of them cuts the
    # stage from per-candidate latency to roughly per-worker latency.
    workers = query_pool_size(config, len(targets))
    if workers > 1:
        shared = LockedConn(conn) if conn is not None else None
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = [future.result() for future in [pool.submit(run_one, shared, candidate) for candidate in targets]]
    else:
        outcomes = [run_one(conn, candidate) for candidate in targets]
    matched = sum(1 for found, _ in outcomes if found)
    errors = sum(task_errors for _, task_errors in outcomes)
    return matched, len(targets), errors


def open_library_candidates(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    queries: list[tuple[str, str, str]],
    refresh: bool,
) -> list[Candidate]:
    cache_hours = int(config["sources"].get("cache_hours", 24))
    contact = str(config.get("open_library", {}).get("contact_email") or "").strip()
    headers = {"User-Agent": f"CalibreBookRecommender/{VERSION} ({contact})"} if contact else None
    fields = "key,title,subtitle,author_name,first_publish_year,publish_date,isbn,cover_i,subject,series,publisher,language"
    rate = 0.36 if contact else 1.05

    def run_one(conn_q: Any, label: str, field_name: str, query: str) -> list[Candidate]:
        found: list[Candidate] = []
        params = {field_name: query, "language": "eng", "lang": "en", "sort": "new", "limit": 40, "fields": fields}
        url = "https://openlibrary.org/search.json?" + urlencode(params)
        data = json.loads(cached_request(conn_q, url, cache_hours, headers=headers, refresh=refresh, rate_seconds=rate))
        for item in data.get("docs", []):
            title = str(item.get("title") or "").strip()
            authors = split_authors(item.get("author_name"))
            languages = item.get("language") or []
            languages = [languages] if isinstance(languages, str) else languages
            if not title or not authors or "eng" not in languages:
                continue
            listed_series = item.get("series") or []
            listed_series = [listed_series] if isinstance(listed_series, str) else listed_series
            clean_title, parsed_series, index = parse_series(title, str(item.get("subtitle") or ""))
            published = str(item.get("first_publish_year") or "")
            if not published:
                dates = item.get("publish_date") or []
                published = str(dates[0]) if dates else ""
            isbns = {clean_isbn(str(value)) for value in item.get("isbn", []) if clean_isbn(str(value))}
            key = str(item.get("key") or "")
            found.append(
                Candidate(
                    title=clean_title,
                    authors=authors,
                    isbns=isbns,
                    cover_url=f'https://covers.openlibrary.org/b/id/{item["cover_i"]}-L.jpg?default=false' if item.get("cover_i") else "",
                    language="en",
                    series=parsed_series or (str(listed_series[0]) if listed_series else ""),
                    series_index=index,
                    published_date=published,
                    publisher=str((item.get("publisher") or [""])[0]),
                    subjects={str(value) for value in item.get("subject", [])[:30]},
                    evidence=[{"source": "Open Library", "url": f"https://openlibrary.org{key}" if key else "https://openlibrary.org", "detail": label}],
                )
            )
        return found

    # Same shape as google_candidates: independent queries through a small pool,
    # the one connection shared via LockedConn.
    workers = query_pool_size(config, len(queries))
    if workers > 1:
        shared = LockedConn(conn) if conn is not None else None
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = [future.result() for future in [pool.submit(run_one, shared, label, field_name, query) for label, field_name, query in queries]]
    else:
        outcomes = [run_one(conn, label, field_name, query) for label, field_name, query in queries]
    results: list[Candidate] = []
    for found in outcomes:
        results.extend(found)
    return results


class BlockParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.current: list[str] = []
        self.current_links: list[str] = []
        self.blocks: list[str] = []
        self.links: list[list[str]] = []
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "li", "h2", "h3", "h4"}:
            if self.depth == 0:
                self.current = []
                self.current_links = []
            self.depth += 1
        elif tag == "br" and self.depth:
            self.current.append("\n")
        if tag == "a" and self.depth:
            href = dict(attrs).get("href")
            if href:
                self.current_links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "li", "h2", "h3", "h4"} and self.depth:
            self.depth -= 1
            if self.depth == 0:
                value = html.unescape("".join(self.current))
                value = re.sub(r"[ \t]+", " ", value)
                value = re.sub(r"\n+", "\n", value).strip()
                if value:
                    self.blocks.append(value)
                    self.links.append(self.current_links)

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.current.append(data)


REACTOR_BOOK_RE = re.compile(
    r"^(?P<title>.+?)(?:\s+\((?P<series>[^()#]{2,100}?)\s+#(?P<index>\d+(?:\.\d+)?)\))?\s+[—–]\s+(?P<author>[^()\n]{2,120})\s+\((?P<publisher>[^()\n]+)\)",
    re.M,
)
AMAZON_ISBN_RE = re.compile(r"/(?:dp|gp/product)/([0-9]{9}[0-9Xx]|[0-9]{13})(?:[/?]|$)", re.I)


def parse_reactor_page(page: str, genre: str, month: date, url: str) -> list[Candidate]:
    parser = BlockParser()
    parser.feed(page)
    results: list[Candidate] = []
    current_day: int | None = None
    for block, links in zip(parser.blocks, parser.links):
        day_match = re.fullmatch(rf"{calendar.month_name[month.month]}\s+(\d{{1,2}})", block.strip(), re.I)
        if day_match:
            current_day = int(day_match.group(1))
            continue
        matches = list(REACTOR_BOOK_RE.finditer(block))
        linked_isbns = {
            clean_isbn(found.group(1))
            for link in links
            if (found := AMAZON_ISBN_RE.search(link)) and clean_isbn(found.group(1))
        }
        for match in matches:
            title = match.group("title").strip()
            author = match.group("author").strip()
            published = date(month.year, month.month, current_day).isoformat() if current_day else f"{month.year}-{month.month:02d}"
            results.append(
                Candidate(
                    title=title,
                    authors=[author],
                    isbns=linked_isbns if len(matches) == 1 else set(),
                    series=(match.group("series") or "").strip(),
                    series_index=safe_float(match.group("index")),
                    published_date=published,
                    publisher=match.group("publisher").strip(),
                    description=block[match.end():].strip(" \n—")[:4000],
                    subjects={genre.replace("-", " ")},
                    evidence=[{"source": "Reactor", "url": url, "detail": f"{genre.replace('-', ' ')} monthly list"}],
                )
            )
    return results


def reactor_candidates(conn: sqlite3.Connection, config: dict[str, Any], refresh: bool) -> tuple[list[Candidate], list[str]]:
    results: list[Candidate] = []
    errors: list[str] = []
    source_cfg = config["sources"]
    cache_hours = int(source_cfg.get("cache_hours", 24))
    today = date.today()
    offsets = range(-int(source_cfg.get("reactor_months_back", 6)), int(source_cfg.get("reactor_months_forward", 1)) + 1)
    for genre in source_cfg.get("reactor_genres", ["science-fiction", "fantasy"]):
        for offset in offsets:
            month = add_months(today, offset)
            slug = f"new-{genre}-books-{calendar.month_name[month.month].lower()}-{month.year}"
            url = f"https://reactormag.com/{slug}/"
            try:
                page = cached_request(conn, url, 24 if offset >= -1 else max(cache_hours, 720), refresh=refresh)
            except Exception as exc:
                if "HTTP 404" not in str(exc) and "Cached HTTP 404" not in str(exc):
                    errors.append(f"Reactor {slug}: {exc}")
                continue
            results.extend(parse_reactor_page(page, genre, month, url))
    return results, errors


def hardcover_candidates(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    authors: list[str],
    refresh: bool,
) -> tuple[list[Candidate], str]:
    source_cfg = config.get("hardcover", {})
    token = os.getenv(str(source_cfg.get("token_env", "HARDCOVER_API_TOKEN")), "")
    if not token or not authors:
        return [], "Hardcover skipped: HARDCOVER_API_TOKEN is not configured"
    run_cfg = config["run"]
    start = date.today() - timedelta(days=int(run_cfg.get("past_days", 550)))
    end = date.today() + timedelta(days=int(run_cfg.get("future_days", 0)))
    query = """
    query EditionsForAuthors($authors: [String!]!, $from: date!, $to: date!) {
      editions(
        where: {release_date: {_gte: $from, _lte: $to}, language: {code3: {_eq: "eng"}}, book: {contributions: {author: {name: {_in: $authors}}}}}
        order_by: {release_date: desc}
        limit: 100
      ) {
        isbn_10 isbn_13 release_date language { code2 code3 } image { url } publisher { name }
        book {
          id slug title description cached_tags image { url }
          contributions { author { name } }
          book_series { position series { name } }
        }
      }
    }
    """
    payload = {"query": query, "variables": {"authors": authors, "from": start.isoformat(), "to": end.isoformat()}}
    text = cached_request(
        conn,
        "https://api.hardcover.app/v1/graphql",
        int(config["sources"].get("cache_hours", 24)),
        headers={"Authorization": f"Bearer {token}"},
        refresh=refresh,
        method="POST",
        payload=payload,
    )
    data = json.loads(text)
    if data.get("errors"):
        raise RuntimeError(f"Hardcover GraphQL error: {data['errors'][0].get('message', data['errors'][0])}")
    results: list[Candidate] = []
    for edition in data.get("data", {}).get("editions", []):
        book = edition.get("book") or {}
        contributions = book.get("contributions") or []
        names = [str((item.get("author") or {}).get("name") or "") for item in contributions]
        names = [name for name in names if name]
        series_items = book.get("book_series") or []
        first_series = series_items[0] if series_items else {}
        tags = book.get("cached_tags") or []
        if isinstance(tags, dict):
            tags = list(tags.keys())
        tag_names = {str(item.get("tag") or item.get("name") or item) if isinstance(item, dict) else str(item) for item in tags}
        isbns = {
            clean_isbn(str(edition.get(field) or ""))
            for field in ("isbn_10", "isbn_13")
            if clean_isbn(str(edition.get(field) or ""))
        }
        covers = [
            str((edition.get("image") or {}).get("url") or ""),
            str((book.get("image") or {}).get("url") or ""),
        ]
        covers = list(dict.fromkeys(value for value in covers if value))
        slug = str(book.get("slug") or book.get("id") or "")
        results.append(
            Candidate(
                title=str(book.get("title") or ""),
                authors=names,
                isbns=isbns,
                cover_url=covers[0] if covers else "",
                cover_urls=covers,
                language="en",
                series=str((first_series.get("series") or {}).get("name") or ""),
                series_index=safe_float(first_series.get("position")),
                published_date=str(edition.get("release_date") or ""),
                publisher=str((edition.get("publisher") or {}).get("name") or ""),
                description=str(book.get("description") or "")[:4000],
                subjects=tag_names,
                evidence=[{"source": "Hardcover", "url": f"https://hardcover.app/books/{slug}", "detail": "edition and series metadata"}],
            )
        )
    return results, "Hardcover queried"


# Mobilism release-forum reference links. Read-only: this finds the topic that
# discusses a release and links it, nothing here fetches a file. Guests are refused
# by search.php ("not permitted to use the search system"), so every lookup needs a
# logged-in session cookie, which is why this source has credentials and the others
# do not.
MOBILISM_FORUM = "https://forum.mobilism.org"
MOBILISM_LOGIN = f"{MOBILISM_FORUM}/ucp.php?mode=login"
# Order-tolerant: the lookahead checks the whole tag for the class, so href may sit
# on either side of it.
MOBILISM_TOPIC_RE = re.compile(
    r'<a\s(?=[^>]*class="topictitle")[^>]*href="([^"]*viewtopic\.php\?[^"]*\bt=\d+)[^"]*"[^>]*>(.*?)</a>',
    re.S,
)
_mobilism_session: Any = False  # False = no login attempted yet, None = unavailable


def mobilism_session(config: dict[str, Any]) -> Any:
    """Opener carrying a logged-in forum cookie, or None when login is not possible.
    Cached for the process: one login per run, never one per candidate."""
    global _mobilism_session
    if _mobilism_session is not False:
        return _mobilism_session
    _mobilism_session = None
    section = config.get("mobilism", {})
    username = os.getenv(str(section.get("username_env", "MOBILISM_USERNAME")), "")
    password = os.getenv(str(section.get("password_env", "MOBILISM_PASSWORD")), "")
    if not username or not password:
        raise RuntimeError("MOBILISM_USERNAME and MOBILISM_PASSWORD are not set")
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    opener.addheaders = [("User-Agent", BROWSER_UA)]
    with opener.open(Request(MOBILISM_LOGIN), timeout=60) as response:
        form_page = response.read().decode("utf-8", errors="replace")
    sid = re.search(r'name="sid" value="([0-9a-f]+)"', form_page)
    form = {"username": username, "password": password, "autologin": "on", "redirect": "index.php", "login": "Login"}
    if sid:
        form["sid"] = sid.group(1)
    request = Request(
        MOBILISM_LOGIN,
        data=urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener.open(request, timeout=60) as response:
        landed = response.read().decode("utf-8", errors="replace")
    if "mode=logout" not in landed:
        # Never echo the response: a failed phpBB login page repeats the username.
        raise RuntimeError("Mobilism login rejected (check MOBILISM_USERNAME / MOBILISM_PASSWORD)")
    _mobilism_session = opener
    return opener


def mobilism_topic(conn: sqlite3.Connection, candidate: Candidate, config: dict[str, Any], refresh: bool) -> str:
    """URL of the forum topic for this book, or "" when no row matches it well enough."""
    session = mobilism_session(config)
    if session is None:
        return ""
    section = config.get("mobilism", {})
    query = f"{candidate.title} {candidate.authors[0] if candidate.authors else ''}".strip()
    url = f"{MOBILISM_FORUM}/search.php?" + urlencode(
        {
            "keywords": query,
            "fid[]": int(section.get("forum_id", 106)),
            "sr": "topics",
            "sf": "titleonly",
        }
    )
    page = cached_request(
        conn,
        url,
        int(section.get("cache_hours", 168)),
        headers={"User-Agent": BROWSER_UA},
        refresh=refresh,
        rate_seconds=3,
        opener=session,
    )
    return pick_mobilism_topic(
        page,
        candidate,
        {value.lower() for value in config.get("download", {}).get("formats", DEFAULT_FORMATS)},
        float(section.get("min_title_match", config.get("download", {}).get("min_title_match", 0.6))),
    )


def pick_mobilism_topic(page: str, candidate: Candidate, formats: set[str], min_match: float) -> str:
    """Best-matching topic URL on a forum search page, or "" when none is close enough."""
    surname = normalize(candidate.authors[0].split()[-1]) if candidate.authors else ""
    ranked: list[tuple[float, float, str]] = []
    for href, raw_title in MOBILISM_TOPIC_RE.findall(page):
        row = plain_text(raw_title)
        # Every release title ends in its format tag: "Title by Author (.ePUB)+".
        # It is the only thing separating a book from the audiobook, comic and
        # magazine subforums, which fid[]=106 searches too.
        tag = re.search(r"\(([^()]*)\)\+?\s*$", row)
        if not tag or not {value.strip(" .").lower() for value in tag.group(1).split("+")} & formats:
            continue
        found = re.split(r"\s+by\s+", row[: tag.start()])[0].strip()
        forward = word_overlap(candidate.title, found)
        if forward < min_match:
            continue
        if surname and surname not in normalize(row):
            continue
        # Reverse overlap breaks ties towards the book itself and away from the
        # companions and box sets that carry the same title words.
        ranked.append((forward, word_overlap(found, candidate.title), html.unescape(href)))
    if not ranked:
        return ""
    best = max(ranked)[2]
    return f"{MOBILISM_FORUM}/{best.lstrip('./')}"


MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_abbr) if name}
MOBILISM_DATE_RE = re.compile(r"<small>\s*([A-Z][a-z]{2})\w*\s+(\d{1,2})\w{0,2},\s*(\d{4})")
MOBILISM_BUNDLE_RE = re.compile(r"\b(series|srs|box ?set|collection|omnibus|books? \d+\s*[-–]\s*\d+|\d+\s*book)\b", re.I)


def author_matches(wanted: str, found: Iterable[str]) -> bool:
    """Same person, allowing for "J. N. Chaney" vs "J.N. Chaney": surnames must be
    equal and the initials of the wanted name must all appear in the found one."""
    wanted_parts = normalize(wanted).split()
    if not wanted_parts:
        return False
    surname, initials = wanted_parts[-1], [part[0] for part in wanted_parts[:-1]]
    for name in found:
        parts = normalize(name).split()
        if not parts or parts[-1] != surname:
            continue
        if all(letter in [part[0] for part in parts[:-1]] for letter in initials):
            return True
    return False


def author_overlap(wanted: Iterable[str], found: Iterable[str]) -> bool:
    found = list(found)
    return any(author_matches(name, found) for name in wanted)


def parse_mobilism_release(page: str, formats: set[str]) -> list[dict[str, Any]]:
    """Release topics on a forum search page: title, authors, format tags, newest
    post date and topic URL. The forum indexes the indie serials that Google Books
    and Open Library barely list, which is where continuations hide."""
    releases: list[dict[str, Any]] = []
    matches = list(MOBILISM_TOPIC_RE.finditer(page))
    for position, match in enumerate(matches):
        row = plain_text(match.group(2))
        tag = re.search(r"\(([^()]*)\)\+?\s*$", row)
        if not tag:
            continue
        tags = {value.strip(" .").lower() for value in tag.group(1).split("+")}
        if not tags & formats:
            continue
        head = row[: tag.start()].strip()
        # Split on the last " by ": "Bound by Trust by Y.V. Larson" is one such title.
        separators = list(re.finditer(r"\s+by\s+", head, re.I))
        title = head[: separators[-1].start()].strip() if separators else head
        # The forum writes "First Last, First Last", never Calibre's "Last, First",
        # so a comma here separates people rather than reversing one name.
        authors = (
            [name for chunk in re.split(r",|&", head[separators[-1].end() :]) for name in split_authors(chunk)]
            if separators
            else []
        )
        # The row carries the topic's own date and its last post's; a bundle topic
        # grows with the series, so the newest date is the one that matters. Stop at
        # the next topic so a neighbouring row's date is never borrowed.
        end = matches[position + 1].start() if position + 1 < len(matches) else len(page)
        window = page[match.end() : min(end, match.end() + 2400)]
        days = [
            f"{year}-{MONTHS[month.lower()]:02d}-{int(day):02d}"
            for month, day, year in MOBILISM_DATE_RE.findall(window)
            if month.lower() in MONTHS
        ]
        # A recent last post is written "Today" or "Yesterday" — exactly the rows that
        # matter, since the last post is when the newest volume was added.
        if re.search(r"<small>\s*Today", window, re.I):
            days.append(date.today().isoformat())
        if re.search(r"<small>\s*Yesterday", window, re.I):
            days.append((date.today() - timedelta(days=1)).isoformat())
        if title and authors:
            releases.append(
                {
                    "title": title,
                    "authors": authors,
                    "formats": sorted(tags),
                    "published_date": max(days) if days else "",
                    "url": f"{MOBILISM_FORUM}/{html.unescape(match.group(1)).lstrip('./')}",
                    "bundle": bool(MOBILISM_BUNDLE_RE.search(head)),
                }
            )
    return releases


def parse_mobilism_volumes(post: str) -> list[tuple[float, str]]:
    """The volume list a series topic opens with: «1. Backyard Starship 6. Distant
    Horizon …», numbered and in order, ending at the download links."""
    text = plain_text(post)
    text = re.split(r"Download Instructions|Trouble downloading", text, maxsplit=1)[0]
    text = re.split(r"\bGenre:\s*", text, maxsplit=1)[-1]
    # Split on the numbering rather than matching whole entries: a volume's blurb can
    # run for thousands of characters, and "1. Call Me Ares - Long live the soldiers!…"
    # keeps only what precedes the dash.
    pieces = re.split(r"(?:^|\s)(?:book\s+)?(\d{1,3})[.)]\s+", text, flags=re.I)
    volumes: list[tuple[float, str]] = []
    for number, body in zip(pieces[1::2], pieces[2::2]):
        title = re.split(r"\s+[-–—]\s+|\s{2,}", body.strip())[0].strip(" .,:;")
        # Some posts run the blurb straight on from the title with no separator, so
        # cap the length: a book title is not fifteen words long.
        title = " ".join(title.split()[:10])[:90].strip()
        if title and re.search(r"[A-Za-z]", title) and not title.lower().startswith("http"):
            volumes.append((float(number), title))
    return volumes


def mobilism_series_volumes(
    conn: sqlite3.Connection, config: dict[str, Any], url: str, session: Any, refresh: bool
) -> list[tuple[float, str]]:
    """Volumes listed by one series topic. One page fetch, cached for a week."""
    section = config.get("mobilism", {})
    page = cached_request(
        conn,
        url,
        int(section.get("cache_hours", 168)),
        headers={"User-Agent": BROWSER_UA},
        refresh=refresh,
        rate_seconds=3,
        opener=session,
    )
    parts = page.split('class="content"')
    return parse_mobilism_volumes(parts[1][:8000]) if len(parts) > 1 else []


def mobilism_series_continuations(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    series_name: str,
    search_page: str,
    formats: set[str],
    session: Any,
    catalog: dict[str, Any] | None,
    refresh: bool,
) -> tuple[list[Candidate], str]:
    """Volumes a series' own forum topic lists beyond the newest one owned.

    The bundle topic is kept current with the series, so its numbered list answers
    the question the metadata APIs cannot for an indie serial: is there a book after
    the one on the shelf?
    """
    entry = (catalog or {}).get("series", {}).get(normalize(series_name))
    if not entry:
        return [], ""
    ranked = [
        release
        for release in parse_mobilism_release(search_page, formats)
        if release["bundle"]
        and word_overlap(series_name, release["title"]) >= 0.9
        and author_overlap(entry["authors"], release["authors"])
    ]
    if not ranked:
        return [], ""
    # Reverse overlap first: "Backyard Starship Series" is the series, "Backyard
    # Starship: Origins srs" is a different one that also contains its name.
    topic = max(ranked, key=lambda release: (word_overlap(release["title"], series_name), release["published_date"]))
    try:
        volumes = mobilism_series_volumes(conn, config, topic["url"], session, refresh)
    except Exception as exc:  # a dead topic page is not worth ending the source for
        return [], f"Mobilism series topic error ({series_name}): {redact_secrets(exc)}"
    owned_titles = entry["titles"]
    # Only volumes past the newest one owned: the topic's date belongs to its latest
    # post, so it can honestly date a new release but not a gap further down the list.
    fresh = [
        (index, title)
        for index, title in volumes
        # A number further ahead than that came out of a blurb, not the volume list.
        if entry["max_index"] < index <= entry["max_index"] + 10 and normalize_title(title) not in owned_titles
    ]
    candidates = [
        Candidate(
            title=title,
            authors=topic["authors"],
            language="en",
            series=entry["name"],
            series_index=index,
            published_date=topic["published_date"],
            evidence=[{"source": "Mobilism release", "url": topic["url"], "detail": f"series topic: {series_name}"}],
        )
        for index, title in fresh
    ]
    if not candidates:
        return [], ""
    return candidates, f"Mobilism series topic: {len(candidates)} volume(s) past #{entry['max_index']:g} of {entry['name']}"


def mobilism_candidates(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    queries: list[tuple[str, str, str]],
    refresh: bool,
    catalog: dict[str, Any] | None = None,
) -> tuple[list[Candidate], list[str]]:
    """Candidates from forum release topics. `queries` is (label, kind, term); a
    `series` query stamps its series on what it finds, so an unowned volume of an
    owned series is reported as the continuation it is."""
    section = config.get("mobilism", {})
    if not section.get("enabled", False) or not section.get("discover", True):
        return [], ["Mobilism discovery disabled"]
    try:
        session = mobilism_session(config)
    except Exception as exc:  # the forum 522s often enough that a run must survive it
        return [], [f"Mobilism discovery unavailable: {redact_secrets(exc)}"]
    if session is None:
        return [], ["Mobilism discovery skipped: no credentials"]
    formats = {value.lower() for value in config.get("download", {}).get("formats", DEFAULT_FORMATS)}
    results: list[Candidate] = []
    notes: list[str] = []
    for label, kind, term in queries:
        url = f"{MOBILISM_FORUM}/search.php?" + urlencode(
            {"keywords": term, "fid[]": int(section.get("forum_id", 106)), "sr": "topics", "sf": "titleonly"}
        )
        try:
            page = cached_request(
                conn,
                url,
                int(section.get("cache_hours", 168)),
                headers={"User-Agent": BROWSER_UA},
                refresh=refresh,
                rate_seconds=3,
                opener=session,
            )
        except Exception as exc:  # one dead search must not end the source
            notes.append(f"Mobilism {label} error: {redact_secrets(exc)}")
            continue
        if kind == "series" and section.get("series_topics", True):
            found, note = mobilism_series_continuations(conn, config, term, page, formats, session, catalog, refresh)
            results.extend(found)
            if note:
                notes.append(note)
        for release in parse_mobilism_release(page, formats):
            if release["bundle"]:
                continue  # "… Series (.ePUB)" is the whole set, not a new volume
            if kind == "author" and not author_matches(term, release["authors"]):
                continue  # a surname search also lands on every other Roberts
            owned_series = (catalog or {}).get("series", {}).get(normalize(term)) if kind == "series" else None
            if owned_series and not author_overlap(owned_series["authors"], release["authors"]):
                # "First Contact" as a series name matches half the forum; without the
                # owned series' authors behind it, a hit is not that series.
                continue
            title, series, index = parse_series(release["title"])
            results.append(
                Candidate(
                    title=title,
                    authors=release["authors"],
                    language="en",
                    series=series or (term if kind == "series" else ""),
                    series_index=index,
                    published_date=release["published_date"],
                    evidence=[{"source": "Mobilism release", "url": release["url"], "detail": label}],
                )
            )
    return results, notes


def mobilism_links(conn: sqlite3.Connection, candidates: list[Candidate], config: dict[str, Any], refresh: bool) -> str:
    """Attach a release-topic link to the candidates the report will actually show."""
    section = config.get("mobilism", {})
    if not section.get("enabled", False):
        return "Mobilism disabled"
    wanted = [item for item in candidates if not item.owned and item.decision != "dismiss"]
    wanted = wanted[: int(section.get("max_lookups", 40))]
    found = 0
    for index, candidate in enumerate(wanted):
        try:
            url = mobilism_topic(conn, candidate, config, refresh)
        except Exception as exc:
            # One search per candidate is 3s of throttle; a broken session or a
            # rate-limit page will not get better by trying the other 39.
            return f"Mobilism: {found} links from {index} lookups, then stopped: {redact_secrets(exc)}"
        if url:
            candidate.evidence.append({"source": "Mobilism release", "url": url})
            found += 1
    return f"Mobilism: {found} release links for {len(wanted)} candidates"


def summarize_http_errors(errors: list[str]) -> list[str]:
    """Collapse repeated per-query HTTP failures into one line per code.

    Google's daily 429 quota prints a full JSON blob per query; thirty-six of
    those drowned every other note in the report's source health list.
    """
    if len(errors) < 3:
        return errors
    by_code: dict[str, list[str]] = {}
    rest: list[str] = []
    for error in errors:
        match = re.search(r"HTTP (\d{3})", error)
        if match:
            by_code.setdefault(match.group(1), []).append(error)
        else:
            rest.append(error)
    out = list(rest)
    for code, group in by_code.items():
        if len(group) >= 3:
            example = group[0].split(" for ", 1)[0]
            out.append(f"{len(group)} of {len(errors)} queries failed with HTTP {code} (quota or rate limit); e.g. {example}")
        else:
            out.extend(group)
    return out


def merge_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    merged: dict[str, Candidate] = {}
    for candidate in candidates:
        if not candidate.title or not candidate.authors:
            continue
        key = candidate_key(candidate.title, candidate.authors)
        candidate.key = key
        if key in merged:
            merged[key].merge(candidate)
        else:
            merged[key] = candidate
    return list(merged.values())


def closest_series(name: str, authors: list[str], catalog_series: dict[str, dict[str, Any]]) -> str:
    key = normalize(name)
    if not key:
        return ""
    if key in catalog_series:
        return key
    author_keys = {normalize(author) for author in authors}
    best_key, best_ratio = "", 0.0
    for candidate_key_value, entry in catalog_series.items():
        # Fuzzy series aliases are only safe when at least one contributor also
        # matches; exact names above still support shared-universe continuations.
        if not author_keys & {normalize(author) for author in entry["authors"]}:
            continue
        ratio = SequenceMatcher(None, key, candidate_key_value).ratio()
        if ratio > best_ratio:
            best_key, best_ratio = candidate_key_value, ratio
    return best_key if best_ratio >= 0.92 else ""


def owned_in_catalog(candidate: Candidate, catalog: dict[str, Any]) -> bool:
    title_key = normalize_title(candidate.title)
    return bool(candidate.isbns & catalog["isbns"]) or any(
        (title_key, normalize(author)) in catalog["signatures"] for author in candidate.authors
    )


def apply_taste_preference(candidate: Candidate, taste: list[str], excluded: list[str], combined: str) -> None:
    """The taste bonus/penalty, reusable once categories arrive after scoring."""
    taste_hits = [term for term in taste if term and term in combined]
    if taste_hits:
        candidate.score += min(30, 10 + 5 * len(taste_hits))
        candidate.reasons.append("Matches: " + ", ".join(taste_hits[:4]))
    if any(term and term in combined for term in excluded):
        candidate.score -= 25
        candidate.reasons.append("Matches an excluded preference")


def apply_category_taste_delta(
    candidate: Candidate,
    taste: list[str],
    excluded: list[str],
    base_combined: str,
    full_combined: str,
) -> None:
    """Second taste pass for candidates categorized after scoring.

    Applies only the category-derived difference: the total equals what a single
    pass over full_combined would have given, so base-haystack hits are never
    counted twice.
    """
    base_hits = [term for term in taste if term and term in base_combined]
    full_hits = [term for term in taste if term and term in full_combined]
    delta = 0
    if full_hits:
        delta += min(30, 10 + 5 * len(full_hits))
    if base_hits:
        delta -= min(30, 10 + 5 * len(base_hits))
    if delta:
        candidate.score += delta
        new_hits = [term for term in full_hits if term not in base_hits]
        if new_hits:
            candidate.reasons.append("Matches: " + ", ".join(new_hits[:4]))
    base_excluded = any(term and term in base_combined for term in excluded)
    if not base_excluded and any(term and term in full_combined for term in excluded):
        candidate.score -= 25
        candidate.reasons.append("Matches an excluded preference")


def match_and_score(candidates: list[Candidate], catalog: dict[str, Any], config: dict[str, Any]) -> None:
    taste = [normalize(value) for value in config.get("taste", {}).get("include", [])]
    excluded = [normalize(value) for value in config.get("taste", {}).get("exclude", [])]
    watch = config.get("watch", {})
    completed = {normalize(value) for value in watch.get("complete_series", [])}
    ignored = {normalize(value) for value in watch.get("ignore_series", [])}
    past = date.today() - timedelta(days=int(config["run"].get("past_days", 550)))
    future = date.today() + timedelta(days=int(config["run"].get("future_days", 0)))
    for candidate in candidates:
        author_keys = [normalize(author) for author in candidate.authors]
        candidate.owned = owned_in_catalog(candidate, catalog)
        for author, author_key in zip(candidate.authors, author_keys):
            if author_key in catalog["author_counts"]:
                candidate.matched_author = catalog["author_counts"][author_key][0]
                break
        series_key = closest_series(candidate.series, candidate.authors, catalog["series"])
        if series_key:
            entry = catalog["series"][series_key]
            candidate.matched_series = entry["name"]
            candidate.series_alert = (
                not candidate.owned
                and series_key not in completed
                and series_key not in ignored
                and (candidate.series_index is None or candidate.series_index > entry["max_index"])
            )
        combined = normalize(" ".join([candidate.title, candidate.series, candidate.description, " ".join(candidate.subjects), " ".join(candidate.categories)]))
        candidate.reissue = bool(re.search(r"\b(reissue|re issue|new edition|paperback edition|indie conversion|audiobook)\b", combined))
        if candidate.owned:
            candidate.score = -1000
            candidate.reasons.append("Already in Calibre")
            continue
        if candidate.series_alert:
            candidate.score += 100
            candidate.reasons.append(f"Possible continuation of owned series: {candidate.matched_series}")
        if candidate.matched_author:
            candidate.score += 50
            candidate.reasons.append(f"You own books by {candidate.matched_author}")
        apply_taste_preference(candidate, taste, excluded, combined)
        source_count = len({item["source"] for item in candidate.evidence})
        if source_count > 1:
            candidate.score += min(15, 5 * (source_count - 1))
            candidate.reasons.append(f"Confirmed by {source_count} sources")
        parsed_date = parse_dateish(candidate.published_date)
        if parsed_date and past <= parsed_date <= future:
            candidate.score += 10
            if parsed_date > date.today():
                candidate.reasons.append("Forthcoming")
        elif parsed_date:
            candidate.score -= 200
            candidate.reasons.append("Outside configured release window")
        else:
            candidate.score -= 40
            candidate.reasons.append("Release date unknown")
        if candidate.reissue:
            candidate.score -= 60
            candidate.reasons.append("Likely reissue or format conversion")


def select_authors(
    conn: sqlite3.Connection,
    catalog: dict[str, Any],
    config: dict[str, Any],
    explicit: list[str],
    limit_override: int | None,
) -> list[str]:
    configured = list(config.get("watch", {}).get("authors", []))
    if explicit:
        names = explicit
    else:
        names = configured[:]
        checked = {row["author_key"]: row["checked_at"] for row in conn.execute("SELECT * FROM author_checks")}
        minimum = int(config["run"].get("min_owned_books", 2))
        available = [
            (checked.get(key, ""), -value[1], value[0])
            for key, value in catalog["author_counts"].items()
            if value[1] >= minimum and normalize(value[0]) not in {normalize(item) for item in names}
        ]
        available.sort()
        limit = limit_override or int(config["run"].get("authors_per_run", 8))
        names.extend(item[2] for item in available[: max(0, limit - len(names))])
    seen: set[str] = set()
    return [name for name in names if not (normalize(name) in seen or seen.add(normalize(name)))]


def select_series(
    conn: sqlite3.Connection,
    catalog: dict[str, Any],
    config: dict[str, Any],
    explicit: list[str],
    limit_override: int | None = None,
) -> list[str]:
    """Series to ask the sources about this run.

    Without this the only series ever queried were the hand-listed ones, so a
    continuation was found only when an author or subject query happened to return
    it. Owned series are rotated the way authors are: least recently checked first,
    then the series whose newest owned volume is most recent.
    """
    watch = config.get("watch", {})
    names = list(explicit) if explicit else list(watch.get("ongoing_series", []))
    if explicit:
        return list(dict.fromkeys(names))
    skip = {normalize(value) for value in [*watch.get("complete_series", []), *watch.get("ignore_series", [])]}
    skip.update(normalize(name) for name in names)
    checked = {row["series_key"]: row["checked_at"] for row in conn.execute("SELECT * FROM series_checks")}
    minimum = int(config["run"].get("min_series_books", 2))
    available = [
        (checked.get(key, ""), _invert(entry["latest"]) if entry["latest"] else "~", -len(entry["titles"]), entry["name"])
        for key, entry in catalog["series"].items()
        if key not in skip and len(entry["titles"]) >= minimum
    ]
    available.sort()
    limit = limit_override if limit_override is not None else int(config["run"].get("series_per_run", 12))
    names.extend(item[3] for item in available[: max(0, limit - len(names))])
    seen: set[str] = set()
    return [name for name in names if not (normalize(name) in seen or seen.add(normalize(name)))]


def _invert(day: str) -> str:
    """Sort key that puts the most recent date first without reversing the whole tuple."""
    return "".join(chr(ord("9") - int(char)) if char.isdigit() else char for char in day)


def load_decisions(conn: sqlite3.Connection, candidates: list[Candidate]) -> None:
    decisions = {row["candidate_key"]: row["status"] for row in conn.execute("SELECT candidate_key,status FROM decisions")}
    for candidate in candidates:
        candidate.decision = decisions.get(candidate.key, "")


def candidate_payload(candidate: Candidate) -> dict[str, Any]:
    """The candidate_history payload shape; the background source thread hands
    results to the report thread in exactly this form."""
    return {
        "title": candidate.title,
        "authors": candidate.authors,
        "isbns": sorted(candidate.isbns),
        "cover_url": candidate.cover_url,
        "cover_urls": candidate.cover_urls,
        "language": candidate.language,
        "series": candidate.series,
        "series_index": candidate.series_index,
        "published_date": candidate.published_date,
        "publisher": candidate.publisher,
        "description": candidate.description,
        "subjects": sorted(candidate.subjects),
        "categories": list(candidate.categories),
        "evidence": candidate.evidence,
    }


def persist_candidates(conn: sqlite3.Connection, candidates: list[Candidate]) -> None:
    now = iso_now()
    for candidate in candidates:
        payload = json.dumps(candidate_payload(candidate), ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO candidate_history(candidate_key,title,authors,first_seen,last_seen,payload)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(candidate_key) DO UPDATE SET title=excluded.title,authors=excluded.authors,last_seen=excluded.last_seen,payload=excluded.payload
            """,
            (candidate.key, candidate.title, "; ".join(candidate.authors), now, now, payload),
        )
    conn.commit()


def apply_cached_categories(conn: sqlite3.Connection, candidates: list[Candidate], taxonomy: list[str] | None = None) -> None:
    """Fill candidate.categories from the ai_categories cache. Pure DB read.

    Cache entries from an older taxonomy (the categories changed to the library's
    own tags) are dropped, so those candidates are classified again instead of
    showing categories the report can no longer filter by."""
    rows = {row["category_key"]: json.loads(row["categories"]) for row in conn.execute("SELECT category_key,categories FROM ai_categories")}
    allowed = {name.casefold(): name for name in taxonomy or DEFAULT_CATEGORY_TAXONOMY}
    for candidate in candidates:
        cached = rows.get(category_key(candidate.title, candidate.authors))
        if cached:
            candidate.categories = [allowed[name.casefold()] for name in cached if name.casefold() in allowed]


def persist_categories(conn: sqlite3.Connection, candidates: list[Candidate], model: str) -> None:
    now = iso_now()
    for candidate in candidates:
        if not candidate.categories:
            continue
        conn.execute(
            """
            INSERT INTO ai_categories(category_key,title,authors,categories,model,categorized_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(category_key) DO UPDATE SET categories=excluded.categories,model=excluded.model,categorized_at=excluded.categorized_at
            """,
            (
                category_key(candidate.title, candidate.authors),
                candidate.title,
                json.dumps(candidate.authors, ensure_ascii=False),
                json.dumps(candidate.categories, ensure_ascii=False),
                model,
                now,
            ),
        )
    conn.commit()


def parse_json_response(text: Any) -> dict[str, Any] | list[Any]:
    if isinstance(text, dict):
        return text
    if isinstance(text, list):
        if text and all(isinstance(item, dict) and isinstance(item.get("text"), str) for item in text):
            text = "".join(item["text"] for item in text)
        else:
            return text
    if not isinstance(text, str) or not text.strip():
        raise ValueError("AI response contained no text")
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S)
    value = parse_noisy_json(text, (dict, list))
    return value


def apply_ai_results(
    results: Any,
    selected: list[Candidate],
    model: str,
    owned_titles: dict[str, list[str]] | None = None,
) -> int:
    by_id = {item.key: item for item in selected}
    by_rank = {rank: item for rank, item in enumerate(selected)}
    by_title = {normalize_title(item.title): item for item in selected}
    applied_ids: set[str] = set()
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        candidate = by_id.get(str(result.get("id") or ""))
        if candidate is None and result.get("title"):
            candidate = by_title.get(normalize_title(str(result["title"])))
        if candidate is None:
            try:
                ranked = by_rank.get(int(result.get("rank")))
                candidate = ranked if ranked and (not result.get("title") or normalize_title(str(result["title"])) == normalize_title(ranked.title)) else None
            except (TypeError, ValueError):
                pass
        if not candidate or candidate.key in applied_ids:
            continue
        try:
            fit = max(0, min(100, round(float(result.get("fit_score") or result.get("fitScore") or result.get("score") or 0))))
        except (TypeError, ValueError, OverflowError):
            fit = 0

        def strings(value: Any) -> list[str]:
            values = value if isinstance(value, list) else [value] if isinstance(value, str) else []
            return [str(item) for item in values if str(item).strip()][:8]

        allowed = {normalize_title(title): title for title in (owned_titles or {}).get(candidate.key, [])}
        owned_title = allowed.get(normalize_title(str(result.get("owned_title") or result.get("ownedTitle") or "")), "")
        candidate.ai = {
            "fit_score": fit,
            "genres": strings(result.get("genres", [])),
            "why": str(result.get("why") or result.get("reasoning") or "")[:500],
            "concerns": strings(result.get("concerns", [])),
            "owned_title": owned_title,
            "model": model,
        }
        candidate.score += fit / 5
        if owned_title:
            candidate.owned = True
            candidate.score = -1000
            candidate.reasons.append(f"AI matched owned title: {owned_title}")
        applied_ids.add(candidate.key)
    return len(applied_ids)


def open_json_with_deadline(request: Request, timeout: int) -> dict[str, Any]:
    result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def fetch() -> None:
        try:
            with urlopen(request, timeout=timeout) as response:
                result.put((True, json.loads(response.read().decode("utf-8"))))
        except Exception as exc:
            result.put((False, exc))

    worker = threading.Thread(target=fetch, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"request exceeded {timeout}s wall-clock limit")
    ok, value = result.get_nowait()
    if not ok:
        raise value
    return value


def ensure_openai_oauth_proxy() -> None:
    """Start the local OpenAI OAuth proxy via ai-suite's shared helper."""
    try:
        start = suite_ai().ensure_openai_oauth_proxy
    except Exception as exc:  # noqa: BLE001 - surfaced like any other proxy failure
        raise RuntimeError(f"OpenAI OAuth unavailable: {redact_secrets(exc)}") from exc
    start()


def openai_oauth_models(base_url: str, key: str = "") -> list[str]:
    # Some gateways (OpenCode Zen) sit behind Cloudflare and 403 a default urllib agent.
    headers = {"User-Agent": BROWSER_UA}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = open_json_with_deadline(Request(base_url.rstrip("/") + "/models", headers=headers), 10)
    models = [
        str(item["id"])
        for item in data.get("data", [])
        if isinstance(item, dict) and item.get("id") and "image" not in str(item["id"]).lower()
    ]
    if not models:
        raise RuntimeError("OpenAI OAuth returned no text models from /v1/models.")
    return list(dict.fromkeys(models))


# OpenAI-compatible gateways beyond the OAuth proxy. Each defaults its base URL
# and API-key env var; every one except openai_oauth can also list models live.
# Key lookup falls back through article-writer's names (AW_API_KEY is hyper's key
# there) and opencode's own auth file, so no duplicate secret lives here.
AI_PROVIDERS: dict[str, dict[str, Any]] = {
    "hyper": {"base_url": "https://hyper.charm.land/v1", "api_key_env": "HYPER_API_KEY", "key_fallbacks": ["AW_API_KEY"], "crush_auth_provider": "hyper", "default_model": "qwen3.8-flash", "live_models": True},
    "opencode": {"base_url": "https://opencode.ai/zen/v1", "api_key_env": "OPENCODE_API_KEY", "opencode_auth_file": True, "default_model": "claude-sonnet-5", "live_models": True},
    "claude": {"base_url": "https://api.anthropic.com/v1", "api_key_env": "ANTHROPIC_API_KEY", "default_model": "claude-sonnet-5"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY", "default_model": None, "live_models": True},
    # CLI provider, not an HTTP gateway: runs `cmdc -p` through ai-suite's
    # adapter with the user's Command Code plan auth. No key, no endpoint.
    "commandcode": {"cli": True, "default_model": "deepseek/deepseek-v4-pro", "timeout_seconds": 1800},
}


def opencode_auth_key() -> str:
    """The Zen key opencode's CLI stored at login, read by ai-suite's shared helper."""
    try:
        return str(suite_ai().load_opencode_go_sync().get("api_key") or "")
    except Exception:  # noqa: BLE001 - no auth file or no ai-suite is just no fallback
        return ""


def jwt_expired(token: str) -> bool:
    """True for a JWT whose exp has passed. Nothing is verified — the point is only to
    stop handing a stale token to a gateway and collecting 401s for the whole run."""
    parts = token.split(".")
    if len(parts) != 3:
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return float(payload["exp"]) <= time.time()
    except Exception:  # noqa: BLE001 - an unreadable token is not a stale one
        return False


def crush_auth_key(provider: str) -> str:
    """The gateway key Crush's CLI stored at login, so book-watch need not duplicate it."""
    roots = [os.getenv("LOCALAPPDATA") or "", str(Path.home() / ".local" / "share"), str(Path.home() / ".config")]
    for root in roots:
        try:
            data = json.loads((Path(root) / "crush" / "crush.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - no Crush install is just no fallback
            continue
        key = ((data.get("providers") or {}).get(provider) or {}).get("api_key")
        if key and jwt_expired(str(key)):
            if provider not in _CRUSH_EXPIRY_NOTED:
                _CRUSH_EXPIRY_NOTED.add(provider)
                progress(f"Crush's stored {provider} token has expired; run crush to refresh it")
            return ""
        if key:
            return str(key)
    return ""


def ai_key(ai_cfg: dict[str, Any]) -> str:
    if ai_cfg.get("cli"):
        # The CLI carries the user's own Command Code plan auth — ai-suite's
        # convention — so a CLI provider is always "usable" without a key.
        return "commandcode-cli"
    for env_name in [ai_cfg.get("api_key_env"), *(ai_cfg.get("key_fallbacks") or [])]:
        if env_name and os.getenv(str(env_name)):
            return os.environ[str(env_name)]
    if ai_cfg.get("opencode_auth_file"):
        return opencode_auth_key()
    if ai_cfg.get("crush_auth_provider"):
        return crush_auth_key(str(ai_cfg["crush_auth_provider"]))
    return ""


# The shared ai-suite package: the sibling checkout when present (AI_SUITE_DIR overrides
# it), else the copy vendored into this repository.
AI_SUITE = Path(os.getenv("AI_SUITE_DIR") or (Path(__file__).resolve().parent.parent / "ai-suite"))
SUITE_PATH = AI_SUITE if AI_SUITE.is_dir() else Path(__file__).resolve().parent
_suite: dict[str, Any] = {}
_suite_lock = threading.Lock()
_commandcode_adapter: dict[str, Any] = {}


def _suite_module(name: str) -> Any:
    """A module of the shared ai_suite package -- the AI suite every workspace AI
    script uses. Raises when neither the checkout nor the vendored copy is there."""
    with _suite_lock:  # report-server threads may ask at once
        if name not in _suite:
            if str(SUITE_PATH) not in sys.path:
                sys.path.insert(0, str(SUITE_PATH))
            _suite[name] = importlib.import_module(name)
        return _suite[name]


def suite_ai() -> Any:
    """ai-suite's service module (AIService, OAuth proxy and auth helpers)."""
    return _suite_module("ai_suite.service")


def suite_config_path(provider: str) -> str:
    """ai-suite's config file for one of its providers (its .local.json copy first)."""
    return _suite_module("ai_suite.providers").provider_config_path(provider)


# book-watch's provider names -> ai-suite's. Book-watch keeps choosing the provider,
# finding its key (env, AW_API_KEY, Crush, opencode auth) and validating models; every
# completion then runs through ai-suite's AIService like the rest of the workspace.
SHARED_PROVIDERS = {
    "hyper": "hyper",
    "opencode": "opencode-zen",
    # Anthropic's API (ANTHROPIC_API_KEY) via its OpenAI-compatible endpoint; book
    # writer's own "claude" is the Claude Code CLI, a different account.
    "claude": "openrouter",
    "openrouter": "openrouter",
    "OpenRouter": "openrouter",
    "commandcode": "commandcode",
    "OpenAI OAuth": "openai-oauth",
}
_CLI_PROVIDERS = {"commandcode"}
_shared_services: dict[tuple, Any] = {}


def shared_ai_service(provider: str, overrides: dict[str, Any]) -> Any:
    """ai-suite's AIService for `provider` with book-watch's key/endpoint/timeout.
    One instance per distinct setting, reused across batches and report requests."""
    cache_key = (provider, tuple(sorted(overrides.items())))
    with _suite_lock:
        cached = _shared_services.get(cache_key)
    if cached is not None:
        return cached
    service = suite_ai().AIService(
        config_path=suite_config_path(provider),
        usage_state_path=str(Path(__file__).resolve().parent / "data" / "ai_usage.json"),
        allow_auth_prompt=False,
        client_max_retries=0,
        config_overrides=dict(overrides),
    )
    with _suite_lock:
        return _shared_services.setdefault(cache_key, service)


def ai_completion(provider_name: str, ai_cfg: dict[str, Any], key: str, base_url: str, model: str,
                  prompt: str, max_tokens: int) -> str:
    """One completion through the shared suite. Fails fast: a report never waits out a usage limit."""
    provider = SHARED_PROVIDERS.get(provider_name, "openrouter")
    overrides: dict[str, Any] = {"timeout": int(ai_cfg.get("timeout_seconds", 120))}
    if provider not in _CLI_PROVIDERS:
        # The report's caps are ceilings on metered gateways, spelled max_tokens as before.
        overrides.update(token_param="max_tokens", cap_is_ceiling=True)
        overrides["base_url"] = base_url
        if key:
            overrides["api_key"] = key
    service = shared_ai_service(provider, overrides)
    return str(service.generate_content(prompt, model=model, max_completion_tokens=max_tokens,
                                        max_retries=1, wait_for_limits=False, temperature=0.0))


def commandcode_adapter() -> dict[str, Any]:
    """ai-suite's Command Code model list and CLI check, so the report's picker
    offers what ai-suite's commandcode config serves. Cached; carries
    {"error": ...} when the module or the CLI is unavailable."""
    if not _commandcode_adapter:
        try:
            module = suite_ai()
            config_path = Path(suite_config_path("commandcode"))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            module.commandcode_executable()
            _commandcode_adapter.update(
                {
                    "models": [str(name) for name in (config.get("models") or {})],
                    "writing_model": str(config.get("writing_model") or ""),
                    "review_model": str(config.get("review_model") or ""),
                    "timeout": int(config.get("timeout") or 1800),
                }
            )
        except Exception as exc:  # noqa: BLE001 - a missing CLI or module is a reported state, not a crash
            _commandcode_adapter["error"] = f"Command Code unavailable: {redact_secrets(exc)}"
    return _commandcode_adapter


def _resolve_ai_provider(config: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
    """(display name, merged provider settings, whether it is the OAuth proxy).

    The name is "none" when AI is switched off for this run.
    """
    ai_default = config.get("ai") if isinstance(config.get("ai"), dict) else {}
    provider = str(config.get("_ai_provider") or ai_default.get("provider") or "").strip().lower()
    if not provider and not isinstance(config.get("openai_oauth"), dict) and not isinstance(config.get("openrouter"), dict):
        # No explicit provider configured anywhere: default to Command Code's CLI.
        provider = "commandcode"
    if provider == "none":
        return "none", {}, False
    using_oauth = provider == "openai_oauth" or (not provider and isinstance(config.get("openai_oauth"), dict))
    if using_oauth:
        return "OpenAI OAuth", dict(config.get("openai_oauth") or {}), True
    if provider in AI_PROVIDERS:
        spec = AI_PROVIDERS[provider]
        section = config.get(provider) if isinstance(config.get(provider), dict) else {}
        legacy = config.get("openrouter") if provider == "openrouter" and isinstance(config.get("openrouter"), dict) else {}
        return provider, {"enabled": True, **spec, **legacy, **section}, False
    # Legacy path: no provider chosen and [openai_oauth] absent, so OpenRouter it is.
    # Only the key/base-url defaults merge: legacy behaviour never live-validated
    # the model list, and that must not change.
    legacy = {"enabled": True, "base_url": AI_PROVIDERS["openrouter"]["base_url"], "api_key_env": AI_PROVIDERS["openrouter"]["api_key_env"]}
    return "OpenRouter", {**legacy, **dict(config.get("openrouter", {}))}, False


_AI_FALLBACK_NOTED: set[str] = set()
_CRUSH_EXPIRY_NOTED: set[str] = set()


def resolve_ai_provider(config: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
    """The configured provider, or a working alternative when its key is unusable.

    An expired Crush token or a missing env var used to leave every AI stage —
    categorization included — silently off for the whole run even when another
    configured provider (e.g. [openai_oauth]) had a working key. Keyed providers
    are tried first; the OAuth proxy is last because starting it may need a
    browser sign-in.
    """
    name, ai_cfg, using_oauth = _resolve_ai_provider(config)
    if name == "none" or using_oauth or ai_key(ai_cfg):
        return name, ai_cfg, using_oauth
    env_file = str(ai_cfg.get("env_file") or "").strip()
    if env_file:
        # A key may live only in the section's env_file; load it before concluding
        # the provider is unusable.
        load_env_file(env_file)
        if ai_key(ai_cfg):
            return name, ai_cfg, using_oauth
    for fallback, spec in AI_PROVIDERS.items():
        # The legacy display name "OpenRouter" must not "fall back" to openrouter,
        # its own spec. CLI providers are only ever an explicit choice: silently
        # shelling out to `cmdc` because hyper's key expired would surprise.
        if fallback == name or (name == "OpenRouter" and fallback == "openrouter") or spec.get("cli"):
            continue
        section = config.get(fallback) if isinstance(config.get(fallback), dict) else {}
        merged = {"enabled": True, **spec, **section}
        if ai_key(merged):
            if f"{name}->{fallback}" not in _AI_FALLBACK_NOTED:
                _AI_FALLBACK_NOTED.add(f"{name}->{fallback}")
                progress(f"{name} has no usable API key; falling back to {fallback}")
            return fallback, merged, False
    if isinstance(config.get("openai_oauth"), dict):
        if f"{name}->oauth" not in _AI_FALLBACK_NOTED:
            _AI_FALLBACK_NOTED.add(f"{name}->oauth")
            progress(f"{name} has no usable API key; falling back to OpenAI OAuth (start the npx proxy; sign in if prompted)")
        return "OpenAI OAuth", dict(config["openai_oauth"]), True
    return name, ai_cfg, using_oauth


def enrich_with_openrouter(candidates: list[Candidate], config: dict[str, Any], catalog: dict[str, Any] | None = None) -> str:
    provider_name, ai_cfg, using_oauth = resolve_ai_provider(config)
    if provider_name == "none":
        return "AI disabled for this run"
    if not ai_cfg.get("enabled", True):
        return "AI disabled in configuration"
    env_file = str(ai_cfg.get("env_file") or "").strip()
    if env_file:
        load_env_file(env_file)
    key_env = str(ai_cfg.get("api_key_env", "" if using_oauth else "OPENROUTER_API_KEY"))
    key = ai_key(ai_cfg) if not using_oauth else ""
    if not key and not using_oauth:
        names = " or ".join(filter(None, [key_env, *(ai_cfg.get("key_fallbacks") or [])]))
        return f"AI skipped: {names} is not configured (set it in .env or the environment)"
    model_override = str(config.get("_ai_model") or "").strip()
    # ponytail: cap slow free-model enrichment; raise only if the top 60 omit useful author matches.
    selected = sorted(
        [item for item in candidates if not item.owned and item.decision != "dismiss" and item.score > 0],
        key=lambda item: (bool(item.series_alert or item.matched_author), item.score, item.published_date),
        reverse=True,
    )[:60]
    if not selected:
        return "AI skipped: no eligible candidates"
    if using_oauth:
        try:
            ensure_openai_oauth_proxy()
        except RuntimeError as exc:
            return f"AI unavailable; deterministic report produced. {exc}"
    taste = config.get("taste", {})
    library_profile = {
        "book_count": len((catalog or {}).get("books", [])),
        "top_tags": [
            item[0]
            for item in sorted((catalog or {}).get("tag_counts", {}).values(), key=lambda item: item[1], reverse=True)[:20]
        ],
        "top_authors": [
            {"name": item[0], "owned_books": item[1]}
            for item in sorted((catalog or {}).get("author_counts", {}).values(), key=lambda item: item[1], reverse=True)[:20]
        ],
    }
    base_url = str(ai_cfg.get("base_url", f"http://127.0.0.1:{OPENAI_OAUTH_PORT}/v1" if using_oauth else "https://openrouter.ai/api/v1")).rstrip("/")
    default_model = ai_cfg.get("default_model") or ("gpt-5.4-mini" if using_oauth else None)
    models = list(dict.fromkeys(str(value) for value in (ai_cfg.get("model", default_model), ai_cfg.get("fallback_model")) if value))
    if model_override:
        # A model chosen in the report outranks config, and the live check below rejects invalid picks.
        models = [model_override]
    if using_oauth:
        try:
            live_models = openai_oauth_models(base_url)
        except (HTTPError, URLError, TimeoutError, KeyError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            return f"AI unavailable; deterministic report produced. Could not load {provider_name} models: {exc}"
        models = [model for model in models if model in live_models] or live_models
        progress(
            f"{provider_name} live text models: {', '.join(live_models)}. "
            "Context/output limits are not reported by /v1/models."
        )
    elif ai_cfg.get("live_models") and models:
        # Validate the configured pick against the catalog without ever falling back
        # to an arbitrary live model: most gateways here bill per model.
        try:
            live_models = openai_oauth_models(base_url, key)
        except (HTTPError, URLError, TimeoutError, KeyError, ValueError, RuntimeError, json.JSONDecodeError):
            live_models = []
        if live_models:
            invalid = [model for model in models if model not in live_models]
            if invalid:
                return f"AI unavailable; deterministic report produced. {provider_name} does not offer: {', '.join(invalid)}"
    if len(models) > 6:
        models = models[:6]
    cli_adapter = commandcode_adapter() if ai_cfg.get("cli") else None
    if cli_adapter is not None and "error" in cli_adapter:
        return f"AI unavailable; deterministic report produced. {cli_adapter['error']}"
    applied_total = 0
    used_models: set[str] = set()
    failed_batches = 0
    last_error = ""
    for start in range(0, len(selected), 12):
        batch = selected[start : start + 12]
        progress(f"{provider_name}: enriching {start + 1}-{start + len(batch)} of {len(selected)}...")
        items = []
        owned_titles: dict[str, list[str]] = {}
        for rank, item in enumerate(batch):
            known_titles = set()
            for author in item.authors:
                known_titles.update((catalog or {}).get("author_titles", {}).get(normalize(author), set()))
            item_title = normalize_title(item.title)
            owned_titles[item.key] = sorted(
                known_titles,
                key=lambda title: SequenceMatcher(None, item_title, title).ratio(),
                reverse=True,
            )[:40]
            items.append({
                "rank": rank,
                "id": item.key,
                "title": item.title,
                "authors": item.authors,
                "series": item.series,
                "date": item.published_date,
                "subjects": sorted(item.subjects)[:20],
                "description": item.description[:700],
                "deterministic_reasons": item.reasons,
                "owned_titles": owned_titles[item.key],
            })
        prompt = (
            "You rank new releases for a private book recommender. Judge how strongly each candidate matches either the "
            "explicit genre preferences (highest priority) or patterns in the user's Calibre library. "
            "Classify and rank only the supplied candidates. "
            "The input list is authoritative: return exactly one entry per input with the same rank, id, and title, "
            "and never introduce another book. Do not invent publication facts or series membership. Return JSON only with this shape: "
            '{"items":[{"rank":0,"id":"work:...","title":"...","fit_score":0,"genres":["..."],"why":"one concise sentence","concerns":["..."],"owned_title":null}]}. '
            "fit_score must be an integer from 0 to 100. Concerns should flag weak metadata, likely reissues, or genre uncertainty. "
            "Set owned_title to an exact value from that candidate's owned_titles only when it is the same underlying work under a translation, retitle, or reissue; otherwise use null.\n\n"
            f"Genre preferences: {json.dumps(taste, ensure_ascii=False)}\n\n"
            f"Aggregate Calibre profile: {json.dumps(library_profile, ensure_ascii=False)}\n\n"
            f"Candidates: {json.dumps(items, ensure_ascii=False)}"
        )
        attempt_models = (models * 3)[:3]
        for model in [item for item in attempt_models if item and item != "None"]:
            try:
                content = ai_completion(provider_name, ai_cfg, key, base_url, model, prompt, 2200)
                parsed = parse_json_response(content)
                results = parsed if isinstance(parsed, list) else next(
                    (parsed.get(key) for key in ("items", "candidates", "recommendations", "results", "books") if isinstance(parsed.get(key), list)),
                    [],
                )
                applied = apply_ai_results(results, batch, model, owned_titles)
                if applied:
                    applied_total += applied
                    used_models.add(model)
                    break
                last_error = f"{model}: response contained no verifiable candidates"
            except Exception as exc:  # noqa: BLE001 - any provider/SDK failure costs this batch, never the report
                last_error = f"{model}: {redact_secrets(exc)[:300]}"
        else:
            failed_batches += 1
    if applied_total:
        model_note = ", ".join(sorted(used_models))
        failure_note = f"; {failed_batches} batches failed" if failed_batches else ""
        return f"AI enriched {applied_total}/{len(selected)} candidates with {model_note}{failure_note}"
    return "AI unavailable; deterministic report produced. " + last_error


def ai_chat(config: dict[str, Any], prompt: str, *, max_tokens: int = 400, model: str | None = None) -> str:
    """One short completion from the configured provider, or "" when unavailable.

    For the small helper prompts (fetch assist, categories, Anna validation); the
    ranking pass keeps its own batching path. `model` overrides the provider's model.
    """
    provider_name, ai_cfg, using_oauth = resolve_ai_provider(config)
    if provider_name == "none" or not ai_cfg.get("enabled", True):
        return ""
    env_file = str(ai_cfg.get("env_file") or "").strip()
    if env_file:
        load_env_file(env_file)
    key = "" if using_oauth else ai_key(ai_cfg)
    if not key and not using_oauth:
        return ""
    if using_oauth:
        try:
            ensure_openai_oauth_proxy()
        except RuntimeError:
            return ""
    base_url = str(ai_cfg.get("base_url", f"http://127.0.0.1:{OPENAI_OAUTH_PORT}/v1" if using_oauth else "https://openrouter.ai/api/v1")).rstrip("/")
    model = str(model or ai_cfg.get("model") or ai_cfg.get("default_model") or "")
    if ai_cfg.get("cli"):
        adapter = commandcode_adapter()
        if "error" in adapter:
            progress(f"  AI assist unavailable: {adapter['error']}")
            return ""
        model = model or str(adapter.get("writing_model") or "")
    if not model:
        return ""
    try:
        return ai_completion(provider_name, ai_cfg, key, base_url, model, prompt, max_tokens)
    except Exception as exc:  # noqa: BLE001 - a failed assist must never abort a download
        progress(f"  AI assist unavailable ({provider_name}): {redact_secrets(exc)}")
        return ""


def ai_title_variants(candidate: Candidate, config: dict[str, Any], limit: int = 4) -> list[str]:
    """Other titles the same book is filed under — original language, subtitle
    dropped, US/UK retitle — for a second pass over the download sources."""
    prompt = (
        "A book file search found nothing under the title below. List up to 4 alternative titles the same book is "
        "catalogued or published under: original-language title, US/UK retitle, the title with or without its subtitle, "
        "the series-numbered form. Same work only — never another book, no author names, no commentary. "
        "Return JSON only: {\"titles\":[\"...\"]}, empty when no alternative title exists.\n\n"
        f"Book: {json.dumps({'title': candidate.title, 'authors': candidate.authors, 'series': candidate.series, 'published': candidate.published_date, 'publisher': candidate.publisher, 'description': candidate.description[:400]}, ensure_ascii=False)}"
    )
    content = ai_chat(config, prompt, max_tokens=300)
    if not content:
        return []
    try:
        parsed = parse_json_response(content)
    except ValueError:
        return []
    titles = parsed if isinstance(parsed, list) else parsed.get("titles") if isinstance(parsed, dict) else []
    seen = {normalize_title(candidate.title)}
    variants = []
    for title in titles or []:
        text = str(title).strip()
        if text and normalize_title(text) not in seen:
            seen.add(normalize_title(text))
            variants.append(text)
    return variants[:limit]


DEFAULT_CATEGORY_TAXONOMY = [
    "Fantasy", "Science Fiction", "Mystery", "Thriller", "Romance", "Horror",
    "Historical", "Literary", "Young Adult", "Middle Grade", "Nonfiction",
    "Biography", "History", "Science", "Self-Help", "Comics/Manga", "Poetry",
]

# Source subjects are phrased differently from this library's tags ("Science
# Fiction" vs the tag "sci-fi"); these map the usual variants onto tag names,
# and a synonym whose target is not one of the library's tags is simply dropped.
# Keys and values are matched through normalize(), so write them in plain words.
SUBJECT_CATEGORY_SYNONYMS = {
    "science fiction": "sci-fi",
    "sci fi": "sci-fi",
    "scifi": "sci-fi",
    "sf": "sci-fi",
    "speculative fiction": "sci-fi",
    "military science fiction": "military sci-fi",
    "hard science fiction": "hard sci-fi",
    "apocalyptic fiction": "post apocalyptic",
    "dystopia": "dystopian",
    "dystopian fiction": "dystopian",
}


def category_taxonomy(catalog: dict[str, Any] | None) -> list[str]:
    """Report categories are the Calibre library's own tags, most-used first.

    The AI classifies into the same vocabulary the library already uses, so the
    report's category chips match how the collection is actually tagged. Falls
    back to the generic taxonomy only when the library has no tags at all.
    """
    if catalog:
        # A comma inside a tag would corrupt the report's comma-joined
        # data-categories attribute, so such tags never become categories.
        tags = [
            name
            for name, _count in sorted((catalog.get("tag_counts") or {}).values(), key=lambda item: item[1], reverse=True)
            if name and "," not in name
        ]
        if tags:
            return tags
    return DEFAULT_CATEGORY_TAXONOMY


def display_categories(candidate: Candidate, taxonomy: list[str]) -> list[str]:
    """Categories a card is filed under: the AI's verdict when present, otherwise
    source subjects mapped onto the library's tags, so the report filters keep
    working when the AI provider is down."""
    if candidate.categories:
        return candidate.categories
    # Both sides go through normalize(): subjects arrive as "Post-Apocalyptic"
    # and tags as "post apocalyptic", and only a shared key makes them meet.
    allowed = {normalize(tag): tag for tag in taxonomy}
    derived: list[str] = []
    for subject in candidate.subjects:
        key = normalize(subject)
        mapped = SUBJECT_CATEGORY_SYNONYMS.get(key)
        tag = allowed.get(normalize(mapped)) if mapped else allowed.get(key)
        if tag and tag not in derived:
            derived.append(tag)
    return derived


def categorize_candidates(
    conn: sqlite3.Connection, candidates: list[Candidate], config: dict[str, Any], taxonomy: list[str] | None = None
) -> str:
    """Classify candidate titles into report categories, cached in ai_categories.

    Cached titles are applied before scoring (apply_cached_categories); this pass
    only asks the model about the rest. Returns a status line, never raises — the
    report must still be written when the provider is down.
    """
    taxonomy = taxonomy or DEFAULT_CATEGORY_TAXONOMY
    provider_name, ai_cfg, using_oauth = resolve_ai_provider(config)
    if provider_name == "none" or not ai_cfg.get("enabled", True):
        return "AI categories skipped: provider disabled"
    env_file = str(ai_cfg.get("env_file") or "").strip()
    if env_file:
        load_env_file(env_file)
    eligible = [
        item for item in candidates
        if not item.owned and item.score > 0 and not item.categories
    ]
    if not eligible:
        return "AI categories: every candidate already classified"
    eligible.sort(key=lambda item: (item.series_alert or bool(item.matched_author), item.score, item.published_date), reverse=True)
    eligible = eligible[:120]
    batches = [eligible[start : start + 40] for start in range(0, len(eligible), 40)]
    applied = 0
    used_models: set[str] = set()
    failed_batches = 0
    last_error = ""
    for batch in batches:
        items = [
            {
                "id": rank,
                "title": item.title,
                "authors": item.authors,
                "series": item.series,
                "subjects": sorted(item.subjects)[:10],
            }
            for rank, item in enumerate(batch)
        ]
        prompt = (
            "Classify each book into 1-3 categories from this fixed list: "
            f"{json.dumps(taxonomy)}. Use only categories from the list; "
            "never invent one, never name another book. Judge from title, authors, "
            "series and subjects. Return JSON only with this shape: "
            '{"items":[{"id":0,"categories":["Fantasy"]}]}\n\n'
            f"Books: {json.dumps(items, ensure_ascii=False)}"
        )
        content = ai_chat(config, prompt, max_tokens=1500)
        if not content:
            failed_batches += 1
            last_error = f"{provider_name} returned no usable response"
            continue
        try:
            parsed = parse_json_response(content)
        except ValueError as exc:
            failed_batches += 1
            last_error = str(exc)
            continue
        results = parsed if isinstance(parsed, list) else next(
            (parsed.get(key) for key in ("items", "books", "results") if isinstance(parsed.get(key), list)),
            [],
        )
        allowed = set(taxonomy)
        for result in results if isinstance(results, list) else []:
            if not isinstance(result, dict):
                continue
            try:
                candidate = batch[int(result.get("id"))]
            except (TypeError, ValueError, IndexError):
                continue
            raw = result.get("categories") if isinstance(result.get("categories"), list) else [result.get("categories")]
            picked = [str(value).strip() for value in raw or [] if str(value).strip()]
            picked = list(dict.fromkeys(picked))[:3]
            if not picked:
                continue
            valid = [name for name in picked if name in allowed]
            if not valid:
                continue
            candidate.categories = valid
            applied += 1
            used_models.add(provider_name)
    if applied:
        persist_categories(conn, [item for item in eligible if item.categories], ",".join(sorted(used_models)) or provider_name)
        note = f"; {failed_batches} batch(es) failed" if failed_batches else ""
        return f"AI categorized {applied}/{len(eligible)} candidates{note}"
    if failed_batches:
        return f"AI categories unavailable; deterministic report produced. {last_error}"
    return "AI categories: model returned nothing usable"


def ai_provider_specs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    oauth_cfg = config.get("openai_oauth") if isinstance(config.get("openai_oauth"), dict) else {}
    return {**AI_PROVIDERS, "openai_oauth": {"base_url": oauth_cfg.get("base_url", f"http://127.0.0.1:{OPENAI_OAUTH_PORT}/v1"), "live_models": True}}


def ai_providers_status(config: dict[str, Any]) -> dict[str, Any]:
    """What the report's AI selector offers, without touching the network.

    Only the configured default model: a live list costs a round trip per gateway and,
    for openai_oauth, starts the npx proxy. `ai_provider_models()` does that on demand,
    when the user actually picks that provider in the report.
    """
    status: dict[str, Any] = {}
    for name, spec in ai_provider_specs(config).items():
        section = config.get(name) if isinstance(config.get(name), dict) else {}
        default_model = str(section.get("model") or spec.get("default_model") or "")
        status[name] = {"models": [default_model] if default_model else [], "error": "", "live": bool(spec.get("live_models"))}
        if spec.get("cli"):
            # Offline like everything else here, but the static model list comes
            # from ai-suite's commandcode config instead of the one default.
            adapter = commandcode_adapter()
            if "error" in adapter:
                status[name]["error"] = str(adapter["error"])[:200]
            else:
                status[name]["models"] = list(dict.fromkeys([default_model, *adapter["models"]]))
    return status


def ai_provider_models(config: dict[str, Any], name: str) -> dict[str, Any]:
    """The live model list for one provider. The only path that starts the OAuth proxy
    for the report, so signing in happens when openai_oauth is selected and not before."""
    spec = ai_provider_specs(config).get(name)
    if not spec:
        return {"models": [], "error": f"Unknown provider: {name}", "live": False}
    section = config.get(name) if isinstance(config.get(name), dict) else {}
    default_model = str(section.get("model") or spec.get("default_model") or "")
    if spec.get("cli"):
        # No endpoint to query: the list is ai-suite's commandcode config.
        adapter = commandcode_adapter()
        if "error" in adapter:
            return {"models": [default_model] if default_model else [], "error": str(adapter["error"])[:200], "live": False}
        return {"models": list(dict.fromkeys([default_model, *adapter["models"]])), "error": "", "live": False}
    base_url = str(section.get("base_url") or spec["base_url"]).rstrip("/")
    entry: dict[str, Any] = {"models": [default_model] if default_model else [], "error": "", "live": bool(spec.get("live_models"))}
    try:
        if not spec.get("live_models"):
            raise RuntimeError(f"{name} does not publish a model list; type a model id")
        if name == "openai_oauth":
            ensure_openai_oauth_proxy()
        models = openai_oauth_models(base_url, ai_key(spec) if name != "openai_oauth" else "")
        if name == "openrouter":
            # OpenRouter lists hundreds of models; keep claude/gpt ones plus the configured pick.
            models = [m for m in models if "claude" in m or "gpt-5" in m]
        entry["models"] = list(dict.fromkeys([default_model, *models]))[:60]
    except Exception as exc:  # a dead gateway must not break the report page
        entry["error"] = redact_secrets(exc)[:200]
    return entry


def source_links(candidate: Candidate) -> str:
    links = []
    for evidence in candidate.evidence:
        links.append(f'<a href="{html.escape(evidence["url"], quote=True)}">{html.escape(evidence["source"])}</a>')
    query = quote_plus(f'{candidate.title} {" ".join(candidate.authors)}')
    links.extend(
        [
            f'<a href="https://www.amazon.com/s?k={query}">Amazon</a>',
            f'<a href="https://www.goodreads.com/search?q={query}">Goodreads</a>',
            f'<a href="https://www.google.com/search?q={query}">Google</a>',
            # Guest search is blocked ("not permitted to use the search system"), so this
            # link only resolves for a browser already logged in to the forum.
            f'<a href="https://forum.mobilism.org/search.php?keywords={query}&amp;fid%5B%5D=106">Mobilism</a>',
        ]
    )
    return " · ".join(links)


def candidate_card(candidate: Candidate, kind: str, taxonomy: list[str] | None = None) -> str:
    series = ""
    if candidate.series:
        index = f" #{candidate.series_index:g}" if candidate.series_index is not None else ""
        series = f'<span class="pill">{html.escape(candidate.series)}{index}</span>'
    ai = ""
    if candidate.ai:
        genres = ", ".join(candidate.ai.get("genres", []))
        concerns = "; ".join(candidate.ai.get("concerns", []))
        ai = (
            f'<div class="ai"><strong>AI fit {candidate.ai.get("fit_score", 0)}/100:</strong> {html.escape(candidate.ai.get("why", ""))}'
            + (f'<br><span class="muted">{html.escape(genres)}</span>' if genres else "")
            + (f'<br><span class="warn">{html.escape(concerns)}</span>' if concerns else "")
            + "</div>"
        )
    elif candidate.categories:
        ai = f'<div class="ai"><span class="muted">{html.escape(", ".join(candidate.categories))}</span></div>'
    categories = display_categories(candidate, taxonomy or DEFAULT_CATEGORY_TAXONOMY)
    categories_attr = html.escape(",".join(categories), quote=True)
    expanded_isbns = set(candidate.isbns)
    expanded_isbns.update(value for isbn in candidate.isbns if (value := isbn10_from_13(isbn)))
    isbns = sorted(expanded_isbns, key=lambda value: (len(value) != 10, value))
    # ponytail: CDN ISBN lookup is best-effort; add a metadata API only if its hit rate becomes insufficient.
    cover_urls = [
        *candidate.cover_urls,
        *(f"https://images-na.ssl-images-amazon.com/images/P/{isbn}.01.LZZZZZZZ.jpg" for isbn in isbns),
        *(f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false" for isbn in reversed(isbns)),
    ]
    cover_urls = list(dict.fromkeys(value for value in cover_urls if value))
    image = ""
    if cover_urls:
        covers = html.escape(json.dumps(cover_urls, ensure_ascii=False), quote=True)
        image = f'<img src="{html.escape(cover_urls[0], quote=True)}" data-covers="{covers}" alt="Cover of {html.escape(candidate.title, quote=True)}" loading="lazy">'
    initial = html.escape((candidate.title.strip()[:1] or "?").upper())
    cover = f'<div class="cover-frame"><span class="cover-fallback" aria-hidden="true">{initial}</span>{image}</div>'
    search = html.escape(
        " ".join(
            [candidate.title, *candidate.authors, candidate.series, candidate.publisher, *candidate.subjects, *candidate.categories, *candidate.ai.get("genres", [])]
        ).casefold(),
        quote=True,
    )
    title = html.escape(candidate.title)
    authors = html.escape("; ".join(candidate.authors))
    score = f'{candidate.score:.0f}'
    score_badge = f'<span class="score" title="Relevance score; higher is a stronger match" aria-label="Relevance score {score}">{score}</span>'
    key_attr = html.escape(candidate.key, quote=True)
    return f"""
    <article class="book-card" data-kind="{kind}" data-key="{key_attr}" data-categories="{categories_attr}" data-search="{search}">
      <input class="pick" type="checkbox" data-key="{key_attr}" aria-label="Select {html.escape(candidate.title, quote=True)} for download">
      <button class="book-open" type="button" aria-label="View details for {html.escape(candidate.title, quote=True)}">
        {cover}
        <span class="card-copy">
          {score_badge}
          <strong class="card-title">{title}</strong>
          <span class="card-author">{authors}</span>
          <span class="card-meta">{html.escape(candidate.published_date or 'date unknown')}</span>
        </span>
      </button>
      <template>
        <div class="modal-book">
          <div class="modal-cover">{cover}</div>
          <div class="modal-copy">
            <div class="score modal-score" title="Relevance score; higher is a stronger match" aria-label="Relevance score {score}">{score}</div>
            <h2>{title}</h2>
            <div class="by">{authors}</div>
            <div class="meta">{series}<span>{html.escape(candidate.published_date or 'date unknown')}</span><span>{html.escape(candidate.publisher)}</span></div>
            <p class="modal-description">{html.escape(candidate.description[:2000])}</p>
            <div class="reasons">{html.escape(' · '.join(candidate.reasons))}</div>
            {ai}
            <div class="links">{source_links(candidate)}</div>
            <div class="actions"><button class="fetch" type="button" data-key="{html.escape(candidate.key, quote=True)}">Download to Calibre</button><span class="fetch-status" role="status"></span></div>
            <code>{html.escape(candidate.key)}</code>
          </div>
        </div>
      </template>
    </article>
    """


def render_report(
    candidates: list[Candidate],
    config: dict[str, Any],
    authors: list[str],
    series_queries: list[str],
    catalog_count: int,
    catalog_status: str,
    source_notes: list[str],
    ai_status: str,
    *,
    output_path: Path | None = None,
    report_day: date | None = None,
    screened_count: int | None = None,
    owned_suppressed_count: int | None = None,
    write_latest: bool = True,
    auto_refresh: int = 0,
    taxonomy: list[str] | None = None,
) -> Path:
    report_day = report_day or date.today()
    taxonomy = taxonomy or DEFAULT_CATEGORY_TAXONOMY
    visible = [
        item
        for item in candidates
        if not item.owned
        and item.decision != "dismiss"
        and item.score > -10
        and (((published := parse_dateish(item.published_date)) is not None and published <= report_day)
             or (published is None and any(e.get('source') == 'missing-list' for e in item.evidence)))
    ]
    owned_suppressed = sum(item.owned for item in candidates) if owned_suppressed_count is None else owned_suppressed_count
    screened = len(candidates) if screened_count is None else screened_count
    visible.sort(key=lambda item: (item.decision == "keep", item.score, item.published_date), reverse=True)
    series = [item for item in visible if item.series_alert]
    authors_section = [item for item in visible if item.matched_author and not item.series_alert]
    discovery = [item for item in visible if not item.matched_author and not item.series_alert]
    # Every category with at least one visible book gets a chip, not just the top
    # few; the derived categories keep the chips usable when the AI stage no-ops.
    category_counts: Counter[str] = Counter(category for item in visible for category in display_categories(item, taxonomy))
    category_chips = "".join(
        f'<button class="filter" type="button" data-filter="cat:{html.escape(name.casefold(), quote=True)}" aria-pressed="false">{html.escape(name)} {count}</button>'
        for name, count in category_counts.most_common()
    )

    def section(title: str, items: list[Candidate], empty: str, kind: str) -> str:
        cards = "".join(candidate_card(item, kind, taxonomy) for item in items)
        content = f'<div class="gallery">{cards}</div>' if cards else f'<p class="empty">{html.escape(empty)}</p>'
        return f'<section class="book-section"><h2>{html.escape(title)} <small>{len(items)}</small></h2>{content}</section>'

    report_dir = Path(config["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = output_path or report_dir / f"book-watch_{stamp}.html"
    notes = "".join(f"<li>{html.escape(redact_secrets(note))}</li>" for note in source_notes)
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{f'<script id="bw-refresh">window.BW_REFRESH={auto_refresh};</script>' if auto_refresh else ''}
<title>Calibre Book Recommender — {report_day.isoformat()}</title>
<style>
:root{{--bg:#f2f0ea;--paper:#fff;--ink:#18201d;--muted:#64706b;--accent:#176b5b;--accent-soft:#e4f1ed;--line:#d8ddd8;--warn:#98502e;--ok:#2e7d4f;--ok-soft:#e2f0e7}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,sans-serif}}
main{{max-width:1440px;margin:auto;padding:36px 24px 80px}} header{{border-bottom:1px solid var(--line);padding-bottom:22px;margin-bottom:24px}}
h1{{font:800 clamp(2.4rem,6vw,5.5rem)/.95 Georgia,serif;letter-spacing:-.04em;margin:.12em 0}} h2{{font:700 1.4rem Georgia,serif;border-bottom:1px solid var(--line);padding-bottom:9px;margin:42px 0 18px}}
h2 small{{color:var(--muted);font:500 .85rem system-ui}} button,input{{font:inherit}} button{{cursor:pointer}}
.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:20px 0}} .summary div{{background:var(--paper);border:1px solid var(--line);padding:12px;border-radius:10px}}
.summary strong{{display:block;font-size:1.45rem}} .filters{{position:sticky;top:0;z-index:5;display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:28px 0;padding:12px;background:#f2f0eaea;backdrop-filter:blur(12px);border:1px solid var(--line);border-radius:14px}}
.filters label{{flex:1;min-width:230px}} .filters input{{width:100%;border:1px solid var(--line);border-radius:10px;background:var(--paper);padding:10px 13px;color:var(--ink)}} .filter-buttons{{display:flex;gap:6px;flex-wrap:wrap}}
.filter{{border:1px solid var(--line);border-radius:99px;background:var(--paper);padding:8px 11px;color:var(--ink)}} .filter[aria-pressed="true"]{{border-color:var(--accent);background:var(--accent);color:#fff}} #result-count{{margin-left:auto;color:var(--muted);font-size:.9rem}}
.ai-pick{{flex:0 1 auto;min-width:0}} .ai-pick select{{width:100%;border:1px solid var(--line);border-radius:10px;background:var(--paper);padding:10px 13px;color:var(--ink)}} #ai-rerank{{font-weight:650;border-color:var(--accent)}} #ai-rerank:disabled{{opacity:.5;cursor:not-allowed}}
.gallery{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:22px}} .book-card{{position:relative;min-width:0;height:460px}}
.pick{{position:absolute;left:11px;top:11px;z-index:2;width:22px;height:22px;accent-color:var(--accent);cursor:pointer;filter:drop-shadow(0 1px 3px #0006)}} .pick:disabled{{cursor:default;opacity:.45}}
.book-open{{display:grid;grid-template-rows:minmax(0,1fr) 150px;width:100%;height:100%;padding:0;text-align:left;color:inherit;background:var(--paper);border:1px solid var(--line);border-radius:13px;overflow:hidden;box-shadow:0 5px 18px #17231f10;transition:transform .18s,box-shadow .18s,border-color .18s}}
.book-open:hover,.book-open:focus-visible{{transform:translateY(-4px);border-color:var(--accent);box-shadow:0 10px 24px #17231f1c;outline:none}} .cover-frame{{position:relative;display:grid;place-items:center;height:100%;overflow:hidden;background:linear-gradient(145deg,#d9e5df,#b9c8c2)}}
.cover-frame img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}} .cover-fallback{{font:800 4.5rem Georgia,serif;color:#ffffffb8;text-shadow:0 2px 10px #173c3150}}
.card-copy{{position:relative;display:flex;flex-direction:column;height:150px;padding:15px}} .card-title,.card-author,.card-meta{{display:block}} .card-title{{padding-right:42px;font:700 1.05rem/1.25 Georgia,serif;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}}
.card-author{{margin-top:6px;color:var(--muted);font-size:.85rem;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}} .card-meta{{margin-top:auto;color:var(--accent);font-size:.78rem;font-weight:700}}
.score{{position:absolute;right:12px;top:12px;background:var(--accent);color:white;border-radius:99px;min-width:36px;text-align:center;padding:4px 8px;font:bold 12px system-ui}}
.by{{font-style:italic;color:var(--muted)}} .meta{{display:flex;gap:9px;flex-wrap:wrap;margin:12px 0;color:var(--muted);font-size:13px}}
.meta span,.pill{{border:1px solid var(--line);border-radius:99px;padding:3px 9px}} .pill{{color:var(--accent);font-weight:700}}
.reasons{{color:var(--accent);font-weight:650;font-size:13px;line-height:1.4}} .ai{{background:var(--accent-soft);border-left:3px solid var(--accent);padding:10px 12px;margin:14px 0;font-size:14px}}
.links{{margin:14px 0;font-size:14px}} a{{color:var(--accent)}} code{{font-size:11px;color:var(--muted)}} .muted,.empty{{color:var(--muted)}} .warn{{color:var(--warn)}}
#download-list h4{{margin:12px 0 4px;font:700 .78rem system-ui;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}}
#download-list ul{{list-style:none;margin:0 0 8px;padding:0}}
#download-list li{{display:flex;align-items:baseline;gap:8px;padding:6px 9px;margin:3px 0;background:var(--paper);border:1px solid var(--line);border-radius:9px;font-size:.92rem}}
#download-list li::before{{content:"";flex:none;width:9px;height:9px;border-radius:99px;align-self:center}}
#download-list li.dl-queued::before{{background:var(--line)}} #download-list li.dl-running::before{{background:var(--accent)}}
#download-list li.dl-failed::before{{background:var(--warn)}} #download-list li.dl-failed{{border-color:var(--warn)}}
#download-list li.dl-done::before{{background:var(--ok)}} #download-list li.dl-done{{border-color:var(--ok);background:var(--ok-soft)}}
#download-list li.dl-saved::before{{background:var(--warn)}}
.dl-retry{{margin-left:auto;flex:none;border:1px solid var(--line);border-radius:99px;background:var(--paper);color:var(--accent);padding:2px 11px;font-size:.8rem;font-weight:650}}
.dl-retry:hover:not(:disabled){{border-color:var(--accent);background:var(--accent-soft)}} .dl-retry:disabled{{opacity:.45;cursor:not-allowed}}
.actions{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0}} .fetch{{border:1px solid var(--accent);border-radius:99px;background:var(--accent);color:#fff;padding:9px 16px;font-weight:650}}
.fetch:disabled{{border-color:var(--line);background:var(--paper);color:var(--muted);cursor:not-allowed}} .fetch-status{{color:var(--muted);font-size:13px}} .fetch-status.warn{{color:var(--warn)}}
dialog{{width:min(920px,calc(100% - 28px));height:min(700px,90vh);overflow:hidden;padding:0;border:0;border-radius:18px;background:var(--paper);color:var(--ink);box-shadow:0 30px 90px #0006}} dialog::backdrop{{background:#13201caa;backdrop-filter:blur(3px)}} .close{{position:absolute;right:14px;top:14px;z-index:2;width:42px;height:42px;border:0;border-radius:99px;background:#fff;color:var(--ink);box-shadow:0 2px 12px #0003;font-size:1.5rem}}
#modal-content,.modal-book{{height:100%}} .modal-book{{display:grid;grid-template-columns:minmax(240px,38%) 1fr;min-height:0}} .modal-cover{{background:#d9e5df}} .modal-cover .cover-frame{{height:100%;aspect-ratio:auto}} .modal-copy{{position:relative;padding:48px 42px 38px;overflow:auto}} .modal-copy h2{{padding:0 55px 0 0;margin:0 0 5px;border:0;font-size:2rem}} .modal-description{{max-height:180px;overflow-y:auto;padding-right:8px;overscroll-behavior:contain}} .modal-score{{top:48px;right:42px}}
details{{margin-top:44px;background:var(--paper);border:1px solid var(--line);padding:14px;border-radius:10px}} .sr-only{{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}}
@media (max-width:650px){{main{{padding:24px 14px 60px}} .gallery{{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}} .book-card{{height:390px}} .filters{{position:static}} #result-count{{width:100%;margin:0}} .modal-book{{grid-template-columns:1fr;grid-template-rows:42% minmax(0,1fr)}} .modal-copy{{padding:28px 22px}} .modal-score{{top:28px;right:22px}}}}
</style></head><body><main>
<header><div class="muted">AI-assisted release recommendations · {report_day.isoformat()}</div><h1>Calibre Book Recommender</h1><p>New books matched to your library and genre preferences.</p></header>
<p class="muted">Card badge = relevance score (higher is a stronger match), not a list index.</p>
<div class="summary"><div><strong>{catalog_count:,}</strong>Calibre books</div><div><strong>{screened:,}</strong>external records screened</div><div><strong>{owned_suppressed:,}</strong>already-owned records suppressed</div><div><strong>{len(visible):,}</strong>report items</div><div><strong>{len(series):,}</strong>series alerts</div></div>
<p><strong>Genre preferences:</strong> {html.escape(', '.join(config.get('taste', {}).get('include', [])) or 'none')}<br><strong>Authors queried:</strong> {html.escape(', '.join(authors) or 'none')}<br><strong>Series queried:</strong> {html.escape(', '.join(series_queries) or 'none')}</p>
<div class="filters"><label><span class="sr-only">Search books</span><input id="book-search" type="search" placeholder="Search title, author, series, genre…"></label>
<label class="ai-pick"><span class="sr-only">AI provider</span><select id="ai-provider" aria-label="AI provider"><option value="">AI ranking off</option></select></label>
<label class="ai-pick"><span class="sr-only">AI model</span><select id="ai-model" aria-label="AI model" disabled></select></label>
<button id="ai-rerank" class="filter" type="button" disabled>Rerank</button><span id="ai-note" class="fetch-status"></span>
<button id="bulk-download" class="filter" type="button" disabled>Download shown (0)</button><span id="bulk-note" class="fetch-status" role="status"></span>
<div class="filter-buttons" role="group" aria-label="Book category"><button class="filter" type="button" data-filter="all" aria-pressed="true">All {len(visible)}</button><button class="filter" type="button" data-filter="series" aria-pressed="false">Series {len(series)}</button><button class="filter" type="button" data-filter="author" aria-pressed="false">Authors {len(authors_section)}</button><button class="filter" type="button" data-filter="discovery" aria-pressed="false">Discovery {len(discovery)}</button><button class="filter" type="button" data-filter="downloaded" aria-pressed="false">Downloaded</button>{category_chips}</div><strong id="result-count" aria-live="polite">{len(visible)} books</strong></div>
<details id="download-log" hidden><summary>Downloads <span id="download-count" class="muted"></span></summary><div id="download-list" class="muted">Nothing fetched yet.</div></details>
{section('Series continuations', series, 'No unowned series continuation was confidently identified in this run.', 'series')}
{section('New releases by authors in your library', authors_section, 'No new books by checked authors were found.', 'author')}
{section('General discovery', discovery, 'No discovery candidates crossed the report threshold.', 'discovery')}
<details><summary>Run details and source health</summary><p>{html.escape(catalog_status)}</p><p>{html.escape(ai_status)}</p><ul>{notes}</ul>
<p>Record feedback with <code>python book_watch.py decide &lt;candidate-id&gt; keep|dismiss</code>.</p></details>
<dialog id="book-dialog" aria-label="Book details"><button class="close" type="button" aria-label="Close">×</button><div id="modal-content"></div></dialog>
<script>
const search = document.querySelector("#book-search");
// Cards are re-read from the DOM everywhere: the in-place refresh swaps card
// elements while the page stays open, so a load-time snapshot would go stale.
function allCards() {{ return [...document.querySelectorAll(".book-card")]; }}
const count = document.querySelector("#result-count");
let kind = "all";
const cats = new Set();
function applyFilters() {{
  const query = search.value.trim().toLocaleLowerCase();
  let shown = 0;
  for (const card of allCards()) {{
    const matchesKind = kind === "all" || (kind === "downloaded" ? Boolean(downloaded[card.dataset.key]) : card.dataset.kind === kind);
    const cardCats = (card.dataset.categories || "").split(",").map(value => value.trim().toLowerCase()).filter(Boolean);
    const matchesCat = !cats.size || cardCats.some(value => cats.has(value));
    const visible = matchesKind && matchesCat && card.dataset.search.includes(query);
    card.hidden = !visible;
    if (visible) shown++;
  }}
  for (const section of document.querySelectorAll(".book-section")) section.hidden = !section.querySelector(".book-card:not([hidden])");
  count.textContent = shown + (shown === 1 ? " book" : " books");
  updateBulk();
}}
search.addEventListener("input", applyFilters);
// Only the chips with data-filter change the card filter: #ai-rerank and #bulk-download
// borrow the .filter class for its styling, and binding them here set `kind` to
// undefined and hid every card. Kind chips are single-select; category chips are
// independent of them and of each other — several can be active at once, and a
// card matches when it carries any of the selected categories. The listener is
// delegated to the document because the in-place refresh replaces the chips.
document.addEventListener("click", event => {{
  const button = event.target.closest(".filter[data-filter]");
  if (!button) return;
  const value = button.dataset.filter;
  const isCategory = value.startsWith("cat:");
  if (isCategory) {{
    const name = value.slice(4);
    if (cats.has(name)) cats.delete(name); else cats.add(name);
    for (const item of document.querySelectorAll('button[data-filter^="cat:"]')) item.setAttribute("aria-pressed", String(cats.has(item.dataset.filter.slice(4))));
  }} else {{
    kind = value;
    for (const item of document.querySelectorAll('button[data-filter]:not([data-filter^="cat:"])')) item.setAttribute("aria-pressed", String(item === button));
  }}
  applyFilters();
}});
const dialog = document.querySelector("#book-dialog");
const modal = document.querySelector("#modal-content");
function nextCover(image) {{
  const covers = JSON.parse(image.dataset.covers);
  const next = Number(image.dataset.coverIndex || 1);
  if (next < covers.length) {{ image.dataset.coverIndex = String(next + 1); image.src = covers[next]; }}
  else image.hidden = true;
}}
document.addEventListener("error", event => {{
  if (event.target.matches?.("img[data-covers]")) nextCover(event.target);
}}, true);
document.addEventListener("load", event => {{
  if (event.target.matches?.("img[data-covers]") && (event.target.naturalWidth <= 1 || event.target.naturalHeight <= 1)) nextCover(event.target);
}}, true);
for (const image of document.querySelectorAll("img[data-covers]")) {{
  if (image.complete && (image.naturalWidth <= 1 || image.naturalHeight <= 1)) nextCover(image);
}}
// The buttons only work under `python book_watch.py serve`: a file:// page has no
// server to run calibredb for it, and the token is minted by that server per session.
const served = location.protocol.startsWith("http") && Boolean(window.BW_TOKEN);
const downloaded = window.BW_STATUS || {{}};
// While the run is still filling the report in, the page reloads itself — but never
// on top of an open book or a download in flight, which a meta refresh would kill.
let busy = 0;
const authorized = {{"Content-Type": "application/json", "X-Book-Watch-Token": window.BW_TOKEN}};
document.addEventListener("click", event => {{
  const opener = event.target.closest(".book-open");
  if (!opener) return;
  modal.replaceChildren(opener.closest(".book-card").querySelector("template").content.cloneNode(true));
  syncModal();
  dialog.showModal();
}});
document.addEventListener("click", event => {{
  const button = event.target.closest(".fetch");
  if (!button || button.disabled) return;
  enqueue([button.dataset.key]);
  syncModal();
}});
// A download belongs to the page, not to the open book: closing the modal must not
// stop the fetch. Everything queued here drains one at a time, because the server
// downloads one book at a time, and the Downloads panel shows the whole queue.
const queue = [];
const state = {{}};  // key -> "queued" | "running" | "failed: …"
const startedAt = {{}};
let draining = false;
let tick = 0;
function bookTitle(key) {{
  const card = allCards().find(item => item.dataset.key === key);
  return card?.querySelector(".card-title")?.textContent?.trim() || key;
}}
function enqueue(keys) {{
  let added = 0;
  for (const key of keys) {{
    if (!served || downloaded[key] || state[key] === "queued" || state[key] === "running") continue;
    state[key] = "queued";
    queue.push(key);
    added++;
  }}
  if (added) {{
    downloadLog.hidden = false;
    downloadLog.open = true;
    renderDownloads();
    drain();
  }}
  updateBulk();
  return added;
}}
async function drain() {{
  if (draining) return;
  draining = true;
  busy++;
  tick = setInterval(renderDownloads, 1000);
  while (queue.length) {{
    const key = queue.shift();
    state[key] = "running";
    startedAt[key] = Date.now();
    renderDownloads();
    try {{
      const response = await fetch("/download", {{method: "POST", headers: authorized, body: JSON.stringify({{key}})}});
      const result = await response.json();
      if (result.ok) {{ downloaded[key] = result.message; delete state[key]; }}
      else state[key] = "failed: " + result.message;
    }} catch (error) {{
      state[key] = "failed: " + String(error);
    }}
    const box = allPicks().find(item => item.dataset.key === key);
    if (box && downloaded[key]) {{ box.checked = false; box.disabled = true; }}
    syncModal();
    refreshDownloadLog();
  }}
  clearInterval(tick);
  busy--;
  draining = false;
  applyFilters();
}}
function syncModal() {{
  const button = modal.querySelector(".fetch");
  if (!button) return;
  const key = button.dataset.key;
  const status = modal.querySelector(".fetch-status");
  const current = downloaded[key] || state[key] || "";
  const pending = current === "queued" || current === "running";
  button.disabled = !served || pending || Boolean(downloaded[key]);
  status.classList.toggle("warn", current.startsWith("failed"));
  status.textContent = current === "queued" ? "Queued — see Downloads"
    : current === "running" ? "Fetching… — see Downloads"
    : current || (served ? "" : "Run: python book_watch.py serve");
}}
// Bulk download: whatever is ticked, or else everything the search/filter leaves on
// screen. Requests go one at a time because the server downloads one book at a time.
function allPicks() {{ return [...document.querySelectorAll(".pick")]; }}
const bulk = document.querySelector("#bulk-download");
const bulkNote = document.querySelector("#bulk-note");
function pendingKeys() {{
  const picks = allPicks();
  const ticked = picks.filter(box => box.checked);
  const chosen = ticked.length ? ticked.map(box => box.dataset.key) : allCards().filter(card => !card.hidden).map(card => card.dataset.key);
  return {{ticked: ticked.length > 0, keys: chosen.filter(key => !downloaded[key] && !state[key])}};
}}
function updateBulk() {{
  const {{ticked, keys}} = pendingKeys();
  bulk.textContent = (ticked ? "Download selected (" : "Download shown (") + keys.length + ")";
  bulk.disabled = !served || !keys.length;
}}
// Shift-click ticks everything between the last box clicked and this one, the way a
// file manager does. Hidden cards are skipped: a filtered-out book is not "in between".
// Delegated, because the in-place refresh swaps the checkboxes under the open page.
let lastPick = -1;
document.addEventListener("click", event => {{
  const box = event.target.closest(".pick");
  if (!box) return;
  const picks = allPicks();
  const position = picks.indexOf(box);
  if (event.shiftKey && lastPick >= 0) {{
    const from = Math.min(lastPick, position), to = Math.max(lastPick, position);
    for (const other of picks.slice(from, to + 1)) {{
      if (!other.disabled && !other.closest(".book-card").hidden) other.checked = box.checked;
    }}
  }}
  lastPick = position;
  updateBulk();
}});
for (const box of allPicks()) if (downloaded[box.dataset.key]) box.disabled = true;
bulk.addEventListener("click", () => {{
  const {{keys}} = pendingKeys();
  const added = enqueue(keys);
  bulkNote.classList.remove("warn");
  bulkNote.textContent = added ? "Queued " + added + " — see Downloads" : "Nothing left to queue";
}});
// Retry on a Failed or Saved, not imported row: the queue re-runs download_candidate,
// which reuses the saved file (import only) or searches the sources again.
document.addEventListener("click", event => {{
  const button = event.target.closest(".dl-retry");
  if (!button || button.disabled) return;
  enqueue([button.dataset.retry]);
  syncModal();
}});
updateBulk();
// Downloads panel: this page's queue, plus whatever the server is fetching for another
// tab, plus what it has already filed in Calibre.
const downloadLog = document.querySelector("#download-log");
const downloadList = document.querySelector("#download-list");
const downloadCount = document.querySelector("#download-count");
const esc = value => String(value).replace(/[&<>"]/g, ch => ({{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}}[ch]));
let serverLog = {{active: [], recent: []}};
let logTimer = 0;
function renderDownloads() {{
  // Grouped and color-coded: dot + row class say queued / running / failed /
  // imported / file-only at a glance instead of one flat list.
  const groups = {{active: [], failed: [], imported: [], saved: []}};
  const row = (cls, body) => '<li class="' + cls + '">' + body + "</li>";
  const retryButton = key => '<button class="dl-retry" type="button" data-retry="' + esc(key) + '"' + (served ? "" : " disabled") + ">Retry</button>";
  for (const key of Object.keys(state)) {{
    const seconds = startedAt[key] ? Math.round((Date.now() - startedAt[key]) / 1000) : 0;
    const title = esc(bookTitle(key));
    if (state[key].startsWith("failed")) {{
      groups.failed.push(row("dl-failed", retryButton(key) + '<strong>Failed</strong> ' + title + ' <span class="muted">' + esc(state[key].slice(8)) + '</span>'));
    }} else {{
      const label = state[key] === "running" ? "fetching " + seconds + "s" : "queued";
      groups.active.push(row(state[key] === "running" ? "dl-running" : "dl-queued", "<strong>" + esc(label) + "</strong> " + title));
    }}
  }}
  for (const item of serverLog.active) {{
    if (item.key in state) continue;
    groups.active.push(row(item.state === "running" ? "dl-running" : "dl-queued",
      "<strong>" + esc(item.state) + "</strong> " + esc(item.title) + ' <span class="muted">other tab · ' + esc(item.seconds) + 's</span>'));
  }}
  for (const item of serverLog.recent) {{
    const when = '<span class="muted">' + esc(item.added_at).slice(0, 16).replace("T", " ") + '</span>';
    if (item.imported === false || !item.calibre_id) {{
      groups.saved.push(row("dl-saved", retryButton(item.key) + esc(item.title) + " " + when));
    }} else {{
      groups.imported.push(row("dl-done", esc(item.title) + ' <span class="muted">Calibre #' + esc(item.calibre_id) + '</span> ' + when));
    }}
  }}
  const section = (heading, items) => items.length ? "<h4>" + heading + "</h4><ul>" + items.join("") + "</ul>" : "";
  const body = section("In progress", groups.active) + section("Failed", groups.failed)
    + section("Added to Calibre", groups.imported) + section("Saved, not imported", groups.saved);
  const running = queue.length + Object.values(state).filter(value => value === "running").length;
  downloadCount.textContent = running ? running + " in progress"
    : (groups.imported.length + groups.saved.length) + " done";
  downloadList.innerHTML = body || "Nothing fetched yet.";
  downloadList.classList.toggle("empty", !body);
  if (body) downloadLog.hidden = false;
}}
async function refreshDownloadLog() {{
  if (!served) return;
  clearTimeout(logTimer);
  try {{
    const response = await fetch("/downloads", {{headers: authorized}});
    serverLog = {{active: [], recent: [], ...(await response.json())}};
    for (const item of serverLog.recent) if (!downloaded[item.key]) downloaded[item.key] = "already downloaded";
    renderDownloads();
    if (serverLog.active.length || queue.length) logTimer = setTimeout(refreshDownloadLog, 3000);
  }} catch (error) {{ /* the server going away is not worth a banner */ }}
}}
refreshDownloadLog();
dialog.querySelector(".close").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", event => {{ if (event.target === dialog) dialog.close(); }});
// AI provider/model picker: only functional under `serve`, which injects BW_AI and
// exposes /rerank. The chosen model is remembered in localStorage across reports.
const aiProvider = document.querySelector("#ai-provider");
const aiModel = document.querySelector("#ai-model");
const aiRerank = document.querySelector("#ai-rerank");
const aiNote = document.querySelector("#ai-note");
const providers = served ? (window.BW_AI || {{}}) : {{}};
for (const [name, info] of Object.entries(providers)) {{
  const option = document.createElement("option");
  option.value = name;
  option.textContent = name + (info.error ? " (unavailable)" : "");
  aiProvider.append(option);
}}
// The live model list of a gateway is fetched only when its provider is chosen:
// listing openai_oauth's models starts the npx proxy and its browser sign-in.
const modelCache = {{}};
async function loadModels() {{
  const name = aiProvider.value;
  if (!served || !name || modelCache[name] || !providers[name]?.live) return;
  modelCache[name] = true;
  aiNote.classList.remove("warn");
  aiNote.textContent = "Loading " + name + " models…";
  try {{
    const response = await fetch("/models?provider=" + encodeURIComponent(name), {{headers: authorized}});
    const info = await response.json();
    providers[name] = info;
    aiNote.textContent = info.error || "";
    if (info.error) aiNote.classList.add("warn");
  }} catch (error) {{
    modelCache[name] = false;
    aiNote.classList.add("warn");
    aiNote.textContent = String(error);
  }}
  fillModels();
}}
function fillModels() {{
  const chosen = aiModel.value;
  aiModel.replaceChildren();
  for (const model of providers[aiProvider.value]?.models || []) {{
    const option = document.createElement("option");
    option.value = model;
    option.textContent = model;
    aiModel.append(option);
  }}
  const custom = document.createElement("option");
  custom.value = "__custom__";
  custom.textContent = "Other (type a model id)…";
  aiModel.append(custom);
  if ([...aiModel.options].some(option => option.value === chosen)) aiModel.value = chosen;
  aiModel.disabled = false;
  aiRerank.disabled = !served;
}}
function chosenModel() {{
  return aiModel.value === "__custom__" ? (prompt("Model id for " + aiProvider.value) || "").trim() : aiModel.value;
}}
try {{
  const saved = JSON.parse(localStorage.getItem("bw-ai") || "null");
  if (saved && providers[saved.provider]) {{
    aiProvider.value = saved.provider;
    fillModels();
    if ((providers[saved.provider].models || []).includes(saved.model)) aiModel.value = saved.model;
  }}
}} catch (error) {{ /* a stale preference is not worth reporting */ }}
if (!aiProvider.value) {{
  aiProvider.value = "commandcode" in providers ? "commandcode" : ("hyper" in providers ? "hyper" : (Object.keys(providers)[0] || ""));
}}
fillModels();
if (aiModel.options.length) aiModel.selectedIndex = 0;
aiProvider.addEventListener("change", () => {{ fillModels(); if (aiModel.options.length) aiModel.selectedIndex = 0; loadModels(); }});
aiModel.addEventListener("focus", loadModels);
aiRerank.addEventListener("click", async () => {{
  const model = chosenModel();
  if (!model) return;
  aiRerank.disabled = true;
  aiNote.classList.remove("warn");
  aiNote.textContent = "Asking " + aiProvider.value + "/" + model + " to re-rank…";
  busy++;
  let ok = false;
  try {{
    const response = await fetch("/rerank", {{method: "POST", headers: authorized, body: JSON.stringify({{provider: aiProvider.value, model}})}});
    const result = await response.json();
    aiNote.textContent = result.message;
    if (result.ok) {{
      ok = true;
      localStorage.setItem("bw-ai", JSON.stringify({{provider: aiProvider.value, model}}));
    }} else aiNote.classList.add("warn");
  }} catch (error) {{
    aiNote.classList.add("warn");
    aiNote.textContent = String(error);
  }} finally {{
    busy--;
    aiRerank.disabled = false;
    // The re-rank rewrote the report under the open page: merge it in place so
    // the selections and filters survive, like every other stage does.
    if (ok) refreshReport();
  }}
}});
// The run is still filling this report in: merge the fresh HTML into the open page
// instead of reloading, so the search text, active chips, ticked checkboxes, the
// queue and the downloads panel all survive the update. Same idle guards as the old
// reload — an open book or a download in flight is never interrupted; a missed tick
// simply waits for the next one.
let refreshing = false;
let refreshTimer = 0;
function stopAutoRefresh() {{ clearInterval(refreshTimer); refreshTimer = 0; }}
async function refreshReport() {{
  if (refreshing || busy || dialog.open || document.hidden) return;
  refreshing = true;
  try {{
    const response = await fetch(location.href);
    if (!response.ok) return;
    const fresh = new DOMParser().parseFromString(await response.text(), "text/html");
    // Stop before any other check: a final write with every section empty is
    // legitimate, and the timer must end rather than poll a finished run.
    if (!fresh.querySelector("#bw-refresh")) stopAutoRefresh();
    if (!fresh.querySelector(".book-card") && !document.querySelector(".book-card")) return;
    const checked = new Set(allPicks().filter(box => box.checked).map(box => box.dataset.key));
    for (const section of document.querySelectorAll(".book-section")) {{
      const heading = section.querySelector("h2")?.childNodes[0]?.textContent?.trim();
      const freshSection = heading && [...fresh.querySelectorAll(".book-section")]
        .find(item => item.querySelector("h2")?.childNodes[0]?.textContent?.trim() === heading);
      if (!freshSection) continue;
      // The whole section, so a gallery that emptied into "no candidates" merges too.
      section.innerHTML = freshSection.innerHTML;
    }}
    for (const box of allPicks()) {{
      if (checked.has(box.dataset.key)) box.checked = true;
      if (downloaded[box.dataset.key]) box.disabled = true;
    }}
    const copy = selector => {{
      const target = document.querySelector(selector);
      const source = fresh.querySelector(selector);
      if (target && source) target.innerHTML = source.innerHTML;
    }};
    copy(".summary");
    copy("details:not(#download-log)");  // run details; the downloads panel belongs to this page's JS
    const buttons = document.querySelector(".filter-buttons");
    const freshButtons = fresh.querySelector(".filter-buttons");
    if (buttons && freshButtons) {{
      buttons.innerHTML = freshButtons.innerHTML;
      for (const item of buttons.querySelectorAll('button[data-filter^="cat:"]'))
        item.setAttribute("aria-pressed", String(cats.has(item.dataset.filter.slice(4))));
      for (const item of buttons.querySelectorAll('button[data-filter]:not([data-filter^="cat:"])'))
        item.setAttribute("aria-pressed", String(item.dataset.filter === kind));
    }}
    applyFilters();
  }} catch (error) {{
    // A file:// page cannot fetch its own file — there the old reload is the only option.
    if (!location.protocol.startsWith("http")) location.reload();
  }} finally {{
    refreshing = false;
  }}
}}
if (window.BW_REFRESH) refreshTimer = setInterval(refreshReport, window.BW_REFRESH * 1000);
</script></main></body></html>"""
    # Atomic: the served page re-reads these files while the run keeps rewriting
    # them, and a torn read would render a half-written report.
    temporary = path.with_suffix(".html.tmp")
    temporary.write_text(body, encoding="utf-8")
    temporary.replace(path)
    if write_latest:
        latest = report_dir / "latest.html"
        latest_tmp = latest.with_suffix(".html.tmp")
        latest_tmp.write_text(body, encoding="utf-8")
        latest_tmp.replace(latest)
    return path


def plain_text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def report_metadata_index(conn: sqlite3.Connection, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT candidate_key,payload FROM candidate_history"):
        try:
            metadata[row["candidate_key"]] = json.loads(row["payload"])
        except json.JSONDecodeError:
            pass
    source_cfg = config.get("sources", {})
    today = date.today()
    offsets = range(-int(source_cfg.get("reactor_months_back", 6)), int(source_cfg.get("reactor_months_forward", 1)) + 1)
    for genre in source_cfg.get("reactor_genres", ["science-fiction", "fantasy"]):
        for offset in offsets:
            month = add_months(today, offset)
            slug = f"new-{genre}-books-{calendar.month_name[month.month].lower()}-{month.year}"
            url = f"https://reactormag.com/{slug}/"
            cache_key = hashlib.sha256(f"GET|{url}|".encode()).hexdigest()
            row = conn.execute("SELECT body FROM http_cache WHERE cache_key=? AND status<400", (cache_key,)).fetchone()
            if not row:
                continue
            for candidate in parse_reactor_page(row["body"], genre, month, url):
                entry = metadata.setdefault(candidate.key, {})
                entry["isbns"] = sorted(set(entry.get("isbns", [])) | candidate.isbns)
    return metadata


def parse_report_cards(source: str, metadata: dict[str, dict[str, Any]]) -> list[Candidate]:
    candidates: list[Candidate] = []

    def extract(pattern: str, value: str) -> str:
        match = re.search(pattern, value, re.I | re.S)
        return plain_text(match.group(1)) if match else ""

    for section_match in re.finditer(r"<section\b[^>]*>\s*<h2[^>]*>(.*?)</h2>(.*?)</section>", source, re.I | re.S):
        heading = plain_text(section_match.group(1)).casefold()
        kind = "series" if "series continuation" in heading else "author" if "author" in heading else "discovery"
        for article_match in re.finditer(r"<article\b([^>]*)>(.*?)</article>", section_match.group(2), re.I | re.S):
            attrs, article = article_match.groups()
            declared_kind = re.search(r'data-kind="([^"]+)"', attrs)
            card_kind = declared_kind.group(1) if declared_kind else kind
            categories_attr = re.search(r'data-categories="([^"]*)"', attrs)
            title = extract(r'<(?:h3|strong\s+class="card-title")[^>]*>(.*?)</(?:h3|strong)>', article)
            authors_text = extract(r'<(?:div\s+class="by"|span\s+class="card-author")[^>]*>(.*?)</(?:div|span)>', article)
            if not title or not authors_text:
                continue
            key_match = re.search(r"work:[0-9a-f]+", article, re.I)
            key = key_match.group() if key_match else candidate_key(title, split_authors(authors_text))
            meta = re.search(r'<div\s+class="meta"[^>]*>(.*?)</div>', article, re.I | re.S)
            meta_html = meta.group(1) if meta else ""
            series_text = extract(r'<span\s+class="pill"[^>]*>(.*?)</span>', meta_html)
            series_match = re.match(r"^(.*?)(?:\s+#(\d+(?:\.\d+)?))?$", series_text)
            other_meta = [plain_text(value) for value in re.findall(r'<span(?!\s+class="pill")[^>]*>(.*?)</span>', meta_html, re.I | re.S)]
            history = metadata.get(key, {})
            candidate = Candidate(
                title=title,
                authors=split_authors(authors_text),
                key=key,
                isbns={clean_isbn(str(value)) for value in history.get("isbns", []) if clean_isbn(str(value))},
                cover_url=str(history.get("cover_url") or ""),
                cover_urls=[str(value) for value in history.get("cover_urls", []) if value],
                language=str(history.get("language") or ""),
                series=series_match.group(1) if series_match else series_text,
                series_index=safe_float(series_match.group(2)) if series_match else None,
                published_date=other_meta[0] if other_meta else extract(r'<span\s+class="card-meta"[^>]*>(.*?)</span>', article),
                publisher=other_meta[1] if len(other_meta) > 1 else "",
                description=extract(r"<p[^>]*>(.*?)</p>", article),
                subjects={str(value) for value in history.get("subjects", [])},
                categories=[value.strip() for value in html.unescape(categories_attr.group(1)).split(",") if value.strip()] if categories_attr else [],
            )
            candidate.score = safe_float(extract(r'<(?:div|span)\s+class="score(?:\s+[^\"]*)?"[^>]*>(.*?)</(?:div|span)>', article)) or 0
            candidate.reasons = [value.strip() for value in extract(r'<div\s+class="reasons"[^>]*>(.*?)</div>', article).split(" · ") if value.strip()]
            candidate.series_alert = card_kind == "series"
            candidate.matched_author = candidate.authors[0] if card_kind == "author" else ""
            for href, label in re.findall(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', article, re.I | re.S):
                label_text = plain_text(label)
                if label_text not in {"Amazon", "Goodreads", "Google"}:
                    candidate.evidence.append({"source": label_text, "url": html.unescape(href), "detail": "historical report"})
            for isbn in re.findall(r"/isbn/([0-9Xx]{10,13})-L\.jpg", article):
                cleaned = clean_isbn(isbn)
                if cleaned:
                    candidate.isbns.add(cleaned)
            covers_match = re.search(r'data-covers="([^"]+)"', article)
            if covers_match:
                try:
                    covers = [
                        str(value)
                        for value in json.loads(html.unescape(covers_match.group(1)))
                        if "/images/P/" not in str(value) and "/b/isbn/" not in str(value)
                    ]
                    candidate.cover_urls = list(dict.fromkeys([*candidate.cover_urls, *(str(value) for value in covers)]))
                    candidate.cover_url = candidate.cover_urls[0] if candidate.cover_urls else ""
                except json.JSONDecodeError:
                    pass
            ai_match = re.search(r'<div\s+class="ai"[^>]*>(.*?)</div>', article, re.I | re.S)
            if ai_match:
                ai_html = ai_match.group(1)
                fit = re.search(r"AI fit\s+(\d+)/100", plain_text(ai_html), re.I)
                why = re.sub(r"^.*?</strong>\s*", "", ai_html, count=1, flags=re.I | re.S).split("<br", 1)[0]
                candidate.ai = {
                    "fit_score": int(fit.group(1)) if fit else 0,
                    "why": plain_text(why),
                    "genres": [value.strip() for value in extract(r'<span\s+class="muted"[^>]*>(.*?)</span>', ai_html).split(",") if value.strip()],
                    "concerns": [value.strip() for value in extract(r'<span\s+class="warn"[^>]*>(.*?)</span>', ai_html).split(";") if value.strip()],
                    "model": "historical report",
                }
            candidates.append(candidate)
    return candidates


def update_reports(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    load_env_file(config_path.parent / ".env")
    report_dir = Path(config["report_dir"])
    paths = sorted(report_dir.glob("book-watch_*.html"))
    if not paths:
        print("No timestamped reports found")
        return 0
    conn = connect_state(config)
    metadata = report_metadata_index(conn, config)
    raw_books, catalog_status = load_calibre(config)
    catalog = build_catalog(raw_books)
    updated = 0
    try:
        for path in paths:
            source = path.read_text(encoding="utf-8")
            original_count = len(re.findall(r"<article\b", source, re.I))
            candidates = parse_report_cards(source, metadata)
            if len(candidates) != original_count:
                print(f"Skipped {path.name}: parsed {len(candidates)}/{original_count} cards")
                continue
            for candidate in candidates:
                candidate.owned = owned_in_catalog(candidate, catalog)
            try:
                matched, targets, errors = fill_missing_covers(
                    conn, config, candidates, False, retry_with_isbn=True
                )
                recheck_note = f"Update recheck: {matched}/{targets} missing covers found"
                if errors:
                    recheck_note += f"; {errors} query errors"
            except Exception as exc:
                recheck_note = f"Update recheck cover error: {redact_secrets(exc)}"

            def count(label: str) -> int:
                match = re.search(rf"<strong>([\d,]+)</strong>{re.escape(label)}", source, re.I)
                return int(match.group(1).replace(",", "")) if match else 0

            query_match = re.search(r"<strong>Authors queried:</strong>\s*(.*?)<br>\s*<strong>Series queried:</strong>\s*(.*?)</p>", source, re.I | re.S)
            authors = [] if not query_match or plain_text(query_match.group(1)) == "none" else [value.strip() for value in plain_text(query_match.group(1)).split(",")]
            series_queries = [] if not query_match or plain_text(query_match.group(2)) == "none" else [value.strip() for value in plain_text(query_match.group(2)).split(",")]
            details = re.search(r"<details[^>]*>.*?</summary>\s*<p>(.*?)</p>\s*<p>(.*?)</p>\s*<ul>(.*?)</ul>", source, re.I | re.S)
            ai_status = plain_text(details.group(2)) if details else "Historical report"
            notes = [plain_text(value) for value in re.findall(r"<li>(.*?)</li>", details.group(3), re.I | re.S)] if details else []
            notes = [note for note in notes if not note.startswith("Update recheck")]
            notes.append(recheck_note)
            day_match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
            report_day = datetime.strptime(day_match.group(1), "%Y-%m-%d").date() if day_match else date.today()
            temporary = path.with_suffix(".html.tmp")
            render_report(
                candidates,
                config,
                authors,
                series_queries,
                len(raw_books),
                catalog_status,
                notes,
                ai_status,
                output_path=temporary,
                report_day=report_day,
                screened_count=count("external records screened"),
                owned_suppressed_count=count("already-owned records suppressed") + sum(item.owned for item in candidates),
                write_latest=False,
                taxonomy=category_taxonomy(catalog),
            )
            rendered = temporary.read_text(encoding="utf-8")
            rendered_count = rendered.count('class="book-card"')
            backup = path.with_suffix(".html.bak")
            if not backup.exists():
                shutil.copy2(path, backup)
            temporary.replace(path)
            updated += 1
            print(f"Updated {path.name}: {original_count} -> {rendered_count} released cards")
    finally:
        conn.close()
    if updated:
        shutil.copy2(paths[-1], report_dir / "latest.html")
    print(f"Updated {updated}/{len(paths)} timestamped reports; original files kept as .html.bak")
    return 0 if updated == len(paths) else 1


def run_report(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    local_env_loaded = load_env_file(config_path.parent / ".env")
    progress(f"Starting report with {config_path.name}")
    if local_env_loaded:
        progress("Loaded local .env")
    conn = connect_state(config)
    started = iso_now()
    conn.execute(
        "UPDATE runs SET completed_at=?,status='failed',notes='Interrupted before completion' WHERE status='running'",
        (started,),
    )
    cursor = conn.execute("INSERT INTO runs(started_at,status,notes) VALUES(?,?,?)", (started, "running", ""))
    run_id = cursor.lastrowid
    conn.commit()
    try:
        # Restarting book-watch is one of the two ways a saved-but-not-imported
        # download completes: the import only re-runs, no re-fetch.
        retry_note = retry_pending_imports(conn, config)
        if retry_note != "no pending imports":
            progress(retry_note)
        progress("Reading Calibre catalog...")
        raw_books, catalog_status = load_calibre(config)
        progress(f"Calibre catalog ready: {len(raw_books):,} books ({catalog_status})")
        catalog = build_catalog(raw_books)
        # Categories are the library's own tags, so AI classification, cached
        # categories and the report chips all share one vocabulary.
        taxonomy = category_taxonomy(catalog)
        if getattr(args, 'like', None) is not None:
            book = next((book for book in raw_books if int(book['id']) == args.like), None)
            if book is None:
                raise ValueError(f'Calibre book ID {args.like} is not in this library')
            args.author = list(args.author or []) + split_authors(book['authors'])
            args.series = list(args.series or []) + ([book['series']] if book.get('series') else [])
        authors = select_authors(conn, catalog, config, args.author or [], args.max_authors)
        series_queries = select_series(conn, catalog, config, list(args.series or []), args.max_series)
        genres = list(dict.fromkeys(value.strip() for value in (args.genre or []) if value.strip()))
        if genres:
            config.setdefault("taste", {})["include"] = genres
        if getattr(args, "ai", None):
            config["_ai_provider"] = args.ai
        progress(f"Selected {len(authors)} author, {len(series_queries)} series, and {len(genres)} explicit genre queries")
        sources = config["sources"]
        query_genres = genres or ([] if args.focused else list(sources.get("subjects", [])))
        source_notes: list[str] = []

        def fetch_source(label: str) -> tuple[str, list[Candidate], list[str], str]:
            """One network source on its own state connection: pool threads never
            share the caller's sqlite connection. emit comes from the consumer."""
            conn_t = connect_state(config)
            try:
                if label == "Google Books":
                    queries = [(f"author: {author}", f'inauthor:"{author}"') for author in authors]
                    queries += [(f"series: {series}", f'"{series}"') for series in series_queries]
                    queries += [(f"subject: {subject}", f'subject:"{subject}"') for subject in query_genres]
                    progress(f"Google Books: running {len(queries)} queries...")
                    found, errors = google_candidates(conn_t, config, queries, args.refresh)
                    return label, found, errors, f"Google Books: {len(found)} raw candidates from {len(queries)} queries"
                if label == "Open Library":
                    queries_ol = [(f"author: {author}", "author", author) for author in authors]
                    queries_ol += [(f"series: {series}", "q", series) for series in series_queries]
                    queries_ol += [(f"subject: {subject}", "subject", subject) for subject in query_genres]
                    progress(f"Open Library: running {len(queries_ol)} queries...")
                    found = open_library_candidates(conn_t, config, queries_ol, args.refresh)
                    return label, found, [], f"Open Library: {len(found)} raw candidates from {len(queries_ol)} queries"
                if label == "Reactor":
                    progress("Reactor: checking monthly release lists...")
                    found, errors = reactor_candidates(conn_t, config, args.refresh)
                    progress(f"Reactor: {len(found)} records, {len(errors)} errors")
                    return label, found, errors, f"Reactor: {len(found)} raw candidates"
                if label == "Hardcover":
                    progress("Hardcover: checking configured authors...")
                    found, hardcover_note = hardcover_candidates(conn_t, config, authors, args.refresh)
                    progress(f"Hardcover: {len(found)} records ({hardcover_note})")
                    return label, found, [], f"Hardcover: {len(found)} raw candidates. {hardcover_note}"
                if label == "Mobilism":
                    queries_mob = [(f"series: {series}", "series", series) for series in series_queries]
                    queries_mob += [(f"author: {author}", "author", author) for author in authors]
                    progress(f"Mobilism: searching {len(queries_mob)} owned series and authors...")
                    found, errors = mobilism_candidates(conn_t, config, queries_mob, args.refresh, catalog)
                    progress(f"Mobilism: {len(found)} records, {len(errors)} search errors")
                    return label, found, errors, f"Mobilism: {len(found)} raw candidates from {len(queries_mob)} searches"
                raise ValueError(f"Unknown source: {label}")
            finally:
                conn_t.close()

        def fetch_sources(conn_w: sqlite3.Connection, emit) -> None:
            """Every enabled source at once — they hit different hosts, so the slowest
            one sets the pace instead of the sum of all of them. emit(label, found,
            errors, note) still runs on the caller's thread, one merge per finished
            source, exactly as the sequential version did. Mobilism keeps its whole
            loop inside one task: a shared forum login must not be used twice."""
            del conn_w  # each task owns its connection; the parameter stays for the direct-call tests
            with ThreadPoolExecutor(max_workers=max(2, len(source_labels))) as pool:
                futures = {pool.submit(fetch_source, label): label for label in source_labels}
                for future in as_completed(futures):
                    label = futures[future]
                    try:
                        emit(*future.result())
                    except Exception as exc:
                        emit(label, [], [], f"{label} error: {redact_secrets(exc)}")
                        progress(f"{label} failed: {exc}")

            if args.no_network:
                emit("sources", [], [], "Network sources disabled; report uses no external candidates")
                progress("Network sources disabled")

        covers_stage = not args.no_network and (sources.get("google_books", True) or sources.get("open_library", True))
        mobilism_stage = not args.no_network and config.get("mobilism", {}).get("enabled", False)
        source_labels = [
            label for enabled, label in [
                (not args.no_network and sources.get("google_books", True), "Google Books"),
                (not args.no_network and sources.get("open_library", True), "Open Library"),
                (not args.no_network and sources.get("reactor", True), "Reactor"),
                (not args.no_network and sources.get("hardcover", True), "Hardcover"),
                (not args.no_network and config.get("mobilism", {}).get("enabled", False) and config.get("mobilism", {}).get("discover", True), "Mobilism"),
            ] if enabled
        ]
        pending = [*source_labels] + ([] if args.no_ai else ["AI categories", "AI ranking"]) + (["cover fallback"] if covers_stage else []) + (["Mobilism topics"] if mobilism_stage else [])
        report: Path | None = None

        def publish(status: str, done: str = "") -> None:
            """Write the report as it stands; every later stage overwrites the same file.

            Candidates are stored first every time: the page is live and its Download
            buttons look candidates up by id, so a card must never reach the browser
            before the row behind it exists.
            """
            nonlocal report
            persist_candidates(conn, candidates)
            if done in pending:
                pending.remove(done)
            waiting = ", ".join(pending)
            report = render_report(
                candidates,
                config,
                authors,
                series_queries,
                len(raw_books),
                catalog_status,
                source_notes + ([f"Still running: {waiting}"] if waiting else []),
                status + (f" · still running: {waiting}" if waiting else ""),
                output_path=report,
                auto_refresh=8 if waiting else 0,
                taxonomy=taxonomy,
            )

        missing_rows: list[tuple[str, dict]] = []
        for row in conn.execute("SELECT candidate_key,payload FROM candidate_history WHERE payload LIKE '%missing-list%'"):
            payload = json.loads(row['payload'])
            if any(item.get('source') == 'missing-list' for item in payload.get('evidence', [])):
                missing_rows.append((row['candidate_key'], payload))

        def rebuild(rows: list[tuple[str, dict]]) -> list[Candidate]:
            """Fresh Candidate objects for every payload row, so re-scoring never
            double-counts: merge/score mutate the objects they are given."""
            raw = [candidate_from_payload(key, payload) for key, payload in [*rows, *missing_rows]]
            merged = merge_candidates(raw)
            apply_cached_categories(conn, merged, taxonomy)
            match_and_score(merged, catalog, config)
            load_decisions(conn, merged)
            merged.sort(key=lambda item: (item.score, item.published_date), reverse=True)
            return merged

        # The report is usable within seconds: everything the last run found is
        # re-scored and published immediately, then the sources refresh in a
        # background thread and each one's finds merge in as they arrive.
        last_complete = conn.execute(
            "SELECT started_at FROM runs WHERE status='complete' AND id!=? ORDER BY id DESC LIMIT 1", (run_id,)
        ).fetchone()
        since = str(last_complete["started_at"]) if last_complete else (utcnow() - timedelta(days=1)).isoformat(timespec="seconds")
        seed_rows = [
            (row["candidate_key"], json.loads(row["payload"]))
            for row in conn.execute(
                "SELECT candidate_key,payload FROM candidate_history WHERE last_seen>=? ORDER BY last_seen DESC LIMIT 4000", (since,)
            )
        ]
        progress(f"Seeding the report from the previous run: {len(seed_rows):,} known candidates")
        candidates = rebuild(seed_rows)

        progress("Writing the first HTML report...")
        publish("Report seeded from the previous run; sources refreshing in the background.")
        progress(f"Report: {report}")
        httpd = None
        if not args.no_serve:
            # Served from a thread so the page (and its Download buttons) is usable
            # while the slow stages below keep filling the same file in.
            httpd = report_httpd(config, args.port)
            url = f"http://127.0.0.1:{httpd.server_address[1]}/"
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            progress(f"Serving at {url} — the page updates itself in place as stages finish")
            # While the report is served, saved-but-not-imported downloads retry
            # every two minutes: closing Calibre is enough, the next pass imports.
            threading.Thread(target=import_retry_worker, args=(config, threading.Event()), daemon=True, name="bw-import-retry").start()
            if not args.no_open:
                webbrowser.open(url)

        results_q: queue.Queue = queue.Queue()
        collected_rows: list[tuple[str, dict]] = []

        def worker() -> None:
            conn_w = None
            try:
                # connect_state takes a write lock (BEGIN IMMEDIATE); it belongs inside
                # the try so a lock contention can never leave the run waiting forever.
                conn_w = connect_state(config)
                fetch_sources(conn_w, lambda label, found, errors, note: results_q.put(
                    (label, [(item.key, candidate_payload(item)) for item in found], errors, note)
                ))
            except Exception as exc:
                results_q.put(("sources", [], [], f"Background source refresh failed: {redact_secrets(exc)}"))
                progress(f"Background source refresh failed: {exc}")
            finally:
                if conn_w is not None:
                    conn_w.close()
                results_q.put(None)

        if source_labels:
            threading.Thread(target=worker, daemon=True, name="bw-sources").start()
            while True:
                item = results_q.get()
                if item is None:
                    break
                label, rows, errors, note = item
                collected_rows.extend(rows)
                source_notes.append(note)
                source_notes.extend(summarize_http_errors(errors))
                if rows:
                    candidates = rebuild(seed_rows + collected_rows)
                publish(note, label)
            # A source that crashed without emitting must not hold its label in
            # pending: the final write would claim work is still running and keep
            # the page auto-reloading forever.
            for label in source_labels:
                if label in pending:
                    pending.remove(label)
                    source_notes.append(f"{label}: background refresh never finished")
        else:
            fetch_sources(conn, lambda label, found, errors, note: (
                source_notes.append(note), source_notes.extend(summarize_http_errors(errors)),
            ))

        if not args.no_ai:
            progress("AI: classifying candidate titles...")
            # Candidates without categories after the cache pass are exactly the ones
            # the AI stage may classify, and the only ones that get a second taste pass.
            newly_classifiable = {candidate.key for candidate in candidates if not candidate.categories and not candidate.owned}
            category_status = categorize_candidates(conn, candidates, config, taxonomy)
            progress(category_status)
            if category_status.startswith("AI categorized"):
                taste = [normalize(value) for value in config.get("taste", {}).get("include", [])]
                excluded = [normalize(value) for value in config.get("taste", {}).get("exclude", [])]
                for candidate in candidates:
                    if candidate.key in newly_classifiable and candidate.categories:
                        base_combined = normalize(" ".join([candidate.title, candidate.series, candidate.description, " ".join(candidate.subjects)]))
                        full_combined = normalize(" ".join([base_combined, " ".join(candidate.categories)]))
                        apply_category_taste_delta(candidate, taste, excluded, base_combined, full_combined)
            source_notes.append(category_status)
            publish(category_status, "AI categories")
        progress("AI: enriching top candidates..." if not args.no_ai else "AI disabled for this run")
        ai_status = "AI disabled for this run" if args.no_ai else enrich_with_openrouter(candidates, config, catalog)
        progress(ai_status)
        publish(ai_status, "AI ranking")
        if covers_stage:
            progress("Filling missing report covers...")
            try:
                matched, targets, errors = fill_missing_covers(conn, config, candidates, args.refresh)
                note = f"Exact cover fallback: {matched}/{targets} missing covers found"
                if errors:
                    note += f"; {errors} query errors"
                source_notes.append(note)
                progress(note)
            except Exception as exc:
                source_notes.append(f"Exact cover fallback error: {redact_secrets(exc)}")
                progress(f"Exact cover fallback failed: {exc}")
            publish(ai_status, "cover fallback")
        if mobilism_stage:
            progress("Mobilism: looking up release topics...")
            note = mobilism_links(conn, candidates, config, args.refresh)
            source_notes.append(note)
            progress(note)
            publish(ai_status, "Mobilism topics")
        candidates.sort(key=lambda item: (item.score, item.published_date), reverse=True)
        progress("Writing final HTML report...")
        publish(ai_status)

        checked_at = iso_now()
        for author in authors:
            conn.execute(
                "INSERT OR REPLACE INTO author_checks(author_key,author_name,checked_at) VALUES(?,?,?)",
                (normalize(author), author, checked_at),
            )
        for name in series_queries:
            conn.execute(
                "INSERT OR REPLACE INTO series_checks(series_key,series_name,checked_at) VALUES(?,?,?)",
                (normalize(name), name, checked_at),
            )
        conn.execute(
            "UPDATE runs SET completed_at=?,status='complete',report_path=?,notes=? WHERE id=?",
            (iso_now(), str(report), redact_secrets("; ".join(source_notes + [ai_status])), run_id),
        )
        conn.commit()
        visible = [item for item in candidates if not item.owned and item.decision != "dismiss" and item.score > -10]
        print(f"Catalog: {len(raw_books):,} books ({catalog_status})")
        print(f"Genre preferences: {', '.join(config.get('taste', {}).get('include', [])) or 'none'}")
        print(f"Authors checked: {', '.join(authors)}")
        print(
            f"Candidates: {len(candidates):,} screened; {sum(item.owned for item in candidates):,} already owned; "
            f"{len(visible):,} reportable; {sum(item.series_alert for item in visible)} series alerts"
        )
        print(ai_status)
        print(f"Report: {report}")
        for item in visible[:8]:
            marker = "SERIES" if item.series_alert else "AUTHOR" if item.matched_author else "DISCOVERY"
            print(f"  [{marker} {item.score:.0f}] {item.title} — {'; '.join(item.authors)} ({item.published_date or 'date unknown'})")
        if httpd is not None:
            progress(f"Report complete and still served at http://127.0.0.1:{httpd.server_address[1]}/ — Ctrl-C to stop")
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                progress("Stopped")
            finally:
                httpd.shutdown()
                httpd.server_close()
        return 0
    except Exception as exc:
        conn.execute("UPDATE runs SET completed_at=?,status='failed',notes=? WHERE id=?", (iso_now(), redact_secrets(exc), run_id))
        conn.commit()
        raise
    finally:
        conn.close()


def decide(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser().resolve())
    conn = connect_state(config)
    exists = conn.execute("SELECT 1 FROM candidate_history WHERE candidate_key=?", (args.candidate_id,)).fetchone()
    if not exists:
        print(f"Unknown candidate id: {args.candidate_id}", file=sys.stderr)
        return 2
    conn.execute(
        "INSERT OR REPLACE INTO decisions(candidate_key,status,updated_at) VALUES(?,?,?)",
        (args.candidate_id, args.status, iso_now()),
    )
    conn.commit()
    print(f"Recorded {args.status}: {args.candidate_id}")
    return 0


# --------------------------------------------------------------------------- #
# Download and Calibre import
# --------------------------------------------------------------------------- #

# Library Genesis mirrors that still answer. libgen.is/.rs/.st are dead; these two
# share one index and the same /index.php -> /ads.php -> /get.php download flow.
LIBGEN_MIRRORS = ("https://libgen.li", "https://libgen.vg")
# Libgen serves the search HTML only to a browser-looking client.
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
SIZE_UNITS = {"kb": 1_000, "mb": 1_000_000, "gb": 1_000_000_000}
# Preference order, best format for Calibre first. A row in no other format is skipped.
DEFAULT_FORMATS = ("epub", "azw3", "mobi", "fb2", "pdf")
# A download that is not what its extension claims is an error page, not a book.
FILE_SIGNATURES = {"epub": (b"PK",), "fb2": (b"<?xml", b"PK"), "pdf": (b"%PDF")}


def download_settings(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("download", {})
    formats = tuple(str(item).casefold().strip() for item in section.get("formats", DEFAULT_FORMATS) if str(item).strip())
    folder = Path(str(section.get("dir", "downloads"))).expanduser()
    if not folder.is_absolute():
        folder = Path(config["report_dir"]).parent / folder
    return {
        "formats": formats or DEFAULT_FORMATS,
        "max_bytes": int(float(section.get("max_mb", 200)) * SIZE_UNITS["mb"]),
        "min_title_match": float(section.get("min_title_match", 0.6)),
        "ai_assist": bool(section.get("ai_assist", True)),
        "dir": folder,
    }


def word_overlap(wanted: str, found: str) -> float:
    """Share of the wanted words present in the found text. The gate that keeps
    Library Genesis from answering one book with a different one."""
    words = {item for item in normalize_title(wanted).split() if len(item) > 2}
    if not words:
        return 0.0
    return len(words & set(normalize(found).split())) / len(words)


def title_similarity(left: str, right: str) -> float:
    left = ' '.join(sorted(set(normalize(left).split())))
    right = ' '.join(sorted(set(normalize(right).split())))
    return SequenceMatcher(None, left, right).ratio() if left and right else 0.0


class LibgenCellParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.title = [], []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.in_title = not self.title and 'edition.php?' in (dict(attrs).get('href') or '')

    def handle_endtag(self, tag):
        if tag == 'a':
            self.in_title = False

    def handle_data(self, data):
        self.parts.append(data)
        if self.in_title:
            self.title.append(data)


def libgen_cell_text(cell: str) -> str:
    parser = LibgenCellParser()
    parser.feed(cell)
    return ' '.join(' '.join(parser.title or parser.parts).split())


def save_download(folder: Path, name: str, body: bytes) -> Path:
    """Publish a complete download without overwriting a different existing file."""
    folder.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix='.download-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(body)
        target = folder / name
        index = 0
        while True:
            try:
                os.link(temporary, target)
                return target
            except FileExistsError:
                if target.is_file() and target.stat().st_size == len(body) and target.read_bytes() == body:
                    return target
                index += 1
                target = folder / f'{Path(name).stem}_{index}{Path(name).suffix}'
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parse_libgen_rows(page: str) -> list[dict[str, Any]]:
    rows = []
    for chunk in re.findall(r'<tr\b[^>]*>(.*?)</tr>', page, re.I | re.S):
        md5 = re.search(r'md5=([0-9a-f]{32})', chunk, re.I)
        raw = re.findall(r'<td\b[^>]*>(.*?)</td>', chunk, re.I | re.S)
        if not md5 or len(raw) < 2:
            continue
        cells = [libgen_cell_text(cell) for cell in raw]
        extension = next((cell.casefold() for cell in cells if cell.casefold() in DEFAULT_FORMATS), '')
        if not extension:
            continue
        size = next((match for cell in cells if (match := re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(kb|mb|gb)', cell, re.I))), None)
        # Some mirrors swap title/author; the edition link identifies the title.
        title_index = next((i for i in range(2) if 'edition.php' in raw[i]), 0)
        rows.append(dict(md5=md5.group(1).casefold(), title=cells[title_index], authors=cells[1-title_index],
                         named=cells[:2], publisher=cells[2] if len(cells) > 2 else '',
                         year=cells[3] if len(cells) > 3 else '',
                         language=cells[4] if len(cells) >= 9 else '', extension=extension,
                         isbns={value for value in (clean_isbn(item) for item in re.findall(r'\b\d{9}[\dXx]\b|\b97[89]\d{10}\b', html.unescape(raw[title_index]))) if value},
                         bytes=int(float(size.group(1).replace(',', '.')) * SIZE_UNITS[size.group(2).lower()]) if size else 10_000_000))
    return rows



def libgen_row_matches(candidate: Candidate, row: dict[str, Any], min_title_match: float) -> bool:
    if candidate.language.casefold().startswith('en') and row['language'] and not row['language'].casefold().startswith('en'):
        return False
    if candidate.isbns & row['isbns']:
        return True
    named = row.get('named', [row['title'], row['authors']])
    # Separate gates: a matching author must never carry the wrong title.
    title_ok = any(title_similarity(candidate.title, text) > 0.70
                   and word_overlap(candidate.title, text) >= min_title_match for text in named)
    author_ok = not candidate.authors or any(title_similarity(author, text) > 0.50
                                            for author in candidate.authors for text in named)
    return title_ok and author_ok



def rank_libgen_rows(candidate: Candidate, rows: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    ranked = []
    for row in rows:
        if row["extension"] not in settings["formats"] or row["bytes"] > settings["max_bytes"]:
            continue
        if not libgen_row_matches(candidate, row, settings["min_title_match"]):
            continue
        # Format preference first, then the smallest file: a 75 MB scan is a last resort.
        ranked.append((settings["formats"].index(row["extension"]), 0 if candidate.isbns & row["isbns"] else 1, row["bytes"] or 10 ** 9, row))
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked]


def http_bytes(url: str, *, timeout: int = 300, max_bytes: int = 200 * SIZE_UNITS["mb"]) -> bytes:
    request = Request(url, headers={"User-Agent": BROWSER_UA})
    with urlopen(request, timeout=timeout) as response:
        return response.read(max_bytes + 1)


def looks_like_book(body: bytes, extension: str, max_bytes: int) -> bool:
    if len(body) < 20_000 or len(body) > max_bytes:
        return False
    signatures = FILE_SIGNATURES.get(extension)
    return not signatures or body.lstrip()[:8].startswith(signatures)


def libgen_fetch(
    conn: sqlite3.Connection,
    candidate: Candidate,
    settings: dict[str, Any],
    cache_hours: int,
    refresh: bool = False,
    errors: list[str] | None = None,
) -> tuple[bytes, dict[str, Any]] | None:
    """Book file for a candidate. Returns (bytes, matched libgen row) or None.

    Search pages go through the shared HTTP cache; the /ads.php page never does,
    because its download key is minted per view and expires. Hosts that never
    answered are appended to `errors`, so None can be told apart from "searched
    every mirror and none of them had it".
    """
    queries = [f"{candidate.title} {candidate.authors[0]}" if candidate.authors else candidate.title, candidate.title]
    for host in LIBGEN_MIRRORS:
        for query in dict.fromkeys(queries):
            url = f"{host}/index.php?" + urlencode({"req": query, "topics[]": "l", "res": "25"})
            try:
                page = cached_request(conn, url, cache_hours, headers={"User-Agent": BROWSER_UA}, refresh=refresh, rate_seconds=3)
            except (RuntimeError, URLError, HTTPError, OSError) as exc:
                progress(f"  libgen search failed ({host}): {redact_secrets(exc)}")
                if errors is not None and not any(note.startswith(host) for note in errors):
                    errors.append(f"{host} did not answer ({redact_secrets(exc)})")
                continue
            for row in rank_libgen_rows(candidate, parse_libgen_rows(page), settings)[:3]:
                try:
                    ads = cached_request(conn, f"{host}/ads.php?md5={row['md5']}", 0, headers={"User-Agent": BROWSER_UA}, rate_seconds=3)
                    key = re.search(r"get\.php\?md5=([0-9a-fA-F]{32})&(?:amp;)?key=(\w+)", ads)
                    if not key:
                        continue
                    body = http_bytes(f"{host}/get.php?md5={key.group(1)}&key={key.group(2)}", max_bytes=settings["max_bytes"])
                except (RuntimeError, URLError, HTTPError, OSError) as exc:
                    progress(f"  libgen download failed ({row['md5'][:8]}): {redact_secrets(exc)}")
                    continue
                if looks_like_book(body, row["extension"], settings["max_bytes"]):
                    return body, row
                progress(f"  discarded {row['extension']} candidate {row['md5'][:8]}: not a {row['extension']} file")
    return None


# Mobilism fallback. The forum itself hosts nothing: a release topic lists mirrors on
# file hosts (mega4upload and friends, all the same XFileSharing script). Their free
# page is gated by a Cloudflare Turnstile widget, and no HTTP client gets past it —
# the widget only issues a token to a real, headed Chrome. Headless Chrome, bundled
# Chromium and a Playwright-launched browser all sit at "token len: 0" forever. What
# does work: launch the user's own Chrome with a remote-debugging port (so it carries
# no automation switches), park its window off-screen, attach over CDP and let the
# page solve itself. Only the resulting direct link comes back to Python, where
# http_bytes()/looks_like_book() verify it exactly like a libgen file.
CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
# Order-tolerant, like MOBILISM_TOPIC_RE: href sits on either side of the class.
MIRROR_LINK_RE = re.compile(r'<a\s(?=[^>]*class="postlink")[^>]*href="(https?://[^"]+)"')


def chrome_binary(configured: str = "") -> str:
    for path in (configured, shutil.which("chrome") or "", shutil.which("google-chrome") or "", *CHROME_PATHS):
        if path and Path(path).exists():
            return path
    raise RuntimeError("Google Chrome not found; set [mobilism] chrome to its path")


def mirror_links(page: str) -> list[str]:
    """File-host links in a release post. Posts link back into the forum (rules,
    uploader threads) with the same class, so those are dropped."""
    links = (html.unescape(link) for link in MIRROR_LINK_RE.findall(page))
    return list(dict.fromkeys(link for link in links if "mobilism" not in link.split("/")[2]))


def mirror_download_link(page: str) -> str:
    """Direct file URL out of an XFileSharing "download2" response, or ""."""
    match = re.search(r'href="(https?://[^"]+/d/[^"?]+)"', page)
    return html.unescape(match.group(1)) if match else ""


def mirror_direct_link(url: str, profile: Path, chrome: str = "", timeout: int = 240, headless: bool = False) -> str:
    """Walk one file host's free-download flow in a real Chrome; return the direct URL.

    Returns "" when the host does not follow the XFileSharing shape or the widget
    never issues a token. Raises only when Chrome or Playwright is missing.
    """
    from playwright.sync_api import sync_playwright  # optional: only the fallback needs it

    profile.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    arguments = [
        chrome_binary(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        # The mirrors are wallpapered in pop-under ads that steal or close the tab.
        "--block-new-web-contents",
        "--window-size=1280,900",
    ]
    # Headless is tried first because no window should appear on the user's desktop,
    # but Turnstile has never issued a token to one, hence the headed retry that
    # mobilism_fetch falls back to — parked off-screen rather than shown.
    arguments += ["--headless=new"] if headless else ["--window-position=-32000,-32000"]
    process = subprocess.Popen([*arguments, "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    try:
        with sync_playwright() as pw:
            browser = None
            while browser is None:
                try:
                    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                except Exception:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(1)
            page = browser.contexts[0].pages[0]
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)

            def ask(script: str) -> Any:
                """The free-download button navigates, and the ad scripts navigate on
                their own schedule, so any evaluate can land mid-navigation."""
                try:
                    return page.evaluate(script)
                except Exception:
                    return None

            # A DOM click, not a real one: the ad layers sit on top of the button and
            # eat pointer events, and the submit button's name/value still get posted.
            if ask("() => !!document.querySelector('[name=method_free]')"):
                ask("() => document.querySelector('[name=method_free]').click()")
                page.wait_for_load_state("domcontentloaded", timeout=60_000)
            for _ in range(30):
                if ask("() => !!document.forms.F1"):
                    break
                time.sleep(1)
            else:
                return ""
            token = ""
            while not token and time.monotonic() < deadline:
                token = ask("() => (document.querySelector('[name=cf-turnstile-response]')||{}).value || ''") or ""
                if not token:
                    time.sleep(2)
            if not token:
                return ""
            # The page's own 30s countdown; human_proof is filled in by its own script.
            while time.monotonic() < deadline:
                left = (ask("() => (document.getElementById('seconds')||{}).textContent || ''") or "").strip()
                if left in ("", "0", "00", "1"):
                    break
                time.sleep(2)
            # Posted from inside the page, not by submitting it: navigating hands the
            # tab to whichever ad script wins the race, and the answer is only HTML.
            answer = ask(
                """async () => {
                    const response = await fetch(location.href,
                        {method: 'POST', body: new FormData(document.forms.F1), credentials: 'include'});
                    return await response.text();
                }"""
            )
            return mirror_download_link(answer or "")
    finally:
        process.terminate()


def mobilism_fetch(
    conn: sqlite3.Connection, candidate: Candidate, config: dict[str, Any], settings: dict[str, Any], refresh: bool = False
) -> tuple[bytes, dict[str, Any]] | None:
    """Book file from a Mobilism release topic's mirrors, or None."""
    section = config.get("mobilism", {})
    if not section.get("enabled", False) or not section.get("download", False):
        return None
    # A candidate discovered on the forum already carries its topic; searching for it
    # again only risks matching a different one (or nothing).
    topic = next(
        (
            str(item.get("url"))
            for item in candidate.evidence
            if str(item.get("source", "")).startswith("Mobilism") and "viewtopic" in str(item.get("url", ""))
        ),
        "",
    )
    topic = topic or mobilism_topic(conn, candidate, config, refresh)
    if not topic:
        return None
    progress(f"  Mobilism topic {topic}")
    page = cached_request(
        conn,
        topic,
        int(section.get("cache_hours", 168)),
        headers={"User-Agent": BROWSER_UA},
        refresh=refresh,
        rate_seconds=3,
        opener=mobilism_session(config),
    )
    profile = Path(config["state_path"]).parent / "chrome_profile"
    topic_id = re.search(r"\bt=(\d+)", topic)
    chrome = str(section.get("chrome", ""))
    for mirror in mirror_links(page):
        host = mirror.split("/")[2]
        link = ""
        if section.get("headless_first", True):
            try:
                link = mirror_direct_link(
                    mirror, profile.with_name("chrome_profile_headless"), chrome,
                    timeout=int(section.get("headless_seconds", 45)), headless=True,
                )
            except Exception as exc:  # a headless attempt that dies just means retrying headed
                progress(f"  headless attempt failed ({host}): {redact_secrets(exc)}")
            if link:
                progress(f"  headless Chrome solved {host}")
        if not link:
            try:
                link = mirror_direct_link(mirror, profile, chrome)
            except Exception as exc:
                # A dead mirror, a missing Chrome or a stalled widget must not kill the run.
                progress(f"  mirror failed ({host}): {redact_secrets(exc)}")
                continue
        if not link:
            progress(f"  no free-download link from {host}")
            continue
        extension = unquote(link).rsplit(".", 1)[-1].casefold()
        if extension not in settings["formats"]:
            progress(f"  skipped {extension} from {host}: not a wanted format")
            continue
        try:
            body = http_bytes(link, max_bytes=settings["max_bytes"])
        except (URLError, HTTPError, OSError) as exc:
            progress(f"  mirror download failed ({host}): {redact_secrets(exc)}")
            continue
        if looks_like_book(body, extension, settings["max_bytes"]):
            return body, {"extension": extension, "source": f"mobilism:{topic_id.group(1) if topic_id else topic}"}
        progress(f"  discarded {extension} from {host}: not a {extension} file")
    return None


def safe_filename(value: str) -> str:
    return re.sub(r"[^\w \-.,'()\[\]&]+", "_", value).strip(". ")[:120] or "book"


def calibre_pubdate(value: str) -> str:
    """Calibre wants a real date; a bare year must not become December 31."""
    value = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return f"{value}-01"
    match = re.fullmatch(r"(\d{4})", value) or re.search(r"\b(19|20)\d{2}\b", value)
    return f"{match.group(0)}-01-01" if match else ""


def _cover_bytes(urls: list[str]) -> bytes | None:
    """First URL whose body is a real, large-enough JPEG/PNG."""
    for url in urls:
        try:
            body = http_bytes(url, timeout=60, max_bytes=20 * SIZE_UNITS["mb"])
        except (URLError, HTTPError, OSError):
            continue
        if len(body) > 2_000 and body[:4] in (b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb", b"\x89PNG"):
            return body
    return None


def fetch_cover(candidate: Candidate, config: dict[str, Any] | None = None, conn: Any = None) -> bytes | None:
    """Cover bytes for an import: the candidate's own URLs first, then a lookup.

    Mobilism-sourced candidates often carry no cover at all; with config+conn the
    same Google Books/Open Library gates the report uses find one. Only ever
    attached to the book being added now — nothing already in Calibre is touched."""
    body = _cover_bytes(candidate.cover_urls[:5])
    if body or config is None:
        return body
    match, _errors = cover_lookup(conn, config, candidate, use_isbn=True)
    if not match:
        return None
    candidate.merge(match)
    return _cover_bytes(match.cover_urls[:5])


def calibre_add_command(config: dict[str, Any], path: Path, candidate: Candidate, cover: Path | None) -> list[str]:
    lib = config["library"]
    command = [str(lib.get("calibredb", "calibredb")), "--with-library", str(lib["path"]), "add", str(path)]
    command += ["--title", candidate.title]
    if candidate.authors:
        command += ["--authors", " & ".join(candidate.authors)]  # Calibre's own author separator
    isbn = next((value for value in sorted(candidate.isbns, key=len, reverse=True)), "")
    if isbn:
        command += ["--isbn", isbn]
    if candidate.series:
        command += ["--series", candidate.series]
        if candidate.series_index is not None:
            command += ["--series-index", f"{candidate.series_index:g}"]
    tags = [tag for tag in [*sorted(candidate.subjects), *candidate.ai.get("genres", [])] if tag][:15]
    if tags:
        command += ["--tags", ",".join(tags)]
    if candidate.language:
        command += ["--languages", candidate.language]
    if cover:
        command += ["--cover", str(cover)]
    return command


def calibre_metadata_fields(candidate: Candidate) -> list[str]:
    """The fields `calibredb add` cannot set; applied afterwards with set_metadata."""
    fields = []
    if candidate.publisher:
        fields.append(f"publisher:{candidate.publisher}")
    pubdate = calibre_pubdate(candidate.published_date)
    if pubdate:
        fields.append(f"pubdate:{pubdate}")
    if candidate.description:
        fields.append(f"comments:{candidate.description[:8000]}")
    return fields


def run_calibredb(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        # calibre's own Python emits SyntaxWarning noise on stderr ("\d" is an
        # invalid escape sequence…) ahead of the real failure. Drop those lines so
        # the reported cause is the actual one.
        lines = [line for line in message.splitlines() if not re.search(r"(?:Syntax|Deprecation|User)Warning:|invalid escape sequence", line)]
        clean = "\n".join(lines).strip()
        raise RuntimeError((clean or message).strip()[-500:])
    return f"{result.stdout}\n{result.stderr}"


def add_to_calibre(config: dict[str, Any], path: Path, candidate: Candidate, cover: Path | None) -> int | None:
    """Add one file to Calibre with its metadata. Returns the new book id, or None
    when Calibre reports it as a duplicate of a book already in the library."""
    output = run_calibredb(calibre_add_command(config, path, candidate, cover))
    match = re.search(r"Added book ids?:\s*([0-9]+)", output)
    if not match:
        return None
    book_id = int(match.group(1))
    fields = calibre_metadata_fields(candidate)
    if fields:
        lib = config["library"]
        command = [str(lib.get("calibredb", "calibredb")), "--with-library", str(lib["path"]), "set_metadata", str(book_id)]
        for field_value in fields:
            command += ["--field", field_value]
        run_calibredb(command)
    return book_id


def candidate_from_payload(key: str, payload: dict[str, Any]) -> Candidate:
    # evidence carries the Mobilism topic the run already found for this book.
    return Candidate(
        title=str(payload.get("title") or ""),
        authors=list(payload.get("authors") or []),
        key=key,
        isbns={value for value in (clean_isbn(item) for item in payload.get("isbns") or []) if value},
        cover_urls=list(payload.get("cover_urls") or []),
        language=str(payload.get("language") or ""),
        series=str(payload.get("series") or ""),
        series_index=safe_float(payload.get("series_index")),
        published_date=str(payload.get("published_date") or ""),
        publisher=str(payload.get("publisher") or ""),
        description=str(payload.get("description") or ""),
        subjects=set(payload.get("subjects") or []),
        categories=[str(item) for item in (payload.get("categories") or []) if str(item).strip()],
        evidence=[item for item in (payload.get("evidence") or []) if isinstance(item, dict)],
    )


def download_candidate(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    settings: dict[str, Any],
    cache_hours: int,
    key: str,
    *,
    force: bool = False,
    refresh: bool = False,
    import_to_calibre: bool = True,
    log=progress,
) -> tuple[bool, str]:
    """Fetch one candidate and file it in Calibre. Returns (ok, message).

    The single path behind both the CLI and the report's Download button.
    """
    row = conn.execute("SELECT payload FROM candidate_history WHERE candidate_key=?", (key,)).fetchone()
    if not row:
        return False, f"Unknown candidate id: {key}"
    done = conn.execute("SELECT file_path,calibre_id,source,imported FROM downloads WHERE candidate_key=?", (key,)).fetchone()
    if done and not force and Path(done['file_path']).is_file() and (done['imported'] or not import_to_calibre):
        return True, f"already downloaded ({done['file_path']})"
    candidate = candidate_from_payload(key, json.loads(row["payload"]))
    settings["dir"].mkdir(parents=True, exist_ok=True)
    log(f"{key}: searching Library Genesis for {candidate.title} — {'; '.join(candidate.authors)}")
    libgen_errors: list[str] = []
    reuse = done and not force and Path(done['file_path']).is_file()
    found = ((Path(done['file_path']).read_bytes(), {'extension': Path(done['file_path']).suffix.lstrip('.'),
              'source': done['source']}) if reuse else
             libgen_fetch(conn, candidate, settings, cache_hours, refresh=refresh, errors=libgen_errors))
    if not found and settings.get("ai_assist", True):
        # The deterministic search matches the title it was given; a retitle or an
        # original-language edition is filed under a name only the AI knows about.
        for variant in ai_title_variants(candidate, config):
            log(f"  AI assist: retrying Library Genesis as “{variant}”")
            found = libgen_fetch(conn, replace(candidate, title=variant), settings, cache_hours, refresh=refresh)
            if found:
                break
    mobilism_note = " or Mobilism"
    if not found:
        try:
            found = mobilism_fetch(conn, candidate, config, settings, refresh=refresh)
        except Exception as exc:  # login refused, no Chrome, no Playwright: libgen-only behaviour
            mobilism_note = f"; Mobilism unavailable: {redact_secrets(exc)}"
            log(f"  Mobilism fallback skipped: {redact_secrets(exc)}")
    if not found:
        # Only name the sources that actually answered: a timed-out mirror was never
        # searched, and reporting it as "nothing found there" would be a lie.
        searched = [host for host in LIBGEN_MIRRORS if not any(note.startswith(host) for note in libgen_errors)]
        where = ", ".join(searched) or "no reachable Library Genesis mirror"
        detail = f"; {'; '.join(libgen_errors)}" if libgen_errors else ""
        return False, f"no usable copy found on {where}{mobilism_note}{detail}"
    body, source_row = found
    name = safe_filename(f"{'; '.join(candidate.authors) or 'Unknown'} - {candidate.title}")
    path = Path(done['file_path']) if reuse else save_download(settings['dir'], f"{name}.{source_row['extension']}", body)
    log(f"  saved {path} ({len(body) / SIZE_UNITS['mb']:.1f} MB, {source_row['extension']})")
    cover_bytes = fetch_cover(candidate, config, conn) if import_to_calibre else None
    if import_to_calibre and candidate.cover_urls:
        # The cover lookup may have found URLs the stored payload lacked; keep
        # them so later runs and re-imports never search for this cover again.
        persist_candidates(conn, [candidate])
    cover_path = None
    if cover_bytes:
        cover_path = settings["dir"] / f"{name}.cover"
        cover_path.write_bytes(cover_bytes)
    try:
        book_id = add_to_calibre(config, path, candidate, cover_path) if import_to_calibre else None
    except Exception as exc:
        if import_to_calibre:
            # The file itself is fine. Record it as a file-only save so the next
            # attempt reuses it instead of re-fetching, and only imports then.
            conn.execute(
                "INSERT OR REPLACE INTO downloads(candidate_key,added_at,calibre_id,file_path,source,imported) VALUES(?,?,?,?,?,?)",
                (key, iso_now(), None, str(path), source_row.get("source") or f"libgen:{source_row['md5']}", 0),
            )
            conn.commit()
        message = redact_secrets(exc)
        if "another calibre program" in message.lower():
            return False, (
                f"Calibre import failed: the Calibre program (or its content server) is running and holds the library. "
                f"Close it and Download again — the saved file will be reused, not re-fetched ({path.name})"
            )
        return False, f"Calibre import failed: {message}"
    finally:
        if cover_path:
            cover_path.unlink(missing_ok=True)
    conn.execute(
        "INSERT OR REPLACE INTO downloads(candidate_key,added_at,calibre_id,file_path,source,imported) VALUES(?,?,?,?,?,?)",
        (key, iso_now(), book_id, str(path), source_row.get("source") or f"libgen:{source_row['md5']}", int(import_to_calibre)),
    )
    conn.commit()
    if not import_to_calibre:
        return True, f"saved {path}; Calibre import not requested"
    if book_id is None:
        return True, "Calibre reports it as a duplicate; file kept, library untouched"
    return True, f"added to Calibre as book {book_id}, with metadata"


def download(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser().resolve())
    conn = connect_state(config)
    try:
        if args.list:
            entries = download_log(conn, limit=int(args.limit))
            for entry in entries:
                place = ("file saved; not imported" if not entry['imported'] else
                         f"Calibre #{entry['calibre_id']}" if entry["calibre_id"] else "file kept, Calibre had it")
                print(f"{entry['added_at'][:19].replace('T', ' ')}  {entry['title']}\n    {place} · {entry['file_path']} · {entry['source']}\n    {entry['key']}")
            print(f"{len(entries)} download{'' if len(entries) == 1 else 's'} on record")
            return 0
        keys = list(dict.fromkeys(args.candidate_id))
        if args.all_keeps:
            keys += [row["candidate_key"] for row in conn.execute("SELECT candidate_key FROM decisions WHERE status='keep'")]
            keys = list(dict.fromkeys(keys))
        if not keys:
            print("Nothing to download: pass a candidate id or --all-keeps", file=sys.stderr)
            return 2
        settings = download_settings(config)
        if args.no_ai:
            settings["ai_assist"] = False
        cache_hours = int(config.get("sources", {}).get("cache_hours", 24))
        failures = 0
        for key in keys:
            ok, message = download_candidate(conn, config, settings, cache_hours, key, force=args.force, refresh=args.refresh)
            progress(f"  {message}" if ok else f"{key}: {message}")
            failures += not ok
        return 1 if failures else 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Local report server (the Download buttons)
# --------------------------------------------------------------------------- #


def download_status(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row["candidate_key"]: (f"in Calibre as book {row['calibre_id']}" if row["calibre_id"] else "downloaded; Calibre had it already")
        for row in conn.execute("SELECT candidate_key,calibre_id FROM downloads WHERE imported=1")
    }


def candidate_title(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT payload FROM candidate_history WHERE candidate_key=?", (key,)).fetchone()
    try:
        payload = json.loads(row["payload"]) if row else {}
    except json.JSONDecodeError:
        payload = {}
    title = str(payload.get("title") or key)
    authors = "; ".join(payload.get("authors") or [])
    return f"{title} — {authors}" if authors else title


def pending_import_keys(conn: sqlite3.Connection) -> list[str]:
    """Saved-but-not-imported downloads whose file is still on disk."""
    return [
        row["candidate_key"]
        for row in conn.execute("SELECT candidate_key,file_path FROM downloads WHERE imported=0")
        if Path(row["file_path"]).is_file()
    ]


def retry_pending_imports(conn: sqlite3.Connection, config: dict[str, Any]) -> str:
    """Re-run the Calibre import for saved-but-not-imported downloads.

    download_candidate() reuses the saved file for these keys, so a retry is the
    import only — nothing is re-fetched. Stops at the first Calibre lock: every
    remaining row would fail identically, and closing the GUI fixes them all."""
    keys = pending_import_keys(conn)
    if not keys:
        return "no pending imports"
    settings = download_settings(config)
    cache_hours = int(config.get("sources", {}).get("cache_hours", 24))
    imported = 0
    failed: list[str] = []
    for key in keys:
        ok, message = download_candidate(conn, config, settings, cache_hours, key)
        if ok:
            imported += 1
        elif "Close it and Download again" in message:
            return f"Import retry: Calibre holds the library lock — {len(keys) - imported} saved file(s) still waiting. Close Calibre; they import on the next pass."
        else:
            failed.append(f"{candidate_title(conn, key)}: {message}")
    note = f"; {len(failed)} failed ({'; '.join(failed)})" if failed else ""
    return f"Import retry: {imported}/{len(keys)} added to Calibre{note}"


def import_retry_worker(config: dict[str, Any], stop: threading.Event, interval: int = 120) -> None:
    """Retry saved-but-not-imported downloads while the report is served: closing
    the Calibre GUI is enough, the next pass imports them."""
    while not stop.wait(interval):
        try:
            conn = connect_state(config)
            try:
                note = retry_pending_imports(conn, config)
            finally:
                conn.close()
            if note != "no pending imports":
                progress(note)
        except Exception as exc:  # noqa: BLE001 - a retry pass must never kill the server
            progress(f"Import retry failed: {redact_secrets(exc)}")


def download_log(conn: sqlite3.Connection, limit: int = 40) -> list[dict[str, Any]]:
    """Books already fetched, newest first: what `download --list` and the report's
    Downloads panel both report."""
    rows = conn.execute(
        "SELECT candidate_key,added_at,calibre_id,file_path,source,imported FROM downloads ORDER BY added_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "key": row["candidate_key"],
            "title": candidate_title(conn, row["candidate_key"]),
            "added_at": row["added_at"],
            "calibre_id": row["calibre_id"],
            "imported": bool(row['imported']),
            "file_path": row["file_path"],
            "source": row["source"],
        }
        for row in rows
    ]


def rerank_report(config: dict[str, Any], provider: str, model: str) -> tuple[bool, str]:
    """Re-run AI ranking over the latest report's candidates with a chosen provider/model."""
    report = Path(config["report_dir"]) / "latest.html"
    if not report.exists():
        return False, "No report to re-rank. Run: python book_watch.py run"
    source = report.read_text(encoding="utf-8")
    conn = connect_state(config)
    try:
        candidates = parse_report_cards(source, report_metadata_index(conn, config))
        raw_books, _ = load_calibre(config)
        catalog = build_catalog(raw_books)
        for candidate in candidates:
            candidate.owned = owned_in_catalog(candidate, catalog)
            # Undo the previous AI pass so scores are comparable across models again.
            if candidate.ai:
                candidate.score -= candidate.ai.get("fit_score", 0) / 5
                candidate.ai = {}
        load_decisions(conn, candidates)
        apply_cached_categories(conn, candidates, category_taxonomy(catalog))
        config = {**config, "_ai_provider": provider, "_ai_model": model}
        status = enrich_with_openrouter(candidates, config, catalog)
        persist_candidates(conn, candidates)
    finally:
        conn.close()
    ok = status.startswith("AI enriched")
    visible = [item for item in candidates if not item.owned and item.decision != "dismiss"]
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = Path(config["report_dir"]) / f"book-watch_{stamp}.html"
    render_report(visible, config, [], [], len(raw_books), "re-ranked from latest report", [], status, output_path=path, taxonomy=category_taxonomy(catalog))
    shutil.copy2(path, report)
    return ok, status


def report_server(config: dict[str, Any], token: str) -> type[http.server.BaseHTTPRequestHandler]:
    settings = download_settings(config)
    cache_hours = int(config.get("sources", {}).get("cache_hours", 24))
    report = Path(config["report_dir"]) / "latest.html"
    # ponytail: one download at a time. Two clicks would race the same libgen host
    # and the same Calibre database; per-candidate locking is not worth it here.
    lock = threading.Lock()
    active: dict[str, tuple[str, float]] = {}  # candidate key -> (queued|running, started)

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = f"BookWatch/{VERSION}"

        def log_message(self, fmt: str, *args: Any) -> None:
            progress(f"{self.address_string()} {fmt % args}")

        def reply(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def reply_json(self, status: int, payload: dict[str, Any]) -> None:
            self.reply(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def authorized(self) -> bool:
            if secrets.compare_digest(self.headers.get("X-Book-Watch-Token", ""), token):
                return True
            self.reply_json(403, {"ok": False, "message": "Bad or missing session token"})
            return False

        def do_GET(self) -> None:  # noqa: N802 - http.server's own naming
            route = self.path.split("?")[0]
            if route == "/downloads":
                if not self.authorized():
                    return
                conn = connect_state(config)
                try:
                    recent = download_log(conn)
                    # POST handlers mutate `active` from other server threads; the
                    # snapshot must not race them.
                    with lock:
                        running = [
                            {"key": key, "title": candidate_title(conn, key), "state": state, "seconds": int(time.time() - started)}
                            for key, (state, started) in sorted(active.items(), key=lambda item: item[1][1])
                        ]
                finally:
                    conn.close()
                self.reply_json(200, {"active": running, "recent": recent})
                return
            if route == "/models":
                if not self.authorized():
                    return
                name = dict(item.split("=", 1) for item in self.path.partition("?")[2].split("&") if "=" in item).get("provider", "")
                self.reply_json(200, ai_provider_models(config, unquote(name)))
                return
            if route not in ("/", "/index.html"):
                self.reply_json(404, {"ok": False, "message": "Not found"})
                return
            if not report.exists():
                self.reply(404, b"No report yet. Run: python book_watch.py run", "text/plain; charset=utf-8")
                return
            conn = connect_state(config)
            try:
                state = json.dumps(download_status(conn))
            finally:
                conn.close()
            # The token lives only in this response. A page on another origin can POST
            # to localhost but cannot read this HTML back, so it cannot learn the token.
            script = f"<script>window.BW_TOKEN={json.dumps(token)};window.BW_STATUS={state};window.BW_AI={json.dumps(ai_providers_status(config))};</script></head>"
            self.reply(200, report.read_text(encoding="utf-8").replace("</head>", script, 1).encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802 - http.server's own naming
            if self.path not in ("/download", "/rerank"):
                self.reply_json(404, {"ok": False, "message": "Not found"})
                return
            if not self.authorized():
                return
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                self.reply_json(400, {"ok": False, "message": "Expected a JSON body"})
                return
            if self.path == "/rerank":
                provider, model = str(payload.get("provider") or ""), str(payload.get("model") or "")
                if provider not in AI_PROVIDERS and provider != "openai_oauth":
                    self.reply_json(400, {"ok": False, "message": f"Unknown provider: {provider or '(none)'}"})
                    return
                with lock:
                    try:
                        ok, message = rerank_report(config, provider, model)
                    except Exception as exc:  # a click must never kill the server
                        ok, message = False, redact_secrets(exc)
                progress(f"rerank {provider}/{model}: {message}")
                self.reply_json(200, {"ok": ok, "message": message})
                return
            try:
                key = str(payload["key"])
            except KeyError:
                self.reply_json(400, {"ok": False, "message": "Expected {\"key\": \"work:…\"}"})
                return
            # Queued/running is tracked so /downloads can report the work in flight:
            # requests pile up behind the lock while one book is being fetched.
            active[key] = ("queued", time.time())
            try:
                with lock:
                    active[key] = ("running", time.time())
                    conn = connect_state(config)
                    try:
                        ok, message = download_candidate(conn, config, settings, cache_hours, key)
                    except Exception as exc:  # a click must never kill the server
                        ok, message = False, redact_secrets(exc)
                    finally:
                        conn.close()
            finally:
                active.pop(key, None)
            progress(f"{key}: {message}")
            self.reply_json(200, {"ok": ok, "message": message})

    return Handler


def report_httpd(config: dict[str, Any], port: int) -> http.server.ThreadingHTTPServer:
    """Loopback only: the server adds books to your Calibre library on request."""
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), report_server(config, secrets.token_urlsafe(24)))


def serve(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser().resolve())
    report = Path(config["report_dir"]) / "latest.html"
    if not report.exists():
        print(f"No report to serve at {report}. Run: python book_watch.py run", file=sys.stderr)
        return 2
    httpd = report_httpd(config, args.port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    progress(f"Serving {report} at {url} — Ctrl-C to stop")
    # A retry pass must never keep the report from being served.
    retry_conn = connect_state(config)
    try:
        retry_note = retry_pending_imports(retry_conn, config)
    except Exception as exc:  # noqa: BLE001 - serving matters more than the retry
        retry_note = f"Import retry failed: {redact_secrets(exc)}"
    finally:
        retry_conn.close()
    if retry_note != "no pending imports":
        progress(retry_note)
    threading.Thread(target=import_retry_worker, args=(config, threading.Event()), daemon=True, name="bw-import-retry").start()
    if not args.no_open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        progress("Stopped")
    finally:
        httpd.server_close()
    return 0


# One real row of a libgen search page (trimmed), so the parser is checked against
# the markup it actually meets instead of a hand-written idealisation of it.
LIBGEN_SAMPLE_ROW = """<tr>
<td><b><a href="series.php?id=410376">Children of Time   </a> &#8470;2</b><br><a href="edition.php?id=138145384">Children of Ruin <i></i></a><br><a href="edition.php?id=138145384"><i><font color="green"> 9780316452540</font></a></i>
<nobr><span class="badge badge-primary">b</span> <span class="badge badge-secondary">l 2368117</span></nobr></td>
<td><a href="author.php?id=357682">Tchaikovsky, Adrian(Author)</a></td>
<td>Orbit</td><td><nobr>2019</nobr></td><td>English</td><td>0 / 0</td>
<td><nobr><a href="/file.php?id=93359978">2 MB</a></nobr></td><td>epub</td>
<td><nobr><a href="/ads.php?md5=c217105dfe3b9e26116a494c228215c6"><span class="badge badge-primary">1</span></a></nobr></td></tr>"""

# Four real rows of one Mobilism search page. fid[]=106 reaches the whole eBooks
# category, so an audiobook and a companion volume come back alongside the book.
MOBILISM_SAMPLE_ROWS = """
<a href="./viewtopic.php?f=124&amp;t=6378969&amp;hilit=Dune" class="topictitle">Full-Cast Audiodrama: Dune by Frank Herbert (.MP3)</a>
<a href="./viewtopic.php?f=1293&amp;t=6118612&amp;hilit=Dune" class="topictitle">Dune: A Brief Guide by Frank Herbert (.ePUB)</a>
<a href="./viewtopic.php?f=1293&amp;t=5551029&amp;hilit=Dune" class="topictitle">Dune (Dune Chronicles 01) by Frank Herbert (.ePUB)+</a>
<a href="./viewtopic.php?f=311&amp;t=5652799&amp;hilit=Dune" class="topictitle">Dune Genesis by Frank Herbert (.CBR)</a>
"""

# Two real rows of a Mobilism search page: a bundle topic (the whole series, skipped)
# and a single volume. The dates are the topic's own and its last post's.
MOBILISM_SAMPLE_SEARCH = """
<a href="./viewtopic.php?f=1293&amp;t=4491684&amp;hilit=Backyard+Starship" class="topictitle">Backyard Starship Series by J.N. Chaney, Terry Maggert (.ePUB)</a>
<br /><i class="icon-user"></i> by <a href="./memberlist.php?mode=viewprofile&amp;u=605346">juandelacruz</a>
<i class="icon-time"></i> <small>Sep 18th, 2021, 3:16 pm</small> <i class="icon-comments"></i> in <a href="./viewforum.php?f=1293">eBooks</a>
</td><td class="center">13 Replies<br />30183 Views</td><td class="center"><small>Aug 2nd, 2026, 9:04 am</small></td>
<a href="./viewtopic.php?f=1293&amp;t=6301112&amp;hilit=Backyard+Starship" class="topictitle">Fist of Orion by J.N. Chaney, Terry Maggert (.ePUB)+</a>
<br /><i class="icon-time"></i> <small>Jul 29th, 2026, 11:02 am</small>
</td><td class="center">0 Replies</td><td class="center"><small>Jul 29th, 2026, 11:02 am</small></td>
<a href="./viewtopic.php?f=124&amp;t=6301999&amp;hilit=Backyard+Starship" class="topictitle">Gone Nova by J.N. Chaney, Terry Maggert (.M4B)</a>
<br /><i class="icon-time"></i> <small>Jul 30th, 2026, 8:00 am</small>
<a href="./viewtopic.php?f=1293&amp;t=6302222" class="topictitle">Bound by Trust by Y.V. Larson (.ePUB)</a>
<br /><i class="icon-time"></i> <small>May 22nd, 2026, 6:30 pm</small>
"""

# The opening post of a series topic, trimmed. It is kept current with the series, so
# its numbered list is what tells a run that book 34 exists when 33 is the newest owned.
MOBILISM_SAMPLE_SERIES_POST = """
<div class="content">Backyard Starship Series by J.N. Chaney, Terry Maggert (1, 6-10, 33-34)
Requirements: epub/azw3/mobi reader, 50.6 mb
Overview: J. N. Chaney is a USA Today Bestselling author.
Genre: Sci-Fi/Fantasy<br>
1. Backyard Starship 6. Distant Horizon 7. Kingdom Come 33. Escape Velocity - Long live the
soldiers! Engage the enemies of mankind and keep them far from Earth, whatever the cost.
34. An Ancient Light
Download Instructions: 1, 6-10 https://mega4upload.net/dp42lqtypji8
34. An Ancient Light https://upfilesgo.com/4FheZd</div>
"""

# A release post links its mirrors and the forum's own rules thread with the same class.
MOBILISM_SAMPLE_POST = """
<a class="postlink" href="https://mega4upload.net/wuw1dpzy0an3">https://mega4upload.net/wuw1dpzy0an3</a>
<a href="https://forum.mobilism.org/viewtopic.php?f=19&amp;t=649944" class="postlink">Rules</a>
"""
# The XFileSharing page that answers a solved widget, trimmed to the link it hands out.
MIRROR_SAMPLE_READY = (
    '<h2>File Download Link Generated</h2><a id="dl2btn" class="dl-bigbtn" rel="nofollow noopener"'
    ' href="https://s13.mega4down.com:183/d/4arfjlzriry6yxccd7errmairnum4abwybw/'
    'SUNDERED_%20Between%20Flame%20and%20Tid%20-%20Reese%20Sherron.epub">Download</a>'
)


def self_test() -> int:
    assert normalize("Charles E. Gannon") == normalize("Charles E Gannon")
    assert clean_isbn("978-0-123456-47-2") == "9780123456472"
    noisy = 'plugin says hi\n[{"id": 1}]Integration status: True'
    assert parse_noisy_json(noisy) == [{"id": 1}]
    title, series, index = parse_series("Children of Strife (Children of Time #4)")
    assert (title, series, index) == ("Children of Strife", "Children of Time", 4.0)
    # Release titles number the volume without a #, and a bare year is not a series.
    assert parse_series("Dune (Dune Chronicles 01)") == ("Dune", "Dune Chronicles", 1.0)
    assert parse_series("Fist of Orion (Backyard Starship, Book 34)") == ("Fist of Orion", "Backyard Starship", 34.0)
    assert parse_series("Some Novel (2026)") == ("Some Novel (2026)", "", None)
    parser = BlockParser()
    parser.feed("<h3>July 7</h3><p>The Seed (Nexus #6) — Rick Campbell (Publisher)<br>Synopsis.</p>")
    assert parser.blocks == ["July 7", "The Seed (Nexus #6) — Rick Campbell (Publisher)\nSynopsis."]
    match = REACTOR_BOOK_RE.search(parser.blocks[1])
    assert match and match.group("series") == "Nexus" and match.group("index") == "6"
    rows = parse_libgen_rows(LIBGEN_SAMPLE_ROW)
    assert len(rows) == 1 and rows[0]["extension"] == "epub" and rows[0]["bytes"] == 2_000_000
    assert rows[0]["md5"] == "c217105dfe3b9e26116a494c228215c6" and "9780316452540" in rows[0]["isbns"]
    wanted = Candidate("Children of Ruin", ["Adrian Tchaikovsky"], language="en")
    assert libgen_row_matches(wanted, rows[0], 0.6)
    assert not libgen_row_matches(Candidate("Children of Dune", ["Frank Herbert"]), rows[0], 0.6)
    formats = set(DEFAULT_FORMATS)
    dune = Candidate("Dune", ["Frank Herbert"])
    # The .MP3 and .CBR rows lose on format, the companion guide loses on reverse overlap.
    assert pick_mobilism_topic(MOBILISM_SAMPLE_ROWS, dune, formats, 0.6).endswith("viewtopic.php?f=1293&t=5551029")
    assert not pick_mobilism_topic(MOBILISM_SAMPLE_ROWS, Candidate("Dune", ["Kevin J. Anderson"]), formats, 0.6)
    assert not pick_mobilism_topic(MOBILISM_SAMPLE_ROWS, Candidate("Elantris", ["Frank Herbert"]), formats, 0.6)
    releases = parse_mobilism_release(MOBILISM_SAMPLE_SEARCH, set(DEFAULT_FORMATS))
    # The .M4B row is an audiobook; the bundle row is flagged, not dropped here.
    assert [item["title"] for item in releases] == ["Backyard Starship Series", "Fist of Orion", "Bound by Trust"]
    assert releases[0]["bundle"] and not releases[1]["bundle"]
    assert releases[0]["published_date"] == "2026-08-02" and releases[1]["published_date"] == "2026-07-29"
    assert releases[1]["authors"] == ["J.N. Chaney", "Terry Maggert"]
    assert releases[2]["authors"] == ["Y.V. Larson"]
    # "by" inside a title must not be read as an initial of the author it precedes.
    assert author_matches("J. N. Chaney", ["J.N. Chaney", "Terry Maggert"])
    assert not author_matches("B. V. Larson", releases[2]["authors"])
    assert not author_matches("R. G. Roberts", ["Nora Roberts"])
    volumes = parse_mobilism_volumes(MOBILISM_SAMPLE_SERIES_POST)
    assert volumes[0] == (1.0, "Backyard Starship") and volumes[-1] == (34.0, "An Ancient Light")
    assert len(volumes) == 5 and all(not title.startswith("http") for _, title in volumes)
    # A blurb that runs on from the title with no dash must not become the title.
    assert parse_mobilism_volumes("Genre: SF 11. The Eleventh Artifact The crew of the Stardust III must investigate a strange "
                                  "planet detected far beyond the rim of explored space, and what they find there changes "
                                  "everything they believed.") == [(11.0, "The Eleventh Artifact The crew of the Stardust III must")]
    assert mirror_links(MOBILISM_SAMPLE_POST) == ["https://mega4upload.net/wuw1dpzy0an3"]
    assert mirror_download_link(MIRROR_SAMPLE_READY).endswith("Reese%20Sherron.epub")
    assert not mirror_download_link('<a href="https://mega4upload.net/wuw1dpzy0an3">retry</a>')
    assert calibre_pubdate("2019") == "2019-01-01" and calibre_pubdate("2019-06") == "2019-06-01"
    assert not looks_like_book(b"<html>error</html>" * 2000, "epub", 10 ** 9)
    print("self-test: ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recommend new books from a Calibre library and genre preferences.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.toml")
    sub = parser.add_subparsers(dest="command")
    missing = sub.add_parser('import-missing', help='Import a missing-book CSV/XLSX into the shared candidate history')
    missing.add_argument('input_file', type=Path)
    atlas = sub.add_parser('export-atlas', help='Export Calibre metadata and summaries to Shelfscape CSV (read-only)')
    atlas.add_argument('--output', required=True, type=Path)
    atlas.add_argument('--summary-column', default='#summary')
    sub.add_parser('lookup', help='Anna lookup; use lookup --help for its options', add_help=False)
    run = sub.add_parser("run", help="Build a report")
    run.add_argument("--author", action="append", help="Focus on an owned author; repeatable")
    run.add_argument("--series", action="append", help="Focus on an owned series; repeatable")
    run.add_argument('--like', type=int, help='Discover releases related to this owned Calibre book ID')
    run.add_argument("-g", "--genre", action="append", help="Steer discovery and AI ranking toward a genre; repeatable")
    run.add_argument("--max-authors", type=int, help="Override the rotating author count")
    run.add_argument("--max-series", type=int, help="Override the rotating owned-series count")
    run.add_argument("--focused", action="store_true", help="Skip general subject API queries")
    run.add_argument("--no-ai", action="store_true")
    run.add_argument("--ai", choices=tuple(AI_PROVIDERS) + ("openai_oauth", "none"), help="AI provider for ranking (default: config, else commandcode)")
    run.add_argument("--no-network", action="store_true")
    run.add_argument("--refresh", action="store_true", help="Ignore the HTTP cache")
    run.add_argument("--port", type=int, default=8787, help="Loopback port for the served report (default 8787; 0 picks a free one)")
    run.add_argument("--no-serve", action="store_true", help="Only write the report; do not serve it or wait")
    run.add_argument("--no-open", action="store_true", help="Do not open a browser tab")
    decision = sub.add_parser("decide", help="Keep or dismiss a candidate")
    decision.add_argument("candidate_id")
    decision.add_argument("status", choices=("keep", "dismiss"))
    fetch = sub.add_parser("download", help="Download candidates and add them to Calibre with their metadata")
    fetch.add_argument("candidate_id", nargs="*", help="Candidate ids from the report; repeatable")
    fetch.add_argument("--all-keeps", action="store_true", help="Download every candidate marked keep")
    fetch.add_argument("--force", action="store_true", help="Download again even if it was downloaded before")
    fetch.add_argument("--refresh", action="store_true", help="Ignore the cached Library Genesis search pages")
    fetch.add_argument("--no-ai", action="store_true", help="Do not ask the AI for alternative titles when nothing is found")
    fetch.add_argument("--list", action="store_true", help="List downloads already on record instead of fetching")
    fetch.add_argument("--limit", type=int, default=40, help="How many records --list shows (default 40)")
    server = sub.add_parser("serve", help="Open the latest report with working Download buttons")
    server.add_argument("--port", type=int, default=8787, help="Loopback port (default 8787; 0 picks a free one)")
    server.add_argument("--no-open", action="store_true", help="Do not open a browser tab")
    sub.add_parser("retry-imports", help="Re-run the Calibre import for saved-but-not-imported downloads")
    sub.add_parser("update-reports", help="Regenerate existing reports with the current layout")
    sub.add_parser("self-test", help="Run fast built-in checks")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    args, unknown = parser.parse_known_args(argv)
    if args.command == 'lookup':
        from finder import main as lookup
        return lookup(['--config', args.config, *unknown]) or 0
    if unknown:
        parser.error('unrecognized arguments: ' + ' '.join(unknown))
    if args.command in ('import-missing', 'export-atlas'):
        import library_exchange
        config = load_config(Path(args.config).expanduser().resolve())
        if args.command == 'import-missing':
            candidates = library_exchange.import_missing(config, args.input_file)
            for candidate in candidates:
                print(f'{candidate.key}  {candidate.title} — {"; ".join(candidate.authors)}')
            print(f'{len(candidates)} candidates imported; run a report or download by candidate id.')
        else:
            count, summarized = library_exchange.export_atlas(config, args.output, args.summary_column)
            print(f'Exported {count} books ({summarized} with summaries) to {args.output}')
        return 0
    if args.command in (None, "run"):
        if args.command is None:
            args = parser.parse_args(["run"])
        return run_report(args)
    if args.command == "retry-imports":
        config = load_config(Path(args.config).expanduser().resolve())
        conn = connect_state(config)
        try:
            print(retry_pending_imports(conn, config))
        except Exception as exc:  # noqa: BLE001 - report the failure, keep the exit clean
            print(f"Import retry failed: {redact_secrets(exc)}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        return 0
    if args.command == "decide":
        return decide(args)
    if args.command == "download":
        return download(args)
    if args.command == "serve":
        return serve(args)
    if args.command == "update-reports":
        return update_reports(args)
    if args.command == "self-test":
        return self_test()
    return 2


if __name__ == "__main__":
    sys.modules.setdefault('book_watch', sys.modules[__name__])
    raise SystemExit(main())

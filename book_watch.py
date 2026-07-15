from __future__ import annotations

import argparse
import calendar
import hashlib
import html
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen


VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.toml"
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
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="seconds")


def progress(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


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
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(STATE_SCHEMA)
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
        for author in authors:
            key = normalize(author)
            if not key:
                continue
            author_counts.setdefault(key, [author, 0])[1] += 1
            author_titles.setdefault(key, set()).add(title_key)
            signatures.add((title_key, key))
        if series_name:
            key = normalize(series_name)
            entry = series.setdefault(key, {"name": series_name, "max_index": 0.0, "titles": set(), "authors": set()})
            entry["titles"].add(title_key)
            entry["authors"].update(authors)
            if series_index is not None:
                entry["max_index"] = max(entry["max_index"], series_index)
    return {
        "books": books,
        "isbns": isbns,
        "signatures": signatures,
        "author_counts": author_counts,
        "author_titles": author_titles,
        "series": series,
    }


_last_request: dict[str, float] = {}


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
    wait = rate_seconds - (time.monotonic() - _last_request.get(host_key, 0))
    if wait > 0:
        time.sleep(wait)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"User-Agent": f"CalibreBookWatch/{VERSION} (personal library report)"}
    request_headers.update(headers or {})
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers, method=method)
    status = 599
    response_text = ""
    try:
        with urlopen(request, timeout=90) as response:
            status = response.status
            response_text = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        status = exc.code
        response_text = exc.read().decode("utf-8", errors="replace")
    finally:
        _last_request[host_key] = time.monotonic()
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
    results: list[Candidate] = []
    errors: list[str] = []
    cache_hours = int(config["sources"].get("cache_hours", 24))
    key = os.getenv(config.get("google_books", {}).get("api_key_env", "GOOGLE_BOOKS_API_KEY"), "")
    for label, query in queries:
        params = {"q": query, "orderBy": "newest", "maxResults": 40, "printType": "books", "langRestrict": "en"}
        if key:
            params["key"] = key
        url = "https://www.googleapis.com/books/v1/volumes?" + urlencode(params)
        try:
            data = json.loads(cached_request(conn, url, cache_hours, refresh=refresh))
        except RuntimeError as exc:
            if not key:
                errors.append(f"Google Books {label} error: {redact_secrets(exc)}")
                continue
            params.pop("key")
            url = "https://www.googleapis.com/books/v1/volumes?" + urlencode(params)
            try:
                data = json.loads(cached_request(conn, url, cache_hours, refresh=refresh))
            except (RuntimeError, URLError, TimeoutError, json.JSONDecodeError) as retry_exc:
                errors.append(f"Google Books {label} error: {redact_secrets(retry_exc)}")
                continue
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            errors.append(f"Google Books {label} error: {redact_secrets(exc)}")
            continue
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
            candidate = Candidate(
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
            results.append(candidate)
    return results, errors


def fill_missing_covers(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    candidates: list[Candidate],
    refresh: bool,
) -> tuple[int, int, int]:
    targets = [
        candidate
        for candidate in candidates
        if not candidate.cover_urls
        and not candidate.isbns
        and not candidate.owned
        and candidate.decision != "dismiss"
        and candidate.score > -10
        and (published := parse_dateish(candidate.published_date)) is not None
        and published <= date.today()
    ]
    matched = errors = 0
    for candidate in targets:
        title = candidate.title.replace('"', " ")
        author = candidate.authors[0].replace('"', " ")
        author_keys = {normalize(value) for value in candidate.authors}
        match = None
        if config.get("sources", {}).get("google_books", True):
            found, failures = google_candidates(
                conn,
                config,
                [(f"cover: {candidate.title}", f'intitle:"{title}" inauthor:"{author}"')],
                refresh,
            )
            errors += len(failures)
            match = next(
                (
                    item
                    for item in found
                    if item.cover_urls
                    and normalize_title(item.title) == normalize_title(candidate.title)
                    and author_keys & {normalize(value) for value in item.authors}
                ),
                None,
            )
        if not match and config.get("sources", {}).get("open_library", True):
            try:
                found = open_library_candidates(
                    conn,
                    config,
                    [(f"cover: {candidate.title}", "title", candidate.title)],
                    refresh,
                )
            except (RuntimeError, URLError, TimeoutError, json.JSONDecodeError):
                found = []
                errors += 1
            match = next(
                (
                    item
                    for item in found
                    if item.cover_urls
                    and normalize_title(item.title) == normalize_title(candidate.title)
                    and author_keys & {normalize(value) for value in item.authors}
                ),
                None,
            )
        if match:
            candidate.merge(match)
            matched += 1
    return matched, len(targets), errors


def open_library_candidates(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    queries: list[tuple[str, str, str]],
    refresh: bool,
) -> list[Candidate]:
    results: list[Candidate] = []
    cache_hours = int(config["sources"].get("cache_hours", 24))
    contact = str(config.get("open_library", {}).get("contact_email") or "").strip()
    headers = {"User-Agent": f"CalibreBookWatch/{VERSION} ({contact})"} if contact else None
    fields = "key,title,subtitle,author_name,first_publish_year,publish_date,isbn,cover_i,subject,series,publisher,language"
    for label, field_name, query in queries:
        params = {field_name: query, "language": "eng", "lang": "en", "sort": "new", "limit": 40, "fields": fields}
        url = "https://openlibrary.org/search.json?" + urlencode(params)
        data = json.loads(cached_request(conn, url, cache_hours, headers=headers, refresh=refresh, rate_seconds=0.36 if contact else 1.05))
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
            results.append(
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


def match_and_score(candidates: list[Candidate], catalog: dict[str, Any], config: dict[str, Any]) -> None:
    taste = [normalize(value) for value in config.get("taste", {}).get("include", [])]
    excluded = [normalize(value) for value in config.get("taste", {}).get("exclude", [])]
    watch = config.get("watch", {})
    completed = {normalize(value) for value in watch.get("complete_series", [])}
    ignored = {normalize(value) for value in watch.get("ignore_series", [])}
    past = date.today() - timedelta(days=int(config["run"].get("past_days", 550)))
    future = date.today() + timedelta(days=int(config["run"].get("future_days", 0)))
    for candidate in candidates:
        title_key = normalize_title(candidate.title)
        author_keys = [normalize(author) for author in candidate.authors]
        candidate.owned = bool(candidate.isbns & catalog["isbns"]) or any(
            (title_key, author_key) in catalog["signatures"] for author_key in author_keys
        )
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
        combined = normalize(" ".join([candidate.title, candidate.series, candidate.description, " ".join(candidate.subjects)]))
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
        taste_hits = [term for term in taste if term and term in combined]
        if taste_hits:
            candidate.score += min(30, 10 + 5 * len(taste_hits))
            candidate.reasons.append("Matches: " + ", ".join(taste_hits[:4]))
        if any(term and term in combined for term in excluded):
            candidate.score -= 25
            candidate.reasons.append("Matches an excluded preference")
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


def load_decisions(conn: sqlite3.Connection, candidates: list[Candidate]) -> None:
    decisions = {row["candidate_key"]: row["status"] for row in conn.execute("SELECT candidate_key,status FROM decisions")}
    for candidate in candidates:
        candidate.decision = decisions.get(candidate.key, "")


def persist_candidates(conn: sqlite3.Connection, candidates: list[Candidate]) -> None:
    now = iso_now()
    for candidate in candidates:
        payload = json.dumps(
            {
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
                "subjects": sorted(candidate.subjects),
                "evidence": candidate.evidence,
            },
            ensure_ascii=False,
        )
        conn.execute(
            """
            INSERT INTO candidate_history(candidate_key,title,authors,first_seen,last_seen,payload)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(candidate_key) DO UPDATE SET title=excluded.title,authors=excluded.authors,last_seen=excluded.last_seen,payload=excluded.payload
            """,
            (candidate.key, candidate.title, "; ".join(candidate.authors), now, now, payload),
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


def enrich_with_openrouter(candidates: list[Candidate], config: dict[str, Any], catalog: dict[str, Any] | None = None) -> str:
    ai_cfg = config.get("openrouter", {})
    if not ai_cfg.get("enabled", True):
        return "AI disabled in configuration"
    env_file = str(ai_cfg.get("env_file") or "").strip()
    if env_file:
        load_env_file(env_file)
    key = os.getenv(str(ai_cfg.get("api_key_env", "OPENROUTER_API_KEY")), "")
    if not key:
        return "AI skipped: OPENROUTER_API_KEY is not configured"
    # ponytail: cap slow free-model enrichment; raise only if the top 60 omit useful author matches.
    selected = sorted(
        [item for item in candidates if not item.owned and item.decision != "dismiss" and item.score > 0],
        key=lambda item: (bool(item.series_alert or item.matched_author), item.score, item.published_date),
        reverse=True,
    )[:60]
    if not selected:
        return "AI skipped: no eligible candidates"
    taste = config.get("taste", {})
    base_url = str(ai_cfg.get("base_url", "https://openrouter.ai/api/v1")).rstrip("/")
    models = list(dict.fromkeys(str(value) for value in (ai_cfg.get("model"), ai_cfg.get("fallback_model")) if value))
    applied_total = 0
    used_models: set[str] = set()
    failed_batches = 0
    last_error = ""
    for start in range(0, len(selected), 12):
        batch = selected[start : start + 12]
        progress(f"OpenRouter: enriching {start + 1}-{start + len(batch)} of {len(selected)}...")
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
            "You curate a private speculative-fiction release report. Classify and rank only the supplied candidates. "
            "The input list is authoritative: return exactly one entry per input with the same rank, id, and title, "
            "and never introduce another book. Do not invent publication facts or series membership. Return JSON only with this shape: "
            '{"items":[{"rank":0,"id":"work:...","title":"...","fit_score":0,"genres":["..."],"why":"one concise sentence","concerns":["..."],"owned_title":null}]}. '
            "fit_score must be an integer from 0 to 100. Concerns should flag weak metadata, likely reissues, or genre uncertainty. "
            "Set owned_title to an exact value from that candidate's owned_titles only when it is the same underlying work under a translation, retitle, or reissue; otherwise use null.\n\n"
            f"Taste profile: {json.dumps(taste, ensure_ascii=False)}\n\n"
            f"Candidates: {json.dumps(items, ensure_ascii=False)}"
        )
        for model in [item for item in models if item and item != "None"]:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 2200,
            }
            request = Request(
                base_url + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://localhost/calibre-book-watch",
                    "X-Title": "Calibre Book Watch",
                },
                method="POST",
            )
            try:
                data = open_json_with_deadline(request, int(ai_cfg.get("timeout_seconds", 120)))
                content = data["choices"][0]["message"]["content"]
                parsed = parse_json_response(content)
                results = parsed if isinstance(parsed, list) else next(
                    (parsed.get(key) for key in ("items", "candidates", "recommendations", "results", "books") if isinstance(parsed.get(key), list)),
                    [],
                )
                applied = apply_ai_results(results, batch, str(data.get("model", model)), owned_titles)
                if applied:
                    applied_total += applied
                    used_models.add(str(data.get("model", model)))
                    break
                last_error = f"{model}: response contained no verifiable candidates"
            except (HTTPError, URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
                if isinstance(exc, HTTPError):
                    try:
                        detail = exc.read().decode("utf-8", errors="replace")[:300]
                    except Exception:
                        detail = ""
                    last_error = f"{model}: HTTP {exc.code} {detail}"
                else:
                    last_error = f"{model}: {exc}"
        else:
            failed_batches += 1
    if applied_total:
        model_note = ", ".join(sorted(used_models))
        failure_note = f"; {failed_batches} batches failed" if failed_batches else ""
        return f"AI enriched {applied_total}/{len(selected)} candidates with {model_note}{failure_note}"
    return "AI unavailable; deterministic report produced. " + last_error


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
        ]
    )
    return " · ".join(links)


def candidate_card(candidate: Candidate, kind: str) -> str:
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
            [candidate.title, *candidate.authors, candidate.series, candidate.publisher, *candidate.subjects, *candidate.ai.get("genres", [])]
        ).casefold(),
        quote=True,
    )
    title = html.escape(candidate.title)
    authors = html.escape("; ".join(candidate.authors))
    score = f'{candidate.score:.0f}'
    score_badge = f'<span class="score" title="Relevance score; higher is a stronger match" aria-label="Relevance score {score}">{score}</span>'
    return f"""
    <article class="book-card" data-kind="{kind}" data-search="{search}">
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
) -> Path:
    report_day = report_day or date.today()
    visible = [
        item
        for item in candidates
        if not item.owned
        and item.decision != "dismiss"
        and item.score > -10
        and (published := parse_dateish(item.published_date)) is not None
        and published <= report_day
    ]
    owned_suppressed = sum(item.owned for item in candidates) if owned_suppressed_count is None else owned_suppressed_count
    screened = len(candidates) if screened_count is None else screened_count
    visible.sort(key=lambda item: (item.decision == "keep", item.score, item.published_date), reverse=True)
    series = [item for item in visible if item.series_alert]
    authors_section = [item for item in visible if item.matched_author and not item.series_alert]
    discovery = [item for item in visible if not item.matched_author and not item.series_alert]

    def section(title: str, items: list[Candidate], empty: str, kind: str) -> str:
        cards = "".join(candidate_card(item, kind) for item in items)
        content = f'<div class="gallery">{cards}</div>' if cards else f'<p class="empty">{html.escape(empty)}</p>'
        return f'<section class="book-section"><h2>{html.escape(title)} <small>{len(items)}</small></h2>{content}</section>'

    report_dir = Path(config["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = output_path or report_dir / f"book-watch_{stamp}.html"
    notes = "".join(f"<li>{html.escape(redact_secrets(note))}</li>" for note in source_notes)
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calibre Book Watch — {report_day.isoformat()}</title>
<style>
:root{{--bg:#f2f0ea;--paper:#fff;--ink:#18201d;--muted:#64706b;--accent:#176b5b;--accent-soft:#e4f1ed;--line:#d8ddd8;--warn:#98502e}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,sans-serif}}
main{{max-width:1440px;margin:auto;padding:36px 24px 80px}} header{{border-bottom:1px solid var(--line);padding-bottom:22px;margin-bottom:24px}}
h1{{font:800 clamp(2.4rem,6vw,5.5rem)/.95 Georgia,serif;letter-spacing:-.04em;margin:.12em 0}} h2{{font:700 1.4rem Georgia,serif;border-bottom:1px solid var(--line);padding-bottom:9px;margin:42px 0 18px}}
h2 small{{color:var(--muted);font:500 .85rem system-ui}} button,input{{font:inherit}} button{{cursor:pointer}}
.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:20px 0}} .summary div{{background:var(--paper);border:1px solid var(--line);padding:12px;border-radius:10px}}
.summary strong{{display:block;font-size:1.45rem}} .filters{{position:sticky;top:0;z-index:5;display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:28px 0;padding:12px;background:#f2f0eaea;backdrop-filter:blur(12px);border:1px solid var(--line);border-radius:14px}}
.filters label{{flex:1;min-width:230px}} .filters input{{width:100%;border:1px solid var(--line);border-radius:10px;background:var(--paper);padding:10px 13px;color:var(--ink)}} .filter-buttons{{display:flex;gap:6px;flex-wrap:wrap}}
.filter{{border:1px solid var(--line);border-radius:99px;background:var(--paper);padding:8px 11px;color:var(--ink)}} .filter[aria-pressed="true"]{{border-color:var(--accent);background:var(--accent);color:#fff}} #result-count{{margin-left:auto;color:var(--muted);font-size:.9rem}}
.gallery{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:22px}} .book-card{{min-width:0;height:460px}}
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
dialog{{width:min(920px,calc(100% - 28px));height:min(700px,90vh);overflow:hidden;padding:0;border:0;border-radius:18px;background:var(--paper);color:var(--ink);box-shadow:0 30px 90px #0006}} dialog::backdrop{{background:#13201caa;backdrop-filter:blur(3px)}} .close{{position:absolute;right:14px;top:14px;z-index:2;width:42px;height:42px;border:0;border-radius:99px;background:#fff;color:var(--ink);box-shadow:0 2px 12px #0003;font-size:1.5rem}}
#modal-content,.modal-book{{height:100%}} .modal-book{{display:grid;grid-template-columns:minmax(240px,38%) 1fr;min-height:0}} .modal-cover{{background:#d9e5df}} .modal-cover .cover-frame{{height:100%;aspect-ratio:auto}} .modal-copy{{position:relative;padding:48px 42px 38px;overflow:auto}} .modal-copy h2{{padding:0 55px 0 0;margin:0 0 5px;border:0;font-size:2rem}} .modal-description{{max-height:180px;overflow-y:auto;padding-right:8px;overscroll-behavior:contain}} .modal-score{{top:48px;right:42px}}
details{{margin-top:44px;background:var(--paper);border:1px solid var(--line);padding:14px;border-radius:10px}} .sr-only{{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}}
@media (max-width:650px){{main{{padding:24px 14px 60px}} .gallery{{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}} .book-card{{height:390px}} .filters{{position:static}} #result-count{{width:100%;margin:0}} .modal-book{{grid-template-columns:1fr;grid-template-rows:42% minmax(0,1fr)}} .modal-copy{{padding:28px 22px}} .modal-score{{top:28px;right:22px}}}}
</style></head><body><main>
<header><div class="muted">Daily release intelligence · {report_day.isoformat()}</div><h1>Calibre Book Watch</h1><p>Read-only comparison of external release data with your Calibre catalog.</p></header>
<p class="muted">Card badge = relevance score (higher is a stronger match), not a list index.</p>
<div class="summary"><div><strong>{catalog_count:,}</strong>Calibre books</div><div><strong>{screened:,}</strong>external records screened</div><div><strong>{owned_suppressed:,}</strong>already-owned records suppressed</div><div><strong>{len(visible):,}</strong>report items</div><div><strong>{len(series):,}</strong>series alerts</div></div>
<p><strong>Authors queried:</strong> {html.escape(', '.join(authors) or 'none')}<br><strong>Series queried:</strong> {html.escape(', '.join(series_queries) or 'none')}</p>
<div class="filters"><label><span class="sr-only">Search books</span><input id="book-search" type="search" placeholder="Search title, author, series, genre…"></label>
<div class="filter-buttons" role="group" aria-label="Book category"><button class="filter" type="button" data-filter="all" aria-pressed="true">All {len(visible)}</button><button class="filter" type="button" data-filter="series" aria-pressed="false">Series {len(series)}</button><button class="filter" type="button" data-filter="author" aria-pressed="false">Authors {len(authors_section)}</button><button class="filter" type="button" data-filter="discovery" aria-pressed="false">Discovery {len(discovery)}</button></div><strong id="result-count" aria-live="polite">{len(visible)} books</strong></div>
{section('Series continuations', series, 'No unowned series continuation was confidently identified in this run.', 'series')}
{section('New releases by authors in your library', authors_section, 'No new books by checked authors were found.', 'author')}
{section('General discovery', discovery, 'No discovery candidates crossed the report threshold.', 'discovery')}
<details><summary>Run details and source health</summary><p>{html.escape(catalog_status)}</p><p>{html.escape(ai_status)}</p><ul>{notes}</ul>
<p>Record feedback with <code>python book_watch.py decide &lt;candidate-id&gt; keep|dismiss</code>.</p></details>
<dialog id="book-dialog" aria-label="Book details"><button class="close" type="button" aria-label="Close">×</button><div id="modal-content"></div></dialog>
<script>
const search = document.querySelector("#book-search");
const cards = [...document.querySelectorAll(".book-card")];
const count = document.querySelector("#result-count");
let kind = "all";
function applyFilters() {{
  const query = search.value.trim().toLocaleLowerCase();
  let shown = 0;
  for (const card of cards) {{
    const visible = (kind === "all" || card.dataset.kind === kind) && card.dataset.search.includes(query);
    card.hidden = !visible;
    if (visible) shown++;
  }}
  for (const section of document.querySelectorAll(".book-section")) section.hidden = !section.querySelector(".book-card:not([hidden])");
  count.textContent = shown + (shown === 1 ? " book" : " books");
}}
search.addEventListener("input", applyFilters);
for (const button of document.querySelectorAll(".filter")) button.addEventListener("click", () => {{
  kind = button.dataset.filter;
  for (const item of document.querySelectorAll(".filter")) item.setAttribute("aria-pressed", String(item === button));
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
document.addEventListener("click", event => {{
  const opener = event.target.closest(".book-open");
  if (!opener) return;
  modal.replaceChildren(opener.closest(".book-card").querySelector("template").content.cloneNode(true));
  dialog.showModal();
}});
dialog.querySelector(".close").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", event => {{ if (event.target === dialog) dialog.close(); }});
</script></main></body></html>"""
    path.write_text(body, encoding="utf-8")
    if write_latest:
        (report_dir / "latest.html").write_text(body, encoding="utf-8")
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
                    covers = json.loads(html.unescape(covers_match.group(1)))
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
    config = load_config(Path(args.config).expanduser().resolve())
    report_dir = Path(config["report_dir"])
    paths = sorted(report_dir.glob("book-watch_*.html"))
    if not paths:
        print("No timestamped reports found")
        return 0
    conn = connect_state(config)
    metadata = report_metadata_index(conn, config)
    conn.close()
    updated = 0
    for path in paths:
        source = path.read_text(encoding="utf-8")
        original_count = len(re.findall(r"<article\b", source, re.I))
        candidates = parse_report_cards(source, metadata)
        if len(candidates) != original_count:
            print(f"Skipped {path.name}: parsed {len(candidates)}/{original_count} cards")
            continue

        def count(label: str) -> int:
            match = re.search(rf"<strong>([\d,]+)</strong>{re.escape(label)}", source, re.I)
            return int(match.group(1).replace(",", "")) if match else 0

        query_match = re.search(r"<strong>Authors queried:</strong>\s*(.*?)<br>\s*<strong>Series queried:</strong>\s*(.*?)</p>", source, re.I | re.S)
        authors = [] if not query_match or plain_text(query_match.group(1)) == "none" else [value.strip() for value in plain_text(query_match.group(1)).split(",")]
        series_queries = [] if not query_match or plain_text(query_match.group(2)) == "none" else [value.strip() for value in plain_text(query_match.group(2)).split(",")]
        details = re.search(r"<details[^>]*>.*?</summary>\s*<p>(.*?)</p>\s*<p>(.*?)</p>\s*<ul>(.*?)</ul>", source, re.I | re.S)
        catalog_status = plain_text(details.group(1)) if details else "Historical report"
        ai_status = plain_text(details.group(2)) if details else "Historical report"
        notes = [plain_text(value) for value in re.findall(r"<li>(.*?)</li>", details.group(3), re.I | re.S)] if details else []
        day_match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
        report_day = datetime.strptime(day_match.group(1), "%Y-%m-%d").date() if day_match else date.today()
        temporary = path.with_suffix(".html.tmp")
        render_report(
            candidates,
            config,
            authors,
            series_queries,
            count("Calibre books"),
            catalog_status,
            notes,
            ai_status,
            output_path=temporary,
            report_day=report_day,
            screened_count=count("external records screened"),
            owned_suppressed_count=count("already-owned records suppressed"),
            write_latest=False,
        )
        rendered = temporary.read_text(encoding="utf-8")
        rendered_count = rendered.count('class="book-card"')
        backup = path.with_suffix(".html.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        temporary.replace(path)
        updated += 1
        print(f"Updated {path.name}: {original_count} -> {rendered_count} released cards")
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
        progress("Reading Calibre catalog...")
        raw_books, catalog_status = load_calibre(config)
        progress(f"Calibre catalog ready: {len(raw_books):,} books ({catalog_status})")
        catalog = build_catalog(raw_books)
        authors = select_authors(conn, catalog, config, args.author or [], args.max_authors)
        series_queries = list(args.series or []) + list(config.get("watch", {}).get("ongoing_series", []))
        progress(f"Selected {len(authors)} author and {len(series_queries)} series queries")
        focused = bool(args.focused)
        sources = config["sources"]
        source_notes: list[str] = []
        collected: list[Candidate] = []

        if not args.no_network and sources.get("google_books", True):
            queries = [(f"author: {author}", f'inauthor:"{author}"') for author in authors]
            queries += [(f"series: {series}", f'"{series}"') for series in series_queries]
            if not focused:
                queries += [(f"subject: {subject}", f'subject:"{subject}"') for subject in sources.get("subjects", [])]
            progress(f"Google Books: running {len(queries)} queries...")
            try:
                found, errors = google_candidates(conn, config, queries, args.refresh)
                collected.extend(found)
                source_notes.append(f"Google Books: {len(found)} raw candidates from {len(queries)} queries")
                source_notes.extend(errors)
                progress(f"Google Books: {len(found)} records, {len(errors)} query errors")
            except Exception as exc:
                source_notes.append(f"Google Books error: {exc}")
                progress(f"Google Books failed: {exc}")

        if not args.no_network and sources.get("open_library", True):
            queries_ol = [(f"author: {author}", "author", author) for author in authors]
            queries_ol += [(f"series: {series}", "q", series) for series in series_queries]
            if not focused:
                queries_ol += [(f"subject: {subject}", "subject", subject) for subject in sources.get("subjects", [])]
            progress(f"Open Library: running {len(queries_ol)} queries...")
            try:
                found = open_library_candidates(conn, config, queries_ol, args.refresh)
                collected.extend(found)
                source_notes.append(f"Open Library: {len(found)} raw candidates from {len(queries_ol)} queries")
                progress(f"Open Library: {len(found)} records")
            except Exception as exc:
                source_notes.append(f"Open Library error: {exc}")
                progress(f"Open Library failed: {exc}")

        if not args.no_network and sources.get("reactor", True):
            progress("Reactor: checking monthly release lists...")
            found, errors = reactor_candidates(conn, config, args.refresh)
            collected.extend(found)
            source_notes.append(f"Reactor: {len(found)} raw candidates")
            source_notes.extend(errors)
            progress(f"Reactor: {len(found)} records, {len(errors)} errors")

        hardcover_note = ""
        if not args.no_network and sources.get("hardcover", True):
            progress("Hardcover: checking configured authors...")
            try:
                found, hardcover_note = hardcover_candidates(conn, config, authors, args.refresh)
                collected.extend(found)
                source_notes.append(f"Hardcover: {len(found)} raw candidates. {hardcover_note}")
                progress(f"Hardcover: {len(found)} records ({hardcover_note})")
            except Exception as exc:
                source_notes.append(f"Hardcover error: {exc}")
                progress(f"Hardcover failed: {exc}")

        if args.no_network:
            source_notes.append("Network sources disabled; report uses no external candidates")
            progress("Network sources disabled")

        progress(f"Merging and scoring {len(collected):,} collected records...")
        candidates = merge_candidates(collected)
        match_and_score(candidates, catalog, config)
        load_decisions(conn, candidates)
        candidates.sort(key=lambda item: (item.score, item.published_date), reverse=True)
        progress("OpenRouter: enriching top candidates..." if not args.no_ai else "OpenRouter disabled for this run")
        ai_status = "AI disabled for this run" if args.no_ai else enrich_with_openrouter(candidates, config, catalog)
        progress(ai_status)
        if not args.no_network and (sources.get("google_books", True) or sources.get("open_library", True)):
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
        persist_candidates(conn, candidates)
        candidates.sort(key=lambda item: (item.score, item.published_date), reverse=True)
        progress("Writing HTML report...")
        report = render_report(candidates, config, authors, series_queries, len(raw_books), catalog_status, source_notes, ai_status)

        checked_at = iso_now()
        for author in authors:
            conn.execute(
                "INSERT OR REPLACE INTO author_checks(author_key,author_name,checked_at) VALUES(?,?,?)",
                (normalize(author), author, checked_at),
            )
        conn.execute(
            "UPDATE runs SET completed_at=?,status='complete',report_path=?,notes=? WHERE id=?",
            (iso_now(), str(report), redact_secrets("; ".join(source_notes + [ai_status])), run_id),
        )
        conn.commit()
        visible = [item for item in candidates if not item.owned and item.decision != "dismiss" and item.score > -10]
        print(f"Catalog: {len(raw_books):,} books ({catalog_status})")
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


def self_test() -> int:
    assert normalize("Charles E. Gannon") == normalize("Charles E Gannon")
    assert clean_isbn("978-0-123456-47-2") == "9780123456472"
    noisy = 'plugin says hi\n[{"id": 1}]Integration status: True'
    assert parse_noisy_json(noisy) == [{"id": 1}]
    title, series, index = parse_series("Children of Strife (Children of Time #4)")
    assert (title, series, index) == ("Children of Strife", "Children of Time", 4.0)
    parser = BlockParser()
    parser.feed("<h3>July 7</h3><p>The Seed (Nexus #6) — Rick Campbell (Publisher)<br>Synopsis.</p>")
    assert parser.blocks == ["July 7", "The Seed (Nexus #6) — Rick Campbell (Publisher)\nSynopsis."]
    match = REACTOR_BOOK_RE.search(parser.blocks[1])
    assert match and match.group("series") == "Nexus" and match.group("index") == "6"
    print("self-test: ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a curated release report from a Calibre library.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to config.toml")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="Build a report")
    run.add_argument("--author", action="append", help="Focus on an owned author; repeatable")
    run.add_argument("--series", action="append", help="Focus on an owned series; repeatable")
    run.add_argument("--max-authors", type=int, help="Override the rotating author count")
    run.add_argument("--focused", action="store_true", help="Skip general subject API queries")
    run.add_argument("--no-ai", action="store_true")
    run.add_argument("--no-network", action="store_true")
    run.add_argument("--refresh", action="store_true", help="Ignore the HTTP cache")
    decision = sub.add_parser("decide", help="Keep or dismiss a candidate")
    decision.add_argument("candidate_id")
    decision.add_argument("status", choices=("keep", "dismiss"))
    sub.add_parser("update-reports", help="Regenerate existing reports with the current layout")
    sub.add_parser("self-test", help="Run fast built-in checks")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "run"):
        if args.command is None:
            args = parser.parse_args(["run"])
        return run_report(args)
    if args.command == "decide":
        return decide(args)
    if args.command == "update-reports":
        return update_reports(args)
    if args.command == "self-test":
        return self_test()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

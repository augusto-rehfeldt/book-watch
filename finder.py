from __future__ import annotations

import argparse
import csv
import html
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    import requests
except ImportError:
    requests = None

import book_watch as bw

from finder_ai import AIService, _token_set_ratio


RESULT_BLOCK_CLASS = "flex pt-3 pb-3 border-b last:border-b-0 border-gray-100"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    )
}

# Anna's Archive is Cloudflare-walled for keyless clients, so the actual file comes
# from Library Genesis. libgen.is/.rs/.st are dead; these two answer.
LIBGEN_MIRRORS = ["https://libgen.li", "https://libgen.vg"]

# Download priority: what an e-reader wants first, PDF only as a last resort.
DOWNLOAD_FORMATS = ("epub", "azw3", "mobi", "fb2", "pdf")


@dataclass
class Book:
    """Represents a book to be searched."""

    title: str
    author: str
    series: str
    index: str

    @property
    def search_identifier(self) -> str:
        if (not self.title or self.title.lower() == "n/a") and self.series and self.series.lower() != "n/a":
            return self.series
        return self.title


ANNAS_MCP_REPO = "https://github.com/iosifache/annas-mcp"
MISSING_BOOKS_PATH_FILE = Path(__file__).resolve().parent.parent / "book_finder" / ".missing_books_path"


def build_annas_mcp_hint(book: Book, reason: str) -> str:
    search_query = f'{book.search_identifier} {book.author}'.strip()
    return (
        f"Could not find a usable Anna's Archive result for '{book.title}' by {book.author} ({reason}).\n"
        f"If you want to try the annas-mcp MCP server / CLI for permitted downloads, see: {ANNAS_MCP_REPO}\n"
        f"Example CLI flow: book-search \"{search_query}\" then book-download <md5> \"{book.title}.pdf\"\n"
        f"MCP usage needs ANNAS_SECRET_KEY and ANNAS_DOWNLOAD_PATH set in the environment."
    )


def build_annas_mcp_notice(failed_books: list[tuple[Book, str]]) -> str:
    reasons = {reason for _, reason in failed_books}
    example_books = ", ".join(f"'{book.title}'" for book, _ in failed_books[:3])
    reason_text = "; ".join(sorted(reasons))
    return (
        f"Anna's Archive couldn't help with {len(failed_books)} book(s) ({reason_text}).\n"
        f"If you want to try the annas-mcp MCP server / CLI for permitted downloads, see: {ANNAS_MCP_REPO}\n"
        f"Example CLI flow: book-search \"<title> <author>\" then book-download <md5> \"<title>.pdf\"\n"
        f"Affected books: {example_books}\n"
        f"MCP usage needs ANNAS_SECRET_KEY and ANNAS_DOWNLOAD_PATH set in the environment."
    )


def load_missing_books_csv_path(path_file: str | Path = MISSING_BOOKS_PATH_FILE) -> str:
    path_file = Path(path_file)
    try:
        raw = path_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Missing book path file not found at {path_file}. Create it and place the CSV path inside."
        ) from exc

    csv_path = raw.strip()
    if not csv_path:
        raise ValueError(f"Missing book path file at {path_file} is empty.")
    return csv_path


def resolve_input_file(input_file: str | None, path_file: str | Path = MISSING_BOOKS_PATH_FILE) -> str:
    if input_file:
        return input_file
    return load_missing_books_csv_path(path_file)


def _cell_value(cell: ET.Element, shared_strings: list[str], namespaces: dict[str, str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline_text = cell.find("main:is/main:t", namespaces)
        if inline_text is not None and inline_text.text is not None:
            return inline_text.text
        return ""

    value = cell.find("main:v", namespaces)
    if value is None or value.text is None:
        return ""

    if cell_type == "s":
        try:
            return shared_strings[int(value.text)]
        except (ValueError, IndexError):
            return ""

    return value.text


def _column_index(cell_ref: str) -> int:
    match = re.match(r"([A-Z]+)", cell_ref.upper())
    if not match:
        return 0
    index = 0
    for char in match.group(1):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _extract_shared_strings(zf: zipfile.ZipFile, namespaces: dict[str, str]) -> list[str]:
    try:
        shared_strings_xml = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []

    root = ET.fromstring(shared_strings_xml)
    shared_strings: list[str] = []
    for si in root.findall("main:si", namespaces):
        text_parts = [node.text or "" for node in si.iterfind(".//main:t", namespaces)]
        shared_strings.append("".join(text_parts))
    return shared_strings


def _read_xlsx_rows(file_path: str) -> list[dict[str, str]]:
    namespaces = {
        "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    }

    with zipfile.ZipFile(file_path) as zf:
        workbook_root = ET.fromstring(zf.read("xl/workbook.xml"))
        sheets = workbook_root.find("main:sheets", namespaces)
        if sheets is None:
            return []

        first_sheet = sheets.find("main:sheet", namespaces)
        if first_sheet is None:
            return []

        rel_id = first_sheet.attrib.get(f"{{{namespaces['r']}}}id")
        if not rel_id:
            return []

        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        sheet_target = None
        for rel in rels_root.findall("rel:Relationship", namespaces):
            if rel.attrib.get("Id") == rel_id:
                sheet_target = rel.attrib.get("Target")
                break
        if not sheet_target:
            return []

        sheet_path = sheet_target.lstrip("/")
        if not sheet_path.startswith("xl/"):
            sheet_path = f"xl/{sheet_path}"

        shared_strings = _extract_shared_strings(zf, namespaces)
        sheet_root = ET.fromstring(zf.read(sheet_path))
        data = sheet_root.find("main:sheetData", namespaces)
        if data is None:
            return []

        rows: list[list[str]] = []
        for row in data.findall("main:row", namespaces):
            values: list[str] = []
            for cell in row.findall("main:c", namespaces):
                index = _column_index(cell.attrib.get("r", ""))
                while len(values) <= index:
                    values.append("")
                values[index] = _cell_value(cell, shared_strings, namespaces).strip()
            rows.append(values)

    if not rows:
        return []

    headers = [header.strip() for header in rows[0]]
    result: list[dict[str, str]] = []
    for row in rows[1:]:
        row_dict = {headers[i]: row[i] if i < len(row) else "" for i in range(len(headers)) if headers[i]}
        result.append(row_dict)
    return result


# Quote-aware: Library Genesis puts HTML inside tooltip attributes
# (title="Add/Edit: …<br>Y. dl_avaxhome…"), and a plain <[^>]+> ends the tag at that
# nested <br>, spilling the attribute's text into the result as if it were content.
_TAG = re.compile(r"""<(?:[^>"']|"[^"]*"|'[^']*')*>""")


def _strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", text))).strip()


def _extract_balanced_div_blocks(html_content: str, class_name: str) -> list[str]:
    blocks: list[str] = []
    start_pattern = re.compile(rf'<div\b[^>]*class="[^"]*{re.escape(class_name)}[^"]*"[^>]*>', re.I)
    tag_pattern = re.compile(r"</?div\b[^>]*>", re.I)

    position = 0
    while True:
        start_match = start_pattern.search(html_content, position)
        if not start_match:
            break

        depth = 1
        cursor = start_match.end()
        while depth > 0:
            tag_match = tag_pattern.search(html_content, cursor)
            if not tag_match:
                cursor = len(html_content)
                break

            tag_text = tag_match.group(0)
            if tag_text.lower().startswith("</div"):
                depth -= 1
            else:
                depth += 1
            cursor = tag_match.end()

        blocks.append(html_content[start_match.start() : cursor])
        position = cursor

    return blocks


def _extract_text(pattern: str, block: str) -> str | None:
    match = re.search(pattern, block, re.I | re.S)
    if not match:
        return None
    return _strip_tags(match.group(1))


def search_anna_archive(query: str, max_attempts: int = 3, backoff_seconds: float = 2.0, verbose: bool = False) -> str | None:
    """Search Anna's Archive and retry transient request failures."""

    base_url = "https://annas-archive.org/search"
    search_query = query.replace(" ", "+")
    url = f"{base_url}?index=&page=1&sort=&display=&q={search_query}"

    if verbose:
        print(f"Searching for: {query} at {url}")

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if verbose:
                print(f"Anna's Archive request attempt {attempt}/{max_attempts} failed: {exc}")
            if attempt >= max_attempts:
                break
            time.sleep(backoff_seconds * attempt)

    if verbose and last_error is not None:
        print(f"Anna's Archive request failed after {max_attempts} attempt(s): {last_error}")
    return None


def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:120] or "book"


def _libgen_cell_text(cell_html):
    return bw.libgen_cell_text(cell_html)



def _libgen_candidates(html_content, title, author, max_bytes):
    candidate = bw.Candidate(title, [author])
    rows = bw.rank_libgen_rows(candidate, bw.parse_libgen_rows(html_content), {
        'formats': DOWNLOAD_FORMATS, 'max_bytes': max_bytes, 'min_title_match': 0.6})
    return [(DOWNLOAD_FORMATS.index(row['extension']), row['bytes'], row['md5'], row['extension']) for row in rows]



def download_book(title, author, dest_dir, max_bytes=200_000_000, verbose=False, config_path=None):
    config = bw.load_config(Path(config_path or bw.DEFAULT_CONFIG))
    candidate = bw.Candidate(title, [author], language='en')
    settings = bw.download_settings(config)
    settings.update(dir=Path(dest_dir).resolve(), max_bytes=max_bytes, ai_assist=False)
    conn = bw.connect_state(config)
    try:
        bw.persist_candidates(conn, [candidate])
        ok, message = bw.download_candidate(conn, config, settings,
            int(config.get('sources', {}).get('cache_hours', 24)), candidate.key,
            import_to_calibre=False, log=print if verbose else lambda *args: None)
        if not ok:
            if verbose: print(message)
            return None
        row = conn.execute('SELECT file_path FROM downloads WHERE candidate_key=?', (candidate.key,)).fetchone()
        return Path(row['file_path'])
    finally:
        conn.close()



def parse_results(html_content: str, book_title: str, verbose: bool = False) -> list[dict[str, str]]:
    """Parse Anna's Archive search results into structured candidate matches."""

    if not html_content:
        return []

    search_results = _extract_balanced_div_blocks(html_content, RESULT_BLOCK_CLASS)
    if not search_results:
        if verbose:
            print(f"No direct results found for '{book_title}'.")
        return []

    target_formats = ["epub", "azw3", "mobi", "fb2"]
    potential_matches: list[dict[str, str]] = []

    for result in search_results:
        metadata_text = _extract_text(
            r'<div\b[^>]*class="[^"]*text-gray-800[^"]*mt-2[^"]*"[^>]*>(.*?)</div>',
            result,
        )
        if not metadata_text:
            continue
        if "English [en]" not in metadata_text:
            continue

        info_text_lower = metadata_text.lower()
        found_format = next((fmt for fmt in target_formats if fmt in info_text_lower), None)
        if not found_format:
            continue

        title_match = re.search(
            r'<a\b[^>]*class="[^"]*js-vim-focus[^"]*"[^>]*>(.*?)</a>', result, re.I | re.S
        )
        link_match = re.search(
            r'<a\b[^>]*class="[^"]*custom-a[^"]*hover:opacity-80[^"]*"[^>]*href="([^"]+)"',
            result,
            re.I | re.S,
        )
        author_match = re.search(r'<a\b[^>]*href="(/search\?q=[^"]+)"[^>]*>(.*?)</a>', result, re.I | re.S)

        if not (title_match and link_match):
            continue

        title = _strip_tags(title_match.group(1))
        md5_link = link_match.group(1)
        author = "Unknown Author"
        if author_match:
            author_text = _strip_tags(author_match.group(2))
            if author_text:
                author = author_text

        potential_matches.append(
            {
                "title": title,
                "author": author,
                "format": found_format.upper(),
                "link": f"https://annas-archive.org{md5_link}",
            }
        )

    if verbose:
        print(f"Found {len(potential_matches)} potential e-reader matches.")
    return potential_matches


def read_books_from_csv(file_path: str) -> list[Book]:
    books_to_search: list[Book] = []
    try:
        if zipfile.is_zipfile(file_path):
            rows = _read_xlsx_rows(file_path)
        else:
            with open(file_path, "r", encoding="utf-8") as csvfile:
                content = csvfile.read()
                if content.startswith("\ufeff"):
                    content = content[1:]
                rows = list(csv.DictReader(content.splitlines()))

        for row in rows:
            book = Book(
                title=str(row.get("Title", "")).strip(),
                author=str(row.get("Author", "")).strip(),
                series=str(row.get("Series", "")).strip(),
                index=str(row.get("Index", "")).strip(),
            )
            if book.author and book.author.lower() != "n/a" and book.search_identifier and book.search_identifier.lower() != "n/a":
                books_to_search.append(book)
    except FileNotFoundError:
        print(f"Error: Input file not found at {file_path}")
        sys.exit(1)
    except Exception as exc:
        print(f"Error reading CSV file: {exc}")
        sys.exit(1)
    return books_to_search


def write_found_books(file_path: str, found_links: list[str]):
    if not found_links:
        print("\nNo books found with suitable e-reader formats and AI validation.")
        return

    print(f"\n--- Writing validated results to {file_path} ---")
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            for item in found_links:
                f.write(item + "\n")
        print("Done.")
    except IOError as exc:
        print(f"Error writing to output file: {exc}")


def _libgen_only(
    book: Book, download_dir: str | Path | None, reason: str, verbose: bool = False, config_path=None
) -> tuple[str | None, str | None]:
    """Last resort when Anna's Archive gives us nothing to validate.

    Anna's Archive is DNS-blocked by some ISPs and Cloudflare-walled for keyless
    clients either way, so gating every download behind a validated result there means
    downloading nothing at all on those networks. Library Genesis holds the same files;
    the title/author gate in `_libgen_candidates` is what stands in for the AI
    validation that the missing search results would otherwise have fed.
    """
    if not download_dir:
        return None, reason

    if verbose:
        print(f"Anna's Archive gave nothing ({reason}); trying Library Genesis directly...")
    path = download_book(book.search_identifier, book.author, download_dir, verbose=verbose, config_path=config_path)
    if not path:
        return None, reason

    return (
        f"{book.title} | {book.author} | {path.as_uri()} | "
        f"Library Genesis ({path.suffix.lstrip('.').upper()}) | "
        f"Reason: direct Library Genesis download, {reason} | Saved: {path}",
        None,
    )


def process_book(
    book: Book,
    ai_service: AIService,
    confidence_threshold: int,
    verbose: bool = False,
    download_dir: str | Path | None = None,
    config_path=None,
) -> tuple[str | None, str | None]:
    search_query = f"{book.search_identifier} {book.author}"
    if verbose:
        print(f"\n--- Searching for: '{book.search_identifier}' by {book.author} (Book: {book.index}) ---")

    html_content = search_anna_archive(search_query, verbose=verbose)
    if not html_content:
        if verbose:
            print(f"Skipping '{book.title}' due to search error.")
        return _libgen_only(book, download_dir, "Anna's Archive search failed", verbose, config_path)

    potential_matches = parse_results(html_content, book.search_identifier, verbose=verbose)
    if not potential_matches:
        if verbose:
            print(f"No suitable e-reader format found for '{book.title}'.")
        return _libgen_only(book, download_dir, "no e-reader matches were found", verbose, config_path)

    if verbose:
        print("Validating matches with AI...")
    selected_match = ai_service.validate_book_match(
        title=book.title,
        author=book.author,
        potential_matches=potential_matches,
        series=book.series,
        index=book.index,
        confidence_threshold=confidence_threshold,
    )

    if selected_match:
        link = selected_match.get("link", "N/A")
        match_title = selected_match.get("title", "N/A")
        match_format = selected_match.get("format", "N/A")
        confidence = selected_match.get("confidence_score", "N/A")

        if verbose:
            print(f"AI Match Selected: {match_title} ({match_format}) with {confidence}% confidence")

        saved = ""
        if download_dir:
            if verbose:
                print("Downloading from Library Genesis...")
            # Search Library Genesis with the catalogue title the AI validated, not the
            # CSV one: it is the full, correctly-spelled edition title, and matching a
            # catalogue against another catalogue's title is like for like.
            path = download_book(
                match_title if match_title != "N/A" else book.search_identifier,
                book.author,
                download_dir,
                verbose=verbose,
                config_path=config_path,
            )
            saved = f" | Saved: {path}" if path else " | Download failed"

        return (
            f"{book.title} | {book.author} | {link} | {match_title} ({match_format}) | "
            f"Reason: AI Validation (Confidence: {confidence}){saved}",
            None,
        )

    if verbose:
        print(f"No strong match found for '{book.title}' by '{book.author}'.")
    # The AI only ever saw Anna's Archive listings; Library Genesis may hold the book
    # it rejected them for. A failure here stays unreported, as it was before.
    if download_dir:
        found_link, _ = _libgen_only(book, download_dir, "AI found no confident match", verbose, config_path)
        if found_link:
            return found_link, None
    return None, None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Find and validate books from Anna's Archive.")
    parser.add_argument('--config', type=Path, default=bw.DEFAULT_CONFIG, help='Book-watch config for shared download state')
    parser.add_argument("--input-file", default=None, help="Path to the input CSV file.")
    parser.add_argument(
        "--input-path-file",
        default=str(MISSING_BOOKS_PATH_FILE),
        help="Gitignored file containing the path to the input CSV.",
    )
    parser.add_argument("--output-file", default="found_books_validated.txt", help="Path to the output text file.")
    parser.add_argument("--model", default="gpt-5-mini", help="OpenAI model to use for validation.")
    parser.add_argument(
        "--confidence-threshold",
        type=int,
        default=80,
        help="Confidence threshold for AI validation (0-100).",
    )
    parser.add_argument(
        "--download-dir",
        default=None,
        help="Download validated books into this directory (Library Genesis). Off by default.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show detailed per-book progress output.")
    args = parser.parse_args(argv)
    if requests is None:
        parser.error("Anna lookup requires requests: pip install -r requirements-finder.txt")

    try:
        books_to_search = read_books_from_csv(resolve_input_file(args.input_file, args.input_path_file))
    except (FileNotFoundError, ValueError) as exc:
        print(f"CRITICAL: {exc}")
        sys.exit(1)

    try:
        ai_service = AIService(model=args.model, verbose=args.verbose)
    except (ValueError, FileNotFoundError, ConnectionError) as exc:
        print(f"CRITICAL: Failed to initialize AI Service. Error: {exc}")
        sys.exit(1)

    if args.verbose:
        print(f"Processing {len(books_to_search)} books from {args.input_file}...")

    found_links = []
    failed_books: list[tuple[Book, str]] = []
    for i, book in enumerate(books_to_search):
        if args.verbose:
            print(f"\n\n--- Book {i + 1}/{len(books_to_search)} ---")
        found_link, failure_reason = process_book(
            book,
            ai_service,
            args.confidence_threshold,
            verbose=args.verbose,
            download_dir=args.download_dir,
            config_path=args.config,
        )
        if not args.verbose:
            # Print concise inline result for each book
            if found_link:
                # Extract the link part from found_link (format: "title | author | link | ...")
                parts = found_link.split(" | ")
                link = parts[2] if len(parts) > 2 else found_link
                saved = next((p for p in parts if p.startswith("Saved: ")), "")
                print(f"✓ {book.title[:40]}{'...' if len(book.title) > 40 else ''} by {book.author[:20]}{'...' if len(book.author) > 20 else ''} -> {link}{' [' + saved + ']' if saved else ''}")
            else:
                reason = failure_reason if failure_reason else "unknown error"
                print(f"✗ {book.title[:40]}{'...' if len(book.title) > 40 else ''} by {book.author[:20]}{'...' if len(book.author) > 20 else ''} -> failed ({reason})")
        if found_link:
            found_links.append(found_link)
        elif failure_reason:
            failed_books.append((book, failure_reason))

        time.sleep(2)

    if failed_books:
        print(build_annas_mcp_notice(failed_books))

    write_found_books(args.output_file, found_links)
    print(f"Wrote {len(found_links)} validated result(s) to {args.output_file}.")


if __name__ == "__main__":
    main()

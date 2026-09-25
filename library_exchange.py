"""Read-only Calibre export and missing-list import for the book workspace."""
import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import book_watch as bw


def missing_candidates(path):
    path = Path(path)
    if path.suffix.lower() == '.xlsx':
        from finder import read_books_from_csv
        rows = [dict(title=b.search_identifier, author=b.author, series=b.series, index=b.index)
                for b in read_books_from_csv(str(path))]
    else:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            headers = {str(key).strip().lower() for key in reader.fieldnames or []}
            if not {'title', 'author'} <= headers:
                raise ValueError('Missing-list CSV requires Title and Author columns')
            rows = [{str(key).strip().lower(): str(value or '').strip()
                     for key, value in row.items() if key is not None} for row in reader]
    candidates = {}
    for number, row in enumerate(rows, 2):
        title, author = row.get('title', ''), row.get('author', '')
        if not title or not author:
            raise ValueError(f'Missing title or author on row {number}')
        index = row.get('index') or row.get('series_index')
        candidate = bw.Candidate(title, [author], series=row.get('series', ''),
            series_index=bw.safe_float(index), language='en',
            evidence=[{'source': 'missing-list', 'url': path.resolve().as_uri()}])
        candidates[candidate.key] = candidate
    return list(candidates.values())


def import_missing(config, path):
    candidates = missing_candidates(path)  # Validate the complete input before writing state.
    conn = bw.connect_state(config)
    try:
        bw.persist_candidates(conn, candidates)
    finally:
        conn.close()
    return candidates


def export_atlas(config, output, summary_column='#summary'):
    library = Path(config['library']['path']).resolve()
    books = bw.calibre_books_from_db(library)
    conn = sqlite3.connect((library / 'metadata.db').as_uri() + '?mode=ro', uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in conn.execute('PRAGMA table_info(books)')}
        extra = {row[0]: row[1:] for row in conn.execute(
            'SELECT id,' + ('uuid' if 'uuid' in columns else "''") + ',' +
            ('path' if 'path' in columns else "''") + ' FROM books')}
        summaries = {}
        if 'custom_columns' in tables:
            row = conn.execute('SELECT id FROM custom_columns WHERE label=?', (summary_column.lstrip('#'),)).fetchone()
            table = f'custom_column_{int(row[0])}' if row else ''
            if table in tables:
                if 'book' in {column[1] for column in conn.execute(f'PRAGMA table_info({table})')}:
                    values = conn.execute(f'SELECT book,value FROM {table}')
                else:
                    values = conn.execute(f'SELECT l.book,v.value FROM books_{table}_link l JOIN {table} v ON v.id=l.value')
                for book_id, value in values:
                    summaries[book_id] = '\n'.join(filter(None, (summaries.get(book_id, ''), str(value or ''))))
        comments = dict(conn.execute('SELECT book,text FROM comments')) if 'comments' in tables else {}
    finally:
        conn.close()
    namespace = hashlib.sha256(str(library).encode()).hexdigest()[:12]
    rows = []
    for book in books:
        uuid, relative = extra[book['id']]
        summary = bw.plain_text(str(summaries.get(book['id']) or comments.get(book['id']) or ''))
        cover = (library / relative / 'cover.jpg').resolve() if relative else None
        if cover and (not cover.is_relative_to(library) or not cover.is_file()):
            cover = None
        rows.append(dict(id=f'calibre-{uuid or namespace + "-" + str(book["id"])}',
            calibre_id=book['id'], title=book['title'], author=book['authors'], summary=summary,
            content=summary or f'{book["title"]}. {book["authors"]}. {book["series"]}',
            series=book['series'], series_index=book['series_index'],
            tags='; '.join(book['tags']), year=book['pubdate'][:4], cover=str(cover or '')))
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='',
                                         dir=output.parent, suffix='.csv', delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=['id', 'calibre_id', 'title', 'author', 'summary',
                'content', 'series', 'series_index', 'tags', 'year', 'cover'])
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(rows), sum(bool(row['summary']) for row in rows)

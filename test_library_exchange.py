import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import book_watch as bw
import library_exchange as exchange


class ExchangeTests(unittest.TestCase):
    def test_missing_list_and_shared_download_can_later_import_without_refetch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'missing.csv'
            source.write_text('Title,Author,Series,Index\nSnow Crash,Neal Stephenson,,\n', encoding='utf-8')
            config = {'state_path': str(root / 'state.sqlite'), 'report_dir': str(root / 'reports')}
            candidates = exchange.import_missing(config, source)
            self.assertEqual(len(exchange.import_missing(config, source)), 1)
            report_config = bw.load_config(bw.DEFAULT_CONFIG)
            report_config['report_dir'] = str(root / 'reports')
            report = bw.render_report(candidates, report_config, [], [], 0, 'fixture', [], 'disabled',
                                      output_path=root / 'report.html', write_latest=False)
            self.assertIn(candidates[0].key, report.read_text(encoding='utf-8'))
            conn = bw.connect_state(config)
            try:
                self.assertEqual(conn.execute('SELECT count(*) FROM candidate_history').fetchone()[0], 1)
                settings = bw.download_settings(config)
                settings['ai_assist'] = False
                body = b'PK\x03\x04' + b'book' * 6000
                with patch.object(bw, 'libgen_fetch', return_value=(body, {'extension': 'epub', 'md5': 'a'*32})) as fetch, \
                     patch.object(bw, 'add_to_calibre', return_value=9) as add, patch.object(bw, 'fetch_cover', return_value=None):
                    self.assertTrue(bw.download_candidate(conn, config, settings, 24, candidates[0].key, import_to_calibre=False)[0])
                    add.assert_not_called()
                    self.assertNotIn(candidates[0].key, bw.download_status(conn))
                    self.assertTrue(bw.download_candidate(conn, config, settings, 24, candidates[0].key)[0])
                    self.assertEqual(fetch.call_count, 1)
                    self.assertEqual(add.call_count, 1)
                    row = conn.execute('SELECT imported,calibre_id FROM downloads').fetchone()
                    self.assertEqual(tuple(row), (1, 9))
                    self.assertIn(candidates[0].key, bw.download_status(conn))
                target = bw.save_download(root, 'same.epub', b'old')
                second = bw.save_download(root, 'same.epub', b'new')
                self.assertEqual(target.read_bytes(), b'old')
                self.assertNotEqual(second, target)
                source.write_text('wrong,columns\na,b\n')
                with self.assertRaises(ValueError): exchange.import_missing(config, source)
            finally:
                conn.close()

    def test_shared_parser_removes_tooltips_and_sequel_matches(self):
        page = '''<tr><td><b>Series noise</b><a href="edition.php?id=3" title="noise<br>more noise">Dune</a>
                  <span>9780441172719</span></td><td>Frank Herbert</td><td>Publisher</td><td>1965</td>
                  <td>English</td><td>400</td><td>2 MB</td><td>epub</td>
                  <td><a href="ads.php?md5=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">GET</a></td></tr>'''
        row = bw.parse_libgen_rows(page)[0]
        self.assertEqual(row['title'], 'Dune')
        self.assertTrue(bw.libgen_row_matches(bw.Candidate('Dune', ['Frank Herbert']), row, 0.6))
        row = bw.parse_libgen_rows(page.replace('>Dune</a>', '>Dune Messiah</a>'))[0]
        self.assertFalse(bw.libgen_row_matches(bw.Candidate('Dune', ['Frank Herbert']), row, 0.6))

    def test_calibre_export_is_read_only_and_preserves_identity_and_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / 'metadata.db'
            conn = sqlite3.connect(db)
            conn.executescript('''
                CREATE TABLE books(id INTEGER, title TEXT, series_index REAL, pubdate TEXT, last_modified TEXT, uuid TEXT, path TEXT);
                INSERT INTO books VALUES(7,'Snow Crash',1,'1992-01-01','2026-01-01','stable-uuid','');
                CREATE TABLE authors(id INTEGER, name TEXT);
                INSERT INTO authors VALUES(1,'Neal Stephenson');
                CREATE TABLE books_authors_link(id INTEGER,book INTEGER,author INTEGER);
                INSERT INTO books_authors_link VALUES(1,7,1);
                CREATE TABLE tags(id INTEGER,name TEXT);
                CREATE TABLE books_tags_link(id INTEGER,book INTEGER,tag INTEGER);
                CREATE TABLE series(id INTEGER,name TEXT);
                CREATE TABLE books_series_link(book INTEGER,series INTEGER);
                CREATE TABLE identifiers(book INTEGER,type TEXT,val TEXT);
                CREATE TABLE custom_columns(id INTEGER,label TEXT);
                INSERT INTO custom_columns VALUES(2,'summary');
                CREATE TABLE custom_column_2(book INTEGER,value TEXT);
                INSERT INTO custom_column_2 VALUES(7,'<p>A synthetic summary.</p>');
            ''')
            conn.commit()
            conn.close()
            before = db.read_bytes()
            output = root / 'atlas' / 'library.csv'
            self.assertEqual(exchange.export_atlas({'library': {'path': str(root)}}, output), (1, 1))
            self.assertEqual(db.read_bytes(), before)
            with output.open(encoding='utf-8', newline='') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row['id'], 'calibre-stable-uuid')
            self.assertEqual(row['calibre_id'], '7')
            self.assertEqual(row['summary'], 'A synthetic summary.')
            self.assertEqual(row['content'], row['summary'])
            conn = sqlite3.connect(db)
            conn.executescript('''
                DROP TABLE custom_column_2;
                CREATE TABLE custom_column_2(id INTEGER,value TEXT);
                INSERT INTO custom_column_2 VALUES(4,'Normalized summary.');
                CREATE TABLE books_custom_column_2_link(book INTEGER,value INTEGER);
                INSERT INTO books_custom_column_2_link VALUES(7,4);
            ''')
            conn.commit()
            conn.close()
            self.assertEqual(exchange.export_atlas({'library': {'path': str(root)}}, output), (1, 1))
            with output.open(encoding='utf-8', newline='') as stream:
                self.assertEqual(next(csv.DictReader(stream))['summary'], 'Normalized summary.')


if __name__ == '__main__':
    unittest.main()

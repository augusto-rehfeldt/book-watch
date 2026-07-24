import tempfile
import time
import unittest
import os
import json
from pathlib import Path
from unittest.mock import patch

import book_watch as bw


class BookWatchTests(unittest.TestCase):
    def test_load_local_env_file(self):
        key = "BOOK_WATCH_TEST_ENV"
        os.environ.pop(key, None)
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / ".env"
                path.write_text(f'{key}="loaded"\n', encoding="utf-8")
                self.assertTrue(bw.load_env_file(path))
                self.assertEqual(os.environ[key], "loaded")
        finally:
            os.environ.pop(key, None)

    def test_noisy_calibre_json(self):
        text = 'plugin output\n[{"id": 7, "title": "Test"}]Integration status: True'
        self.assertEqual(bw.parse_noisy_json(text)[0]["id"], 7)
        self.assertEqual(bw.redact_secrets("https://example.test?q=1&key=secret&x=2"), "https://example.test?q=1&key=[redacted]&x=2")

    def test_openrouter_top_level_array(self):
        response = '```json\n[{"rank": 0, "title": "A Book"}]\n```'
        self.assertEqual(bw.parse_json_response(response)[0]["title"], "A Book")
        self.assertEqual(bw.parse_json_response([{"type": "text", "text": response}])[0]["title"], "A Book")

    def test_openrouter_null_content_falls_back(self):
        with self.assertRaisesRegex(ValueError, "no text"):
            bw.parse_json_response(None)

    def test_openrouter_rejects_hallucinated_candidate(self):
        real = bw.Candidate("Real Book", ["A. Writer"])
        results = [
            {"rank": 0, "id": "work:invented", "title": "Imaginary Book", "fit_score": 99},
            {"id": real.key, "title": real.title, "fit_score": 80, "why": "A sound match."},
        ]
        self.assertEqual(bw.apply_ai_results(results, [real], "test-model"), 1)
        self.assertEqual(real.ai["fit_score"], 80)
        self.assertEqual(real.score, 16)

    def test_openrouter_can_suppress_a_validated_translated_title(self):
        translated = bw.Candidate("Eu, Robô", ["Isaac Asimov"])
        results = [{"id": translated.key, "fit_score": "80.0", "owned_title": "i robot"}]
        bw.apply_ai_results(results, [translated], "test-model", {translated.key: ["i robot"]})
        self.assertTrue(translated.owned)
        self.assertEqual(translated.score, -1000)

    def test_openrouter_accepts_rank_only_when_title_still_matches(self):
        candidate = bw.Candidate("Exact Title", ["A. Writer"])
        result = {"rank": "0", "id": "mangled", "title": "Exact Title", "fitScore": "75"}
        self.assertEqual(bw.apply_ai_results([result], [candidate], "test-model"), 1)
        self.assertEqual(candidate.ai["fit_score"], 75)

    def test_openrouter_wall_clock_timeout(self):
        def slow_open(*args, **kwargs):
            time.sleep(0.05)

        with patch.object(bw, "urlopen", slow_open):
            with self.assertRaises(TimeoutError):
                bw.open_json_with_deadline(object(), 0.01)

    def test_openrouter_enriches_every_batch(self):
        candidates = [bw.Candidate(f"Book {number}", ["A. Writer"]) for number in range(13)]
        for candidate in candidates:
            candidate.score = 1
        responses = []
        for batch in (candidates[:12], candidates[12:]):
            content = json.dumps({"items": [{"id": item.key, "fit_score": 80} for item in batch]})
            responses.append({"choices": [{"message": {"content": content}}], "model": "test-model"})
        config = {
            "openrouter": {"enabled": True, "api_key_env": "BOOK_WATCH_TEST_AI_KEY", "model": "test-model"},
            "taste": {},
        }
        with patch.dict(os.environ, {"BOOK_WATCH_TEST_AI_KEY": "test-key"}), patch.object(
            bw, "open_json_with_deadline", side_effect=responses
        ) as request:
            catalog = bw.build_catalog([{"title": "Owned", "authors": "A. Writer", "tags": ["Science Fiction"]}])
            status = bw.enrich_with_openrouter(candidates, config, catalog)
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(candidate.ai for candidate in candidates))
        self.assertIn("13/13", status)
        prompt = json.loads(request.call_args_list[0].args[0].data)["messages"][0]["content"]
        self.assertIn('"top_tags": ["Science Fiction"]', prompt)

    def test_openai_oauth_needs_no_api_key(self):
        candidate = bw.Candidate("Book", ["A. Writer"])
        candidate.score = 1
        response = {"choices": [{"message": {"content": json.dumps({"items": [{"id": candidate.key, "fit_score": 80}]})}}]}
        config = {"openai_oauth": {"model": "gpt-5.4-mini"}, "taste": {}}
        with patch.object(bw, "ensure_openai_oauth_proxy"), patch.object(
            bw, "openai_oauth_models", return_value=["gpt-5.4-mini"]
        ), patch.object(
            bw, "open_json_with_deadline", return_value=response
        ) as send:
            status = bw.enrich_with_openrouter([candidate], config)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:10531/v1/chat/completions")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIn("AI enriched 1/1", status)

    def test_oauth_models_are_live_and_text_only(self):
        response = {"data": [{"id": "gpt-5.6-sol"}, {"id": "gpt-5.4-mini"}, {"id": "gpt-image-2"}]}
        with patch.object(bw, "open_json_with_deadline", return_value=response):
            models = bw.openai_oauth_models("http://127.0.0.1:10531/v1")
        self.assertEqual(models, ["gpt-5.6-sol", "gpt-5.4-mini"])

    def test_starts_missing_oauth_proxy_with_npx(self):
        with patch.object(bw, "_openai_oauth_proxy_running", side_effect=[False, True]), patch.object(
            bw.shutil, "which", return_value="npx"
        ), patch.object(bw.subprocess, "run") as run:
            bw.ensure_openai_oauth_proxy()
        run.assert_called_once_with(["npx", "openai-oauth@latest", "--detach"], check=True)

    def test_genre_option_is_repeatable(self):
        args = bw.build_parser().parse_args(["run", "--genre", "cozy fantasy", "-g", "mystery"])
        self.assertEqual(args.genre, ["cozy fantasy", "mystery"])

    def test_openrouter_retries_a_failed_batch_three_times(self):
        candidate = bw.Candidate("Book", ["A. Writer"])
        candidate.score = 1
        config = {
            "openrouter": {
                "enabled": True,
                "api_key_env": "BOOK_WATCH_TEST_AI_KEY",
                "model": "strong-primary",
                "fallback_model": "strong-fallback",
            },
            "taste": {},
        }
        with patch.dict(os.environ, {"BOOK_WATCH_TEST_AI_KEY": "test-key"}), patch.object(
            bw, "open_json_with_deadline", side_effect=TimeoutError("slow")
        ) as request:
            status = bw.enrich_with_openrouter([candidate], config)
        self.assertEqual(request.call_count, 3)
        self.assertIn("AI unavailable", status)

    def test_reactor_series_line(self):
        parser = bw.BlockParser()
        parser.feed(
            '<h3>March 17</h3><p><a href="https://www.amazon.com/dp/0316569364/"><strong>Children of Strife (Children of Time #4)</strong></a> '
            "— Adrian Tchaikovsky (Orbit)<br>Spiders return.</p>"
        )
        match = bw.REACTOR_BOOK_RE.search(parser.blocks[1])
        self.assertIsNotNone(match)
        self.assertEqual(match.group("series"), "Children of Time")
        self.assertEqual(float(match.group("index")), 4.0)
        self.assertEqual(bw.AMAZON_ISBN_RE.search(parser.links[1][0]).group(1), "0316569364")

    def test_open_library_keeps_only_english_editions(self):
        response = json.dumps({"docs": [
            {"title": "Eu, Robô", "author_name": ["Isaac Asimov"], "language": ["por"]},
            {"title": "I, Robot", "author_name": ["Isaac Asimov"], "language": ["eng"], "cover_i": 7},
        ]})
        with patch.object(bw, "cached_request", return_value=response) as request:
            found = bw.open_library_candidates(None, {"sources": {"cache_hours": 24}}, [("author", "author", "Isaac Asimov")], False)
        self.assertEqual([item.title for item in found], ["I, Robot"])
        self.assertIn("language=eng", request.call_args.args[1])

    def test_cover_fallback_requires_an_exact_book_match(self):
        candidate = bw.Candidate("Example Book", ["A. Writer"], published_date=str(bw.date.today()))
        candidate.score = 1
        found = [bw.Candidate("Example Book", ["A Writer"], cover_url="https://books.example/cover.jpg")]
        with patch.object(bw, "google_candidates", return_value=(found, [])) as request:
            matched, targets, errors = bw.fill_missing_covers(None, {}, [candidate], False)
        self.assertEqual((matched, targets, errors), (1, 1, 0))
        self.assertEqual(candidate.cover_url, "https://books.example/cover.jpg")
        self.assertIn('intitle:"Example Book" inauthor:"A. Writer"', request.call_args.args[2][0][1])

    def test_cover_fallback_uses_open_library_when_google_misses(self):
        candidate = bw.Candidate("Example Book", ["A. Writer"], published_date=str(bw.date.today()))
        candidate.score = 1
        found = [bw.Candidate("Example Book", ["A Writer"], cover_url="https://openlibrary.example/cover.jpg")]
        with patch.object(bw, "google_candidates", return_value=([], [])), patch.object(
            bw, "open_library_candidates", return_value=found
        ) as request:
            matched, targets, errors = bw.fill_missing_covers(None, {}, [candidate], False)
        self.assertEqual((matched, targets, errors), (1, 1, 0))
        self.assertEqual(candidate.cover_url, "https://openlibrary.example/cover.jpg")
        self.assertEqual(request.call_args.args[2][0][1:], ("title", "Example Book"))

    def test_google_books_retries_without_a_blocked_api_key(self):
        response = json.dumps({"items": [{"volumeInfo": {
            "title": "Example Book",
            "authors": ["A. Writer"],
            "language": "en",
            "imageLinks": {"thumbnail": "http://books.example/cover.jpg"},
        }}]})
        config = {
            "sources": {"cache_hours": 24},
            "google_books": {"api_key_env": "BOOK_WATCH_TEST_GOOGLE_KEY"},
        }
        with patch.dict(os.environ, {"BOOK_WATCH_TEST_GOOGLE_KEY": "blocked-key"}), patch.object(
            bw, "cached_request", side_effect=[RuntimeError("HTTP 403"), response]
        ) as request:
            found, errors = bw.google_candidates(None, config, [("test", "Example")], False)
        self.assertFalse(errors)
        self.assertEqual(found[0].cover_url, "https://books.example/cover.jpg")
        self.assertIn("key=blocked-key", request.call_args_list[0].args[1])
        self.assertNotIn("key=", request.call_args_list[1].args[1])

    def test_hardcover_supplies_english_cover(self):
        response = {"data": {"editions": [{
            "isbn_13": "9781529340587",
            "release_date": str(bw.date.today()),
            "language": {"code3": "eng"},
            "image": {"url": "https://hardcover.example/edition.jpg"},
            "book": {"title": "Example", "slug": "example", "contributions": [{"author": {"name": "A. Writer"}}]},
        }]}}
        config = {"hardcover": {"token_env": "BOOK_WATCH_TEST_HC_KEY"}, "run": {"past_days": 550, "future_days": 0}, "sources": {"cache_hours": 24}}
        with patch.dict(os.environ, {"BOOK_WATCH_TEST_HC_KEY": "test-key"}), patch.object(
            bw, "cached_request", return_value=json.dumps(response)
        ) as request:
            found, _ = bw.hardcover_candidates(None, config, ["A. Writer"], False)
        self.assertEqual(found[0].cover_url, "https://hardcover.example/edition.jpg")
        self.assertIn('language: {code3: {_eq: "eng"}}', request.call_args.kwargs["payload"]["query"])

    def test_series_continuation_and_owned_detection(self):
        raw = [
            {"id": 1, "title": "Children of Time", "authors": "Adrian Tchaikovsky", "series": "Children of Time", "series_index": 1},
            {"id": 2, "title": "Children of Ruin", "authors": "Adrian Tchaikovsky", "series": "Children of Time", "series_index": 2},
            {"id": 3, "title": "Children of Memory", "authors": "Adrian Tchaikovsky", "series": "Children of Time", "series_index": 3},
        ]
        catalog = bw.build_catalog(raw)
        candidates = [
            bw.Candidate(
                "Children of Strife",
                ["Adrian Tchaikovsky"],
                series="Children of Time",
                series_index=4,
                published_date=str(bw.date.today().year),
                subjects={"science fiction"},
            ),
            bw.Candidate("Children of Time", ["Adrian Tchaikovsky"]),
        ]
        config = {
            "run": {"past_days": 550, "future_days": 365},
            "watch": {"complete_series": [], "ignore_series": []},
            "taste": {"include": ["science fiction"], "exclude": []},
        }
        bw.match_and_score(candidates, catalog, config)
        self.assertTrue(candidates[0].series_alert)
        self.assertGreater(candidates[0].score, 100)
        self.assertTrue(candidates[1].owned)

    def test_old_author_result_is_suppressed(self):
        catalog = bw.build_catalog([{"id": 1, "title": "Owned", "authors": "A. Writer"}])
        old = bw.Candidate("Old Backlist", ["A. Writer"], published_date="2001")
        config = {
            "run": {"past_days": 550, "future_days": 365},
            "watch": {"complete_series": [], "ignore_series": []},
            "taste": {"include": [], "exclude": []},
        }
        bw.match_and_score([old], catalog, config)
        self.assertLess(old.score, -10)

    def test_merge_same_work_from_two_sources(self):
        one = bw.Candidate(
            "Example Book",
            ["A. Writer"],
            isbns={"9780123456789"},
            evidence=[{"source": "One", "url": "https://one", "detail": ""}],
        )
        two = bw.Candidate(
            "Example Book",
            ["A Writer"],
            description="A longer description.",
            evidence=[{"source": "Two", "url": "https://two", "detail": ""}],
        )
        merged = bw.merge_candidates([one, two])
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0].evidence), 2)
        self.assertEqual(merged[0].description, "A longer description.")

    def test_report_card_has_cover_filter_data_and_modal_details(self):
        candidate = bw.Candidate(
            "Example Book",
            ["A. Writer"],
            isbns={"9780123456789"},
            series="Example Series",
            description="Full details.",
        )
        card = bw.candidate_card(candidate, "series")
        self.assertIn("images-na.ssl-images-amazon.com/images/P/9780123456789.01.LZZZZZZZ.jpg", card)
        self.assertIn("covers.openlibrary.org/b/isbn/9780123456789-L.jpg", card)
        self.assertIn("data-covers=", card)
        self.assertIn('data-kind="series"', card)
        self.assertIn('aria-label="Relevance score 0"', card)
        self.assertIn("<template>", card)
        self.assertIn('<p class="modal-description">Full details.</p>', card)
        self.assertEqual(bw.isbn10_from_13("9781529340587"), "1529340586")

    def test_report_has_fixed_modal_and_cover_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            path = bw.render_report(
                [bw.Candidate("Example Book", ["A. Writer"], isbns={"9780123456789"}, description="Details")],
                {"report_dir": folder},
                [],
                [],
                1,
                "test catalog",
                [],
                "AI disabled",
            )
            report = path.read_text(encoding="utf-8")
        self.assertIn("height:min(700px,90vh)", report)
        self.assertIn(".modal-description{max-height:180px;overflow-y:auto", report)
        self.assertIn("event.target.naturalWidth <= 1", report)
        self.assertIn('if (image.complete && (image.naturalWidth <= 1', report)
        self.assertIn("not a list index", report)

    def test_report_excludes_unreleased_books(self):
        today = bw.date.today()
        released = bw.Candidate("Released", ["A. Writer"], published_date=today.isoformat())
        future = bw.Candidate("Not Yet Released", ["A. Writer"], published_date=(today + bw.timedelta(days=1)).isoformat())
        with tempfile.TemporaryDirectory() as folder:
            report = bw.render_report(
                [released, future], {"report_dir": folder}, [], [], 1, "test", [], "AI disabled", report_day=today
            ).read_text(encoding="utf-8")
        self.assertIn("Released", report)
        self.assertNotIn("Not Yet Released", report)

    def test_legacy_report_card_can_be_regenerated(self):
        source = '''<section><h2>Series continuations <small>1</small></h2><article>
        <div class="score">175</div><h3>Erebus-13</h3><div class="by">David Wellington</div>
        <div class="meta"><span class="pill">Red Space #3</span><span>2026-07-14</span><span>Orbit</span></div>
        <p>Final mission.</p><div class="reasons">Possible continuation · Forthcoming</div>
        <code>work:9905a025f8084d92</code></article></section>'''
        candidates = bw.parse_report_cards(source, {"work:9905a025f8084d92": {"isbns": ["0316569364"]}})
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].series, "Red Space")
        self.assertEqual(candidates[0].series_index, 3)
        self.assertEqual(candidates[0].isbns, {"0316569364"})
        self.assertTrue(candidates[0].series_alert)

    def test_update_reports_rechecks_calibre_and_missing_covers(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / f"book-watch_{bw.date.today():%Y-%m-%d}_000000.html"
            owned = bw.Candidate("Owned Book", ["A. Writer"])
            available = bw.Candidate("Available Book", ["B. Writer"], isbns={"9780123456789"})
            report.write_text(
                f'''<section><h2>General discovery</h2>
                <article><div class="score">10</div><h3>{owned.title}</h3><div class="by">A. Writer</div><div class="meta"><span>{bw.date.today()}</span></div><code>{owned.key}</code></article>
                <article><img data-covers="[&quot;https://images-na.ssl-images-amazon.com/images/P/9780123456789.01.LZZZZZZZ.jpg&quot;]"><div class="score">10</div><h3>{available.title}</h3><div class="by">B. Writer</div><div class="meta"><span>{bw.date.today()}</span></div><code>{available.key}</code></article>
                </section>''',
                encoding="utf-8",
            )
            config = {
                "report_dir": folder,
                "state_path": str(Path(folder) / "state.sqlite"),
                "library": {"path": folder, "snapshot": str(Path(folder) / "library.json")},
                "sources": {"google_books": True, "open_library": False},
                "run": {"past_days": 550, "future_days": 0},
                "watch": {},
                "taste": {},
            }
            conn = bw.connect_state(config)
            bw.persist_candidates(conn, [owned, available])
            conn.close()
            cover = bw.Candidate(available.title, available.authors, cover_url="https://books.example/cover.jpg")
            args = type("Args", (), {"config": "ignored.toml"})()
            with patch.object(bw, "load_config", return_value=config), patch.object(
                bw, "load_env_file", return_value=False
            ), patch.object(
                bw, "load_calibre", return_value=([{"title": owned.title, "authors": "A. Writer"}], "fresh Calibre export")
            ), patch.object(bw, "google_candidates", return_value=([cover], [])) as request:
                self.assertEqual(bw.update_reports(args), 0)
            updated = report.read_text(encoding="utf-8")
            self.assertNotIn(owned.title, updated)
            self.assertIn(cover.cover_url, updated)
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()

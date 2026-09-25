import http.server
import sqlite3
import subprocess
import io
import tempfile
import threading
import time
import unittest
import os
import json
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import book_watch as bw


class FakeService:
    """Stands in for book writer's AIService: replays replies, records calls."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def generate_content(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply



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

    def shared(self, *replies):
        """Patch book writer's AIService factory; returns (factory mock, fake service)."""
        service = FakeService(replies)
        factory = patch.object(bw, "shared_ai_service", return_value=service)
        mock = factory.start()
        self.addCleanup(factory.stop)
        return mock, service

    def test_openrouter_enriches_every_batch(self):
        candidates = [bw.Candidate(f"Book {number}", ["A. Writer"]) for number in range(13)]
        for candidate in candidates:
            candidate.score = 1
        replies = [json.dumps({"items": [{"id": item.key, "fit_score": 80} for item in batch]})
                   for batch in (candidates[:12], candidates[12:])]
        factory, service = self.shared(*replies)
        config = {
            "openrouter": {"enabled": True, "api_key_env": "BOOK_WATCH_TEST_AI_KEY", "model": "test-model"},
            "taste": {},
        }
        with patch.dict(os.environ, {"BOOK_WATCH_TEST_AI_KEY": "test-key"}):
            catalog = bw.build_catalog([{"title": "Owned", "authors": "A. Writer", "tags": ["Science Fiction"]}])
            status = bw.enrich_with_openrouter(candidates, config, catalog)
        self.assertEqual(len(service.calls), 2)
        self.assertTrue(all(candidate.ai for candidate in candidates))
        self.assertIn("13/13", status)
        self.assertIn('"top_tags": ["Science Fiction"]', service.calls[0][0])
        self.assertEqual(service.calls[0][1], {"model": "test-model", "max_completion_tokens": 2200, "max_retries": 1, "wait_for_limits": False, "temperature": 0.0})
        provider, overrides = factory.call_args.args
        self.assertEqual(provider, "openrouter")
        self.assertEqual(overrides["api_key"], "test-key")
        self.assertEqual(overrides["base_url"], "https://openrouter.ai/api/v1")
        # A report's caps are ceilings on metered gateways, spelled as before.
        self.assertEqual((overrides["token_param"], overrides["cap_is_ceiling"]), ("max_tokens", True))

    def test_openai_oauth_needs_no_api_key(self):
        candidate = bw.Candidate("Book", ["A. Writer"])
        candidate.score = 1
        factory, _service = self.shared(json.dumps({"items": [{"id": candidate.key, "fit_score": 80}]}))
        config = {"openai_oauth": {"model": "gpt-5.4-mini"}, "taste": {}}
        with patch.object(bw, "ensure_openai_oauth_proxy"), patch.object(
            bw, "openai_oauth_models", return_value=["gpt-5.4-mini"]
        ):
            status = bw.enrich_with_openrouter([candidate], config)
        provider, overrides = factory.call_args.args
        self.assertEqual(provider, "openai-oauth")
        self.assertEqual(overrides["base_url"], "http://127.0.0.1:10531/v1")
        self.assertNotIn("api_key", overrides)
        self.assertIn("AI enriched 1/1", status)

    def test_commandcode_defaults_when_no_provider_configured(self):
        candidate = bw.Candidate("Book", ["A. Writer"])
        candidate.score = 1
        factory, service = self.shared(json.dumps({"items": [{"id": candidate.key, "fit_score": 80}]}))
        adapter = {"models": ["deepseek/deepseek-v4-pro"], "writing_model": "deepseek/deepseek-v4-pro"}
        with patch.object(bw, "commandcode_adapter", return_value=adapter):
            status = bw.enrich_with_openrouter([candidate], {"taste": {}})
        # The CLI provider is the default; book writer's service runs it, with no endpoint or key.
        provider, overrides = factory.call_args.args
        self.assertEqual(provider, "commandcode")
        self.assertFalse({"api_key", "base_url"} & set(overrides))
        self.assertEqual(service.calls[0][1]["model"], "deepseek/deepseek-v4-pro")
        self.assertIn("AI enriched 1/1 candidates with deepseek/deepseek-v4-pro", status)

    def test_commandcode_models_come_from_book_writer(self):
        adapter = {"models": ["deepseek/deepseek-v4-pro", "claude-sonnet-5"], "writing_model": "deepseek/deepseek-v4-pro"}
        with patch.object(bw, "commandcode_adapter", return_value=adapter):
            status = bw.ai_providers_status({"ai": {"provider": "commandcode"}})
            models = bw.ai_provider_models({"ai": {"provider": "commandcode"}}, "commandcode")
        self.assertEqual(status["commandcode"]["models"], ["deepseek/deepseek-v4-pro", "claude-sonnet-5"])
        self.assertEqual(models["models"], ["deepseek/deepseek-v4-pro", "claude-sonnet-5"])

    def test_commandcode_is_never_a_silent_fallback(self):
        # A dead hyper key must not start shelling out to the CLI; it falls back
        # exactly as before (here: the OAuth section).
        config = {"ai": {"provider": "hyper"}, "openai_oauth": {"model": "gpt-test"}}
        with patch.dict(os.environ, {}, clear=False), patch.object(bw, "crush_auth_key", return_value=""), patch.object(
            bw, "opencode_auth_key", return_value=""
        ):
            for env_name in ("HYPER_API_KEY", "AW_API_KEY", "OPENCODE_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
                os.environ.pop(env_name, None)
            name, _cfg, oauth = bw.resolve_ai_provider(config)
        self.assertEqual((name, oauth), ("OpenAI OAuth", True))

    def test_ai_chat_routes_through_the_shared_service(self):
        factory, service = self.shared("hello back")
        adapter = {"models": [], "writing_model": "deepseek/deepseek-v4-pro"}
        with patch.object(bw, "commandcode_adapter", return_value=adapter):
            text = bw.ai_chat({"ai": {"provider": "commandcode"}}, "hello", max_tokens=300)
        self.assertEqual(text, "hello back")
        self.assertEqual(factory.call_args.args[0], "commandcode")
        self.assertEqual(service.calls, [("hello", {"model": "deepseek/deepseek-v4-pro", "max_completion_tokens": 300, "max_retries": 1, "wait_for_limits": False, "temperature": 0.0})])

    def test_ai_chat_failure_is_empty_text(self):
        self.shared(RuntimeError("provider down"))
        with patch.dict(os.environ, {"HYPER_API_KEY": "k"}):
            self.assertEqual(bw.ai_chat({"ai": {"provider": "hyper", "model": "m"}}, "hi"), "")

    def test_hyper_defaults_when_explicitly_configured(self):
        candidate = bw.Candidate("Book", ["A. Writer"])
        candidate.score = 1
        factory, service = self.shared(json.dumps({"items": [{"id": candidate.key, "fit_score": 80}]}))
        with patch.dict(os.environ, {"HYPER_API_KEY": "test-key"}), patch.object(
            bw, "openai_oauth_models", return_value=["qwen3.8-flash", "kimi-k3"]
        ):
            status = bw.enrich_with_openrouter([candidate], {"ai": {"provider": "hyper"}, "taste": {}})
        provider, overrides = factory.call_args.args
        self.assertEqual((provider, overrides["base_url"], overrides["api_key"]),
                         ("hyper", "https://hyper.charm.land/v1", "test-key"))
        self.assertEqual(service.calls[-1][1]["model"], "qwen3.8-flash")
        self.assertIn("AI enriched 1/1 candidates with qwen3.8-flash", status)

    def test_anna_validation_uses_the_configured_provider(self):
        import finder_ai

        config = {"ai": {"provider": "hyper", "model": "m"}}
        matches = [{"title": "Voyagers", "author": "Meg Charlton", "link": "x"},
                   {"title": "Cooking Basics", "author": "Someone Else", "link": "y"}]
        reply = 'Here you go:\n```json\n{"title": "Voyagers", "author": "Meg Charlton", "link": "x", "confidence_score": 91}\n```'
        with patch.object(bw, "ai_chat", return_value=reply) as chat:
            found = finder_ai.AIService(config, model="picked").validate_book_match("Voyagers", "Meg Charlton", matches, "", "")
        self.assertEqual(found["link"], "x")
        sent_config, prompt = chat.call_args.args
        self.assertIs(sent_config, config)
        self.assertEqual(chat.call_args.kwargs["model"], "picked")
        self.assertIn("Voyagers", prompt)
        self.assertNotIn("Cooking Basics", prompt)  # fuzzy pre-filter still runs first
        with patch.object(bw, "ai_chat", return_value='{"link": "x", "confidence_score": 40}'):
            self.assertEqual(finder_ai.AIService(config).validate_book_match("Voyagers", "Meg Charlton", matches, "", ""), {})
        with patch.object(bw, "ai_chat", return_value=""):
            self.assertEqual(finder_ai.AIService(config).validate_book_match("Voyagers", "Meg Charlton", matches, "", ""), {})
        # A score the model words or writes as a decimal is read, never a crash.
        for score, found in (("high", False), ("85.5", True), (79.9, False)):
            reply_json = json.dumps({"link": "x", "confidence_score": score})
            with patch.object(bw, "ai_chat", return_value=reply_json):
                result = finder_ai.AIService(config).validate_book_match("Voyagers", "Meg Charlton", matches, "", "")
            self.assertEqual(bool(result), found, score)

    def test_finder_has_no_provider_client_of_its_own(self):
        import finder_ai

        source = Path(finder_ai.__file__).read_text(encoding="utf-8")
        for needle in ("api_key", "requests", "api.openai.com", "chat/completions"):
            self.assertNotIn(needle, source)

    def test_ai_chat_model_override(self):
        _factory, service = self.shared("ok")
        with patch.dict(os.environ, {"HYPER_API_KEY": "k"}):
            bw.ai_chat({"ai": {"provider": "hyper", "model": "configured"}}, "hi", model="chosen")
        self.assertEqual(service.calls[0][1]["model"], "chosen")

    def test_claude_section_stays_an_api_key_provider(self):
        # book-watch's [claude] is Anthropic's API with ANTHROPIC_API_KEY, not the Claude Code
        # CLI that book writer's "claude" provider runs, so it rides the shared OpenAI-compatible client.
        factory, _service = self.shared("{}")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant"}):
            bw.ai_chat({"ai": {"provider": "claude"}}, "hi")
        provider, overrides = factory.call_args.args
        self.assertEqual((provider, overrides["base_url"], overrides["api_key"]),
                         ("openrouter", "https://api.anthropic.com/v1", "sk-ant"))

    def test_html_model_choice_overrides_config(self):
        candidate = bw.Candidate("Book", ["A. Writer"])
        candidate.score = 1
        _factory, service = self.shared(json.dumps({"items": [{"id": candidate.key, "fit_score": 80}]}))
        config = {"_ai_provider": "hyper", "_ai_model": "kimi-k3", "taste": {}}
        with patch.dict(os.environ, {"HYPER_API_KEY": "test-key"}), patch.object(
            bw, "openai_oauth_models", return_value=["qwen3.8-flash", "kimi-k3"]
        ):
            bw.enrich_with_openrouter([candidate], config)
        self.assertEqual(service.calls[-1][1]["model"], "kimi-k3")

    def test_unknown_model_for_a_gateway_is_rejected(self):
        candidate = bw.Candidate("Book", ["A. Writer"])
        candidate.score = 1
        factory, _service = self.shared()
        config = {"_ai_provider": "hyper", "_ai_model": "not-a-model", "taste": {}}
        with patch.dict(os.environ, {"HYPER_API_KEY": "test-key"}), patch.object(
            bw, "openai_oauth_models", return_value=["qwen3.8-flash"]
        ):
            status = bw.enrich_with_openrouter([candidate], config)
        factory.assert_not_called()
        self.assertIn("does not offer: not-a-model", status)

    def test_hyper_falls_back_to_article_writer_key(self):
        candidate = bw.Candidate("Book", ["A. Writer"])
        candidate.score = 1
        factory, _service = self.shared(json.dumps({"items": [{"id": candidate.key, "fit_score": 80}]}))
        os.environ.pop("HYPER_API_KEY", None)
        with patch.dict(os.environ, {"AW_API_KEY": "article-writer-key"}), patch.object(
            bw, "openai_oauth_models", return_value=["qwen3.8-flash"]
        ):
            status = bw.enrich_with_openrouter([candidate], {"ai": {"provider": "hyper"}, "taste": {}})
        self.assertEqual(factory.call_args.args[1]["api_key"], "article-writer-key")
        self.assertIn("AI enriched 1/1", status)

    def test_hyper_falls_back_to_the_crush_login_key(self):
        os.environ.pop("HYPER_API_KEY", None)
        os.environ.pop("AW_API_KEY", None)
        with tempfile.TemporaryDirectory() as folder:
            store = bw.Path(folder) / "crush"
            store.mkdir()
            (store / "crush.json").write_text(json.dumps({"providers": {"hyper": {"api_key": "crush-key"}}}), encoding="utf-8")
            with patch.dict(os.environ, {"LOCALAPPDATA": folder}):
                self.assertEqual(bw.ai_key(bw.AI_PROVIDERS["hyper"]), "crush-key")

    def test_ai_section_provider_beats_the_oauth_section(self):
        candidate = bw.Candidate("Book", ["A. Writer"])
        candidate.score = 1
        factory, _service = self.shared(json.dumps({"items": [{"id": candidate.key, "fit_score": 80}]}))
        config = {"ai": {"provider": "hyper"}, "openai_oauth": {"model": "gpt-5.4-mini"}, "taste": {}}
        with patch.dict(os.environ, {"HYPER_API_KEY": "test-key"}), patch.object(
            bw, "openai_oauth_models", return_value=["qwen3.8-flash"]
        ):
            bw.enrich_with_openrouter([candidate], config)
        self.assertEqual(factory.call_args.args[0], "hyper")

    def test_missing_api_key_names_the_env_var(self):
        # Every provider key must be gone, or the key fallback picks another
        # provider and the message names it instead.
        with patch.dict(os.environ, {}, clear=False):
            for name in ("HYPER_API_KEY", "AW_API_KEY", "OPENCODE_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
                os.environ.pop(name, None)
            candidate = bw.Candidate("Book", ["A. Writer"])
            candidate.score = 1
            with patch.object(bw, "crush_auth_key", return_value=""), patch.object(bw, "opencode_auth_key", return_value=""):
                status = bw.enrich_with_openrouter([candidate], {"_ai_provider": "hyper", "taste": {}})
        self.assertIn("HYPER_API_KEY or AW_API_KEY is not configured", status)

    def test_oauth_models_are_live_and_text_only(self):
        response = {"data": [{"id": "gpt-5.6-sol"}, {"id": "gpt-5.4-mini"}, {"id": "gpt-image-2"}]}
        with patch.object(bw, "open_json_with_deadline", return_value=response):
            models = bw.openai_oauth_models("http://127.0.0.1:10531/v1")
        self.assertEqual(models, ["gpt-5.6-sol", "gpt-5.4-mini"])

    def test_starts_missing_oauth_proxy_with_npx(self):
        shared = bw.book_writer_ai()  # the proxy starter is book writer's, shared across the workspace
        with patch.object(shared, "_openai_oauth_proxy_running", side_effect=[False, True]), patch.object(
            shared.shutil, "which", return_value="npx"
        ), patch.object(shared.subprocess, "run") as run:
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
        _factory, service = self.shared(TimeoutError("slow"), TimeoutError("slow"), TimeoutError("slow"))
        with patch.dict(os.environ, {"BOOK_WATCH_TEST_AI_KEY": "test-key"}):
            status = bw.enrich_with_openrouter([candidate], config)
        self.assertEqual([kwargs["model"] for _prompt, kwargs in service.calls],
                         ["strong-primary", "strong-fallback", "strong-primary"])
        self.assertIn("AI unavailable", status)

    def test_book_watch_sends_no_completion_requests_itself(self):
        source = Path(bw.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/chat/completions", source)

    def test_shared_service_is_book_writers_and_cached(self):
        built = []

        class Service:
            def __init__(self, **kwargs):
                built.append(kwargs)

        module = type("M", (), {"AIService": Service})
        with patch.object(bw, "book_writer_ai", return_value=module), patch.object(
            bw, "book_writer_config_path", side_effect=lambda name: f"/cfg/{name}.json"
        ), patch.dict(bw._shared_services, clear=True):
            first = bw.shared_ai_service("hyper", {"api_key": "k", "timeout": 120})
            again = bw.shared_ai_service("hyper", {"api_key": "k", "timeout": 120})
            other = bw.shared_ai_service("hyper", {"api_key": "k2", "timeout": 120})
        self.assertIs(first, again)
        self.assertIsNot(first, other)
        self.assertEqual(len(built), 2)
        self.assertEqual(built[0]["config_path"], "/cfg/hyper.json")
        self.assertEqual(built[0]["config_overrides"], {"api_key": "k", "timeout": 120})
        self.assertFalse(built[0]["allow_auth_prompt"])
        self.assertEqual(built[0]["client_max_retries"], 0)

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

    def test_progressive_report_reuses_one_file_and_stops_refreshing(self):
        candidate = bw.Candidate("Example Book", ["A. Writer"])
        with tempfile.TemporaryDirectory() as folder:
            first = bw.render_report(
                [candidate], {"report_dir": folder}, [], [], 1, "test", [], "pending", auto_refresh=8
            )
            pending = first.read_text(encoding="utf-8")
            self.assertIn("window.BW_REFRESH=8;", pending)
            self.assertIn('<script id="bw-refresh">', pending)
            # A meta refresh would close an open book and abort a running download.
            self.assertNotIn("http-equiv=\"refresh\"", pending)
            # The refresh merges the fresh HTML into the open page; the reload
            # only remains as the file:// fallback.
            self.assertIn("async function refreshReport()", pending)
            self.assertIn("if (refreshing || busy || dialog.open || document.hidden) return;", pending)
            # The stop check matches the head script by id — the page's own main
            # script contains the string BW_REFRESH and would never stop the timer.
            self.assertIn('if (!fresh.querySelector("#bw-refresh")) stopAutoRefresh();', pending)
            second = bw.render_report(
                [candidate], {"report_dir": folder}, [], [], 1, "test", [], "done", output_path=first
            )
            self.assertEqual(first, second)
            self.assertNotIn("window.BW_REFRESH=", second.read_text(encoding="utf-8"))
            self.assertEqual(len(list(bw.Path(folder).glob("book-watch_*.html"))), 1)

    def test_owned_series_are_rotated_like_authors(self):
        catalog = bw.build_catalog([
            {"title": "Live One", "authors": "A. Writer", "series": "Live Series", "series_index": 1, "pubdate": "2026-01-01"},
            {"title": "Live Two", "authors": "A. Writer", "series": "Live Series", "series_index": 2, "pubdate": "2026-06-01"},
            {"title": "Old One", "authors": "B. Writer", "series": "Old Series", "series_index": 1, "pubdate": "2001-01-01"},
            {"title": "Old Two", "authors": "B. Writer", "series": "Old Series", "series_index": 2, "pubdate": "2002-01-01"},
            {"title": "Done One", "authors": "C. Writer", "series": "Finished Series", "series_index": 1, "pubdate": "2026-05-01"},
            {"title": "Done Two", "authors": "C. Writer", "series": "Finished Series", "series_index": 2, "pubdate": "2026-05-02"},
            {"title": "Only One", "authors": "D. Writer", "series": "Single Series", "series_index": 1, "pubdate": "2026-07-01"},
        ])
        config = {"run": {"series_per_run": 2}, "watch": {"complete_series": ["Finished Series"]}}
        with tempfile.TemporaryDirectory() as folder:
            conn = bw.connect_state({"state_path": str(Path(folder) / "state.sqlite")})
            # Newest owned volume first; a finished series and a one-book series are skipped.
            self.assertEqual(bw.select_series(conn, catalog, config, []), ["Live Series", "Old Series"])
            self.assertEqual(bw.select_series(conn, catalog, config, [], 1), ["Live Series"])
            conn.execute(
                "INSERT INTO series_checks(series_key,series_name,checked_at) VALUES(?,?,?)",
                ("live series", "Live Series", bw.iso_now()),
            )
            conn.commit()
            # Checked series go to the back of the queue on the next run.
            self.assertEqual(bw.select_series(conn, catalog, config, [], 1), ["Old Series"])
            self.assertEqual(bw.select_series(conn, catalog, config, ["Chosen"]), ["Chosen"])
            conn.close()

    def test_only_category_chips_change_the_card_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            report = bw.render_report(
                [bw.Candidate("Example Book", ["A. Writer"])], {"report_dir": folder}, [], [], 1, "test", [], "AI disabled"
            ).read_text(encoding="utf-8")
        # #bulk-download and #ai-rerank share the .filter class for styling; binding them
        # as category filters set `kind` to undefined and hid every card on the page.
        self.assertIn('event.target.closest(".filter[data-filter]")', report)
        self.assertNotIn('document.querySelectorAll(".filter")', report)

    def test_google_candidates_runs_queries_in_parallel_and_keeps_order(self):
        response = json.dumps({"items": [{"volumeInfo": {
            "title": "Example Book",
            "authors": ["A. Writer"],
            "language": "en",
        }}]})
        queries = [(f"label {index}", f"query {index}") for index in range(6)]
        config = {"sources": {"cache_hours": 24, "query_workers": 4}}
        with patch.object(bw, "cached_request", return_value=response) as request:
            found, errors = bw.google_candidates(None, config, queries, False)
        self.assertFalse(errors)
        self.assertEqual(len(request.call_args_list), 6)
        # The pool must not scramble candidate order: each query's finds keep
        # their position in the results.
        self.assertEqual([item.evidence[0]["detail"] for item in found], [label for label, _ in queries])

    def test_open_library_candidates_also_run_through_the_pool(self):
        response = json.dumps({"docs": [{"title": "I, Robot", "author_name": ["Isaac Asimov"], "language": ["eng"], "cover_i": 7}]})
        queries = [(f"label {index}", "author", "Isaac Asimov") for index in range(3)]
        config = {"sources": {"cache_hours": 24, "query_workers": 3}}
        with patch.object(bw, "cached_request", return_value=response):
            found = bw.open_library_candidates(None, config, queries, False)
        self.assertEqual([item.evidence[0]["detail"] for item in found], [label for label, _, _ in queries])

    def test_fetch_cover_looks_up_a_cover_when_the_candidate_has_none(self):
        candidate = bw.Candidate("Example Book", ["A. Writer"])
        match = bw.Candidate("Example Book", ["A Writer"], cover_url="https://books.example/cover.jpg")
        with patch.object(bw, "google_candidates", return_value=([match], [])), patch.object(
            bw, "http_bytes", return_value=b"\xff\xd8\xff\xe0" + b"x" * 3_000
        ) as image:
            body = bw.fetch_cover(candidate, {"sources": {}}, None)
        self.assertTrue(body.startswith(b"\xff\xd8"))
        # The found URLs stay on the candidate so the next run never re-searches.
        self.assertEqual(candidate.cover_urls, ["https://books.example/cover.jpg"])
        image.assert_called_once()

    def test_fetch_cover_without_a_config_only_tries_the_candidates_urls(self):
        candidate = bw.Candidate("Example Book", ["A. Writer"], cover_url="https://books.example/cover.jpg")
        with patch.object(bw, "http_bytes", return_value=b"\x89PNG" + b"x" * 3_000):
            self.assertTrue(bw.fetch_cover(candidate))
        bare = bw.Candidate("Example Book", ["A. Writer"])
        self.assertIsNone(bw.fetch_cover(bare))

    def test_downloads_panel_renders_grouped_color_coded_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            script = bw.render_report(
                [bw.Candidate("Example Book", ["A. Writer"])], {"report_dir": folder}, [], [], 1, "test", [], "AI disabled"
            ).read_text(encoding="utf-8")
        for marker in ('"In progress"', '"Failed"', '"Added to Calibre"', '"Saved, not imported"', "dl-running", "dl-failed", "dl-done", "dl-saved", "dl-queued", "dl-retry", "data-retry"):
            self.assertIn(marker, script)
        # drain() must read the current DOM: a load-time snapshot breaks after
        # the in-place refresh swaps the cards.
        self.assertIn("const box = allPicks().find(item => item.dataset.key === key)", script)

    def test_pending_import_keys_only_list_files_on_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = bw.connect_state({"state_path": str(Path(folder) / "state.sqlite")})
            try:
                saved = Path(folder) / "kept.epub"
                saved.write_bytes(b"PK")
                conn.executemany(
                    "INSERT INTO downloads(candidate_key,added_at,calibre_id,file_path,source,imported) VALUES(?,?,?,?,?,?)",
                    [
                        ("work:kept", bw.iso_now(), None, str(saved), "libgen:x", 0),
                        ("work:gone", bw.iso_now(), None, str(Path(folder) / "missing.epub"), "libgen:x", 0),
                        ("work:done", bw.iso_now(), 5, str(Path(folder) / "done.epub"), "libgen:x", 1),
                    ],
                )
                conn.commit()
                self.assertEqual(bw.pending_import_keys(conn), ["work:kept"])
            finally:
                conn.close()

    def test_retry_pending_imports_reimports_saved_files(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"report_dir": folder, "state_path": str(Path(folder) / "state.sqlite"), "sources": {"cache_hours": 24}, "download": {"dir": folder}}
            conn = bw.connect_state(config)
            try:
                saved = Path(folder) / "kept.epub"
                saved.write_bytes(b"PK")
                conn.execute(
                    "INSERT INTO downloads(candidate_key,added_at,calibre_id,file_path,source,imported) VALUES(?,?,?,?,?,?)",
                    ("work:kept", bw.iso_now(), None, str(saved), "libgen:x", 0),
                )
                conn.commit()
                with patch.object(bw, "download_candidate", return_value=(True, "added to Calibre as book 9")) as fetch:
                    note = bw.retry_pending_imports(conn, config)
                self.assertIn("Import retry: 1/1 added to Calibre", note)
                self.assertEqual(fetch.call_args.args[4], "work:kept")
            finally:
                conn.close()

    def test_retry_pending_imports_reports_the_calibre_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {"report_dir": folder, "state_path": str(Path(folder) / "state.sqlite"), "sources": {"cache_hours": 24}, "download": {"dir": folder}}
            conn = bw.connect_state(config)
            try:
                saved = Path(folder) / "kept.epub"
                saved.write_bytes(b"PK")
                conn.execute(
                    "INSERT INTO downloads(candidate_key,added_at,calibre_id,file_path,source,imported) VALUES(?,?,?,?,?,?)",
                    ("work:kept", bw.iso_now(), None, str(saved), "libgen:x", 0),
                )
                conn.commit()
                locked = "Calibre import failed: the Calibre program (or its content server) is running and holds the library. Close it and Download again"
                with patch.object(bw, "download_candidate", return_value=(False, locked)) as fetch:
                    note = bw.retry_pending_imports(conn, config)
                self.assertIn("Calibre holds the library lock", note)
                self.assertEqual(fetch.call_count, 1)
            finally:
                conn.close()

    def test_cached_categories_apply_without_the_network(self):
        with tempfile.TemporaryDirectory() as folder:
            conn = bw.connect_state({"state_path": str(Path(folder) / "state.sqlite")})
            try:
                known = bw.Candidate("A Categorized Book", ["A. Writer"])
                conn.execute(
                    "INSERT INTO ai_categories(category_key,title,authors,categories,model,categorized_at) VALUES(?,?,?,?,?,?)",
                    (bw.category_key(known.title, known.authors), known.title, json.dumps(known.authors), json.dumps(["Fantasy", "Romance"]), "test-model", bw.iso_now()),
                )
                conn.commit()
                unknown = bw.Candidate("An Uncached Book", ["A. Writer"])
                bw.apply_cached_categories(conn, [known, unknown])
                self.assertEqual(known.categories, ["Fantasy", "Romance"])
                self.assertEqual(unknown.categories, [])
            finally:
                conn.close()

    def test_ai_categorization_fills_candidates_and_the_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {
                "state_path": str(Path(folder) / "state.sqlite"),
                "run": {"past_days": 550, "future_days": 365},
                "watch": {"complete_series": [], "ignore_series": []},
                "taste": {"include": [], "exclude": []},
                "ai": {"provider": "hyper", "model": "test-model"},
            }
            conn = bw.connect_state(config)
            try:
                candidate = bw.Candidate("Star Ship Saga", ["N. Auth"], published_date=str(bw.date.today().year))
                catalog = bw.build_catalog([])
                bw.match_and_score([candidate], catalog, config)
                self.assertGreater(candidate.score, 0)

                def fake_chat(cfg, prompt, **kwargs):
                    self.assertIn("Star Ship Saga", prompt)
                    self.assertIn("Science Fiction", prompt)
                    return json.dumps({"items": [{"id": 0, "categories": ["Science Fiction", "Not In The List"]}]})

                with patch.object(bw, "ai_chat", fake_chat):
                    status = bw.categorize_candidates(conn, [candidate], config)
                self.assertIn("AI categorized 1/1", status)
                # Invented categories are dropped; only the fixed taxonomy survives.
                self.assertEqual(candidate.categories, ["Science Fiction"])
                row = conn.execute(
                    "SELECT categories FROM ai_categories WHERE category_key=?",
                    (bw.category_key(candidate.title, candidate.authors),),
                ).fetchone()
                self.assertEqual(json.loads(row["categories"]), ["Science Fiction"])

                called = []

                def counting_fake(cfg, prompt, **kwargs):
                    called.append(prompt)
                    return json.dumps({"items": []})

                with patch.object(bw, "ai_chat", counting_fake):
                    again = bw.categorize_candidates(conn, [candidate], config)
                self.assertIn("already classified", again)
                self.assertEqual(called, [])
            finally:
                conn.close()

    def test_ai_categorization_failure_is_a_status_not_an_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {
                "state_path": str(Path(folder) / "state.sqlite"),
                "run": {"past_days": 550, "future_days": 365},
                "watch": {"complete_series": [], "ignore_series": []},
                "taste": {"include": [], "exclude": []},
                "ai": {"provider": "hyper", "model": "test-model"},
            }
            conn = bw.connect_state(config)
            try:
                candidate = bw.Candidate("Star Ship Saga", ["N. Auth"], published_date=str(bw.date.today().year))
                bw.match_and_score([candidate], bw.build_catalog([]), config)
                with patch.object(bw, "ai_chat", lambda cfg, prompt, **kwargs: ""):
                    status = bw.categorize_candidates(conn, [candidate], config)
                self.assertIn("unavailable", status)
                self.assertEqual(candidate.categories, [])
            finally:
                conn.close()

    def test_categories_arriving_after_scoring_get_one_taste_pass(self):
        catalog = bw.build_catalog([])
        config = {
            "run": {"past_days": 550, "future_days": 365},
            "watch": {"complete_series": [], "ignore_series": []},
            "taste": {"include": ["space opera", "fantasy"], "exclude": []},
        }
        # The base haystack already matches a taste term, so a naive second pass
        # over title+series+description+subjects+categories would count it twice.
        candidate = bw.Candidate("A Space Opera Book", ["A. Writer"], description="A sprawling space opera.", published_date="2001-01-01")
        bw.match_and_score([candidate], catalog, config)
        base_score = candidate.score
        base_reasons = list(candidate.reasons)

        base_combined = bw.normalize(" ".join([candidate.title, candidate.series, candidate.description, " ".join(candidate.subjects)]))
        candidate.categories = ["Fantasy"]
        full_combined = bw.normalize(" ".join([base_combined, " ".join(candidate.categories)]))
        bw.apply_category_taste_delta(candidate, config["taste"]["include"], config["taste"]["exclude"], base_combined, full_combined)

        # The total equals a single pass over the full haystack: base hit counted once,
        # the new category hit added, no duplicated reason lines.
        reference = bw.Candidate(candidate.title, candidate.authors, description=candidate.description, published_date=candidate.published_date)
        reference.categories = ["Fantasy"]
        bw.match_and_score([reference], catalog, config)
        self.assertEqual(candidate.score, reference.score)
        self.assertNotEqual(candidate.score, base_score)
        self.assertEqual(candidate.reasons.count("Matches: space opera"), 1)
        self.assertEqual(len(candidate.reasons), len(base_reasons) + 1)

    def test_report_card_carries_and_restores_categories(self):
        candidate = bw.Candidate("Example Book", ["A. Writer"], published_date="2001-01-01", categories=["Fantasy", "Romance"])
        card = bw.candidate_card(candidate, "discovery")
        self.assertIn('data-categories="Fantasy,Romance"', card)
        with tempfile.TemporaryDirectory() as folder:
            source = bw.render_report(
                [candidate], {"report_dir": folder}, [], [], 1, "test", [], "AI disabled"
            ).read_text(encoding="utf-8")
            self.assertIn('data-filter="cat:fantasy"', source)
            self.assertIn("matchesCat", source)
            parsed = bw.parse_report_cards(source, {})
        self.assertEqual(parsed[0].categories, ["Fantasy", "Romance"])

    def test_expired_crush_token_is_not_used(self):
        fresh = self._jwt(time.time() + 3600)
        stale = self._jwt(time.time() - 3600)
        self.assertFalse(bw.jwt_expired(fresh))
        self.assertTrue(bw.jwt_expired(stale))
        self.assertFalse(bw.jwt_expired("not-a-jwt"))
        with tempfile.TemporaryDirectory() as folder:
            store = Path(folder) / "crush"
            store.mkdir()
            path = store / "crush.json"
            path.write_text(json.dumps({"providers": {"hyper": {"api_key": stale}}}), encoding="utf-8")
            with patch.dict(os.environ, {"LOCALAPPDATA": folder}):
                self.assertEqual(bw.crush_auth_key("hyper"), "")
                path.write_text(json.dumps({"providers": {"hyper": {"api_key": fresh}}}), encoding="utf-8")
                self.assertEqual(bw.crush_auth_key("hyper"), fresh)

    @staticmethod
    def _jwt(expiry: float) -> str:
        import base64

        body = base64.urlsafe_b64encode(json.dumps({"exp": int(expiry)}).encode()).decode().rstrip("=")
        return f"header.{body}.signature"

    def test_series_topic_reports_volumes_past_the_newest_owned(self):
        catalog = bw.build_catalog([
            {"title": "Backyard Starship", "authors": "J. N. Chaney & Terry Maggert", "series": "Backyard Starship",
             "series_index": 1, "pubdate": "2021-09-01"},
            {"title": "Escape Velocity", "authors": "J. N. Chaney & Terry Maggert", "series": "Backyard Starship",
             "series_index": 33, "pubdate": "2026-07-01"},
        ])
        volumes = [(1.0, "Backyard Starship"), (33.0, "Escape Velocity"), (34.0, "An Ancient Light")]
        formats = set(bw.DEFAULT_FORMATS)
        with tempfile.TemporaryDirectory() as folder:
            conn = bw.connect_state({"state_path": str(Path(folder) / "state.sqlite")})
            with patch.object(bw, "mobilism_series_volumes", return_value=volumes):
                found, note = bw.mobilism_series_continuations(
                    conn, {"mobilism": {}}, "Backyard Starship", bw.MOBILISM_SAMPLE_SEARCH, formats, None, catalog, False
                )
                # A series nobody in the catalog wrote is not this series.
                other = bw.build_catalog([
                    {"title": "Elsewhere", "authors": "Nora Roberts", "series": "Backyard Starship", "series_index": 1},
                    {"title": "Elsewhere 2", "authors": "Nora Roberts", "series": "Backyard Starship", "series_index": 2},
                ])
                stranger, _ = bw.mobilism_series_continuations(
                    conn, {"mobilism": {}}, "Backyard Starship", bw.MOBILISM_SAMPLE_SEARCH, formats, None, other, False
                )
            conn.close()
        self.assertEqual([(item.series_index, item.title) for item in found], [(34.0, "An Ancient Light")])
        self.assertEqual(found[0].series, "Backyard Starship")
        self.assertEqual(found[0].published_date, "2026-08-02")  # the topic's latest post
        self.assertIn("1 volume(s) past #33", note)
        self.assertEqual(stranger, [])

    def test_report_script_parses_and_queues_downloads_outside_the_modal(self):
        with tempfile.TemporaryDirectory() as folder:
            report = bw.render_report(
                [bw.Candidate("Example Book", ["A. Writer"])], {"report_dir": folder}, [], [], 1, "test", [], "AI disabled"
            ).read_text(encoding="utf-8")
            script = report.split("<script>")[-1].split("</script>")[0]
            source = Path(folder) / "report.js"
            source.write_text(script, encoding="utf-8")
            checked = subprocess.run(["node", "--check", str(source)], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        # The fetch is owned by the page queue, so closing the book cannot abort it.
        self.assertIn("function enqueue(keys)", script)
        self.assertIn("async function drain()", script)
        self.assertIn("enqueue([button.dataset.key]);", script)
        self.assertIn("function renderDownloads()", script)
        # Shift-click ticks the range between the last box and this one.
        self.assertIn("if (event.shiftKey && lastPick >= 0)", script)
        self.assertIn("if (!other.disabled && !other.closest(\".book-card\").hidden) other.checked = box.checked;", script)

    def test_cards_are_selectable_for_a_bulk_download(self):
        candidate = bw.Candidate("Example Book", ["A. Writer"])
        card = bw.candidate_card(candidate, "series")
        self.assertIn(f'data-key="{candidate.key}"', card)
        self.assertIn('<input class="pick" type="checkbox"', card)
        with tempfile.TemporaryDirectory() as folder:
            report = bw.render_report(
                [candidate], {"report_dir": folder}, [], [], 1, "test", [], "AI disabled"
            ).read_text(encoding="utf-8")
        self.assertIn('id="bulk-download"', report)
        self.assertIn('data-filter="downloaded"', report)
        self.assertIn("Download selected (", report)

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

    def test_download_adds_kept_candidate_to_calibre_once(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {
                "report_dir": str(Path(folder) / "reports"),
                "state_path": str(Path(folder) / "state.sqlite"),
                "library": {"path": folder, "snapshot": str(Path(folder) / "library.json")},
                "sources": {"cache_hours": 24},
                "download": {"dir": str(Path(folder) / "downloads")},
            }
            wanted = bw.Candidate(
                "Available Book",
                ["B. Writer"],
                series="A Series",
                series_index=2,
                published_date="2025",
                publisher="Orbit",
                description="Blurb.",
                subjects={"science fiction"},
                isbns={"9780123456789"},
            )
            conn = bw.connect_state(config)
            bw.persist_candidates(conn, [wanted])
            conn.execute("INSERT INTO decisions(candidate_key,status,updated_at) VALUES(?,?,?)", (wanted.key, "keep", bw.iso_now()))
            conn.commit()
            conn.close()
            args = type("Args", (), {"config": "ignored.toml", "candidate_id": [], "all_keeps": True, "force": False, "refresh": False, "no_ai": False, "list": False, "limit": 40})()
            row = {"md5": "0" * 32, "extension": "epub", "title": wanted.title, "isbns": set(), "bytes": 2_000_000}
            with patch.object(bw, "load_config", return_value=config), patch.object(
                bw, "libgen_fetch", return_value=(b"PK\x03\x04" + b"x" * 30_000, row)
            ), patch.object(bw, "fetch_cover", return_value=None), patch.object(bw, "run_calibredb") as calibredb:
                calibredb.return_value = "Added book ids: 7"
                self.assertEqual(bw.download(args), 0)
                add_command, set_command = [call.args[0] for call in calibredb.call_args_list]
                # A second run must not download the same book again.
                self.assertEqual(bw.download(args), 0)
                self.assertEqual(calibredb.call_count, 2)
            self.assertIn("--series-index", add_command)
            self.assertEqual(add_command[add_command.index("--authors") + 1], "B. Writer")
            self.assertEqual(add_command[add_command.index("--isbn") + 1], "9780123456789")
            self.assertIn("publisher:Orbit", set_command)
            self.assertIn("pubdate:2025-01-01", set_command)
            self.assertIn("comments:Blurb.", set_command)
            self.assertTrue((Path(folder) / "downloads" / "B. Writer - Available Book.epub").exists())
            conn = bw.connect_state(config)
            stored = conn.execute("SELECT calibre_id,source FROM downloads WHERE candidate_key=?", (wanted.key,)).fetchone()
            conn.close()
            self.assertEqual((stored["calibre_id"], stored["source"]), (7, "libgen:" + "0" * 32))

    def test_calibredb_error_hides_calibres_own_syntax_warning(self):
        stderr = (
            '<stdin>:1: SyntaxWarning: "\\d" is an invalid escape sequence. Such sequences will not work in the '
            'future. Did you mean "\\\\d"? A raw string is also an option.\n'
            "Another calibre program such as calibre-server.exe or the main calibre program is running."
        )
        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)
        with patch.object(bw.subprocess, "run", return_value=fake):
            with self.assertRaises(RuntimeError) as raised:
                bw.run_calibredb(["calibredb", "add", "x.epub"])
        message = str(raised.exception)
        self.assertIn("Another calibre program", message)
        self.assertNotIn("escape sequence", message)

    def test_failed_calibre_import_keeps_the_file_for_a_later_import(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {
                "report_dir": folder,
                "state_path": str(Path(folder) / "state.sqlite"),
                "library": {"path": folder, "snapshot": str(Path(folder) / "library.json")},
                "download": {"dir": str(Path(folder) / "downloads")},
            }
            wanted = bw.Candidate("Fleet of Ghosts", ["G. Author"])
            conn = bw.connect_state(config)
            bw.persist_candidates(conn, [wanted])
            settings = bw.download_settings(config)
            row = {"md5": "0" * 32, "extension": "epub", "title": wanted.title, "isbns": set(), "bytes": 2_000_000}
            locked = RuntimeError(
                "Another calibre program such as calibre-server.exe or the main calibre program is running."
            )
            with patch.object(bw, "libgen_fetch", return_value=(b"PK\x03\x04" + b"x" * 30_000, row)), patch.object(
                bw, "fetch_cover", return_value=None
            ), patch.object(bw, "run_calibredb", side_effect=locked):
                ok, message = bw.download_candidate(conn, config, settings, 24, wanted.key)
            self.assertFalse(ok)
            self.assertIn("Close it and Download again", message)
            saved = conn.execute("SELECT file_path,calibre_id,imported FROM downloads WHERE candidate_key=?", (wanted.key,)).fetchone()
            self.assertIsNotNone(saved)
            self.assertEqual(saved["imported"], 0)
            self.assertTrue(Path(saved["file_path"]).is_file())

            # The retry must import the saved file, not re-fetch from Library Genesis.
            searched = []
            with patch.object(bw, "libgen_fetch", side_effect=lambda *a, **k: searched.append(1)), patch.object(
                bw, "fetch_cover", return_value=None
            ), patch.object(bw, "run_calibredb", return_value="Added book ids: 42"):
                ok, message = bw.download_candidate(conn, config, settings, 24, wanted.key)
            self.assertTrue(ok, message)
            self.assertEqual(searched, [])
            stored = conn.execute("SELECT calibre_id,imported FROM downloads WHERE candidate_key=?", (wanted.key,)).fetchone()
            self.assertEqual((stored["calibre_id"], stored["imported"]), (42, 1))
            conn.close()

    def test_repeated_quota_errors_collapse_into_one_note(self):
        quota = 'Google Books author: X error: HTTP 429 for https://books.example: {"error": {"code": 429}}'
        errors = [quota.replace("author: X", f"author: {name}") for name in ("A", "B", "C", "D")]
        errors.append("Google Books series: Y error: HTTP 503 for https://books.example")
        summarized = bw.summarize_http_errors(errors)
        self.assertEqual(len(summarized), 2)
        self.assertIn("4 of 5 queries failed with HTTP 429", summarized[0])
        self.assertNotIn('{"error"', summarized[0])
        # A handful of errors is already readable; leave it alone.
        self.assertEqual(bw.summarize_http_errors(errors[:2]), errors[:2])

    def test_ai_falls_back_to_a_provider_with_a_usable_key(self):
        def without_keys():
            # patch.dict restores whatever the rest of the suite left behind.
            return patch.dict(os.environ, {}, clear=False)

        def strip_keys():
            for name in ("HYPER_API_KEY", "AW_API_KEY", "OPENCODE_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
                os.environ.pop(name, None)

        config = {"ai": {"provider": "hyper"}, "openai_oauth": {"enabled": True, "model": "gpt-test"}}
        with without_keys():
            strip_keys()
            with patch.object(bw, "crush_auth_key", return_value=""), patch.object(bw, "opencode_auth_key", return_value=""):
                name, cfg, oauth = bw.resolve_ai_provider(config)
            self.assertEqual((name, oauth), ("OpenAI OAuth", True))
            self.assertEqual(cfg["model"], "gpt-test")

        keyed = {"ai": {"provider": "hyper"}, "opencode": {"model": "claude-test"}}
        with without_keys():
            strip_keys()
            os.environ["OPENCODE_API_KEY"] = "present"
            with patch.object(bw, "crush_auth_key", return_value=""):
                name, cfg, oauth = bw.resolve_ai_provider(keyed)
            self.assertEqual((name, oauth), ("opencode", False))
            self.assertEqual(cfg["model"], "claude-test")

        # Nothing configured with a key: the provider (and its clear message) is unchanged.
        bare = {"ai": {"provider": "hyper"}}
        with without_keys():
            strip_keys()
            with patch.object(bw, "crush_auth_key", return_value=""), patch.object(bw, "opencode_auth_key", return_value=""):
                name, cfg, oauth = bw.resolve_ai_provider(bare)
            self.assertEqual((name, oauth), ("hyper", False))
            self.assertEqual(bw.ai_key(cfg), "")

    def test_run_report_seeds_the_report_from_the_previous_run(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {
                "report_dir": str(Path(folder) / "reports"),
                "state_path": str(Path(folder) / "state.sqlite"),
                "library": {"path": folder, "snapshot": str(Path(folder) / "library.json")},
                "sources": {"cache_hours": 24, "google_books": True, "open_library": True, "reactor": True, "hardcover": True},
                "run": {"past_days": 550, "future_days": 365},
                "watch": {"complete_series": [], "ignore_series": []},
                "taste": {"include": [], "exclude": []},
            }
            old = bw.Candidate("Last Weeks Find", ["P. Author"], published_date="2025-06-01")
            conn = bw.connect_state(config)
            bw.persist_candidates(conn, [old])
            conn.execute(
                "INSERT INTO runs(started_at,status,completed_at) VALUES(?,?,?)",
                ("2026-09-20T00:00:00+00:00", "complete", "2026-09-20T01:00:00+00:00"),
            )
            conn.commit()
            conn.close()

            fresh = bw.Candidate("Fresh Find", ["N. Author"], published_date=f"{bw.date.today().year}-0{max(1, bw.date.today().month - 1)}-15")
            args = type("Args", (), {
                "config": "ignored.toml", "like": None, "author": [], "series": [], "genre": [], "ai": None,
                "max_authors": 8, "max_series": 12, "focused": False, "no_network": False, "refresh": False,
                "no_ai": True, "no_serve": True, "port": 8787, "no_open": True,
            })()
            with patch.object(bw, "load_config", return_value=config), patch.object(
                bw, "load_env_file", return_value=False
            ), patch.object(bw, "load_calibre", return_value=([], "test catalog")), patch.object(
                bw, "google_candidates", return_value=([fresh], [])
            ), patch.object(bw, "open_library_candidates", return_value=[]), patch.object(
                bw, "reactor_candidates", return_value=([], [])
            ), patch.object(bw, "hardcover_candidates", return_value=([], "skipped")), patch.object(
                bw, "mobilism_candidates", return_value=([], [])
            ), patch.object(bw, "fill_missing_covers", return_value=(0, 0, [])), patch.object(
                bw, "mobilism_links", return_value="Mobilism: 0 release links"
            ):
                self.assertEqual(bw.run_report(args), 0)
            report = (Path(folder) / "reports" / "latest.html").read_text(encoding="utf-8")
            # Seeded from the previous run before any source answered...
            self.assertIn("Last Weeks Find", report)
            # ...and the refreshed source's finds merged in.
            self.assertIn("Fresh Find", report)

    def test_ai_assist_retries_with_another_title_and_reports_mobilism_outages(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {
                "report_dir": folder,
                "state_path": str(Path(folder) / "state.sqlite"),
                "library": {"path": folder, "snapshot": str(Path(folder) / "library.json")},
                "download": {"dir": str(Path(folder) / "downloads")},
            }
            wanted = bw.Candidate("Voyagers", ["Meg Charlton"])
            conn = bw.connect_state(config)
            bw.persist_candidates(conn, [wanted])
            settings = bw.download_settings(config)
            self.assertTrue(settings["ai_assist"])
            row = {"md5": "0" * 32, "extension": "epub", "title": "Voyagers: A Novel", "isbns": set(), "bytes": 2_000_000}
            searched = []

            def search(_conn, candidate, *args, **kwargs):
                searched.append(candidate.title)
                return (b"PK\x03\x04" + b"x" * 30_000, row) if candidate.title == "Voyagers: A Novel" else None

            with patch.object(bw, "libgen_fetch", side_effect=search), patch.object(
                bw, "ai_title_variants", return_value=["Voyagers: A Novel"]
            ), patch.object(bw, "fetch_cover", return_value=None), patch.object(
                bw, "run_calibredb", return_value="Added book ids: 7"
            ):
                ok, message = bw.download_candidate(conn, config, settings, 24, wanted.key)
            self.assertEqual(searched, ["Voyagers", "Voyagers: A Novel"])
            self.assertTrue(ok, message)

            settings["ai_assist"] = False
            with patch.object(bw, "libgen_fetch", return_value=None), patch.object(
                bw, "mobilism_fetch", side_effect=RuntimeError("HTTP Error 522")
            ), patch.object(bw, "ai_title_variants") as never:
                ok, message = bw.download_candidate(conn, config, settings, 24, wanted.key, force=True)
            conn.close()
            never.assert_not_called()
            self.assertFalse(ok)
            self.assertIn("no usable copy found on https://libgen.li, https://libgen.vg", message)
            self.assertIn("Mobilism unavailable: HTTP Error 522", message)

            # A mirror that timed out was never searched, so it must not be reported
            # as a source that had nothing.
            def unreachable(_conn, _candidate, *args, errors=None, **kwargs):
                for host in bw.LIBGEN_MIRRORS:
                    errors.append(f"{host} did not answer (timed out)")

            conn = bw.connect_state(config)
            with patch.object(bw, "libgen_fetch", side_effect=unreachable), patch.object(
                bw, "mobilism_fetch", return_value=None
            ):
                ok, message = bw.download_candidate(conn, config, settings, 24, wanted.key, force=True)
            conn.close()
            self.assertFalse(ok)
            self.assertIn("no usable copy found on no reachable Library Genesis mirror or Mobilism", message)
            self.assertIn("did not answer (timed out)", message)

    def test_ai_title_variants_keeps_only_other_titles(self):
        candidate = bw.Candidate("Voyagers", ["Meg Charlton"])
        with patch.object(bw, "ai_chat", return_value='{"titles": ["Voyagers", "The Voyagers", "Voyagers: A Novel"]}'):
            self.assertEqual(bw.ai_title_variants(candidate, {}), ["The Voyagers", "Voyagers: A Novel"])
        with patch.object(bw, "ai_chat", return_value=""):
            self.assertEqual(bw.ai_title_variants(candidate, {}), [])

    def test_report_button_downloads_only_with_the_session_token(self):
        with tempfile.TemporaryDirectory() as folder:
            reports = Path(folder) / "reports"
            reports.mkdir()
            (reports / "latest.html").write_text("<html><head></head><body>report</body></html>", encoding="utf-8")
            config = {
                "report_dir": str(reports),
                "state_path": str(Path(folder) / "state.sqlite"),
                "library": {"path": folder, "snapshot": str(Path(folder) / "library.json")},
                "sources": {"cache_hours": 24},
                "download": {"dir": str(Path(folder) / "downloads")},
            }
            conn = bw.connect_state(config)
            conn.execute(
                "INSERT INTO downloads(candidate_key,added_at,calibre_id,file_path,source) VALUES(?,?,?,?,?)",
                ("work:already", bw.iso_now(), 7, "book.epub", "libgen:x"),
            )
            conn.commit()
            conn.close()
            token = "session-token"
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), bw.report_server(config, token))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{httpd.server_address[1]}/"
            try:
                page = urlopen(url, timeout=10).read().decode("utf-8")
                self.assertIn(f'window.BW_TOKEN="{token}"', page)
                self.assertIn('"work:already": "in Calibre as book 7"', page)
                body = json.dumps({"key": "work:abc"}).encode("utf-8")
                with self.assertRaises(HTTPError) as refused:
                    urlopen(Request(url + "download", data=body, headers={"Content-Type": "application/json"}), timeout=10)
                self.assertEqual(refused.exception.code, 403)
                refused.exception.close()
                with patch.object(bw, "download_candidate", return_value=(True, "added to Calibre as book 8")) as fetch:
                    request = Request(url + "download", data=body, headers={"Content-Type": "application/json", "X-Book-Watch-Token": token})
                    result = json.loads(urlopen(request, timeout=30).read())
                self.assertEqual(result, {"ok": True, "message": "added to Calibre as book 8"})
                self.assertEqual(fetch.call_args.args[4], "work:abc")
                with self.assertRaises(HTTPError) as refused:
                    urlopen(url + "downloads", timeout=10)
                self.assertEqual(refused.exception.code, 403)
                refused.exception.close()
                log = json.loads(urlopen(Request(url + "downloads", headers={"X-Book-Watch-Token": token}), timeout=10).read())
                self.assertEqual(log["active"], [])
                self.assertEqual([item["key"] for item in log["recent"]], ["work:already"])
                # The model list of a gateway is only fetched when it is asked for.
                with patch.object(bw, "ai_provider_models", return_value={"models": ["m1"], "error": "", "live": True}) as models:
                    info = json.loads(urlopen(Request(url + "models?provider=openai_oauth", headers={"X-Book-Watch-Token": token}), timeout=10).read())
                self.assertEqual(models.call_args.args[1], "openai_oauth")
                self.assertEqual(info["models"], ["m1"])
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_provider_status_never_starts_the_oauth_proxy(self):
        config = {"openai_oauth": {"model": "gpt-5.6-terra"}, "hyper": {"model": "qwen3.8-flash"}}
        with patch.object(bw, "ensure_openai_oauth_proxy") as proxy, patch.object(bw, "openai_oauth_models") as live:
            status = bw.ai_providers_status(config)
        proxy.assert_not_called()
        live.assert_not_called()
        self.assertEqual(status["openai_oauth"], {"models": ["gpt-5.6-terra"], "error": "", "live": True})
        self.assertEqual(status["hyper"]["models"], ["qwen3.8-flash"])
        with patch.object(bw, "ensure_openai_oauth_proxy") as proxy, patch.object(
            bw, "openai_oauth_models", return_value=["gpt-5.6-terra", "gpt-5.4-mini"]
        ):
            entry = bw.ai_provider_models(config, "openai_oauth")
        proxy.assert_called_once()
        self.assertEqual(entry["models"], ["gpt-5.6-terra", "gpt-5.4-mini"])

    def test_download_list_reports_what_was_fetched(self):
        with tempfile.TemporaryDirectory() as folder:
            config = {
                "report_dir": folder,
                "state_path": str(Path(folder) / "state.sqlite"),
                "library": {"path": folder, "snapshot": str(Path(folder) / "library.json")},
                "download": {"dir": str(Path(folder) / "downloads")},
            }
            candidate = bw.Candidate("Filed Book", ["A. Writer"])
            conn = bw.connect_state(config)
            bw.persist_candidates(conn, [candidate])
            conn.execute(
                "INSERT INTO downloads(candidate_key,added_at,calibre_id,file_path,source) VALUES(?,?,?,?,?)",
                (candidate.key, "2026-08-30T10:00:00+00:00", 12, "book.epub", "libgen:abc"),
            )
            conn.commit()
            self.assertEqual(bw.download_log(conn)[0]["title"], "Filed Book — A. Writer")
            conn.close()
            args = type("Args", (), {"config": "ignored.toml", "candidate_id": [], "all_keeps": False, "force": False,
                                     "refresh": False, "no_ai": False, "list": True, "limit": 40})()
            with patch.object(bw, "load_config", return_value=config), patch("sys.stdout", new=io.StringIO()) as out:
                self.assertEqual(bw.download(args), 0)
            printed = out.getvalue()
        self.assertIn("Filed Book — A. Writer", printed)
        self.assertIn("Calibre #12", printed)
        self.assertIn("1 download on record", printed)

    def test_calibre_books_from_db_matches_calibredb_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp)
            conn = sqlite3.connect(library / "metadata.db")
            conn.executescript(
                """
                CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, series_index REAL, pubdate TEXT, last_modified TEXT);
                CREATE TABLE authors(id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE books_authors_link(id INTEGER PRIMARY KEY, book INTEGER, author INTEGER);
                CREATE TABLE tags(id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE books_tags_link(id INTEGER PRIMARY KEY, book INTEGER, tag INTEGER);
                CREATE TABLE series(id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE books_series_link(id INTEGER PRIMARY KEY, book INTEGER, series INTEGER);
                CREATE TABLE identifiers(id INTEGER PRIMARY KEY, book INTEGER, type TEXT, val TEXT);
                INSERT INTO books VALUES(7, 'Deep Water', 2.0, '2011-06-22 21:42:01.276854+00:00',
                                         '2026-04-21 20:39:33.979269+00:00');
                INSERT INTO books VALUES(8, 'Loose End', 1.0, NULL, NULL);
                INSERT INTO authors VALUES(1, 'Ann Lee'), (2, 'Mather| Matthew');
                INSERT INTO books_authors_link VALUES(1, 7, 1), (2, 7, 2);
                INSERT INTO tags VALUES(1, 'sci-fi'), (2, 'space');
                INSERT INTO books_tags_link VALUES(1, 7, 1), (2, 7, 2);
                INSERT INTO series VALUES(1, 'Tides');
                INSERT INTO books_series_link VALUES(1, 7, 1);
                INSERT INTO identifiers VALUES(1, 7, 'isbn', '9780345448354'), (2, 7, 'mobi-asin', 'abc');
                """
            )
            conn.commit()
            conn.close()
            books = {row["id"]: row for row in bw.calibre_books_from_db(library)}
        self.assertEqual(
            books[7],
            {
                "id": 7,
                "title": "Deep Water",
                "authors": "Ann Lee & Mather, Matthew",
                "series": "Tides",
                "series_index": 2.0,
                "tags": ["sci-fi", "space"],
                "isbn": "9780345448354",
                "identifiers": {"isbn": "9780345448354", "mobi-asin": "abc"},
                "pubdate": "2011-06-22T21:42:01+00:00",
                "last_modified": "2026-04-21T20:39:33+00:00",
            },
        )
        self.assertEqual(books[8]["authors"], "")
        self.assertEqual(books[8]["pubdate"], "")
        catalog = bw.build_catalog(list(books.values()))
        self.assertEqual(catalog["series"]["tides"]["max_index"], 2.0)
        self.assertIn("9780345448354", catalog["isbns"])


if __name__ == "__main__":
    unittest.main()

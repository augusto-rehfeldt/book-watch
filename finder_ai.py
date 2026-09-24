from __future__ import annotations

import json
import re
import time
from typing import Any

import os
from pathlib import Path
try:
    import requests
except ImportError:
    requests = None


def _token_set_ratio(left, right):
    from book_watch import title_similarity
    return int(round(title_similarity(left, right) * 100))


class AIService:
    def __init__(
        self,
        api_key_path: str = "api_key.txt",
        model: str = "gpt-5-mini",
        api_base_url: str = "https://api.openai.com/v1",
        timeout: int = 60,
        validate_connection: bool = False,
        verbose: bool = False,
    ):
        """Small OpenAI chat wrapper with fuzzy pre-filtering and JSON validation."""
        self.model = model
        self.timeout = timeout
        self.api_base_url = api_base_url.rstrip("/")
        self.verbose = verbose
        self.api_key = self._load_api_key(api_key_path)

        if validate_connection:
            self._validate_connection()

    def _load_api_key(self, path: str) -> str:
        if os.getenv('OPENAI_API_KEY'):
            return os.environ['OPENAI_API_KEY']
        if path == 'api_key.txt' and not Path(path).exists():
            path = str(Path(__file__).resolve().parent.parent / 'book_finder' / 'api_key.txt')
        try:
            with open(path, "r", encoding="utf-8") as f:
                key = f.read().strip()
                if not key or key.startswith("#"):
                    raise ValueError("API key is empty or commented out.")
                return key
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"Error: API key file not found at {path}. Please create it and add your key."
            ) from exc
        except IOError as exc:
            raise IOError(f"Error reading API key file at {path}: {exc}") from exc

    def _validate_connection(self) -> None:
        """Perform a lightweight API check after the key is loaded."""
        try:
            response = requests.get(
                f"{self.api_base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ConnectionError(
                f"Failed to connect to OpenAI. Please check your API key and network connection. Error: {exc}"
            ) from exc

    def _pre_filter_matches(
        self,
        title: str,
        author: str,
        potential_matches: list[dict[str, Any]],
        series: str,
    ) -> list[dict[str, Any]]:
        filtered_matches: list[dict[str, Any]] = []
        search_term = title if title and title.lower() != "n/a" else series

        for match in potential_matches:
            title_ratio = _token_set_ratio(search_term, match.get("title", ""))
            author_ratio = _token_set_ratio(author, match.get("author", ""))

            if self.verbose:
                print(
                    f"Pre-filtering Match: '{match.get('title', '')}' by '{match.get('author', '')}' | "
                    f"Title Ratio: {title_ratio}, Author Ratio: {author_ratio}"
                )

            if title_ratio > 70 and author_ratio > 50:
                filtered_matches.append(match)

        return filtered_matches

    def _chat_completion(self, prompt: str, retries: int = 3, backoff_seconds: float = 1.5) -> str:
        url = f"{self.api_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant that responds in JSON."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }

        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                if not content:
                    raise ValueError("OpenAI returned an empty response.")
                return content
            except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
                last_error = exc
                if attempt >= retries:
                    break
                time.sleep(backoff_seconds * attempt)

        assert last_error is not None
        raise RuntimeError(f"Failed to get a valid OpenAI response: {last_error}") from last_error

    def validate_book_match(
        self,
        title: str,
        author: str,
        potential_matches: list[dict[str, Any]],
        series: str,
        index: str,
        confidence_threshold: int = 80,
    ) -> dict[str, Any]:
        if not potential_matches:
            return {}

        filtered_matches = self._pre_filter_matches(title, author, potential_matches, series)
        if not filtered_matches:
            print("No promising matches found after pre-filtering.")
            return {}

        prompt = f"""
        You are a book validation expert with a high standard for accuracy. Your task is to determine if any of the following book listings are a correct match for the book I'm looking for.

        I am looking for:
        - Title: "{title}"
        - Author: "{author}"
        - Series: "{series if series else 'N/A'}"
        - Series index: "{index if index else 'N/A'}"

        Here are the potential matches:
        {filtered_matches}

        Please analyze the titles, authors, and series carefully. A good match must have a very similar title and author.
        - **Confidence Score**: Provide a confidence score (0-100) for how certain you are that the match is correct.
        - **Strict Matching**: Be very strict. If the title and author do not closely match, it's not a good fit.
        - **Series Awareness**: If the book is part of a series, the series name and book number are critical.

        Respond with a JSON object containing the best match ONLY IF the confidence score is {confidence_threshold} or higher. If no match meets this threshold, return an empty JSON object.

        The JSON object should have the following keys: "title", "author", "format", "link", "confidence_score".
        """.strip()

        try:
            content = self._chat_completion(prompt)
            match_data = json.loads(content)
        except json.JSONDecodeError:
            print("Error: Failed to decode JSON from AI response.")
            return {}
        except Exception as exc:
            print(f"Error during AI validation: {exc}")
            return {}

        if match_data and match_data.get("confidence_score", 0) >= confidence_threshold:
            return match_data
        return {}


if __name__ == "__main__":
    try:
        ai_service = AIService(validate_connection=True)
    except (ValueError, FileNotFoundError, ConnectionError) as exc:
        print(f"Initialization Error: {exc}")
    except Exception as exc:
        print(f"An unexpected error occurred during initialization: {exc}")

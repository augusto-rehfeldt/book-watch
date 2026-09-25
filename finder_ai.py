"""Anna's Archive match validation.

Fuzzy pre-filtering happens here; the judgement call goes to book-watch's configured
AI provider (`ai_chat`), which runs on book writer's shared AI suite like every other
AI script in the workspace. No keys, endpoints or HTTP clients of its own.
"""
from __future__ import annotations

from typing import Any


def _token_set_ratio(left, right):
    from book_watch import title_similarity
    return int(round(title_similarity(left, right) * 100))


class AIService:
    def __init__(self, config: dict[str, Any] | None = None, model: str | None = None, verbose: bool = False):
        """`config` is book-watch's loaded config; `model` overrides its provider's model."""
        self.config = config or {}
        self.model = model
        self.verbose = verbose

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

    def validate_book_match(
        self,
        title: str,
        author: str,
        potential_matches: list[dict[str, Any]],
        series: str,
        index: str,
        confidence_threshold: int = 80,
    ) -> dict[str, Any]:
        import book_watch as bw

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
        Respond with JSON only.
        """.strip()

        content = bw.ai_chat(self.config, prompt, max_tokens=1200, model=self.model)
        if not content:
            return {}
        try:
            match_data = bw.parse_json_response(content)
        except ValueError:
            print("Error: Failed to decode JSON from AI response.")
            return {}

        if not isinstance(match_data, dict):
            return {}
        try:
            confidence = float(match_data.get("confidence_score") or 0)
        except (TypeError, ValueError):
            return {}  # a worded score ("high") is no measurable confidence
        return match_data if confidence >= confidence_threshold else {}

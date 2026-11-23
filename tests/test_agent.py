"""
Tests for the AI Taste Concierge agentic workflow (src/agent.py).

These never call the real Anthropic API — a FakeClient stands in for
anthropic.Anthropic so the tests are deterministic, free, and don't require
ANTHROPIC_API_KEY. That lets `pytest` run the same way for everyone, with
or without a key, per the project's reproducibility requirement.
"""

import json

import pytest

from src import agent
from src.recommender import load_songs


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeThinkingBlock:
    """Mirrors the extended-thinking block real Sonnet 5.5 responses can put
    before the text block — content[0] is not reliably the reply text."""

    def __init__(self):
        self.type = "thinking"
        self.thinking = "reasoning about the request..."


class _FakeResponse:
    def __init__(self, text, include_thinking_block=False):
        blocks = [_FakeThinkingBlock()] if include_thinking_block else []
        blocks.append(_FakeTextBlock(text))
        self.content = blocks


class FakeClient:
    """Replays a scripted sequence of replies, one per call to
    client.messages.create(), regardless of the prompt. Pass a (text, bool)
    tuple instead of a bare string to prepend a thinking block."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

        class _Messages:
            def create(inner_self, **kwargs):
                self.calls.append(kwargs)
                if not self._replies:
                    raise AssertionError("FakeClient ran out of scripted replies")
                reply = self._replies.pop(0)
                if isinstance(reply, tuple):
                    return _FakeResponse(*reply)
                return _FakeResponse(reply)

        self.messages = _Messages()


@pytest.fixture
def songs():
    return load_songs("data/songs.csv")


def test_extract_vocab_matches_catalog(songs):
    vocab = agent.extract_vocab(songs)
    assert "pop" in vocab["genres"]
    assert "happy" in vocab["moods"]
    assert all(isinstance(g, str) for g in vocab["genres"])


def test_validate_prefs_fills_safe_defaults(songs):
    vocab = agent.extract_vocab(songs)
    prefs = agent.validate_prefs({}, vocab)
    assert prefs["favorite_genre"] == ""
    assert prefs["favorite_mood"] == ""
    assert prefs["target_energy"] == 0.5
    assert prefs["likes_acoustic"] is False


def test_validate_prefs_clamps_out_of_range_energy(songs):
    vocab = agent.extract_vocab(songs)
    prefs = agent.validate_prefs({"target_energy": 7.0}, vocab)
    assert prefs["target_energy"] == 1.0
    prefs = agent.validate_prefs({"target_energy": -3.0}, vocab)
    assert prefs["target_energy"] == 0.0


def test_validate_prefs_drops_decade_not_in_catalog(songs):
    vocab = agent.extract_vocab(songs)
    prefs = agent.validate_prefs({"favorite_decade": "1800s"}, vocab)
    assert "favorite_decade" not in prefs


def test_parse_request_extracts_json_from_markdown_fence(songs):
    vocab = agent.extract_vocab(songs)
    fenced = "```json\n" + json.dumps(
        {"favorite_genre": "pop", "favorite_mood": "happy", "target_energy": 0.8, "likes_acoustic": False}
    ) + "\n```"
    client = FakeClient([fenced])
    raw = agent.parse_request(client, "fake-model", "upbeat pop", vocab)
    assert raw["favorite_genre"] == "pop"


def test_parse_request_skips_a_leading_thinking_block(songs):
    """Regression test: a live run against claude-sonnet-5-5 returned an
    extended-thinking block before the text block, and content[0].text
    crashed with AttributeError. The text block must be found by type,
    not by position."""
    vocab = agent.extract_vocab(songs)
    good = json.dumps({"favorite_genre": "pop", "favorite_mood": "happy", "target_energy": 0.8, "likes_acoustic": False})
    client = FakeClient([(good, True)])
    raw = agent.parse_request(client, "fake-model", "upbeat pop", vocab)
    assert raw["favorite_genre"] == "pop"


def test_parse_request_retries_once_on_bad_json_then_succeeds(songs):
    vocab = agent.extract_vocab(songs)
    good = json.dumps({"favorite_genre": "lofi", "favorite_mood": "chill", "target_energy": 0.4, "likes_acoustic": True})
    client = FakeClient(["this is not json", good])
    raw = agent.parse_request(client, "fake-model", "chill lofi", vocab)
    assert raw["favorite_genre"] == "lofi"
    assert len(client.calls) == 2


def test_parse_request_raises_after_exhausting_retries(songs):
    vocab = agent.extract_vocab(songs)
    client = FakeClient(["nope", "still nope"])
    with pytest.raises(agent.LLMParseError):
        agent.parse_request(client, "fake-model", "anything", vocab)


def test_evaluate_flags_genre_and_mood_not_in_catalog(songs):
    vocab = agent.extract_vocab(songs)
    prefs = {"favorite_genre": "opera", "favorite_mood": "euphoric", "target_energy": 0.5, "likes_acoustic": False}
    from src.recommender import recommend_songs

    results = recommend_songs(prefs, songs, k=5)
    issues = agent.evaluate(results, prefs, vocab)
    assert any("opera" in issue for issue in issues)
    assert any("euphoric" in issue for issue in issues)


def test_evaluate_passes_a_strong_match(songs):
    vocab = agent.extract_vocab(songs)
    prefs = {"favorite_genre": "pop", "favorite_mood": "happy", "target_energy": 0.8, "likes_acoustic": False}
    from src.recommender import recommend_songs

    results = recommend_songs(prefs, songs, k=5)
    issues = agent.evaluate(results, prefs, vocab)
    assert issues == []


def test_evaluate_does_not_flag_a_genre_that_exists_but_misses_the_top_spot(songs):
    """A song can match the stated genre/mood and still not be the #1 overall
    pick once other preferences are weighed in — that's the scorer working
    as intended (partial credit), not a sign the genre doesn't exist."""
    vocab = agent.extract_vocab(songs)
    prefs = {
        "favorite_genre": "classical",
        "favorite_mood": "intense",
        "target_energy": 0.75,
        "likes_acoustic": True,
        "prefers_vocals": True,
    }
    from src.recommender import recommend_songs

    results = recommend_songs(prefs, songs, k=5)
    assert all(song["genre"] != "classical" for song, _, _ in results[:1])
    issues = agent.evaluate(results, prefs, vocab)
    assert issues == []


def test_run_concierge_requires_a_client(songs):
    with pytest.raises(agent.MissingAPIKeyError):
        agent.run_concierge("anything", songs, client=None)


def test_run_concierge_self_corrects_on_a_weak_first_guess(songs):
    """Simulates the LLM first guessing a genre that doesn't exist in the
    catalog, then correcting itself on the second iteration once it sees
    the agent's self-check flagged the problem."""
    bad_guess = json.dumps(
        {"favorite_genre": "opera", "favorite_mood": "euphoric", "target_energy": 0.5, "likes_acoustic": False}
    )
    good_guess = json.dumps(
        {"favorite_genre": "pop", "favorite_mood": "happy", "target_energy": 0.8, "likes_acoustic": False}
    )
    client = FakeClient([bad_guess, good_guess])

    result = agent.run_concierge("something happy and poppy", songs, client, k=3, max_iters=2)

    assert len(result["trace"]) == 2
    assert result["trace"][0]["issues"]
    assert result["trace"][1]["issues"] == []
    assert result["recommendations"][0][0]["genre"] == "pop"


def test_run_concierge_stops_at_max_iters_even_if_still_weak(songs):
    always_bad = json.dumps(
        {"favorite_genre": "opera", "favorite_mood": "euphoric", "target_energy": 0.5, "likes_acoustic": False}
    )
    client = FakeClient([always_bad, always_bad])

    result = agent.run_concierge("obscure taste", songs, client, k=3, max_iters=2)

    assert len(result["trace"]) == 2
    assert result["trace"][-1]["issues"]

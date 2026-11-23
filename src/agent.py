"""
AI Taste Concierge — an agentic workflow layered on top of the existing
scoring recommender in recommender.py.

A listener types a free-text request ("something upbeat for a workout, not
too mainstream"). The agent:

  1. PLAN  — asks an LLM to translate the request into the structured
             preference dict recommend_songs() already understands,
             grounded in the genres/moods that actually exist in the catalog.
  2. ACT   — runs the existing score_song/recommend_songs engine unchanged.
  3. CHECK — inspects the results for signs the match is weak (nothing in
             the catalog matched the stated genre/mood, or the top score is
             low) and, if so, asks the LLM to revise its guess and tries
             again, up to max_iters times.

This file owns the LLM calls and the retry/guardrail logic. It never
reimplements scoring — recommend_songs() stays the single source of truth
for "what makes a good match."
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.recommender import recommend_songs

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MAX_PARSE_ATTEMPTS = 2  # per iteration, if the LLM replies with bad JSON
WEAK_SCORE_THRESHOLD = 2.0  # below this, we treat the top match as weak

_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "agent.log"


def _get_logger() -> logging.Logger:
    logger = logging.getLogger("music_recommender.agent")
    if not logger.handlers:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


logger = _get_logger()


class AgentError(Exception):
    """Base class for recoverable concierge failures (always caught by main.py)."""


class MissingAPIKeyError(AgentError):
    pass


class LLMParseError(AgentError):
    """The LLM never returned usable JSON after MAX_PARSE_ATTEMPTS tries."""


def extract_vocab(songs: List[Dict]) -> Dict[str, List[str]]:
    """Pulls the real genre/mood/decade/language values out of the catalog
    so the LLM is grounded in what the catalog can actually serve, instead
    of inventing a genre that will silently score zero everywhere."""
    return {
        "genres": sorted({s["genre"] for s in songs}),
        "moods": sorted({s["mood"] for s in songs}),
        "decades": sorted({s.get("release_decade") for s in songs if s.get("release_decade")}),
        "languages": sorted({s.get("language") for s in songs if s.get("language")}),
    }


def _extract_json(text: str) -> Optional[dict]:
    """LLMs sometimes wrap JSON in markdown fences or add a sentence before
    it. Pull out the first {...} block and parse it."""
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fence_match.group(1) if fence_match else text
    brace_match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not brace_match:
        return None
    try:
        return json.loads(brace_match.group(0))
    except json.JSONDecodeError:
        return None


def _call_llm_json(client, model: str, system_prompt: str, user_prompt: str) -> dict:
    """Calls the LLM and retries once if it doesn't return parseable JSON.
    Raises LLMParseError (a guardrail against silently running with junk
    data) only after every attempt fails."""
    last_raw = ""
    for attempt in range(1, MAX_PARSE_ATTEMPTS + 1):
        prompt = user_prompt
        if attempt > 1:
            prompt += (
                "\n\nYour previous reply was not valid JSON. "
                "Respond with ONLY a single JSON object and nothing else."
            )
        try:
            response = client.messages.create(
                model=model,
                max_tokens=500,
                system=system_prompt,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # network/auth/rate-limit errors from the SDK
            logger.error("LLM call failed on attempt %d: %s", attempt, exc)
            raise AgentError(f"The AI service call failed: {exc}") from exc

        # response.content can include non-text blocks (e.g. extended-thinking
        # blocks) before the actual text reply, so find the first text block
        # instead of assuming it's content[0].
        last_raw = next(
            (block.text for block in response.content if getattr(block, "type", None) == "text"),
            "",
        )
        parsed = _extract_json(last_raw)
        if parsed is not None:
            return parsed
        logger.warning("Attempt %d produced unparseable JSON: %r", attempt, last_raw[:200])

    raise LLMParseError(f"Could not get valid JSON from the model. Last reply: {last_raw[:200]!r}")


_PARSE_SYSTEM_PROMPT = """You convert a listener's free-text music request into a strict JSON \
preference object for a recommendation engine. You MUST only use genre/mood/decade/language \
values from the provided catalog vocabulary — never invent a value that isn't listed, pick the \
closest existing one instead. Reply with ONLY a JSON object, no prose, no markdown fences.

JSON schema (all keys required unless marked optional):
{
  "favorite_genre": string, one of the given genres,
  "favorite_mood": string, one of the given moods,
  "target_energy": number from 0.0 (very mellow) to 1.0 (very intense),
  "likes_acoustic": boolean,
  "favorite_decade": string or null (optional, one of the given decades),
  "prefers_popular": boolean or null (optional),
  "favorite_language": string or null (optional, one of the given languages),
  "prefers_vocals": boolean or null (optional),
  "allow_explicit": boolean (optional, default true)
}"""


def parse_request(client, model: str, query: str, vocab: Dict[str, List[str]]) -> dict:
    user_prompt = (
        f"Catalog vocabulary:\n{json.dumps(vocab, indent=2)}\n\n"
        f"Listener request: {query!r}"
    )
    return _call_llm_json(client, model, _PARSE_SYSTEM_PROMPT, user_prompt)


_REVISE_SYSTEM_PROMPT = """You previously guessed preferences for a listener's request but the \
recommendation engine found weak matches. Revise the JSON preference object to fix the listed \
issues, still only using values from the given catalog vocabulary. Reply with ONLY the corrected \
JSON object, no prose, no markdown fences. Use the same schema as before."""


def revise_request(
    client,
    model: str,
    query: str,
    vocab: Dict[str, List[str]],
    previous_prefs: dict,
    issues: List[str],
) -> dict:
    user_prompt = (
        f"Catalog vocabulary:\n{json.dumps(vocab, indent=2)}\n\n"
        f"Listener request: {query!r}\n"
        f"Previous guess: {json.dumps(previous_prefs)}\n"
        f"Problems found: {issues}"
    )
    return _call_llm_json(client, model, _REVISE_SYSTEM_PROMPT, user_prompt)


def validate_prefs(raw: dict, vocab: Dict[str, List[str]]) -> dict:
    """Sanitizes whatever the LLM returned into a dict recommend_songs() can
    safely consume. Never raises — every field has a safe default, so a
    partially-broken LLM reply degrades the match quality instead of
    crashing the app."""
    prefs: Dict = {
        "favorite_genre": str(raw.get("favorite_genre") or "").strip().lower(),
        "favorite_mood": str(raw.get("favorite_mood") or "").strip().lower(),
        "likes_acoustic": bool(raw.get("likes_acoustic", False)),
    }

    try:
        energy = float(raw.get("target_energy", 0.5))
    except (TypeError, ValueError):
        energy = 0.5
    prefs["target_energy"] = max(0.0, min(1.0, energy))

    decade = raw.get("favorite_decade")
    if isinstance(decade, str) and decade in vocab["decades"]:
        prefs["favorite_decade"] = decade

    language = raw.get("favorite_language")
    if isinstance(language, str) and language in vocab["languages"]:
        prefs["favorite_language"] = language

    if isinstance(raw.get("prefers_popular"), bool):
        prefs["prefers_popular"] = raw["prefers_popular"]

    if isinstance(raw.get("prefers_vocals"), bool):
        prefs["prefers_vocals"] = raw["prefers_vocals"]

    prefs["allow_explicit"] = bool(raw.get("allow_explicit", True))

    return prefs


def evaluate(
    results: List[Tuple[Dict, float, str]], prefs: dict, vocab: Dict[str, List[str]]
) -> List[str]:
    """The agent's self-check: does this look like a confident match, or did
    we just run out of signal because the stated genre/mood aren't in the
    catalog at all?

    This checks catalog membership directly (is 'favorite_genre' one of the
    genres that actually exist?) rather than scanning the top result's
    reasons — a song can genuinely match a stated genre/mood and still not
    be the #1 overall pick once other preferences are weighed in, and that's
    the scorer working as intended, not something to flag as an issue."""
    issues: List[str] = []
    if not results:
        issues.append("No songs survived filtering (check allow_explicit).")
        return issues

    top_score = results[0][1]
    if top_score < WEAK_SCORE_THRESHOLD:
        issues.append(f"Top match score ({top_score:.2f}) is weak.")
    if prefs["favorite_genre"] and prefs["favorite_genre"] not in vocab["genres"]:
        issues.append(f"Genre '{prefs['favorite_genre']}' doesn't exist in the catalog.")
    if prefs["favorite_mood"] and prefs["favorite_mood"] not in vocab["moods"]:
        issues.append(f"Mood '{prefs['favorite_mood']}' doesn't exist in the catalog.")
    return issues


def run_concierge(
    query: str,
    songs: List[Dict],
    client,
    model: str = DEFAULT_MODEL,
    k: int = 5,
    max_iters: int = 2,
) -> dict:
    """Runs the plan -> act -> check -> (revise ->) loop and returns the
    final recommendations plus a trace of every iteration, for transparency."""
    if client is None:
        raise MissingAPIKeyError(
            "No Anthropic client was provided. Set ANTHROPIC_API_KEY to use --ask."
        )

    vocab = extract_vocab(songs)
    prefs: Optional[dict] = None
    results: List[Tuple[Dict, float, str]] = []
    issues: List[str] = []
    trace: List[dict] = []

    logger.info("Starting concierge run for query=%r", query)

    for iteration in range(1, max_iters + 1):
        try:
            if prefs is None:
                raw = parse_request(client, model, query, vocab)
            else:
                raw = revise_request(client, model, query, vocab, prefs, issues)
        except AgentError as exc:
            logger.error("Iteration %d aborted: %s", iteration, exc)
            trace.append({"iteration": iteration, "error": str(exc)})
            break

        prefs = validate_prefs(raw, vocab)
        results = recommend_songs(prefs, songs, k=k)
        issues = evaluate(results, prefs, vocab)

        logger.info(
            "Iteration %d: prefs=%s top_score=%s issues=%s",
            iteration,
            prefs,
            results[0][1] if results else None,
            issues,
        )
        trace.append(
            {
                "iteration": iteration,
                "prefs": dict(prefs),
                "top_score": results[0][1] if results else None,
                "issues": list(issues),
            }
        )

        if not issues:
            break

    return {"prefs": prefs, "recommendations": results, "trace": trace, "vocab": vocab}

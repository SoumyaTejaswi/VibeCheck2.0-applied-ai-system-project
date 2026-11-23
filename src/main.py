"""
Command line runner for the Music Recommender Simulation.

Two ways to get recommendations:

1. Default (no flags) — the original structured-profile demo, scored by
   recommender.py's deterministic rule engine. No API key needed.
2. `--ask "<free text>"` — the AI Taste Concierge: an agentic workflow
   (src/agent.py) that uses Claude to turn your request into that same
   preference format, runs it through the same scorer, checks whether the
   match is actually good, and revises/retries on its own before showing
   you the final picks. Requires ANTHROPIC_API_KEY (see README).
"""

import argparse
import os
import sys

from src.recommender import load_songs, recommend_songs

try:
    import anthropic
except ImportError:  # pragma: no cover - exercised only when dependency missing
    anthropic = None

from src import agent


def print_recommendations(recommendations) -> None:
    print("\nTop Recommendations")
    print("=" * 40)
    if not recommendations:
        print("\nNo songs matched — try loosening your preferences.")
        return
    for rank, (song, score, explanation) in enumerate(recommendations, start=1):
        print(f"\n{rank}. {song['title']} ({score:.2f} pts)")
        reasons = explanation.split("; ") if explanation else []
        if reasons:
            for reason in reasons:
                print(f"   - {reason}")
        else:
            print("   - No strong matches with your preferences")
    print()


def run_structured_demo(songs) -> None:
    user_prefs = {
        "favorite_genre": "pop",
        "favorite_mood": "happy",
        "target_energy": 0.8,
        "likes_acoustic": False,
    }
    print(f"User profile: {user_prefs}")
    print_recommendations(recommend_songs(user_prefs, songs, k=5))


def run_concierge_demo(songs, query: str, k: int, max_iters: int) -> int:
    """Returns a process exit code (0 on success, 1 on a handled failure)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic is None:
        print(
            "\nThe 'anthropic' package isn't installed. Run "
            "`pip install -r requirements.txt` and try again.",
            file=sys.stderr,
        )
        return 1
    if not api_key:
        print(
            "\nANTHROPIC_API_KEY is not set, so the AI Taste Concierge can't run.\n"
            "Set it in your shell (export ANTHROPIC_API_KEY=sk-...) or in a .env "
            "file, then re-run with --ask. See README.md for setup steps.",
            file=sys.stderr,
        )
        return 1

    client = anthropic.Anthropic(api_key=api_key)

    print(f"\nListener request: {query!r}")
    try:
        result = agent.run_concierge(query, songs, client, k=k, max_iters=max_iters)
    except agent.AgentError as exc:
        print(f"\nThe AI Taste Concierge couldn't complete your request: {exc}", file=sys.stderr)
        print("See logs/agent.log for details.", file=sys.stderr)
        return 1

    print("\nAgent trace")
    print("=" * 40)
    for step in result["trace"]:
        if "error" in step:
            print(f"Iteration {step['iteration']}: failed — {step['error']}")
            continue
        print(f"Iteration {step['iteration']}: prefs={step['prefs']}")
        print(f"   top score: {step['top_score']}")
        if step["issues"]:
            print(f"   issues found, retrying: {step['issues']}")
        else:
            print("   looks good, stopping")

    print_recommendations(result["recommendations"])
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Music Recommender Simulation")
    parser.add_argument(
        "--ask",
        metavar="TEXT",
        help="Describe what you want in plain language and let the AI Taste "
        "Concierge (agentic workflow) find it, instead of the structured demo profile.",
    )
    parser.add_argument("--k", type=int, default=5, help="How many songs to recommend (default 5).")
    parser.add_argument(
        "--max-iters",
        type=int,
        default=2,
        help="Max self-correction attempts for the AI Taste Concierge (default 2).",
    )
    return parser


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    args = build_arg_parser().parse_args()
    songs = load_songs("data/songs.csv")

    if args.ask:
        exit_code = run_concierge_demo(songs, args.ask, args.k, args.max_iters)
        sys.exit(exit_code)

    run_structured_demo(songs)


if __name__ == "__main__":
    main()

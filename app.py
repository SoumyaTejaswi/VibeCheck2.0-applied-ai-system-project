"""
Streamlit front end for the Music Recommender Simulation.

Run with:
    streamlit run app.py

Two tabs:
- "Describe what you want" — the AI Taste Concierge, an agentic workflow
  (src/agent.py) that turns a free-text request into the recommender's
  preference format, scores it with the existing rule engine, checks the
  results, and self-corrects before showing you anything.
- "Set preferences manually" — the original structured-profile scorer,
  with no AI involved, for comparison.
"""

import os

import streamlit as st

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from src import agent
from src.recommender import load_songs, recommend_songs

st.set_page_config(page_title="Music Recommender", page_icon="🎵")


@st.cache_data
def get_songs():
    return load_songs("data/songs.csv")


def render_recommendations(recommendations) -> None:
    if not recommendations:
        st.warning("No songs matched — try loosening your preferences.")
        return
    for rank, (song, score, explanation) in enumerate(recommendations, start=1):
        reasons = explanation.split("; ") if explanation else []
        with st.container(border=True):
            st.markdown(f"**{rank}. {song['title']}** — {song['artist']} ({score:.2f} pts)")
            if reasons:
                for reason in reasons:
                    st.write(f"- {reason}")
            else:
                st.write("- No strong matches with your preferences")


def get_client():
    """Reads the API key from the environment, or lets the user paste one
    into the sidebar for a local demo. Never logged or persisted."""
    api_key = os.environ.get("ANTHROPIC_API_KEY") or st.session_state.get("api_key", "")
    if not api_key:
        return None, None
    try:
        import anthropic
    except ImportError:
        return None, "missing_package"
    return anthropic.Anthropic(api_key=api_key), None


st.title("🎵 Music Recommender Simulation")

with st.sidebar:
    st.subheader("Anthropic API key")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        st.session_state["api_key"] = st.text_input(
            "ANTHROPIC_API_KEY", type="password", help="Only used for this session, never saved."
        )
    else:
        st.caption("Loaded from the ANTHROPIC_API_KEY environment variable.")

songs = get_songs()

tab_ai, tab_manual = st.tabs(["Describe what you want", "Set preferences manually"])

with tab_ai:
    st.caption(
        "AI Taste Concierge — an agentic workflow. Claude parses your request, the same "
        "scoring engine as the manual tab ranks songs, and the agent checks its own results, "
        "revising and re-running if the match looks weak."
    )
    query = st.text_area(
        "What do you want to hear?",
        placeholder="Something upbeat for a workout, but nothing too mainstream.",
    )
    k = st.slider("How many recommendations?", 1, 10, 5, key="ai_k")
    max_iters = st.slider("Max self-correction attempts", 1, 4, 2, key="ai_iters")

    if st.button("Find my music", type="primary"):
        client, client_error = get_client()
        if client_error == "missing_package":
            st.error("The 'anthropic' package isn't installed. Run `pip install -r requirements.txt`.")
        elif client is None:
            st.error(
                "No Anthropic API key found. Set ANTHROPIC_API_KEY in your environment, "
                "or paste one in the sidebar."
            )
        elif not query.strip():
            st.warning("Type a request first.")
        else:
            with st.spinner("Thinking..."):
                try:
                    result = agent.run_concierge(query, songs, client, k=k, max_iters=max_iters)
                except agent.AgentError as exc:
                    st.error(f"The AI Taste Concierge couldn't complete your request: {exc}")
                    st.caption("See logs/agent.log for details.")
                else:
                    with st.expander("Agent trace (what it tried)"):
                        for step in result["trace"]:
                            if "error" in step:
                                st.write(f"Iteration {step['iteration']}: failed — {step['error']}")
                                continue
                            st.write(f"**Iteration {step['iteration']}**: `{step['prefs']}`")
                            st.write(f"Top score: {step['top_score']}")
                            if step["issues"]:
                                st.write(f"Issues found, retrying: {step['issues']}")
                            else:
                                st.write("Looks good, stopping.")
                    render_recommendations(result["recommendations"])

with tab_manual:
    st.caption("No AI involved — the original deterministic scoring rule, for comparison.")
    col1, col2 = st.columns(2)
    with col1:
        genres = sorted({s["genre"] for s in songs})
        moods = sorted({s["mood"] for s in songs})
        favorite_genre = st.selectbox("Favorite genre", genres, key="manual_genre")
        favorite_mood = st.selectbox("Favorite mood", moods, key="manual_mood")
    with col2:
        target_energy = st.slider("Target energy", 0.0, 1.0, 0.8, key="manual_energy")
        likes_acoustic = st.checkbox("Likes acoustic sound", value=False, key="manual_acoustic")

    manual_k = st.slider("How many recommendations?", 1, 10, 5, key="manual_k")

    if st.button("Get recommendations"):
        user_prefs = {
            "favorite_genre": favorite_genre,
            "favorite_mood": favorite_mood,
            "target_energy": target_energy,
            "likes_acoustic": likes_acoustic,
        }
        render_recommendations(recommend_songs(user_prefs, songs, k=manual_k))

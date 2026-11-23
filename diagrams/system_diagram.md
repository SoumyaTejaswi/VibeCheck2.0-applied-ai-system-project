# System Diagram — AI Taste Concierge

This diagram covers the agentic workflow added in `src/agent.py` and how it's
wired into the rest of the app (`src/main.py`, `app.py`, `src/recommender.py`).

## Main components

- **Entry point** (`CLI` / `UI`) — `python -m src.main --ask` and the
  Streamlit "Describe what you want" tab. Both call the same
  `agent.run_concierge()`.
- **Vocab extractor** — reads the real genres/moods/decades/languages out of
  `data/songs.csv` so the agent can only ground its guesses in things the
  catalog actually has.
- **Agent (Plan → Act → Check → Revise)** — the core loop:
  - *Plan* (`parse_request`): Claude turns free text into a structured
    preference dict.
  - *Act* (`recommend_songs`): the same deterministic scorer the manual tab
    uses — this is the "retriever/ranker" component, unchanged by the AI
    layer.
  - *Check* (`evaluate`): a self-check that flags weak or unmatched results.
  - *Revise* (`revise_request`): asks Claude to correct its guess when
    `Check` finds a problem, then loops back to *Act* (bounded by
    `max_iters`).
- **Guardrails** — missing API key, bad/unparseable LLM output, and API
  errors are all caught, logged, and surfaced as a plain message instead of
  a crash.
- **Logs** — every iteration (prefs tried, score, issues) is written to
  `logs/agent.log`.
- **Human / testing checkpoints**:
  - The **listener** reads the iteration trace and final picks in the
    CLI/UI and judges whether they're actually good — the loop stops at
    `max_iters` specifically so a human always gets to see the result and
    decide, rather than the agent silently retrying forever.
  - The **manual tab** runs the identical scorer with human-set
    sliders/dropdowns and no AI step, giving a non-AI baseline a person can
    compare the AI path's picks against.
  - **`tests/test_agent.py`** scripts a `FakeClient` with canned LLM replies
    so `Plan`/`Check`/`Revise` are verified automatically, offline, on
    every test run — no live API call required to catch a regression.

## Data flow (input → process → output)

Free-text request in → agent plans structured preferences, grounded in the
real catalog vocabulary → the existing deterministic scorer ranks every
song against those preferences → the agent checks its own ranking and
revises/retries if it looks weak → ranked songs + plain-language reasons +
a full iteration trace come out, shown to the listener and logged to disk.

## Diagram

```mermaid
flowchart TD
    User(["Listener types a free-text request<br/>e.g. 'upbeat songs for a workout'"])
    Entry["Entry point<br/>CLI: python -m src.main --ask<br/>UI: Streamlit 'Describe what you want' tab"]
    Vocab["extract_vocab()<br/>real genres / moods / decades / languages<br/>read live from data/songs.csv"]

    User --> Entry --> Vocab

    subgraph AgentLoop["Agentic workflow — src/agent.py: run_concierge()"]
        direction TB
        Plan["PLAN<br/>parse_request()<br/>Claude turns the request into a<br/>preference dict, grounded in Vocab"]
        Act["ACT<br/>recommend_songs()<br/>same deterministic scorer the<br/>manual tab uses"]
        Check{"CHECK<br/>evaluate()<br/>genre / mood matched?<br/>top score strong enough?"}
        Revise["REVISE<br/>revise_request()<br/>Claude is told what was wrong,<br/>returns a corrected guess"]
        Plan --> Act --> Check
        Check -->|"issues found, under max_iters"| Revise --> Act
    end

    Vocab --> Plan

    Output["Output<br/>ranked recommendations + reasons<br/>+ full iteration trace"]
    Errors["Guardrails<br/>missing API key / unparseable LLM output /<br/>API errors — caught + logged,<br/>shown as a plain message, never a crash"]
    Logs[("logs/agent.log<br/>every iteration: prefs, score, issues")]

    Check -->|"looks good, or max_iters reached"| Output
    Plan -. on failure .-> Errors
    Revise -. on failure .-> Errors
    AgentLoop -.-> Logs

    Output --> Entry
    Errors --> Entry

    Listener{{"Human review<br/>listener reads the trace + results<br/>and judges whether the picks are good"}}
    Manual["Manual tab (no AI)<br/>same recommend_songs(), human-set<br/>sliders/dropdowns — a baseline to<br/>compare the AI path against"]
    Tests["tests/test_agent.py<br/>FakeClient scripts canned LLM replies<br/>so Plan / Check / Revise are verified<br/>offline, deterministically, every run"]

    Entry --> Listener
    Entry --> Manual --> Act
    Tests -. exercises offline, no network .-> AgentLoop
```

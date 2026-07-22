# Asendia — Real-Time AI Voice Interviewer

A voice-only AI interviewer that conducts ML/AI job interviews in the browser in
real time. The candidate joins a call, the mic stays open, and **"Alex"** — a
senior AI/ML engineer — greets them, asks questions, catches bluffs, and wraps up
on his own. You can **talk over him any time** (barge-in) and he stops and
listens, exactly like a real phone call. When it's done, a single scoring pass
produces a scored report.

This is a ground-up rebuild of an earlier cascaded stack (STT → LLM → TTS wired up
by hand). That version chased sub-second latency by
*hiding* an unavoidable ~0.8 s generation gap with layered tricks. This one uses a
single **duplex speech-to-speech** model (Gemini Live): it hears the candidate and
streams speech back through one API, so there's no client-side STT or TTS for us to
run and no per-turn eval loop on the critical path. Google runs the speech pipeline
server-side; we just stream audio in and out.

> **Version:** V4 — Duplex speech-to-speech rebuild. Single Gemini key, one live
> connection, one final scoring call.
> **Stack:** FastAPI + WebSocket | **Gemini Live** (speech-to-speech conversation) |
> **Gemini Flash** (structured-JSON scoring) | Supabase

---

## Highlights

- **Duplex, human-timed conversation** — Gemini Live ingests the candidate's audio
  continuously and replies *as speech*. No release-to-transcribe wait, no
  generate-then-speak wait: Alex responds with true conversational timing.
- **Barge-in (interruptible)** — the mic is always open. Cut Alex off mid-sentence
  and the client **flushes every queued audio buffer instantly** on the
  `interrupted` signal, so he shuts up the moment you start.
- **Conversation / scoring split** — Gemini Live owns the whole live conversation
  and its own follow-ups; a single Gemini Flash call at the end owns the scored
  judgment. The scorer never sits on the critical path — no per-turn eval loop
  slows the talk.
- **One key, free tier** — the **same** `GEMINI_API_KEY` powers both the live
  conversation and the final scoring. No STT key, no TTS key, no separate
  reasoning provider. The whole system is one live socket + one JSON call.
- **Alex ends it himself** — he decides when he has enough signal and calls an
  `end_interview` tool *after* speaking his closing out loud. Manual **End** button
  and a hard turn-cap are always-on backstops.
- **ML-specific bluff detection** — the persona ships six ML/AI bluff patterns
  (layer/concept mix-ups, impossible combinations, metric confusion,
  overfitting muddle, buzzword strings, numbers that don't add up) and is told to
  name the error, correct it in one line, and ask what they meant.
- **Resilient by design** — reconnect loop with exponential backoff + **session
  resumption** (context preserved across a dropped socket), **context-window
  compression** for long calls, a **tool-fallback** if a model rejects tools at
  connect, and a **neutral-report** fallback so scoring can never crash the session.
- **Config-driven** — model ids, voice, sample rate, turn caps, and VAD tuning all
  live in one committed file (`config.py`); only API keys live in `.env`.
- **Real-audio wave UI** — cream/terracotta, dark-mode aware. Two morphing
  waveforms driven by `AnalyserNode`: Alex's 24 kHz output and the candidate's
  live mic. Live state (Listening / Thinking / Speaking) is derived from the audio
  each frame.

---

## How It Works

```
Browser (WebSocket, mic always open)
    │
    │  AudioWorklet: mic → 16 kHz Int16 PCM, ~64 ms chunks
    │      │
    │      ▼  binary frames
  FastAPI /ws  ──── send_realtime_input(PCM) ────▶  Gemini Live session
    │                                                    │
    │                                          half-cascade S2S model:
    │                                          hears candidate, streams speech back
    │                                                    │
    │  ◀──── 24 kHz PCM audio ────────────────────────────┤
    │  ◀──── input_transcript  (what the candidate said) ─┤   (both sides captured
    │  ◀──── output_transcript (what Alex said) ──────────┤    for the scoring pass)
    │  ◀──── interrupted  (candidate barged in) ──────────┤
    │  ◀──── turn_complete ───────────────────────────────┤
    │  ◀──── tool_call: end_interview ────────────────────┤
    │  ◀──── resumption handle / go_away ─────────────────┘
    │
    │  Gapless Web Audio playback (nextStartTime cursor); on {interrupted}
    │  every scheduled AudioBufferSourceNode is stopped → instant cut-off.
    │
    │  Wave viz: orange output wave when Alex speaks, mic wave when you speak.
    │
    │  [end: Alex's tool call, manual End, turn-cap, or disconnect]
    │        │
    │        ▼
    │   TranscriptBuilder.finalize()  → ordered [{role, text}, …]
    │        │
    │        ▼
    │   Gemini Flash — ONE structured-JSON call → scored report
    │        │
    │        ▼
    │   Supabase (interviews table)  ──▶  {type: report} back to the browser
```

### Conversation vs. scoring split

Two models, two jobs, deliberately decoupled:

| | **Conversation** (Gemini Live) | **Scoring** (Gemini Flash) |
|---|---|---|
| When | Live, the whole call | Once, at the end |
| Input | Streamed candidate audio | The full text transcript |
| Output | Alex's spoken audio | Structured JSON report |
| Sees the persona? | Yes (`system_instruction`) | No — scores blind on evidence |
| On the critical path? | Yes (it *is* the conversation) | No (post-call) |

The old cascaded design ran an eval **every turn** to steer the next follow-up,
which put an LLM call on the latency-critical path of every answer. Here the live
model handles the follow-ups itself in-context (it's a capable model with the
persona and bluff framework in its system prompt), so the only structured judgment
is a single post-call pass. Nothing about scoring can slow the conversation.

### Catching bluffs

Alex is told to treat bluff detection as the whole point. The persona
(`persona.py`) enumerates the patterns and the response shape — name the specific
error, correct it in one line, ask what they actually meant, stay kind:

| Bluff pattern | Example |
|---|---|
| Layer/concept mix-ups | calling a tokenizer a "model"; confusing embeddings with fine-tuning; "training" when they mean prompting/inference |
| Impossible combinations | tools/architectures glued together in ways that can't work |
| Metric confusion | precision vs recall; accuracy on imbalanced data; loss vs metric; AUC misread |
| Overfitting/regularization muddle | "dropout prevents overfitting" with no idea why; bias/variance reversed |
| Buzzword strings | real terms in nonsensical order — "we used RAG to fine-tune the embeddings in the transformer's attention" |
| Numbers that don't add up | "we ran a 70B model in real time on a laptop"; impossible latency/scale |

### Autonomous ending

Alex owns when the interview ends. He has one tool, `end_interview`, and is
instructed to **speak his warm closing out loud first**, then call it. The spoken
closing streams ahead of the tool call, so it's already playing in the browser by
the time the server sees the call and finalizes.

| Trigger | `reason` | Guard |
|---|---|---|
| Enough signal to evaluate fairly (usually 5–8 solid questions) | `satisfied` / `enough_signal` | — |
| Candidate keeps bluffing after a fair shot | `persistent_bluffing` | not before `MIN_TURNS_BEFORE_EARLY_END` (4) |
| Candidate clearly disengaged / won't answer | `candidate_disengaged` | — |

Backstops if the tool never fires: the manual **End** button, and a hard
`MAX_ALEX_TURNS` (12) turn-cap in the server that force-ends and still produces a
report. If a Live model rejects tools at connect, the server retries **without**
the tool and falls back to End + the turn-cap — the interview always works.

### Resilience

The Gemini session runs inside a reconnect loop (`server._run_interview`):

- **Session resumption** — Gemini emits a resumption handle; on a drop we reconnect
  *with the handle* and the conversation continues with context intact. If a resume
  fails, we drop the handle and reconnect fresh (greeting only once).
- **Exponential backoff** — up to `MAX_RECONNECTS` (5), capped at 4 s between tries.
- **`go_away` handling** — when Gemini signals it's about to close, we return and
  the outer loop reconnects with the saved handle before the socket dies.
- **Context-window compression** — a sliding window keeps long interviews under the
  model's context limit automatically.
- **Neutral-report fallback** — any scoring failure returns a safe neutral report
  instead of raising, so a bad final call never crashes the session.

---

## Setup

**Requirements:** Python 3.10+, a **Gemini API key** ([AI Studio](https://aistudio.google.com/apikey)),
and a Supabase project.

```bash
cd app
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux

cp .env.example .env      # then fill in GEMINI_API_KEY (Supabase values may
                          # already be set from the old project)
```

### Run

```bash
.venv\Scripts\python server.py       # or: uvicorn server:app --reload
```

Open **http://localhost:8000** → click **Join Interview** → allow the microphone →
talk to Alex. Interrupt him whenever you like. Click **End** (or let him wrap up on
his own) to get the scored report.

> Mic capture (`getUserMedia` + AudioWorklet) requires a **secure context** —
> `localhost` counts. Behind a proxy or on a LAN IP, serve over **HTTPS** or the
> mic won't initialize.

### Configuration vs. secrets

The two are deliberately separated (same discipline as the old build):

- **`config.py`** (committed) — *which* models, *which* voice, and interview shape.
  Swapping the Live model or the voice is a one-line edit here, no code changes:
  ```python
  GEMINI_LIVE_MODEL = "gemini-3.1-flash-live-preview"  # the conversation
  GEMINI_VOICE      = "Charon"                          # prebuilt voice for Alex
  SCORING_MODEL     = "gemini-flash-latest"             # the final scoring call
  ```
- **`.env`** (git-ignored) — **API keys only**, never model names:

  | Variable | Required | What for |
  |---|---|---|
  | `GEMINI_API_KEY` | yes | **Both** the live conversation and the scoring call |
  | `SUPABASE_URL` / `SUPABASE_KEY` | yes | Report persistence (`interviews` table) |

  `GOOGLE_API_KEY` is accepted as an alias for `GEMINI_API_KEY`.

> Live model ids are **preview ids and they rotate.** Confirm the current one in
> Google AI Studio (`client.models.list()`) if a connect starts 404-ing.

---

## Audio contract

Fixed by the API — do not change unless the API does:

- **To Gemini (mic):** 16-bit PCM, **16 kHz**, mono, little-endian.
- **From Gemini (Alex):** 16-bit PCM, **24 kHz**, mono, little-endian.

The `pcm-worklet.js` AudioWorklet linear-interpolates the mic from the
AudioContext's native rate down to 16 kHz, carrying the fractional phase across
128-frame blocks so there are no seams, and posts ~64 ms Int16 chunks (small
enough for snappy barge-in). Playback resamples Gemini's 24 kHz PCM into Web Audio
buffers scheduled back-to-back from a `nextStartTime` cursor for gapless output.

---

## File Structure

```
app/
├── server.py          # FastAPI app + /ws bridge; reconnect loop; end→score→save orchestration
├── config.py          # Non-secret config: model ids, voice, sample rate, turn caps, VAD tuning
├── persona.py         # Alex's system_instruction (persona + ML bluff framework) + kickoff prompt
├── gemini_live.py     # Gemini Live adapter — the ONLY file that touches the google-genai SDK
├── scoring.py         # One Gemini Flash structured-JSON call → report (+ neutral fallback)
├── db.py              # Supabase persistence (interviews table), non-blocking insert
├── requirements.txt   # fastapi, uvicorn, python-dotenv, google-genai, supabase, websockets
├── .env.example       # Secrets template (GEMINI_API_KEY, SUPABASE_*)
└── static/
    ├── index.html     # Cream/terracotta UI, dark-mode aware, morphing wave lines
    ├── app.js         # WS client: open-mic + mute, gapless playback, barge-in flush, waveforms, report
    └── pcm-worklet.js # Mic capture → 16 kHz Int16 PCM (phase-accurate resample)
```

**Separation of concerns:** `gemini_live.py` is the only module that imports the
SDK. It normalizes the raw `session.receive()` stream into simple event dicts
(`audio`, `input_transcript`, `output_transcript`, `interrupted`, `turn_complete`,
`tool_call`, `resumption`, `go_away`), so `server.py` orchestrates without knowing
a single SDK type. Swapping the realtime provider later means rewriting one file.

---

## Current Configuration Values

| Setting | Value | Where |
|---|---|---|
| Live model (conversation) | `gemini-3.1-flash-live-preview` | `config.GEMINI_LIVE_MODEL` |
| Voice | `Charon` (warm, neutral) | `config.GEMINI_VOICE` |
| Scoring model | `gemini-flash-latest` | `config.SCORING_MODEL` |
| Interview role | `AI/ML Engineer` | `config.INTERVIEW_ROLE` |
| Max Alex turns (hard cap) | **12** | `config.MAX_ALEX_TURNS` |
| Min turns before early-end | **4** | `config.MIN_TURNS_BEFORE_EARLY_END` |
| Autonomous end tool | **on** | `config.ENABLE_END_TOOL` |
| Max reconnects | **5** (exp. backoff, capped 4 s) | `config.MAX_RECONNECTS` |
| Mic sample rate → Gemini | **16 kHz** | `config.INPUT_SAMPLE_RATE` |
| VAD start/end sensitivity | **LOW / LOW** | `config.VAD_*_SENSITIVITY` |
| VAD prefix padding | **300 ms** | `config.VAD_PREFIX_PADDING_MS` |
| VAD end-of-turn silence | **800 ms** | `config.VAD_SILENCE_MS` |

### Scoring dimensions

The Flash call is forced (Pydantic response schema) to return all of: an
`overall_score`, five dimensions — **technical_depth, communication, consistency,
problem_solving, experience_authenticity** (each 0–10) — plus `strong_areas`,
`weak_areas`, `red_flags`, `improvement_tips`, a `recommended_action`
(`advance` | `reject` | `hold`), and a `summary`. Scores are clamped to 0–10 and
every key is guaranteed present before it reaches the UI or DB.

---

## Design Decisions

### Why one streaming speech API instead of wiring STT → LLM → TTS yourself

V3 wired up STT, an LLM, and TTS by hand and fought the resulting latency with
streaming STT, grounded recaps, and branch pre-generation — clever, but all of it
was *hiding* a gap. Gemini Live folds that whole pipeline into one streaming
speech-to-speech API: it understands the candidate's audio as they talk and streams
speech back, server-side, with no hops for us to run or key and no per-turn eval on
our critical path. (It's a half-cascade model, so there *is* a TTS step — it just
runs inside Gemini's infra as one low-latency stream, not three services we
operate.) The tradeoff is less mechanical control over each turn (no per-turn
structured eval for free), which is exactly why scoring is split out.

### Why one scoring call at the end (not per-turn eval)

Putting an eval on every turn was the old design's biggest self-imposed latency
tax. Moving all structured judgment to a single post-call Flash pass means (a) the
conversation is never gated on a JSON call, and (b) the scorer sees the *whole*
interview at once, so it catches cross-turn contradictions and fabricated
experience better than a turn-local eval could. It scores **blind to the persona** —
it never sees Alex's system prompt — so it judges the candidate on transcript
evidence, not on how Alex framed things.

### Why one Gemini key for everything

Conversation and scoring both run on Gemini, so the whole system needs exactly one
credential and stays inside a single free-tier quota. No STT vendor, no TTS vendor,
no separate reasoning provider to bill, rate-limit, or key-manage. `gemini-flash-latest`
(rather than a dated id) means the scorer won't 404 when a specific Flash version is
retired.

### Why LOW VAD sensitivity + echo cancellation

The mic is open while Alex is speaking, so his voice coming out of the speakers can
be misheard as the candidate barging in. Two guards: the browser captures the mic
with `echoCancellation` on, and Gemini's automatic activity detection is set to
**LOW** start/end sensitivity (clearer speech required before Alex yields). On
headphones you can bump these to `HIGH` in `config.py` for snappier barge-in.

### Why the model ends the interview

A fixed question count feels robotic and wastes turns on a candidate who's already
bluffed out (or nails everything in four questions). Letting Alex call
`end_interview` — gated so he can't cut anyone before 4 questions — makes the length
adaptive and human, while the manual End button and hard turn-cap guarantee the
session always terminates and always scores.

---

## Core Design Principle

**No AI feel — a smooth human interviewer.** Everything serves the illusion that
you're on a phone call with a sharp senior engineer:

- Duplex timing (no release-wait, no generate-then-speak gap)
- Instant barge-in (he stops the moment you talk)
- In-context, non-scripted reactions (name a bluff when it's earned, not every turn)
- Honest evaluation (scored blind on the full transcript, no invented praise)
- Resilience (a dropped socket resumes; a bad scoring call degrades to neutral) so
  the seams never show

---

## Roadmap

See **[PLANS.md](PLANS.md)** for:

- What shipped in this rebuild (V4) and how it maps to the old north star
- Cost per interview on the Gemini Live + Flash stack, and the free-tier reality
- Model/voice swap axes and the native-audio prosody experiment
- Re-introducing **config-driven fields + personas** (the one capability the rebuild
  traded away vs V3)
- Structured signal capture during the call, streaming the report, analytics, and auth
```
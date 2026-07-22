# Roadmap — Cost, Models, Future Work

Companion doc to [README.md](README.md). Covers what this rebuild costs to run,
which knobs swap models/voices, and the work worth planning for next. The framing
matters: the old cascaded build spent most of its length describing how to *hide* an
unavoidable generation gap (streaming STT,
grounded recaps, pre-generated right/wrong branches) and named a duplex speech-to-
speech "mind" as the expensive **end-project north star**. This build **is** that
north star. So this roadmap is not about shaving latency anymore — the latency
problem is solved by architecture — it's about **breadth, cost, and product**.

---

## Shipped this rebuild (V4)

The whole thing is new; the load-bearing pieces:

- **Duplex speech-to-speech conversation** — Gemini Live (half-cascade) ingests
  candidate audio continuously and streams speech back. No client-side STT/TTS to
  run, no per-turn eval on the critical path. Human conversational timing, not
  "fast for an AI."
- **Barge-in** — mic always open; on Gemini's `interrupted` event the client stops
  every scheduled `AudioBufferSourceNode` and resets the playback cursor, so Alex
  cuts off the instant the candidate speaks.
- **Conversation / scoring split** — the live model owns the conversation and its
  own follow-ups; a **single** Gemini Flash structured-JSON call at the end owns the
  scored judgment. The two never contend for the critical path.
- **Single-key, free-tier system** — the same `GEMINI_API_KEY` powers conversation
  *and* scoring. No STT/TTS/reasoning vendors to key, bill, or rate-limit.
- **Autonomous ending** — Alex speaks his closing, then calls an `end_interview`
  tool with a reason. Gated so he can't cut anyone before 4 questions; backed by a
  manual End button and a hard turn-cap.
- **Resilience** — reconnect loop with exponential backoff + **session resumption**
  (context preserved across drops), **`go_away` pre-close handling**,
  **context-window compression** for long calls, a **tool-fallback** at connect,
  and a **neutral-report fallback** so scoring never crashes the session.
- **Structured scoring schema** — Gemini's response-schema mode forces valid JSON
  (5 dimensions + overall + strengths/weaknesses/red-flags/tips + action + summary),
  then it's clamped and key-completed before it reaches the UI/DB.
- **Config/secret split** — models, voice, sample rate, turn caps, and VAD tuning
  in `config.py`; only keys in `.env`.
- **Real-audio wave UI** — cream/terracotta, dark-mode aware, two `AnalyserNode`
  waveforms, live Listening/Thinking/Speaking state derived from the audio.

### How V4 maps onto the old north star

The old PLANS.md end-state table, and where we landed:

| Old "north star" layer | Old plan | V4 reality |
|---|---|---|
| Remove the STT wait | Deepgram Nova-3 streaming | **Gone** — model hears audio directly, no STT |
| Hide reply generation | stream reply into TTS + grounded recap | **Gone** — no separate reply-gen step; Gemini streams speech directly |
| Remove the TTS first-audio gap | EL Flash → WS streaming TTS | **Gone** — Gemini streams the audio server-side; no TTS for us to run |
| The eval-before-speak wait | parallel eval + branch pre-gen | **Gone** — eval moved off the call entirely (post-call Flash) |
| The duplex "mind" | Sesame / Realtime / Gemini Live (the expensive *end* state) | **This is it** — and on the free tier, not a premium GPU bill |

The one thing the rebuild **traded away**: the old build's per-turn structured eval
also drove config-driven *fields* and *personas* (`config/fields.py`,
`config/personas.py`). V4 is currently hardwired to one role (AI/ML Engineer) and
one persona (Alex). Re-introducing that configurability is the top roadmap item below.

---

## Cost per Interview — Gemini Live + Flash

Assumptions: ~5–8 questions, ~6–10 min of two-way audio, one final Flash scoring
call over a few-thousand-token transcript, one Supabase write.

| Component | Volume per interview | Notes |
|---|---|---|
| Gemini Live — audio in + audio out | ~6–10 min duplex audio | Dominant cost. Audio tokens are billed both directions on the paid tier. |
| Gemini Flash — one scoring call | ~2–5k input, ~500 output tok | Rounding error next to the live audio. |
| Supabase — one row insert | 1 row | ~$0.00 |

> **Free-tier reality first.** This build was designed to run entirely inside the
> Gemini **free tier** on one key — which is the whole point of the single-key
> architecture. At low/dev volume, **cost per interview ≈ $0.** The paid-tier
> numbers below only matter once you exceed free-tier quota.

### Paid-tier, directional (verify before committing)

Gemini Live audio pricing is per-token on audio in *and* out and is materially more
than text tokens; a multi-minute two-way voice call is where essentially all the
cost sits. Treat any per-interview dollar figure as **order-of-magnitude until
measured against a live bill** — preview pricing and free-tier limits move. Two
things are certain regardless of the exact rate:

1. **Audio dominates.** The scoring call and the DB write are negligible.
2. **The only real cost lever is call length.** Alex's autonomous ending (wrap up at
   5–8 questions, cut persistent bluffers after a fair shot) is therefore also the
   primary *cost* control, not just a UX choice. Shortening the average call is worth
   more than any model swap.

### Pricing caveats

- Gemini Live is preview; audio token rates and free-tier quotas change — check
  [AI Studio](https://aistudio.google.com/) before relying on a number.
- Native-audio preview models may price differently from the half-cascade Live model.
- The scoring cost is trivial on either tier; don't optimize it.

---

## Model & Voice — the swap axes

Three independent knobs, all in `config.py`, no code changes:

### Live model (the conversation) — `GEMINI_LIVE_MODEL`

Currently `gemini-3.1-flash-live-preview` — a **half-cascade** Live model chosen for
*stability*: in testing it held long sessions without early drops, emitted
session-resumption handles, and accepted tools cleanly.

| Option | Why | Watch out |
|---|---|---|
| **Current — half-cascade Live** | Stable long sessions, clean resumption, tool support | Slightly less "human" prosody than native-audio |
| `gemini-2.5-flash-native-audio-*` | More human prosody/emotion | In testing: closed sessions early, didn't resume cleanly → reconnect loops. **Experiment only.** |

> **Pick:** stay on the half-cascade model for reliability. Try a native-audio
> preview *only* to A/B prosody, and expect to harden reconnect/resumption first.
> Preview ids rotate — confirm with `client.models.list()` if connects start 404-ing.

### Voice — `GEMINI_VOICE`

Currently `Charon` (warm, neutral). Other prebuilt voices: Puck, Kore, Aoede,
Fenrir, Zephyr, Orus, … Audition them in AI Studio; it's a one-line change and
purely cosmetic.

### Scoring model — `SCORING_MODEL`

Currently `gemini-flash-latest` (always points at the current Flash, so it won't 404
when a dated id retires). Only worth touching if the scoring judgment ever feels
shallow — a smarter model here costs almost nothing because it's one short call, but
the current Flash is already well-matched to a rubric-scored transcript.

---

## Where the latency goes (and why there's little to chase)

There is **no STT stage, no TTS stage, and no per-turn LLM eval** on the critical
path — the three things the old build spent its entire roadmap fighting. What remains:

```
Candidate speaks ─▶ Gemini Live (listening as they talk) ─▶ Alex replies as audio
                    │                                        │
              VAD end-of-turn (config: 800 ms silence)   model TTFB (half-cascade)
```

The two tunables that actually affect *perceived* responsiveness are VAD settings,
not a pipeline:

- **`VAD_SILENCE_MS` (800 ms)** — how much trailing silence counts as "the candidate
  is done." Lower = snappier turn-taking but more risk of Alex jumping in on a pause.
- **`VAD_*_SENSITIVITY` (LOW)** — deliberately conservative so speaker echo isn't
  misheard as barge-in. On headphones, `HIGH` gives crisper interruption.

Tuning these is a UX-feel exercise, not a latency-reduction project. The architecture
already removed the latency problem.

---

## Roadmap — planned work

Ordered by value. None of these are latency fixes (that's done); they're breadth,
signal quality, and product.

### 1. Config-driven fields + personas (the rebuild's one regression)

V3 had `config/fields.py` (AI/ML, Python Backend, Full Stack, generic) and
`config/personas.py` (Alex's tone + reaction style), swappable per session. V4
hardwired one role + one persona into `persona.py` and `config.INTERVIEW_ROLE`.
Re-introduce the same shape:

- A `FieldConfig` (topics, bluff patterns, keywords) injected into
  `SYSTEM_INSTRUCTION` and passed to the scoring prompt's `{role}`.
- A `PersonaConfig` (voice, tone block, opening/closing style) selecting the Live
  voice and system prompt.
- Choose both at session start (`/ws?field=…&persona=…` or a start-screen picker).

This is the highest-leverage item — it turns a single-role demo back into the
general interviewer the old build was.

### 2. Structured signal capture *during* the call

Right now the scorer reconstructs everything from the raw transcript. Give it richer
input by having Alex optionally emit lightweight tool calls when he catches something
(`flag_bluff`, `note_contradiction`) — captured as structured events alongside the
transcript and fed into the scoring prompt. Keeps the conversation duplex (the tool
calls are fire-and-forget) while sharpening the final judgment.

### 3. Stream / progressive report

Scoring is one post-call Flash pass, so the candidate waits a beat on the "Scoring…"
screen. Options: stream the report fields as they generate, or show a partial
(dimensions first, prose after). Small polish, not urgent.

### 4. Analytics + candidate management

The `interviews` table already stores role, language, duration, transcript, and the
full report as JSONB. Build a dashboard over it (score distributions, red-flag
frequency, per-role funnels) and basic candidate/session management. This is where
the product value compounds.

### 5. Auth + multi-tenant

Sessions are anonymous today. Add candidate identity + recruiter accounts so reports
attach to people and roles, gated behind auth.

### 6. Native-audio prosody (gated on stability)

Revisit the native-audio Live models once their session-resumption behavior is
reliable enough to survive the reconnect loop. Pure quality-of-voice upgrade; do it
after (1)–(4), and only if the half-cascade voice ever feels like the weak link.

---

## Known tradeoffs vs the old cascaded stack

Honest ledger of what changed direction:

| Dimension | V3 (cascade) | V4 (duplex) |
|---|---|---|
| Perceived latency | ~0.8–3 s, *hidden* with tricks | Human timing, no gap to hide |
| Barge-in | Not really (hold-to-talk) | Native, instant |
| Per-turn control | Full structured eval every turn | None mid-call; one eval at end |
| Fields/personas | Config-driven, multi | Hardwired (roadmap item #1) |
| Vendors/keys | Cerebras + Groq + ElevenLabs + Supabase | Gemini + Supabase (one AI key) |
| Cost driver | TTS characters (ElevenLabs) | Live audio minutes (Gemini) |
| Cost floor | ~$0.11/interview | ~$0 on free tier; audio-minutes on paid |

The rebuild trades fine-grained per-turn control for genuine conversational realism
and a radically simpler operational surface (one key, one live socket, one scoring
call). Item #1 buys back the configurability without giving up the duplex win.

---

## Adding new fields / personas (target design)

Once roadmap item #1 lands, adding a field or persona should be config-only, exactly
like the old build:

**New field**
1. Define a `FieldConfig` — `topics`, `bluff_patterns`, `keywords`.
2. Register it in an `ALL_FIELDS` list.
3. It auto-injects into Alex's system prompt and the scoring prompt's `{role}`.

**New persona**
1. Define a `PersonaConfig` — `voice`, `tone_block`, `opening`/`closing` style.
2. Register it in `ALL_PERSONAS`.
3. Select via `persona=…` at session start.

Until then: edit `persona.py` (the `SYSTEM_INSTRUCTION`), `config.INTERVIEW_ROLE`,
and `config.GEMINI_VOICE` directly.

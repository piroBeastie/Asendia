"""Non-secret configuration for Asendia.

Provider and model choices live HERE (committed to git). Only API keys go in
.env. To swap a model or a voice, edit this file — never .env.
"""

# ─── Gemini Live (the conversation) ──────────────────────────────────────────
# A Live model that streams audio in/out and is interruptible. The default is a
# half-cascade Live model: in testing it held long sessions without early drops,
# emitted session-resumption handles, and supported tools — i.e. it's stable.
#
# The "native-audio" preview models sound a touch more human but, in testing,
# closed sessions early and didn't resume cleanly (caused reconnect loops). Try
# one only to experiment with prosody:
#   "gemini-2.5-flash-native-audio-latest"
#   "gemini-2.5-flash-native-audio-preview-12-2025"
# Confirm current ids in Google AI Studio (client.models.list()) — ids rotate.
GEMINI_LIVE_MODEL = "gemini-3.1-flash-live-preview"

# Prebuilt voice for Alex. Audition voices in AI Studio; this one is warm and
# neutral. Others: Puck, Kore, Aoede, Fenrir, Zephyr, Orus, ...
GEMINI_VOICE = "Charon"

# Audio contract — do NOT change unless the API changes.
#   input  → Gemini : 16-bit PCM, 16 kHz, mono, little-endian  (used in send_pcm's mime type)
#   output ← Gemini : 16-bit PCM, 24 kHz, mono, little-endian  (the browser plays it back)
INPUT_SAMPLE_RATE = 16000

# ─── Scoring (one final call — reuses the Gemini key, free tier) ─────────────
# Gemini Flash scores the transcript with structured JSON output, using the SAME
# GEMINI_API_KEY as the conversation (no extra key, no cost on the free tier).
# "gemini-flash-latest" always points at the current Flash model, so it won't
# 404 when a dated id (e.g. gemini-2.5-flash) is retired for new users.
SCORING_MODEL = "gemini-flash-latest"

# ─── Interview shape ─────────────────────────────────────────────────────────
INTERVIEW_ROLE = "AI/ML Engineer"

# Lock the session to one language. This forces the input/output transcription to
# a fixed language instead of auto-detecting — which stops speech-to-text from
# hallucinating random foreign phrases on fillers/pauses (e.g. "um" -> Spanish).
INTERVIEW_LANGUAGE = "en-US"

# Hard ceiling on Alex's turns (greeting + questions) so a runaway session always
# ends and produces a report. Alex normally ends himself well before this.
MAX_ALEX_TURNS = 12

# If the Live session drops mid-interview, how many times to reconnect before
# giving up (with exponential backoff, and preserving context via the resumption
# handle when the model supports it).
MAX_RECONNECTS = 5

# ─── Autonomous end ──────────────────────────────────────────────────────────
# Alex ends the interview himself via an `end_interview` tool call — when he's
# satisfied he has enough signal, or when the candidate keeps bluffing after a
# fair shot, or when they've disengaged. If a Live model rejects tools at connect,
# the server retries WITHOUT the tool and falls back to the manual "End" button +
# the MAX_ALEX_TURNS backstop, so the interview always works.
ENABLE_END_TOOL = True

# Alex won't cut a candidate early before this many of his turns — one bad answer
# shouldn't end the interview. (He can still go the full length; this is a floor.)
MIN_TURNS_BEFORE_EARLY_END = 4

# ─── Barge-in / VAD tuning ───────────────────────────────────────────────────
# The mic is always open (duplex). To stop speaker echo from being misheard as
# the candidate barging in, we make Gemini's voice-activity detection a little
# less trigger-happy. LOW start-sensitivity = clearer speech required before Alex
# yields. Bump to "HIGH" if you're on headphones and want snappier barge-in.
VAD_START_SENSITIVITY = "LOW"   # "HIGH" | "LOW"
VAD_END_SENSITIVITY = "LOW"     # "HIGH" | "LOW"
VAD_PREFIX_PADDING_MS = 300     # audio kept just before detected speech start
VAD_SILENCE_MS = 800            # silence that counts as end-of-turn

"""Gemini Live adapter — the only file that knows the google-genai SDK.

Responsibilities (one job): build the Live config, open/reconnect the session,
push PCM up, and turn the raw `session.receive()` stream into simple normalized
event dicts the server can forward without knowing any SDK types.
"""

import os

from google import genai
from google.genai import types

import config
from persona import SYSTEM_INSTRUCTION, KICKOFF_PROMPT


def make_client() -> genai.Client:
    """Create the Gemini client from the API key in the environment."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set in .env")
    # Default http options work for the Live API on the Gemini Developer API.
    return genai.Client(api_key=api_key)


_START_SENS = {
    "HIGH": types.StartSensitivity.START_SENSITIVITY_HIGH,
    "LOW": types.StartSensitivity.START_SENSITIVITY_LOW,
}
_END_SENS = {
    "HIGH": types.EndSensitivity.END_SENSITIVITY_HIGH,
    "LOW": types.EndSensitivity.END_SENSITIVITY_LOW,
}


def _end_tool() -> types.Tool:
    """The one tool Alex can call to end the interview himself."""
    return types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="end_interview",
            description=(
                "End the interview now. Call this ONLY after you've spoken your warm closing "
                "out loud. Use it when you're satisfied you can fairly evaluate the candidate, "
                "or when they keep bluffing after a fair number of questions, or when they've "
                "clearly disengaged."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "reason": types.Schema(
                        type=types.Type.STRING,
                        enum=["satisfied", "enough_signal", "persistent_bluffing",
                              "candidate_disengaged", "other"],
                        description="Why you're ending now.",
                    ),
                    "note": types.Schema(
                        type=types.Type.STRING,
                        description="One short sentence of context (optional).",
                    ),
                },
                required=["reason"],
            ),
        )
    ])


def _vad_config() -> types.RealtimeInputConfig:
    """Tune voice-activity detection so speaker echo isn't misheard as barge-in."""
    return types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(
            start_of_speech_sensitivity=_START_SENS.get(
                config.VAD_START_SENSITIVITY, types.StartSensitivity.START_SENSITIVITY_LOW),
            end_of_speech_sensitivity=_END_SENS.get(
                config.VAD_END_SENSITIVITY, types.EndSensitivity.END_SENSITIVITY_LOW),
            prefix_padding_ms=config.VAD_PREFIX_PADDING_MS,
            silence_duration_ms=config.VAD_SILENCE_MS,
        )
    )


def _build_config(resumption_handle: str | None, use_tools: bool = True) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part(text=SYSTEM_INSTRUCTION)]),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=config.GEMINI_VOICE
                )
            ),
            language_code=config.INTERVIEW_LANGUAGE,
        ),
        # Capture BOTH sides as text for scoring. (Per-transcription language codes
        # are Vertex-only, so on the developer API we rely on speech_config's
        # language_code + the English-only persona to keep it from drifting.)
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        # Less trigger-happy barge-in (see config VAD_* settings).
        realtime_input_config=_vad_config(),
        # Let Alex end the interview himself.
        tools=[_end_tool()] if use_tools else None,
        # Resilience: lets us reconnect and keep context if the socket drops.
        session_resumption=types.SessionResumptionConfig(handle=resumption_handle),
        # Keeps long interviews under the context limit automatically.
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
    )


def connect(client: genai.Client, resumption_handle: str | None = None, use_tools: bool = True):
    """Return the async context manager for a Live session."""
    return client.aio.live.connect(
        model=config.GEMINI_LIVE_MODEL,
        config=_build_config(resumption_handle, use_tools),
    )


async def respond_tool(session, call_id: str | None, name: str, result: dict) -> None:
    """Acknowledge a function call so the model isn't left waiting."""
    await session.send_tool_response(
        function_responses=[types.FunctionResponse(id=call_id, name=name, response=result)]
    )


async def kickoff(session) -> None:
    """Prompt Alex to greet and ask the first question (Alex speaks first)."""
    await session.send_client_content(
        turns=types.Content(role="user", parts=[types.Part(text=KICKOFF_PROMPT)]),
        turn_complete=True,
    )


async def send_pcm(session, pcm: bytes) -> None:
    """Stream one chunk of 16 kHz mono PCM16 up to Gemini."""
    await session.send_realtime_input(
        audio=types.Blob(
            data=pcm, mime_type=f"audio/pcm;rate={config.INPUT_SAMPLE_RATE}"
        )
    )


async def iter_events(session):
    """Yield normalized events from the Live stream.

    Event shapes:
      {"type": "audio",            "data": bytes}     # 24 kHz PCM16 to play
      {"type": "input_transcript", "text": str}       # what the candidate said
      {"type": "output_transcript","text": str}       # what Alex said
      {"type": "interrupted"}                          # candidate barged in
      {"type": "turn_complete"}                        # Alex finished a turn
      {"type": "resumption",       "handle": str}      # save for reconnect
      {"type": "go_away",          "time_left": str}   # session ending soon
      {"type": "tool_call", "id": str, "name": str, "args": dict}  # Alex called a tool

    `session.receive()` returns at each turn boundary, so we loop it: the session
    stays open across turns, and only an actual close (which raises, or returns an
    empty stream) ends this generator — that's what the server treats as a drop.
    """
    while True:
        produced = 0
        async for message in session.receive():
            produced += 1

            # Function calls (e.g. Alex ending the interview) arrive top-level.
            tool_call = getattr(message, "tool_call", None)
            if tool_call is not None and getattr(tool_call, "function_calls", None):
                for fc in tool_call.function_calls:
                    yield {
                        "type": "tool_call",
                        "id": fc.id,
                        "name": fc.name,
                        "args": dict(fc.args or {}),
                    }

            # Session-resumption handle (capture it whenever it updates).
            update = getattr(message, "session_resumption_update", None)
            if update is not None and getattr(update, "new_handle", None):
                if getattr(update, "resumable", True):
                    yield {"type": "resumption", "handle": update.new_handle}

            # Server is about to close this connection.
            go_away = getattr(message, "go_away", None)
            if go_away is not None:
                yield {"type": "go_away", "time_left": str(getattr(go_away, "time_left", ""))}

            sc = getattr(message, "server_content", None)
            if sc is None:
                continue

            # Barge-in: emit before audio so the client flushes immediately.
            if getattr(sc, "interrupted", None):
                yield {"type": "interrupted"}

            it = getattr(sc, "input_transcription", None)
            if it is not None and getattr(it, "text", None):
                yield {"type": "input_transcript", "text": it.text}

            ot = getattr(sc, "output_transcription", None)
            if ot is not None and getattr(ot, "text", None):
                yield {"type": "output_transcript", "text": ot.text}

            model_turn = getattr(sc, "model_turn", None)
            if model_turn is not None and model_turn.parts:
                for part in model_turn.parts:
                    inline = getattr(part, "inline_data", None)
                    if inline is not None and inline.data:
                        yield {"type": "audio", "data": inline.data}

            if getattr(sc, "turn_complete", None):
                yield {"type": "turn_complete"}

        # An empty receive() means the connection closed rather than a normal turn
        # boundary — stop so the server can reconnect (resuming via the handle).
        if produced == 0:
            return

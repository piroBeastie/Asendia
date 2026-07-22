"""FastAPI server — the orchestrator.

Bridges the browser and one Gemini Live session over a WebSocket:
  - browser mic PCM (16 kHz)  → Gemini
  - Gemini audio (24 kHz)     → browser
  - transcripts assembled turn-by-turn for scoring
  - barge-in and turn signals relayed to the browser
  - reconnects to Gemini on drop/go_away (context preserved via resumption handle)
On end (manual, turn-cap backstop, or disconnect) it runs one scoring call,
saves to Supabase, and sends the report.
"""

import asyncio
import json
import logging
import time

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import config
import gemini_live as gl
from db import save_interview
from scoring import score_interview

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("asendia")

app = FastAPI(title="Asendia — AI Voice Interviewer")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    with open("static/index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/health")
async def health():
    return {"status": "ok"}


class TranscriptBuilder:
    """Assembles streamed input/output transcription into ordered turns.

    Gemini streams transcription in fragments. We buffer the candidate's text and
    Alex's text separately; when Alex starts speaking we flush the candidate's
    turn, and each `turn_complete` flushes Alex's turn. Order is preserved.
    """

    def __init__(self):
        self._user = ""
        self._model = ""
        self.turns: list[dict] = []
        self.language = "en"

    def add_user(self, text: str):
        # Candidate resumed talking after Alex → Alex's turn is done.
        if self._model:
            self._flush_model()
        self._user += text

    def add_model(self, text: str):
        # Alex started talking → the candidate's turn is done.
        if self._user:
            self._flush_user()
        self._model += text

    def interrupted(self):
        # Candidate cut in — close out whatever Alex had said so far.
        self._flush_model()

    def end_turn(self):
        self._flush_user()
        self._flush_model()

    def _flush_user(self):
        t = self._user.strip()
        if t:
            self.turns.append({"role": "candidate", "text": t})
        self._user = ""

    def _flush_model(self):
        t = self._model.strip()
        if t:
            self.turns.append({"role": "interviewer", "text": t})
        self._model = ""

    def finalize(self) -> list[dict]:
        self.end_turn()
        return self.turns


async def _pump_up(ws: WebSocket, session, state: dict):
    """Browser → Gemini. Binary frames are mic PCM; a JSON {type:end} finishes."""
    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            state["reason"] = "disconnect"
            state["finish"].set()
            return
        data = msg.get("bytes")
        if data:
            await gl.send_pcm(session, data)
            continue
        text = msg.get("text")
        if text:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "end":
                state["reason"] = "user_end"
                state["finish"].set()
                return


async def _pump_down(ws: WebSocket, session, transcript: TranscriptBuilder, state: dict):
    """Gemini → browser. Forwards audio and control events; assembles transcript."""
    async for ev in gl.iter_events(session):
        etype = ev["type"]
        if etype == "audio":
            await ws.send_bytes(ev["data"])
        elif etype == "interrupted":
            transcript.interrupted()
            await ws.send_json({"type": "interrupted"})
        elif etype == "input_transcript":
            transcript.add_user(ev["text"])
        elif etype == "output_transcript":
            transcript.add_model(ev["text"])
        elif etype == "turn_complete":
            transcript.end_turn()
            state["turns"] += 1
            await ws.send_json({"type": "turn", "n": state["turns"]})
            if state["turns"] >= config.MAX_ALEX_TURNS:
                state["reason"] = "max_turns"
                state["finish"].set()
                return
        elif etype == "tool_call":
            if ev["name"] == "end_interview":
                # Alex decided he's done. His spoken closing already streamed
                # ahead of this call, so it's playing in the browser now.
                reason = ev["args"].get("reason", "satisfied")
                state["reason"] = f"alex_ended:{reason}"
                logger.info("Alex ended the interview (%s): %s",
                            reason, ev["args"].get("note", ""))
                try:
                    await gl.respond_tool(session, ev["id"], "end_interview", {"status": "ended"})
                except Exception:
                    pass
                state["finish"].set()
                return
        elif etype == "resumption":
            state["handle"] = ev["handle"]
        elif etype == "go_away":
            # Connection is closing; return so the outer loop reconnects with the
            # saved handle (interview continues, context preserved).
            return


async def _run_interview(ws: WebSocket, client, transcript: TranscriptBuilder, state: dict):
    """Own the Gemini session with a resilient reconnect loop.

    On a drop we reconnect, resuming with the session handle when the model gives
    one (context preserved). If a resume fails, we drop the handle and reconnect
    fresh. Backoff avoids hammering the API; we greet only once.
    """
    use_tools = config.ENABLE_END_TOOL
    tool_fallback_tried = False
    attempt = 0
    while not state["finish"].is_set():
        try:
            async with gl.connect(client, state["handle"], use_tools) as session:
                if not state["greeted"]:
                    await gl.kickoff(session)  # greet exactly once
                    state["greeted"] = True
                up = asyncio.create_task(_pump_up(ws, session, state))
                down = asyncio.create_task(_pump_down(ws, session, transcript, state))
                done, pending = await asyncio.wait(
                    {up, down}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                for t in done:
                    exc = t.exception()
                    if exc and not isinstance(exc, WebSocketDisconnect):
                        raise exc
        except WebSocketDisconnect:
            state["reason"] = "disconnect"
            state["finish"].set()
            return
        except Exception as e:  # noqa: BLE001
            # Never greeted yet + tools on → the model may have rejected the tool
            # at connect. Retry once without it (manual End + backstop remain).
            if use_tools and not tool_fallback_tried and not state["greeted"]:
                logger.warning("Connect failed with end_interview tool (%s) — "
                               "retrying without it", e)
                use_tools = False
                tool_fallback_tried = True
                continue
            # A resume with a handle failed — drop it and reconnect fresh.
            if state["handle"]:
                logger.warning("Resume failed (%s) — dropping handle, reconnecting fresh", e)
                state["handle"] = None
            else:
                logger.warning("Gemini session error: %s", e)

        if state["finish"].is_set():
            return
        attempt += 1
        if attempt > config.MAX_RECONNECTS:
            logger.error("Too many reconnects — ending interview")
            state["reason"] = "reconnect_limit"
            return
        delay = min(0.5 * (2 ** (attempt - 1)), 4.0)
        logger.info("Reconnecting to Gemini (attempt %d/%d) in %.1fs…",
                    attempt, config.MAX_RECONNECTS, delay)
        await asyncio.sleep(delay)


async def _finalize(ws: WebSocket, transcript: TranscriptBuilder, state: dict, start: float):
    """Score + persist + deliver the report. Best-effort on a live socket."""
    turns = transcript.finalize()
    duration_min = (time.time() - start) / 60.0
    logger.info("Interview ended (%s) — %d turns, %.1f min",
                state.get("reason"), len(turns), duration_min)

    if not turns:
        try:
            await ws.close()
        except Exception:
            pass
        return

    try:
        await ws.send_json({"type": "status", "state": "scoring"})
    except Exception:
        pass

    report = await score_interview(
        turns,
        role=config.INTERVIEW_ROLE,
        duration_minutes=duration_min,
        language=transcript.language,
    )
    report["end_reason"] = state.get("reason") or "unknown"
    await save_interview(report, turns, duration_min)  # own try/except inside

    try:
        await ws.send_json({"type": "report", "data": report})
    except Exception:
        pass
    try:
        await ws.close()
    except Exception:
        pass


@app.websocket("/ws")
async def ws_interview(ws: WebSocket):
    await ws.accept()
    logger.info("Interview WebSocket connected")

    try:
        client = gl.make_client()
    except Exception as e:  # noqa: BLE001 — missing key, etc.
        await ws.send_json({"type": "error", "message": str(e)})
        await ws.close()
        return

    transcript = TranscriptBuilder()
    state = {"turns": 0, "handle": None, "greeted": False,
             "reason": None, "finish": asyncio.Event()}
    start = time.time()

    try:
        await _run_interview(ws, client, transcript, state)
    except Exception as e:  # noqa: BLE001
        logger.exception("Interview failed: %s", e)
    finally:
        await _finalize(ws, transcript, state, start)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

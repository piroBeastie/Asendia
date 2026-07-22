"""Scoring — ONE Gemini Flash call over the finished transcript.

Uses Gemini's structured output (a response schema), so the model returns JSON
that maps straight onto the report. Reuses the SAME GEMINI_API_KEY as the
conversation — no extra key, free tier. Any failure degrades to a neutral
report; it never raises.
"""

import json
import logging
import os
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel

import config

logger = logging.getLogger(__name__)


class _Dimensions(BaseModel):
    technical_depth: float
    communication: float
    consistency: float
    problem_solving: float
    experience_authenticity: float


class _Report(BaseModel):
    """The structure Gemini is forced to return (all scores 0–10)."""
    overall_score: float
    dimensions: _Dimensions
    strong_areas: list[str]
    weak_areas: list[str]
    red_flags: list[str]
    improvement_tips: list[str]
    recommended_action: Literal["advance", "reject", "hold"]
    summary: str


_PROMPT = """You are a rigorous but fair hiring evaluator. Below is the full transcript of a \
voice interview for a {role} role, conducted by an AI interviewer named Alex.

This transcript is produced by imperfect speech-to-text. It may contain transcription errors: \
random foreign words or phrases, garbled fragments, filler words ("um", "uh"), repeated words, or \
short nonsensical bits the candidate did NOT actually say. Do NOT treat apparent language-switching, \
stray foreign phrases, or garbled snippets as things the candidate did — those are transcription \
glitches, not behavior. Never list them as red flags. Judge only the substance the candidate was \
clearly trying to convey; if a passage looks like an ASR glitch, ignore it.

Score the CANDIDATE only (ignore Alex's competence). Be honest and evidence-based:
- Reward genuine technical depth, clear communication, and consistency.
- Penalize bluffs, hand-waving, contradictions, and fabricated experience — cite them as red flags.
- Do not inflate scores to be nice. A weak interview should score low.
- Every score is 0 to 10. Give 2 to 4 specific, kind, actionable improvement tips.
- recommended_action must be exactly one of: advance, reject, hold.

Transcript:
{transcript}

Return your evaluation as JSON matching the schema."""


def _neutral(reason: str = "") -> dict:
    """Safe fallback report — used if scoring can't run. Never crashes the app."""
    if reason:
        logger.warning("Returning neutral report: %s", reason)
    return {
        "overall_score": 0,
        "dimensions": {
            "technical_depth": 0, "communication": 0, "consistency": 0,
            "problem_solving": 0, "experience_authenticity": 0,
        },
        "strong_areas": [],
        "weak_areas": [],
        "red_flags": [],
        "improvement_tips": [
            "We couldn't score this session automatically.",
            "Please try the interview again.",
        ],
        "recommended_action": "hold",
        "summary": "The interview could not be scored automatically.",
    }


def _clamp(x, lo=0.0, hi=10.0) -> float:
    try:
        return round(max(lo, min(hi, float(x))), 1)
    except (TypeError, ValueError):
        return 0.0


def _normalize(data: dict) -> dict:
    """Clamp scores and guarantee every expected key exists."""
    dims = data.get("dimensions") or {}
    data["dimensions"] = {
        k: _clamp(dims.get(k, 0))
        for k in ["technical_depth", "communication", "consistency",
                  "problem_solving", "experience_authenticity"]
    }
    data["overall_score"] = _clamp(data.get("overall_score", 0))
    for key in ["strong_areas", "weak_areas", "red_flags", "improvement_tips"]:
        val = data.get(key)
        data[key] = val if isinstance(val, list) else []
    if data.get("recommended_action") not in ("advance", "reject", "hold"):
        data["recommended_action"] = "hold"
    data["summary"] = data.get("summary") or ""
    return data


async def score_interview(
    transcript: list[dict],
    role: str,
    duration_minutes: float,
    language: str = "en",
) -> dict:
    """Produce the report dict. `transcript` is a list of {role, text} entries."""
    if not transcript:
        report = _neutral("empty transcript")
    else:
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            report = _neutral("GEMINI_API_KEY not set")
        else:
            convo = "\n".join(
                f"{'Alex' if t['role'] == 'interviewer' else 'Candidate'}: {t['text']}"
                for t in transcript
            )
            try:
                client = genai.Client(api_key=api_key)
                resp = await client.aio.models.generate_content(
                    model=config.SCORING_MODEL,
                    contents=_PROMPT.format(role=role, transcript=convo),
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=_Report,
                    ),
                )
                parsed = resp.parsed
                data = parsed.model_dump() if isinstance(parsed, _Report) else json.loads(resp.text)
                report = _normalize(data)
            except Exception as e:  # noqa: BLE001 — scoring must never crash the session
                report = _neutral(f"gemini scoring failed: {e}")

    # Metadata the UI and DB need (not part of the model's judgment).
    report["role"] = role
    report["duration_minutes"] = round(duration_minutes, 1)
    report["language_detected"] = language or "en"
    return report

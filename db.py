"""Supabase persistence. Reuses the existing `interviews` table schema:
  id, role, language, status, duration_seconds, transcript (jsonb), report (jsonb).

Insert runs in a thread so it never blocks the event loop, and a failure is
logged rather than raised — a DB hiccup must not crash the interview.
"""

import asyncio
import logging
import os
import uuid

from supabase import create_client

logger = logging.getLogger(__name__)

_REPORT_KEYS = [
    "overall_score", "dimensions", "strong_areas", "weak_areas",
    "red_flags", "improvement_tips", "recommended_action", "summary", "end_reason",
]


def _client():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    return create_client(url, key)


async def save_interview(
    report: dict,
    transcript: list[dict],
    duration_minutes: float,
) -> str | None:
    """Persist one completed interview. Returns the row id, or None on failure."""
    row_id = str(uuid.uuid4())
    row = {
        "id": row_id,
        "role": report.get("role"),
        "language": report.get("language_detected", "en"),
        "status": "completed",
        "duration_seconds": int(duration_minutes * 60),
        "transcript": transcript,
        "report": {k: report.get(k) for k in _REPORT_KEYS},
    }

    def _insert():
        _client().table("interviews").insert(row).execute()

    try:
        await asyncio.to_thread(_insert)
        logger.info("Saved interview %s to Supabase", row_id)
        return row_id
    except Exception as e:  # noqa: BLE001
        logger.error("Supabase insert failed: %s", e)
        return None

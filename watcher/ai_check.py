"""Step 2: ask a small vision-capable OpenAI model whether a message shows availability.

Fails open: any error, timeout or bad output -> treat as "please look".
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass

import openai
from openai import AsyncOpenAI

log = logging.getLogger("watcher.ai")

AI_FAILED_SUMMARY = "(AI check failed - please look)"

SYSTEM_PROMPT = """You review messages from public Telegram groups where Indian H-1B/H-4 visa \
applicants report US visa appointment availability they saw on the official scheduling portal.
Two appointment types: OFC/VAC (biometrics) and CA/consular (interview), in New Delhi, Mumbai, \
Chennai, Hyderabad, Kolkata.
Abbreviations: NA = not available; CHN = Chennai, HYD = Hyderabad, MUM/BOM = Mumbai, \
DEL/ND = New Delhi, KOL/CCU = Kolkata; OFC/VAC = biometrics; CA/consular = interview. \
Numbers like "1 h1b" or "1+1" are applicant counts, not dates.

Decide if the message (text and/or screenshot) indicates that an OFC or interview appointment \
is open / was just seen available.
availability = true for: reports like "CHN OFC Nov 20 available", "HYD CA Dec 3", "slots released \
in Kolkata", "got/booked OFC in Chennai for 20 Nov" (implies slots were open), or a screenshot of \
the portal calendar with selectable/highlighted dates or a list of open times.
availability = false for: NA / not-available reports, "No appointments available" screens, errors \
such as "The system is processing too many requests", questions, chatter, admin notices, and ads \
or DMs offering paid booking services or agents.
ONLY INDIA COUNTS: the applicant needs an appointment in India (New Delhi, Mumbai, Chennai, \
Hyderabad, Kolkata). If the message or screenshot shows a location outside India (e.g. Bangkok, \
Singapore, Frankfurt, Dubai, Toronto, Mexico, Kathmandu, Colombo), set availability = false and say \
"outside India" in the summary. If no location is shown at all, judge on the rest as usual.

summary: one short line (max ~90 characters). If true, name the city, the appointment type \
(OFC or interview) and dates if visible, e.g. "Chennai OFC open on 20 Nov" or "Calendar \
screenshot showing open dates in Hyderabad (interview)". If false, say briefly why."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "availability": {"type": "boolean"},
        "appointment_type": {"type": "string",
                             "enum": ["biometrics", "interview", "both", "unknown"]},
        "location": {"type": "string"},
        "dates": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["availability", "appointment_type", "location", "dates", "summary"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class AIResult:
    availability: bool
    summary: str
    failed: bool = False
    appointment_type: str = "unknown"  # biometrics | interview | both | unknown
    location: str = ""
    dates: str = ""


def _data_url(image: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"


def parse_ai_output(raw: str | None) -> AIResult | None:
    try:
        data = json.loads(raw or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    avail, summary = data.get("availability"), data.get("summary")
    if not isinstance(avail, bool) or not isinstance(summary, str):
        return None
    appt = data.get("appointment_type")
    if appt not in ("biometrics", "interview", "both", "unknown"):
        appt = "unknown"

    def _s(key: str, n: int) -> str:
        v = data.get(key)
        return v.strip()[:n] if isinstance(v, str) else ""

    return AIResult(availability=avail, summary=summary.strip()[:200], appointment_type=appt,
                    location=_s("location", 60), dates=_s("dates", 80))


class AIChecker:
    def __init__(self, api_key: str | None, model: str, timeout: float = 10.0,
                 image_timeout: float | None = None, fallback_model: str | None = None):
        self.model = model
        # used only if the main model is missing / not allowed (e.g. retired by OpenAI)
        self.fallback_model = fallback_model if fallback_model and fallback_model != model else None
        self.timeout = timeout  # text-only messages
        self.image_timeout = image_timeout or timeout  # screenshots take longer to read
        self.client = (
            AsyncOpenAI(api_key=api_key, timeout=max(self.timeout, self.image_timeout),
                        max_retries=0) if api_key else None
        )

    async def check(
        self, text: str | None, images: list[tuple[bytes, str]] | None = None
    ) -> AIResult:
        if self.client is None:
            log.warning("OPENAI_API_KEY not set; failing open")
            return AIResult(True, AI_FAILED_SUMMARY, failed=True)

        content: list[dict] = [
            {"type": "text", "text": f"Message text:\n{text.strip()}" if text and text.strip()
             else "Message text: (none - image only)"}
        ]
        for img, mime in images or []:
            content.append(
                {"type": "image_url", "image_url": {"url": _data_url(img, mime), "detail": "auto"}}
            )

        limit = self.image_timeout if images else self.timeout
        try:
            try:
                raw = await self._call(self.model, content, limit)
            except (openai.NotFoundError, openai.PermissionDeniedError) as e:
                if not self.fallback_model:
                    raise
                log.warning("Model %s unavailable (%s); trying fallback %s",
                            self.model, type(e).__name__, self.fallback_model)
                raw = await self._call(self.fallback_model, content, limit)
        except asyncio.TimeoutError:
            log.warning("AI check timed out after %.0fs; failing open", limit)
            return AIResult(True, AI_FAILED_SUMMARY, failed=True)
        except Exception as e:  # network, auth, quota, model errors -> fail open
            log.warning("AI check error (%s: %s); failing open", type(e).__name__, e)
            return AIResult(True, AI_FAILED_SUMMARY, failed=True)

        parsed = parse_ai_output(raw)
        if parsed is None:
            log.warning("AI returned unparseable output %r; failing open", (raw or "")[:200])
            return AIResult(True, AI_FAILED_SUMMARY, failed=True)
        return parsed

    async def _call(self, model: str, content: list[dict], limit: float) -> str | None:
        resp = await asyncio.wait_for(
                self.client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": content},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "slot_check",
                            "strict": True,
                            "schema": RESPONSE_SCHEMA,
                        },
                    },
                ),
                timeout=limit,
            )
        return resp.choices[0].message.content

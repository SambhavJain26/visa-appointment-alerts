"""Step 1: cheap, AI-free filter that drops "not available" reports.

Rule: a message containing NA in any form (NA, Na, nA, na, N/A, N.A., NALL, H1NA, NA1 ...)
is skipped directly, even if it is ambiguous, UNLESS it also clearly reports availability:
an availability word (available, open, released, got, booked, ...) or a real date
(month name, "20th", "20/11"). Example kept: "NA Delhi but CHN OFC Nov 20 available".
Images always go to the AI (screenshots are the main way real slots get posted).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# NA as its own token, any case. "na" glued to digits/H1/H4 counts ("H1NA", "NA1", "H1NALL"),
# but not inside normal words ("national", "Chennai", "Nagpur", "Nah", "banana").
_NA_MARKER = re.compile(r"(?<![a-z])n[/.]?a\.?(?:ll)?(?![a-z])")
_VISA_TOKEN = re.compile(r"(h-?1b|h-?1|h-?4)")

_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_MONTH = re.compile(rf"\b(?:{_MONTHS})\b", re.IGNORECASE)

_AVAIL_WORDS = re.compile(
    r"\b(?:avail\w*|open(?:ed|ing|s)?|released?|got|booked|grabbed|showing|appeared"
    r"|slots?\s+(?:on|for|in|at)|dates?\s+(?:on|for|in|at)|bulk)\b",
    re.IGNORECASE,
)

# Real dates: ordinals ("20th") or numeric dates ("20/11", "20-11", "20.11").
_ORDINAL = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)\b", re.IGNORECASE)
_NUMERIC_DATE = re.compile(r"\b\d{1,2}\s*[/\-.]\s*\d{1,2}\b")

_HAS_WORD_CHAR = re.compile(r"[A-Za-z0-9ऀ-ॿ]")


@dataclass(frozen=True)
class FilterResult:
    skip: bool
    reason: str


def has_na_marker(text: str) -> bool:
    low = _VISA_TOKEN.sub(r" \1 ", text.lower())  # "h1bna" -> " h1b na"
    return bool(_NA_MARKER.search(low))


def has_availability_signal(text: str) -> bool:
    return bool(
        _AVAIL_WORDS.search(text)
        or _MONTH.search(text)
        or _ORDINAL.search(text)
        or _NUMERIC_DATE.search(text)
    )


def classify(
    text: str | None,
    *,
    has_image: bool = False,
    is_service: bool = False,
    is_sticker: bool = False,
) -> FilterResult:
    """Return whether to skip the message before the AI check, and why."""
    if is_service:
        return FilterResult(True, "service message")
    if is_sticker:
        return FilterResult(True, "sticker")

    text = (text or "").strip()
    if has_image:
        return FilterResult(False, "has image")
    if not text:
        return FilterResult(True, "empty")
    if not _HAS_WORD_CHAR.search(text):
        return FilterResult(True, "no letters/digits (emoji or punctuation only)")

    if has_na_marker(text):
        if has_availability_signal(text):
            return FilterResult(False, "NA but also availability words/dates")
        return FilterResult(True, "NA report")

    return FilterResult(False, "no NA")

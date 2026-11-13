"""A source-independent fingerprint for "the same slot": type | city | first date.

Used to merge the same sighting arriving from Telegram and CheckVisaSlots (or several groups)
within a short window. If the city or the date can't be pinned down, there is no fingerprint
and nothing is merged (the alert is always sent).
"""
from __future__ import annotations

import re
from datetime import date

from .timeutil import IST

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_YEAR = r"(\d{4}|\d{2})(?![\d:.])"
# "30 Nov 2026", "30th Nov", "30 Nov, 26", "30 of November"
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?{_MON}(?:,?\s*{_YEAR})?",
                        re.IGNORECASE)
# "Nov 30", "November 30, 2026", "Nov 30th 2026"
_MONTH_DAY = re.compile(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(\d{{4}})(?![\d:.]))?",
                        re.IGNORECASE)

_CITY_ALIASES = {
    "new delhi": "new delhi", "delhi": "new delhi", "nd": "new delhi", "del": "new delhi",
    "mumbai": "mumbai", "bombay": "mumbai", "mum": "mumbai", "bom": "mumbai",
    "chennai": "chennai", "madras": "chennai", "chn": "chennai",
    "hyderabad": "hyderabad", "hyd": "hyderabad",
    "kolkata": "kolkata", "calcutta": "kolkata", "kol": "kolkata", "ccu": "kolkata",
}


def normalize_city(location: str | None) -> str | None:
    loc = " ".join((location or "").lower().replace("vac", " ").split())
    if not loc or re.search(r",|/|&|\band\b", loc):  # several cities: not one specific slot
        return None
    return _CITY_ALIASES.get(loc, loc)


def _year(raw: str | None, month: int, day: int, today: date) -> int:
    if raw:
        y = int(raw)
        y = 2000 + y if y < 100 else y
        if 2024 <= y <= 2040:
            return y
    # no (usable) year: this year, or next year if that date is already well past
    y = today.year
    try:
        if (today - date(y, month, day)).days > 60:
            y += 1
    except ValueError:
        pass
    return y


def first_date(text: str | None, today: date | None = None) -> date | None:
    """The first calendar date mentioned in free text, or None."""
    text = text or ""
    today = today or date.today()
    found = []
    for m in _DAY_MONTH.finditer(text):
        found.append((m.start(), int(m.group(1)), _MONTHS[m.group(2).lower()[:3]], m.group(3)))
    for m in _MONTH_DAY.finditer(text):
        found.append((m.start(), int(m.group(2)), _MONTHS[m.group(1).lower()[:3]], m.group(3)))
    for _, day, month, year in sorted(found):
        try:
            return date(_year(year, month, day, today), month, day)
        except ValueError:
            continue  # e.g. "31 Nov": try the next mention
    return None


def slot_key(appointment_type: str | None, location: str | None, dates: str | None,
             today: date | None = None) -> str | None:
    """'biometrics|chennai|2026-11-30', or None if the slot can't be pinned down."""
    from datetime import datetime

    city = normalize_city(location)
    d = first_date(dates, today or datetime.now(IST).date())
    if not city or not d:
        return None
    return f"{(appointment_type or 'unknown').lower()}|{city}|{d.isoformat()}"

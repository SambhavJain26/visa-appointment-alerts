from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def format_seen(dt: datetime, zones: list[str]) -> str:
    """'06 Nov, 07:12 PM IST / 08:42 AM EST' - first zone gets the date, others just time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    zones = zones or ["Asia/Kolkata"]
    parts = []
    for i, z in enumerate(zones):
        local = dt.astimezone(ZoneInfo(z))
        fmt = "%d %b, %I:%M %p %Z" if i == 0 else "%I:%M %p %Z"
        parts.append(local.strftime(fmt))
    return " / ".join(parts)

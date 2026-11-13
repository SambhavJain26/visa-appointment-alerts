"""Source 2: CheckVisaSlots public H-1B page. Read about once per site refresh (~every 2 min).

The site only rebuilds its table about every 2 minutes, so instead of
re-reading an unchanged page every 25 s, each check is timed to land just after a refresh
(see RefreshSync): roughly 1/5 of the requests, for ~15-45 s instead of ~0-25 s delay.
Full Chromium uses its native identity, a persistent profile, and normal resource loading.
A browser is reused between checks; occasional restarts retain its cookies, local storage and cache.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .timeutil import IST
from .browser_network import BrowserNetwork

log = logging.getLogger("watcher.cvs")

FETCH_TIMEOUT_SECONDS = 90
COLUMNS = {
    "visa location": "location",
    "visa type": "visa_type",
    "earliest date": "earliest_date",
    "slots on earliest date": "slots_on_earliest",
    "total dates available": "total_dates",
    "last seen at": "last_seen",
    "relative time": "relative",
}


class BlockedError(Exception):
    def __init__(self, message, *, status=None, retry_after=0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


# The site's soft rate limit serves a frozen copy of its visa-type index at the requested URL,
# often without the x-matched-path header. The Next.js route data embedded in that page names
# the rate-limited route (as a segment list, not the plain path that every page's scripts mention),
# and its status line says the data was generated months ago; the real page is minutes old.
_RATE_LIMIT_ROUTE = re.compile(r'latest-us-visa-availability\\*"\s*,\s*\\*"rate-limited')
_STALE_GENERATED = re.compile(r"generated\s+(?:<[^>]*>\s*)*\d+\s+(?:mos?|months?|yrs?|years?)\s+ago",
                              re.IGNORECASE)  # the age sits inside <b><span> tags in the raw HTML


def rate_limit_marker(html: str) -> str | None:
    """Why this HTML is the site's rate-limit page, or None if it does not look like one."""
    if _RATE_LIMIT_ROUTE.search(html):
        return "page route is rate-limited"
    if _STALE_GENERATED.search(html):
        return "page data is months old"
    return None


def retry_after_seconds(value: str | None) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


# --------------------------------------------------------------------------- parsing
_GENERATED_AT = re.compile(r"generated at\s+(\d{1,2} [A-Za-z]{3} \d{4}, \d{1,2}:\d{2} [AP]M)",
                           re.IGNORECASE)
_TIME_FMT = "%d %b %Y, %I:%M %p"
IST_OFFSET_MIN = 330


def page_utc_offset_minutes(html_text: str, now: datetime | None = None) -> int:
    """Which time zone is the page showing? The site renders times in the *viewer's* zone, but the
    first paint can still be UTC. "The current info is generated at <time>" uses the same zone as
    the table and is always ~now, so comparing it to the real clock tells us the offset.
    Returns 0 (UTC) or 330 (IST); raises ValueError if it can't tell."""
    m = _GENERATED_AT.search(html_text)
    if not m:
        raise ValueError("'generated at' time not found; cannot tell the page's time zone")
    shown = datetime.strptime(m.group(1), _TIME_FMT)
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    diff = (shown - now_utc).total_seconds() / 60
    offset = int(round(diff / 30.0)) * 30  # page regenerates every ~2 min; round to zone step
    if offset not in (0, IST_OFFSET_MIN):
        raise ValueError(f"unexpected page time zone (offset {offset} min)")
    return offset


def _to_ist(value: str, offset_min: int) -> str:
    if offset_min == IST_OFFSET_MIN:
        return value
    try:
        dt = datetime.strptime(value, _TIME_FMT) + timedelta(minutes=IST_OFFSET_MIN - offset_min)
    except ValueError:
        return value
    return dt.strftime(_TIME_FMT)


def page_generated_at_utc(html_text: str, offset_min: int) -> datetime:
    """'The current info is generated at <time>' as an aware UTC datetime (minute precision)."""
    m = _GENERATED_AT.search(html_text)
    if not m:
        raise ValueError("'generated at' time not found")
    shown = datetime.strptime(m.group(1), _TIME_FMT) - timedelta(minutes=offset_min)
    return shown.replace(tzinfo=timezone.utc)


def parse_cvs(html: str, now: datetime | None = None) -> dict[str, dict]:
    """Parse the first availability table into {location: row}, with 'Last Seen At' in IST."""
    return parse_cvs_page(html, now)[0]


def parse_cvs_page(html: str, now: datetime | None = None) -> tuple[dict[str, dict], datetime]:
    """Like parse_cvs, also returning when the site last rebuilt the table (UTC)."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    if "security checkpoint" in title.lower():
        raise BlockedError("Vercel Security Checkpoint served")
    text = soup.get_text(" ", strip=True)
    offset = page_utc_offset_minutes(text, now)
    generated = page_generated_at_utc(text, offset)
    return _parse_table(soup, offset), generated


def _parse_table(soup: BeautifulSoup, offset: int) -> dict[str, dict]:
    for table in soup.find_all("table"):
        header_cells = table.find("tr").find_all(["th", "td"]) if table.find("tr") else []
        headers = [" ".join(c.get_text(" ", strip=True).split()).lower() for c in header_cells]
        keys = [COLUMNS.get(h) for h in headers]
        if "location" not in keys or "last_seen" not in keys:
            continue
        rows: dict[str, dict] = {}
        for tr in table.find_all("tr")[1:]:
            cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tr.find_all("td")]
            if len(cells) < len(keys):
                continue
            row = {k: v for k, v in zip(keys, cells) if k}
            if row.get("last_seen"):
                row["last_seen"] = _to_ist(row["last_seen"], offset)
            if row.get("location"):
                rows[row["location"]] = row
        if not rows:
            raise ValueError("availability table has no rows (page not fully rendered?)")
        return rows
    raise ValueError("availability table not found")


def changed_rows(old: dict[str, dict], new: dict[str, dict]) -> list[dict]:
    """Rows whose 'Last Seen At' changed (or new locations), given a non-empty previous snapshot."""
    if not old:
        return []
    out = []
    for loc, row in new.items():
        prev = old.get(loc)
        if prev is None or prev.get("last_seen") != row.get("last_seen"):
            out.append(row)
    return out


def parse_last_seen(value: str | None) -> datetime | None:
    """'06 Nov 2026, 01:31 PM' (IST) -> aware datetime, or None if unparseable."""
    try:
        return datetime.strptime((value or "").strip(), "%d %b %Y, %I:%M %p").replace(tzinfo=IST)
    except ValueError:
        return None


def _pretty_date(value: str | None) -> str:
    """'30 Nov, 26' -> '30 Nov 2026' (left unchanged if it doesn't match)."""
    v = (value or "").strip()
    try:
        return datetime.strptime(v, "%d %b, %y").strftime("%d %b %Y").lstrip("0")
    except ValueError:
        return v or "?"


def row_fields(row: dict) -> dict:
    """Structured alert fields for a CheckVisaSlots row (no AI needed)."""
    loc = row.get("location", "?").upper()
    return {
        "appointment_type": "biometrics" if loc.endswith("VAC") else "interview",
        "location": loc.removesuffix(" VAC").title(),
        "dates": f"Earliest {_pretty_date(row.get('earliest_date'))}",
        "extra": _slots_line(row.get("slots_on_earliest"), row.get("total_dates")),
    }


def _count_text(value: str | None, one: str, many: str) -> str | None:
    v = (value or "").strip()
    if not v.isdigit():
        return None
    return f"{v} {one if int(v) == 1 else many}"


def _slots_line(slots: str | None, total: str | None) -> str:
    """'6 slots on that date · 11 dates open' / 'Number of slots not reported · 1 date open'.
    The site shows N/A when a date was seen open but nobody reported how many slots it had."""
    s = _count_text(slots, "slot on that date", "slots on that date") or "Number of slots not reported"
    t = _count_text(total, "date open", "dates open")
    return f"{s} · {t}" if t else s


def describe_row(row: dict) -> str:
    f = row_fields(row)
    kind = "OFC/biometrics" if f["appointment_type"] == "biometrics" else "interview"
    return f"{f['location']} {kind}: {f['dates']} ({f['extra']}); reported {row.get('last_seen', '?')} IST"


# --------------------------------------------------------------------------- fetching
class BrowserFetcher:
    """Full Chromium with a durable profile; headed mode requires a display (Xvfb on the VPS).
    Restart after `max_uses` checks or an error, preserving browser state on disk."""

    def __init__(self, max_uses: int = 180, *, profile_dir: Path | None = None,
                 headed: bool = True, network_controls: bool = False):
        self.max_uses = max_uses
        self.profile_dir = profile_dir or Path("data/cvs-browser")
        self.headed = headed
        self.network_controls = network_controls
        self._pw = self._context = self._page = None
        self._uses = 0
        self._request_types = Counter()
        self._request_hosts = Counter()
        self._site_paths = Counter()
        self._prefetches = 0
        self._response_audit = {}
        self._network = None

    async def fetch(self, url: str) -> str:
        try:
            html = await asyncio.wait_for(self._fetch(url), FETCH_TIMEOUT_SECONDS)
        except BaseException:
            await self.close()  # any problem (incl. timeout): start clean next time
            raise
        finally:
            log.info("CVS network audit: %s", json.dumps({
                "resource_types": dict(self._request_types), "hosts": dict(self._request_hosts),
                "site_paths": dict(self._site_paths), "prefetches": self._prefetches,
                "main_response": self._response_audit,
                "transfer": self._network.snapshot() if self._network else {},
            }, ensure_ascii=True))
            self._request_types.clear()
            self._request_hosts.clear()
            self._site_paths.clear()
            self._prefetches = 0
            self._response_audit = {}
        self._uses += 1
        try:
            await self._leave_site()
        except Exception:
            # The table was already read; closing the browser also stops the page.
            log.warning("Could not blank the CVS tab; closing the browser instead", exc_info=True)
            await self.close()
        if self._uses >= self.max_uses:
            await self.close()
        return html

    async def _leave_site(self) -> None:
        """Park the tab on a blank page between checks, so none of the site's scripts keep
        running (or making requests) until the next check loads the page fresh."""
        if self._page is not None:
            await asyncio.wait_for(self._page.goto("about:blank"), 15)

    def _audit_request(self, request) -> None:
        """Observe normal traffic only: no queries, cookies, credentials or new requests."""
        url = urlsplit(request.url)
        self._request_types[request.resource_type] += 1
        self._request_hosts[url.hostname or "unknown"] += 1
        if url.hostname == "checkvisaslots.com" and len(self._site_paths) < 100:
            self._site_paths[url.path] += 1
        headers = request.headers
        if (headers.get("next-router-prefetch") or
                "prefetch" in headers.get("purpose", "") or
                "prefetch" in headers.get("sec-purpose", "")):
            self._prefetches += 1

    async def _ensure_page(self):
        if self._page is not None:
            return self._page
        from playwright.async_api import async_playwright  # heavy import, only when enabled

        self._pw = await async_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._context = await self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), channel="chromium", headless=not self.headed,
            timezone_id="Asia/Kolkata", locale="en-IN", viewport={"width": 1280, "height": 720},
            args=["--disable-dev-shm-usage"])
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        if self.network_controls:
            self._context.on("request", self._audit_request)
            self._network = BrowserNetwork(await self._context.new_cdp_session(self._page))
            await self._network.start()
        self._uses = 0
        log.info("CVS full Chromium started (headed=%s, persistent profile, normal resources)",
                 self.headed)
        return self._page

    @staticmethod
    def _rate_limited(resp, headers, reason: str) -> BlockedError:
        return BlockedError(f"HTTP {resp.status if resp else 'unknown'}: server returned rate-limit page; "
                            f"{reason}; request_id={headers.get('x-vercel-id', 'unknown')}",
                            status=429, retry_after=retry_after_seconds(headers.get("retry-after")))

    async def _fetch(self, url: str) -> str:
        page = await self._ensure_page()
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        headers = await resp.all_headers() if resp else {}
        self._response_audit = {k: v for k, v in headers.items() if k in {
            "date", "age", "cache-control", "x-vercel-cache", "x-vercel-id", "x-matched-path",
            "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"}}
        self._response_audit["status"] = resp.status if resp else None
        if "rate-limited" in headers.get("x-matched-path", "").lower():
            raise self._rate_limited(resp, headers, f"x-matched-path={headers['x-matched-path']}")
        if resp is not None and resp.status in (401, 403, 429):
            raise BlockedError(f"HTTP {resp.status}: the site is refusing access; "
                               f"request_id={headers.get('x-vercel-id', 'unknown')}",
                               status=resp.status,
                               retry_after=retry_after_seconds(headers.get("retry-after")))
        # The server's HTML is fully parsed at domcontentloaded: spot the rate-limit page now,
        # instead of waiting 45 s for a table it will never show.
        marker = rate_limit_marker(await page.content())
        if marker:
            raise self._rate_limited(resp, headers, marker)
        try:
            # This country preference is part of the site's normal first-visit UI.
            india = page.get_by_role("button", name="India", exact=True).or_(
                page.get_by_role("checkbox", name="India", exact=True)).first
            # The selector is hydrated asynchronously, after domcontentloaded.
            # Wait for either the actual table or the selector instead of testing too early.
            await page.locator("table tr td").or_(india).first.wait_for(state="visible", timeout=45_000)
            if await india.is_visible():
                await india.click()
            await page.wait_for_selector("table tr td", timeout=45_000)
            # let the page finish switching times to the viewer's zone (normalised anyway)
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
        except Exception:
            title = await page.title()
            if "403" in title or "forbidden" in title.lower():
                raise BlockedError(f"the site is refusing access ({title!r})")
            if "checkpoint" in title.lower():
                raise BlockedError(f"security checkpoint did not clear ({title!r})")
            html = await page.content()
            # Keep the returned public page for offline diagnosis; never fetch again to debug it.
            try:
                diagnostic = self.profile_dir.parent / "cvs-last-error.html"
                diagnostic.write_text(html, encoding="utf-8")
                os.chmod(diagnostic, 0o600)
            except Exception:
                log.exception("Could not save CVS failure page")
            marker = rate_limit_marker(html)
            if marker:
                raise self._rate_limited(resp, headers, marker)
            raise ValueError(f"availability table did not appear (HTTP {resp.status if resp else 'unknown'}, "
                             f"URL {page.url!r}, title {title!r})")
        return await page.content()

    async def close(self) -> None:
        context, pw = self._context, self._pw
        self._pw = self._context = self._page = None
        for closer in ((context.close if context else None), (pw.stop if pw else None)):
            if closer is None:
                continue
            try:
                await asyncio.wait_for(closer(), 15)
            except BaseException:
                pass


# --------------------------------------------------------------------------- scheduling
@dataclass
class RefreshSync:
    """Times each check to land just after the site rebuilds its table, instead of re-reading an
    unchanged page every 25 s. All times are epoch seconds (our clock).

    The page says when it was last rebuilt ("generated at", minute precision). Combined with when
    our previous check still saw the old version, that brackets the refresh moment. The next
    refresh is expected `period` seconds later; we check `lead` seconds after it.
    Feedback: if we arrive and the page has not been rebuilt yet ("early"), we aim later from then
    on; when it has ("fresh"), the timing is tightened a little. Whatever the estimate, two checks
    never start less than `min_gap` seconds apart."""

    period: float = 120.0     # how often the site rebuilds the table
    min_gap: float = 115.0    # hard floor between two check starts
    lead: float = 15.0        # how long after the expected refresh we arrive
    lead_min: float = 10.0
    lead_max: float = 60.0
    early_step: float = 10.0  # arrived before the refresh: aim this much later from now on
    tighten: float = 0.25     # arrived after it: aim this much earlier next time
    lead_decay: float = 0.25  # ... and let the extra margin from earlier misses shrink slowly
    gen_slack: float = 15.0   # their timestamp may lag the actual publish (or clocks differ) a bit
    period_gain: float = 0.05  # how quickly the cycle length follows observed timing errors
    nominal_period: float | None = None
    last_gen: float | None = None      # 'generated at' of the newest version we've seen
    last_refresh: float | None = None  # our estimate of when that version was built
    prev_start: float | None = None    # start of the previous check

    def observe(self, generated: float, start: float, end: float) -> str:
        """Record one successful check. Returns 'first', 'fresh' (a new version) or 'early'."""
        prev_start, self.prev_start = self.prev_start, start
        if self.last_gen is not None and generated <= self.last_gen:
            # Same version as last time: we came before their refresh. The refresh we were aiming
            # for is still to come, i.e. after `start`.
            self.lead = min(self.lead_max, self.lead + self.early_step)
            if self.last_refresh is not None:
                moved = max(0.0, start - self.period - self.last_refresh)
                self.last_refresh += moved
                self._learn_period(moved)
            return "early"

        first = self.last_gen is None
        # The rebuild happened within the shown minute, after the previous check (which still saw
        # the old version) and before this check finished.
        lo = max([generated] + ([prev_start] if (prev_start is not None and not first) else []))
        hi = min(generated + 60.0 + self.gen_slack, end)
        if lo > hi:  # our clock and theirs disagree: rely on our own observations only
            lo = prev_start if (prev_start is not None and not first) else end - self.period
            hi = end
        if self.last_refresh is None:
            est = (lo + hi) / 2
        else:
            k = max(1, round(((lo + hi) / 2 - self.last_refresh) / self.period))
            predicted = self.last_refresh + k * self.period
            est = min(max(predicted - self.tighten, lo), hi)
            self._learn_period((est - predicted) / k)
        self.last_gen, self.last_refresh = generated, est
        if not first:
            self.lead = max(self.lead_min, self.lead - self.lead_decay)
        return "first" if first else "fresh"

    def _learn_period(self, error: float) -> None:
        """Our prediction was `error` seconds off for one cycle: nudge the cycle length, so a site
        that rebuilds every 118 s or 123 s is followed too (kept within 10% of the nominal)."""
        if self.nominal_period is None:
            self.nominal_period = self.period
        p = self.period + self.period_gain * error
        self.period = min(max(p, self.nominal_period * 0.9), self.nominal_period * 1.1)

    def next_start(self, start: float, now: float) -> float:
        """When the next check should start, given the current check started at `start`."""
        floor = start + self.min_gap
        if self.last_refresh is None:
            return max(floor, now)
        target = self.last_refresh + self.period + self.lead
        while target < floor:  # too soon for that refresh: aim at the one after, staying in step
            target += self.period
        return max(target, now)


@dataclass
class CvsPoller:
    url: str
    interval: float
    store: object
    on_change: object  # callable(rows: list[dict]) -> None
    health: object  # SourceHealth
    on_status: object = None  # optional callable(text) for one-off status notes (feed bot)
    fetcher: object = field(default_factory=BrowserFetcher)
    refresh_period: float = 120.0  # how often the site rebuilds its table
    sync: RefreshSync | None = None
    last_generated: float | None = None  # 'generated at' of the latest page (epoch seconds)
    on_check: object = None  # callable(plain_text, audible): owner's feed only
    check_summaries: bool = False  # routine success notices are optional; failures/recovery always notify
    sync_checks: bool = True
    _failure_key: object = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if self.sync is None:
            self.sync = RefreshSync(period=self.refresh_period, min_gap=self.interval)

    async def poll_once(self) -> list[dict]:
        html = await self.fetcher.fetch(self.url)
        rows, generated = parse_cvs_page(html, datetime.fromtimestamp(time.time(), timezone.utc))
        self.last_generated = generated.timestamp()
        old = self.store.get_cvs_snapshot()
        changes = changed_rows(old, rows)
        self.store.save_cvs_snapshot(rows)
        if not old:
            log.info("CVS first fetch: stored %d rows, no alerts", len(rows))
            if self.on_status:
                self.on_status(f"🔌 CheckVisaSlots connected: reading {len(rows)} locations "
                               f"about once every {self.refresh_period / 60:g} minutes, just after the site refreshes.")
        elif changes:
            log.info("CVS changes: %s", [r["location"] for r in changes])
            self.on_change(changes)
        else:
            log.debug("CVS: no changes (%d rows)", len(rows))
        self.health.ok()
        recovered = self._failure_key is not None
        self._failure_key = None
        if self.check_summaries or recovered:
            self._notify(self.check_summary(rows, generated, changes, baseline=not old,
                                            recovered=recovered), recovered)
        return changes

    def _notify(self, text: str, audible: bool = False) -> None:
        if self.on_check:
            try:
                self.on_check(text, audible)
            except Exception:
                log.exception("CVS feed notification failed")  # feed cannot affect polling

    def check_summary(self, rows, generated, changes, *, baseline=False, recovered=False) -> str:
        def stamp(dt):
            return dt.astimezone(IST).strftime("%d %b, %I:%M %p IST")

        lines = ["✅ CheckVisaSlots " + ("recovered" if recovered else "checked") +
                 " · " + stamp(datetime.now(timezone.utc)),
                 f"Page updated: {stamp(generated)} · {len(rows)} locations"]
        dated = [(parse_last_seen(r.get("last_seen")), r) for r in rows.values()]
        dated = [(dt, r) for dt, r in dated if dt is not None]
        if dated:
            seen, row = max(dated, key=lambda item: item[0])
            f = row_fields(row)
            kind = "OFC/biometrics" if f["appointment_type"] == "biometrics" else "interview"
            lines += [f"Latest report: {f['location']} {kind} · {f['dates']}",
                      f"Report last seen: {stamp(seen)}", f["extra"]]
        else:
            lines.append("Latest report: timestamp unavailable")
        lines.append("Baseline saved; no old reports alerted" if baseline else
                     (f"Reports changed: {len(changes)}" if changes else "No new reports"))
        return "\n".join(lines)

    def _notify_failure(self, error, wait) -> None:
        key = ("blocked", error.status) if isinstance(error, BlockedError) else type(error).__name__
        audible = key != self._failure_key
        self._failure_key = key
        next_step = f"Next retry in {wait:.0f} seconds."
        detail = str(error).split("Browser logs:")[0].split("Call log:")[0].strip()
        diagnostics = [line.strip() for line in str(error).splitlines()
                       if "ERROR:" in line or "profile appears to be in use" in line]
        # Keep browser root causes, not pages of repeated launch flags / call-log copies.
        diagnostics = list(dict.fromkeys(diagnostics))
        if diagnostics:
            detail += "\n" + "\n".join(diagnostics[:3])
        self._notify(f"⚠️ CheckVisaSlots check failed · {datetime.now(IST):%d %b, %I:%M %p IST}\n"
                     f"{type(error).__name__}: {detail[:2200]}\n{next_step}", audible)

    async def run(self) -> None:
        """Synced rhythm: each check is timed to start just after the site's next expected
        refresh (RefreshSync). Two check starts are never closer than `interval` seconds (and
        never overlap); this is enforced on the monotonic clock too, whatever the estimate says.
        Failures of any kind (rate-limit page, 403, errors) are retried on the same ~2-minute
        rhythm: no backoff, no Retry-After, no pause (owner's choice, 10 Nov 2026)."""
        # Preserve the minimum request gap across process/container restarts too.
        previous = float(self.store.get_meta("cvs_last_check_started") or 0)
        not_before = float(self.store.get_meta("cvs_next_check_not_before") or 0)
        # Only an explicitly requested operator cooldown can hold checks back.
        cooldown_until = float(self.store.get_meta("cvs_manual_cooldown_until") or 0)
        if cooldown_until > time.time():
            self.health.defer_checks_until(max(cooldown_until, not_before))
            if self.on_status:
                due = datetime.fromtimestamp(max(cooldown_until, not_before), IST)
                self.on_status(f"⏸ CheckVisaSlots: requested cooldown. "
                               f"Automatic checks resume no earlier than {due:%d %b, %I:%M %p IST}.")
        await asyncio.sleep(max(random.uniform(5, 20), previous + self.interval - time.time(),
                                not_before - time.time(), cooldown_until - time.time()))
        retry_gap = max(self.interval, self.refresh_period)  # start-to-start gap after a failure
        try:
            while True:
                started = time.monotonic()
                started_wall = time.time()
                try:
                    self.store.set_meta("cvs_last_check_started", str(started_wall))
                    await self.poll_once()
                    self._count("cvs_check")
                    end_wall = time.time()
                    if self.sync_checks:
                        result = self.sync.observe(self.last_generated, started_wall, end_wall)
                        if result == "early":
                            self._count("cvs_early")
                        target = self.sync.next_start(started_wall, end_wall)
                        wait = max(target - end_wall, self.interval - (time.monotonic() - started))
                        log.info("CVS %s; lead %.0fs, cycle %.1fs; next check in %.0fs",
                                 result, self.sync.lead, self.sync.period, wait)
                    else:
                        wait = max(0.0, self.interval - (time.monotonic() - started))
                        log.info("CVS fixed %.0fs schedule; next check in %.0fs", self.interval, wait)
                except BlockedError as e:
                    self._count("cvs_error")
                    self.health.fail(f"blocked by the site: {e}")
                    wait = max(0.0, retry_gap - (time.monotonic() - started))
                    self._notify_failure(e, wait)
                    log.warning("CVS blocked (%s); next try in %.0fs", e, wait)
                except Exception as e:
                    self._count("cvs_error")
                    self.health.fail(f"{type(e).__name__}: {e}")
                    wait = max(0.0, retry_gap - (time.monotonic() - started))
                    self._notify_failure(e, wait)
                    log.warning("CVS error (%s: %s); next try in %.0fs", type(e).__name__, e, wait)
                self.store.set_meta("cvs_next_check_not_before", str(time.time() + max(0.0, wait)))
                await asyncio.sleep(max(0.0, wait))
        finally:
            close = getattr(self.fetcher, "close", None)
            if close:
                await close()

    def _count(self, kind: str) -> None:
        try:
            self.store.add_event(kind)  # request volume, reported in the daily check-in
        except Exception:
            pass

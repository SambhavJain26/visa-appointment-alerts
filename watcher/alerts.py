"""Step 3: alert delivery. Main channel: your own Telegram bot (official Bot API, send-only).
Optional: WhatsApp via CallMeBot.

The bot never reads incoming messages (the service never polls bot updates and has no webhook),
so nothing a user types to the bot can affect the watcher.

Each target has its own queue + worker, so a failing target never delays the others.
"""
from __future__ import annotations

import asyncio
import dataclasses
import html
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from .config import BotChat, Recipient

log = logging.getLogger("watcher.alerts")
alert_log = logging.getLogger("watcher.audit")

CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
TELEGRAM_API = "https://api.telegram.org"
RETRY_DELAYS = [5, 20, 60, 180, 600]  # seconds; then give up

HEADERS = {
    "biometrics": "🚨 <b>BIOMETRICS (OFC) SLOT</b>",
    "interview": "🚨 <b>INTERVIEW SLOT</b>",
    "both": "🚨 <b>BIOMETRICS + INTERVIEW SLOTS</b>",
    "unknown": "🚨 <b>SLOT SEEN</b> · type not clear",
}
AI_FAILED_HEADER = "⚠️ <b>CHECK THIS POST</b> · automatic check failed"
OFC_REMINDER = "⏳ After booking OFC you have ~45 min to book the interview."


# --------------------------------------------------------------------------- formatting
def _trim(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def format_time(dt: datetime, zones: list[str]) -> str:
    """'30 Sep, 1:06 PM IST · 3:36 AM EDT' - date only on the first zone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    parts = []
    for i, z in enumerate(zones or ["Asia/Kolkata"]):
        local = dt.astimezone(ZoneInfo(z))
        t = f"{_clock(local)} {local.strftime('%Z')}"
        parts.append(f"{local.day} {local.strftime('%b')}, {t}" if i == 0 else t)
    return " · ".join(parts)


def clean_group_title(title: str) -> str:
    """'H1B/H4 Regular/ER Visa Slots(No discussions)' -> 'H1B/H4 Regular/ER Visa Slots'."""
    t = re.sub(r"\s*\([^)]*\)\s*", " ", title or "")
    return _trim(t, 60) or title


@dataclass
class SlotAlert:
    appointment_type: str  # biometrics | interview | both | unknown
    location: str
    dates: str
    summary: str
    original_text: str | None
    source: str
    link: str
    link_label: str
    seen: datetime
    zones: list[str] = field(default_factory=lambda: ["Asia/Kolkata"])
    extra: str = ""  # e.g. CheckVisaSlots slot counts
    ai_failed: bool = False
    has_image: bool = False


def render_alert(a: SlotAlert) -> str:
    """HTML for Telegram (parse_mode=HTML). All dynamic text is escaped."""
    e = html.escape
    header = AI_FAILED_HEADER if a.ai_failed else HEADERS.get(a.appointment_type, HEADERS["unknown"])
    lines = [header, ""]
    if not a.ai_failed:
        lines.append(f"📍 <b>{e(a.location)}</b>" if a.location else "📍 Location not mentioned")
        lines.append(f"📅 {e(a.dates)}" if a.dates else "📅 Date not mentioned")
        if a.extra:
            lines.append(f"🎟 {e(a.extra)}")
        lines.append("")
    orig = _trim(a.original_text or "", 180)
    if orig:
        lines.append(f"💬 <i>{e(orig)}</i>")
    elif a.has_image:
        lines.append(f"🖼 <i>Screenshot: {e(_trim(a.summary, 120))}</i>")
    if orig or a.has_image:
        lines.append("")
    lines.append(f"👥 {e(a.source)}")
    lines.append(f"🕒 Posted {e(format_time(a.seen, a.zones))}")
    lines.append(f'🔗 <a href="{e(a.link, quote=True)}">{e(a.link_label)}</a>')
    if a.ai_failed or a.appointment_type in ("biometrics", "both", "unknown"):
        lines += ["", OFC_REMINDER]
    return "\n".join(lines)


def html_to_plain(s: str) -> str:
    """For WhatsApp: <b>x</b> -> *x*, <i>x</i> -> _x_, links -> 'label: url', strip the rest."""
    s = re.sub(r"</?b>", "*", s)
    s = re.sub(r"</?i>", "_", s)
    s = re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"\2: \1", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


# --------------------------------------------------------------------------- targets
@dataclass
class Outgoing:
    html: str
    kind: str  # "slot" | "system" | "feed"
    preview: bool = False  # show link preview (useful for screenshot posts)
    silent: bool = False  # deliver without a notification sound (feed bot)
    # Optional screenshot (in memory only, never written to disk). Sent with sendPhoto,
    # the html becomes the caption. Dropped automatically if Telegram rejects it.
    photo: bytes | None = field(default=None, repr=False)
    photo_mime: str = "image/jpeg"


PHOTO_CAPTION_LIMIT = 1024  # Telegram's caption limit (characters)
MAX_QUEUED_PHOTOS = 50  # memory guard: beyond this, photos are dropped (link stays)


class Target:
    """One alert destination. send() returns (ok, detail, retry_after, permanent_failure)."""

    label: str
    min_gap: float

    async def send(self, http: httpx.AsyncClient, item: Outgoing):
        raise NotImplementedError


class TelegramBotTarget(Target):
    def __init__(self, token: str, chat: BotChat, min_gap: float = 1.5, bot: str = "alerts-bot"):
        self.token = token
        self.chat = chat
        self.label = f"{bot}:{chat.masked}"
        self.min_gap = min_gap

    def _clean(self, s: str) -> str:
        return s.replace(self.token, "<token>")

    async def send(self, http, item):
        if item.photo is not None and len(item.html) <= PHOTO_CAPTION_LIMIT:
            return await self._send_photo(http, item)
        url = f"{TELEGRAM_API}/bot{self.token}/sendMessage"
        try:
            resp = await http.post(url, json={
                "chat_id": self.chat.chat_id,
                "text": item.html[:4000],
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": not item.preview},
                "disable_notification": item.silent,
            })
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            return False, self._clean(f"{type(e).__name__}: {e}"), None, False
        return self._result(resp, data)

    async def _send_photo(self, http, item):
        url = f"{TELEGRAM_API}/bot{self.token}/sendPhoto"
        ext = {"image/png": "png", "image/webp": "webp"}.get(item.photo_mime, "jpg")
        try:
            resp = await http.post(url, data={
                "chat_id": self.chat.chat_id,
                "caption": item.html,
                "parse_mode": "HTML",
                "disable_notification": "true" if item.silent else "false",
            }, files={"photo": (f"screenshot.{ext}", item.photo, item.photo_mime)})
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            return False, self._clean(f"{type(e).__name__}: {e}"), None, False
        return self._result(resp, data, photo=True)

    def _result(self, resp, data, photo: bool = False):
        if data.get("ok"):
            return True, "ok", None, False
        retry = (data.get("parameters") or {}).get("retry_after")
        detail = self._clean(f"HTTP {resp.status_code}: {data.get('description')}")
        # 400 bad request / 401 bad token / 403 bot blocked / 404: retrying won't help.
        permanent = resp.status_code in (400, 401, 403, 404)
        if permanent and resp.status_code == 400 and ("parse" in detail.lower() or photo):
            permanent = False  # formatting/photo problem: retried below as text / without photo
        return False, detail, retry, permanent


class WhatsAppTarget(Target):
    def __init__(self, r: Recipient, min_gap: float = 10.0):
        self.r = r
        self.label = f"whatsapp:{r.masked}"
        self.min_gap = min_gap

    async def send(self, http, item):
        try:
            resp = await http.get(CALLMEBOT_URL, params={
                "phone": self.r.phone, "text": html_to_plain(item.html), "apikey": self.r.apikey})
        except httpx.HTTPError as e:
            return False, type(e).__name__, None, False
        low = (resp.text or "").lower()
        if resp.status_code != 200 or any(
                m in low for m in ("error", "invalid", "not activated", "apikey is not", "blocked")):
            return False, f"HTTP {resp.status_code}: {_trim(re.sub('<[^>]+>', ' ', resp.text), 200)}", None, False
        return True, "ok", None, False


# --------------------------------------------------------------------------- sender
class AlertSender:
    """Fan-out to per-target queues. In dry-run mode prints/logs instead of sending."""

    def __init__(self, targets: list[Target], *, dry_run: bool):
        self.targets = targets
        self.dry_run = dry_run
        self._queues: dict[str, asyncio.Queue[Outgoing]] = {t.label: asyncio.Queue() for t in targets}
        self._last_sent: dict[str, float] = {}
        self._http = httpx.AsyncClient(timeout=30)

    def enqueue_html(self, html_text: str, kind: str = "slot", preview: bool = False,
                     silent: bool = False, photo: bytes | None = None,
                     photo_mime: str = "image/jpeg") -> None:
        if photo is not None and self._queued_photos() >= MAX_QUEUED_PHOTOS:
            log.warning("Too many queued photos; sending this one as a link only")
            photo = None
        item = Outgoing(html_text, kind, preview, silent, photo, photo_mime)
        if self.dry_run:
            alert_log.info("DRY-RUN would send (%s):\n%s", kind, html_to_plain(html_text))
            print(f"\n----- DRY-RUN alert ({kind}) -----\n{html_to_plain(html_text)}\n", flush=True)
            return
        if not self.targets:
            log.error("No alert targets configured; dropping message:\n%s", html_to_plain(html_text))
            return
        for q in self._queues.values():
            q.put_nowait(item)

    def _queued_photos(self) -> int:
        return sum(1 for q in self._queues.values() for it in list(q._queue) if it.photo is not None)

    def enqueue(self, text: str, kind: str = "system", silent: bool = False) -> None:
        """Plain text (system messages)."""
        self.enqueue_html(html.escape(text), kind, silent=silent)

    async def run(self) -> None:
        await asyncio.gather(*(self._worker(t) for t in self.targets))
        if not self.targets:
            await asyncio.Event().wait()

    async def _worker(self, t: Target) -> None:
        q = self._queues[t.label]
        while True:
            item = await q.get()
            try:
                await self._send_with_retry(t, item)
            except Exception:
                log.exception("Unexpected error sending to %s", t.label)
            finally:
                q.task_done()

    async def send_now(self, target: Target, html_text: str, preview: bool = False) -> tuple[bool, str]:
        ok, detail, _, _ = await target.send(self._http, Outgoing(html_text, "test", preview))
        return ok, detail

    async def _send_with_retry(self, t: Target, item: Outgoing) -> bool:
        delays = [0, *RETRY_DELAYS]
        for attempt, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            await self._pace(t)
            ok, detail, retry_after, permanent = await t.send(self._http, item)
            if ok:
                alert_log.info("SENT %s to %s", item.kind, t.label)
                return True
            log.warning("Send to %s failed (attempt %d): %s", t.label, attempt + 1, detail)
            if item.photo is not None and "HTTP 400" in detail and "parse" not in detail.lower():
                # Telegram rejected the image (format/size): send the same message without it.
                item = dataclasses.replace(item, photo=None)
                continue
            if "parse" in detail.lower() and "<" in item.html:
                # Telegram rejected the HTML: fall back to plain text so the alert still arrives.
                item = dataclasses.replace(item, html=html.escape(html_to_plain(item.html)))
                continue
            if permanent:
                log.error("Permanent error sending to %s; not retrying: %s", t.label, detail)
                return False
            if retry_after:
                await asyncio.sleep(float(retry_after) + 1)
        log.error("Giving up sending to %s after %d attempts", t.label, len(delays))
        return False

    async def _pace(self, t: Target) -> None:
        wait = t.min_gap - (time.monotonic() - self._last_sent.get(t.label, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_sent[t.label] = time.monotonic()

    async def drain(self, timeout: float = 30) -> None:
        try:
            await asyncio.wait_for(
                asyncio.gather(*(q.join() for q in self._queues.values())), timeout)
        except asyncio.TimeoutError:
            pass

    def pending(self) -> int:
        return sum(q.qsize() for q in self._queues.values())

    async def aclose(self) -> None:
        await self._http.aclose()


def build_targets(cfg) -> list[Target]:
    targets: list[Target] = []
    if cfg.tg_bot_token:
        targets += [TelegramBotTarget(cfg.tg_bot_token, c) for c in cfg.bot_chats]
    targets += [WhatsAppTarget(r, cfg.callmebot_min_gap) for r in cfg.recipients]
    return targets

"""Wires everything together: pollers -> NA filter -> AI check -> alert queue.

Robustness:
- every loop runs under a supervisor that restarts it with backoff instead of crashing the process
- a watchdog thread hard-exits (Docker restarts us) if the Telegram loop or the event loop freezes
- startup messages are suppressed on quick restarts; repeated restarts trigger one warning
- posts older than MAX_ALERT_AGE_MINUTES are never alerted (e.g. backlog after downtime)
- the alert bot is send-only: incoming bot messages are never read
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone

from .ai_check import AIChecker
from .alerts import (AlertSender, SlotAlert, TelegramBotTarget, build_targets, clean_group_title,
                     format_time, render_alert)
from .config import Config
from .cvs_poller import BrowserFetcher, CvsPoller, describe_row, parse_last_seen, row_fields
from .db import Store
from .health import SourceHealth
from .na_filter import classify, has_availability_signal
from .slotkey import slot_key
from .telegram_poller import TelegramPoller, TgMessage, download_image, make_client
from .timeutil import IST, now_utc

log = logging.getLogger("watcher")
audit = logging.getLogger("watcher.audit")

WATCHDOG_STALE_SECONDS = 600
QUICK_RESTART_SECONDS = 600
RESTART_WARN_COUNT = 3  # restarts within an hour that trigger a warning


def setup_logging(cfg: Config) -> None:
    logs = cfg.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(cfg.log_level)
    root.handlers.clear()
    fh = logging.handlers.RotatingFileHandler(
        logs / "watcher.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    # Separate audit file: every skip / forward decision, easy to grep.
    ah = logging.handlers.RotatingFileHandler(
        logs / "decisions.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    ah.setFormatter(fmt)
    audit.handlers.clear()
    audit.addHandler(ah)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # never log URLs (bot token)


def _short(s: str, n: int = 80) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


class Pipeline:
    def __init__(self, cfg: Config, store: Store, ai: AIChecker, sender: AlertSender,
                 client=None, ai_health: SourceHealth | None = None,
                 feed: AlertSender | None = None):
        self.cfg = cfg
        self.feed_sender = feed  # optional feed bot: copy of every message + decision
        self.store = store
        self.ai = ai
        self.sender = sender
        self.client = client
        self.ai_health = ai_health
        self._recent_summaries: dict[str, float] = {}
        self._recent_by_sender: dict[tuple, float] = {}  # in memory only; never persisted
        self._sem = asyncio.Semaphore(4)
        self._tasks: set[asyncio.Task] = set()  # keep references so tasks aren't GC'd

    # --- Telegram ---------------------------------------------------------
    async def on_telegram(self, msg: TgMessage) -> None:
        self.store.add_event("read")
        tag = f"@{msg.group}/{msg.id}"
        fr = classify(msg.text, has_image=msg.has_image,
                      is_service=msg.is_service, is_sticker=msg.is_sticker)
        if fr.skip:
            audit.info("SKIP filter [%s] %s | %r", fr.reason, tag, _short(msg.text))
            self._feed(msg, f"⏭ Skipped · {fr.reason}")
            return
        age = (now_utc() - msg.date).total_seconds()
        if age > self.cfg.max_alert_age_minutes * 60:
            audit.info("SKIP stale (%d min old) %s | %r", age // 60, tag, _short(msg.text))
            self._feed(msg, f"⏭ Skipped · too old ({int(age // 60)} min)")
            return
        if self._is_same_sender_repeat(msg):
            audit.info("SKIP dup-sender (same person, same post, another group) %s | %r",
                       tag, _short(msg.text))
            self._feed(msg, "⏭ Skipped · same person already posted this in another group")
            return
        # AI check runs concurrently so polling is never blocked.
        t = asyncio.create_task(self._ai_and_alert(msg, tag))
        self._tasks.add(t)
        t.add_done_callback(self._task_done)

    def _is_same_sender_repeat(self, msg: TgMessage) -> bool:
        """Same person posting the same text (or forwarding the same screenshot) within the window,
        e.g. cross-posting one sighting to several groups. Checked before the AI, so no extra cost."""
        if msg.sender_id is None:
            return False
        norm = re.sub(r"\W+", " ", (msg.text or "").lower()).strip()
        if norm:
            key = (msg.sender_id, norm, msg.has_image)
        elif msg.media_id is not None:
            key = (msg.sender_id, "", msg.media_id)
        else:
            return False
        now = time.time()
        window = self.cfg.same_sender_window
        self._recent_by_sender = {k: t for k, t in self._recent_by_sender.items() if now - t < window}
        if key in self._recent_by_sender:
            return True
        self._recent_by_sender[key] = now
        return False

    def _task_done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception():
            log.error("AI/alert task failed: %r", t.exception())

    async def _ai_and_alert(self, msg: TgMessage, tag: str) -> None:
        async with self._sem:
            images = []
            if msg.has_image and self.client is not None:
                img = await download_image(self.client, msg, self.cfg.max_image_bytes)
                if img:
                    images.append(img)
            res = await self.ai.check(msg.text, images)
        if self.ai_health:
            (self.ai_health.fail("AI check failing (see logs)") if res.failed else self.ai_health.ok())
        kind = "img+text" if images and msg.text else ("img" if images else "text")
        if res.failed and not looks_like_sighting(msg):
            # AI is down and this post has no screenshot and no slot words: don't alert the
            # family about chatter. The owner still sees it in the feed + gets the AI warning.
            audit.info("SKIP ai-failed, no slot signs [%s] %s | %r", kind, tag, _short(msg.text))
            self._feed(msg, "⚠️ AI check failed · not alerted (no screenshot or slot words)",
                       images)
            return
        if not res.availability:
            audit.info("SKIP ai [%s] %s | %r -> %s", kind, tag, _short(msg.text), res.summary)
            self._feed(msg, f"🤖 AI: no slot · {html.escape(res.summary)}", images)
            return
        alert = SlotAlert(
            appointment_type=res.appointment_type, location=res.location, dates=res.dates,
            summary=res.summary, original_text=msg.text,
            source=clean_group_title(msg.group_title), link=msg.link,
            link_label="Open the post in Telegram", seen=msg.date, zones=self.cfg.timezones,
            ai_failed=res.failed, has_image=msg.has_image,
        )
        outcome = self.forward(
            key=f"tg:{msg.group.lower()}:{msg.id}",
            album_key=f"tgalbum:{msg.group.lower()}:{msg.grouped_id}" if msg.grouped_id else None,
            alert=alert, tag=tag, preview=msg.has_image,
            dedupe_text=None if res.failed else slot_key(res.appointment_type, res.location,
                                                         res.dates),
            photo=images[0] if images else None,
        )
        if outcome != "sent":
            self._feed(msg, "⏭ Skipped · duplicate of an alert already sent", images)
        elif res.failed:
            self._feed(msg, "⚠️ <b>AI check failed → alert sent anyway</b>", images)
        else:
            what = " · ".join(html.escape(x) for x in
                              (res.appointment_type, res.location, res.dates) if x)
            self._feed(msg, f"🚨 <b>ALERT SENT</b> · {what}", images)

    def _feed(self, msg: TgMessage, decision_html: str, images=None) -> None:
        """Copy to the feed bot. If the AI already downloaded the screenshot (in memory), send that
        exact image as a photo; otherwise a text message with the post link (preview on)."""
        if self.feed_sender is None:
            return
        photo, mime = (images[0] if images else (None, "image/jpeg"))
        try:
            self.feed_sender.enqueue_html(render_feed(msg, decision_html), "feed",
                                          preview=msg.has_image and photo is None, silent=True,
                                          photo=photo, photo_mime=mime)
        except Exception:
            log.exception("feed enqueue failed")  # the feed must never affect alerting

    # --- CheckVisaSlots ---------------------------------------------------
    def on_cvs_changes(self, rows: list[dict]) -> None:
        """A row's 'Last Seen At' changed = someone reported a new sighting. No AI needed."""
        for row in rows:
            self.store.add_event("read")
            f = row_fields(row)
            tag = f"cvs:{row['location']}"
            seen = parse_last_seen(row.get("last_seen"))
            age = (now_utc() - seen).total_seconds() if seen else 0
            if seen and age > self.cfg.cvs_max_alert_age_minutes * 60:
                audit.info("SKIP stale cvs (%d min old) %s -> %s", age // 60, tag, describe_row(row))
                self._feed_cvs(row, f"⏭ Skipped · too old ({int(age // 60)} min)")
                continue
            alert = SlotAlert(
                appointment_type=f["appointment_type"], location=f["location"], dates=f["dates"],
                summary="", original_text=None, source="CheckVisaSlots website",
                link=self.cfg.cvs_url, link_label="Open CheckVisaSlots", seen=seen or now_utc(),
                zones=self.cfg.timezones, extra=f["extra"],
            )
            outcome = self.forward(key=f"cvs:{row['location']}:{row.get('last_seen')}",
                                   album_key=None, alert=alert, tag=tag, preview=False,
                                   dedupe_text=slot_key(f["appointment_type"], f["location"],
                                                        row.get("earliest_date")))
            self._feed_cvs(row, "🚨 <b>ALERT SENT</b>" if outcome == "sent"
                           else "⏭ Skipped · duplicate of an alert already sent")

    def _feed_cvs(self, row: dict, decision_html: str) -> None:
        if self.feed_sender is None:
            return
        try:
            self.feed_sender.enqueue_html(render_cvs_feed(row, decision_html), "feed", silent=True)
        except Exception:
            log.exception("feed enqueue failed")

    # --- common -----------------------------------------------------------
    def forward(self, *, key, album_key, alert: SlotAlert, tag, preview, dedupe_text,
                photo=None) -> str:
        """Dedupe, then queue the alert. `photo` = (bytes, mime) of the screenshot the AI read,
        sent as a real photo with the alert as caption (falls back to text if rejected)."""
        if album_key and self.store.is_alerted(album_key):
            audit.info("SKIP dup-album %s", tag)
            return "dup-album"
        if not self.store.mark_alerted(key):
            audit.info("SKIP dup-id %s", tag)
            return "dup-id"
        if album_key:
            self.store.mark_alerted(album_key)
        if dedupe_text and alert.location:
            now = time.time()
            self._recent_summaries = {k: t for k, t in self._recent_summaries.items()
                                      if now - t < self.cfg.dup_summary_window}
            if dedupe_text in self._recent_summaries:
                audit.info("SKIP dup-summary %s -> %s", tag, dedupe_text)
                return "dup-summary"
            self._recent_summaries[dedupe_text] = now
        self.store.add_event("forwarded")
        audit.info("FORWARD %s -> %s | %s | %s | %s", tag, alert.appointment_type,
                   alert.location, alert.dates, alert.summary)
        img, mime = photo if photo else (None, "image/jpeg")
        self.sender.enqueue_html(render_alert(alert), "slot", preview=preview and img is None,
                                 photo=img, photo_mime=mime)
        return "sent"


_SIGHTING_WORDS = re.compile(
    r"\b(?:ofc|vac|biometric\w*|interview|consular|ca|slots?|dates?|appointments?|calendar)\b",
    re.IGNORECASE)
_CITY_WORDS = re.compile(
    r"\b(?:chn|chennai|hyd|hyderabad|mum|bom|mumbai|del|nd|delhi|kol|ccu|kolkata|calcutta)\b",
    re.IGNORECASE)


def looks_like_sighting(msg: TgMessage) -> bool:
    """Used only when the AI is unavailable: could this post be a real sighting?
    A screenshot, availability words / dates, or a city together with an appointment word."""
    text = msg.text or ""
    return bool(msg.has_image or has_availability_signal(text)
                or (_CITY_WORDS.search(text) and _SIGHTING_WORDS.search(text)))


def render_cvs_feed(row: dict, decision_html: str) -> str:
    """Feed-bot copy of a CheckVisaSlots change."""
    e = html.escape
    return "\n".join([
        "📥 <b>CheckVisaSlots website</b> · new report",
        f"📍 {e(row.get('location', '?'))} · earliest {e(row.get('earliest_date', '?'))} · "
        f"{e(row.get('slots_on_earliest', '?'))} slot(s) · {e(row.get('total_dates', '?'))} date(s)",
        f"🕒 Reported {e(row.get('last_seen', '?'))} IST",
        decision_html,
    ])


def render_sender(msg: TgMessage) -> str:
    """'👤 Ravi Kumar (@ravik) · id 123456' - feed bot only."""
    e = html.escape
    parts = []
    if msg.sender_name:
        parts.append(e(msg.sender_name))
    if msg.sender_username:
        u = e(msg.sender_username, quote=True)
        parts.append(f'(<a href="https://t.me/{u}">@{u}</a>)')
    who = " ".join(parts) or "unknown sender"
    sid = f" · id {msg.sender_id}" if msg.sender_id is not None else ""
    return f"👤 {who}{sid}"


def render_feed(msg: TgMessage, decision_html: str) -> str:
    """One feed-bot message: which group, when, what was posted, and what the watcher decided."""
    e = html.escape
    when = format_time(msg.date, ["Asia/Kolkata"]).split(", ", 1)[-1]
    text = " ".join((msg.text or "").split())
    if len(text) > 300:
        text = text[:299] + "…"
    if msg.is_service:
        body = "⚙️ <i>service message (join/leave/pin)</i>"
    elif msg.is_sticker:
        body = "🏷 <i>sticker</i>"
    elif text:
        body = f"💬 {e(text)}" + ("\n🖼 <i>+ screenshot</i>" if msg.has_image else "")
    elif msg.has_image:
        body = "🖼 <i>screenshot, no text</i>"
    else:
        body = "<i>(no text)</i>"
    return "\n".join([
        f"📥 <b>{e(clean_group_title(msg.group_title))}</b> · {e(when)}",
        render_sender(msg),
        body,
        decision_html,
        f'🔗 <a href="{e(msg.link, quote=True)}">Open</a>',
    ])


# --------------------------------------------------------------------------- supervision
async def supervise(name: str, factory, health: SourceHealth | None = None) -> None:
    """Run factory() forever; on crash log, mark health, and restart with backoff."""
    backoff = 5
    while True:
        started = time.monotonic()
        try:
            await factory()
            log.warning("Task %s exited; restarting", name)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Task %s crashed; restarting in %ds", name, backoff)
            if health:
                health.fail(f"{name} crashed: {type(e).__name__}: {e}")
        if time.monotonic() - started > 300:
            backoff = 5
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300)


class Watchdog:
    """Background thread: if the event loop or the Telegram loop freezes, exit so Docker restarts."""

    def __init__(self, stale_seconds: int = WATCHDOG_STALE_SECONDS):
        self.stale = stale_seconds
        self.beats: dict[str, callable] = {}

    def watch(self, name: str, get_ts) -> None:
        self.beats[name] = get_ts

    def start(self) -> None:
        threading.Thread(target=self._loop, name="watchdog", daemon=True).start()

    def _loop(self) -> None:
        while True:
            time.sleep(30)
            now = time.time()
            for name, get_ts in list(self.beats.items()):
                age = now - get_ts()
                if age > self.stale:
                    log.critical("WATCHDOG: %s stuck for %ds; exiting so Docker restarts", name, age)
                    logging.shutdown()
                    os._exit(3)


async def housekeeping(cfg: Config, store: Store, sender: AlertSender,
                       healths: list[SourceHealth], tick: dict) -> None:
    hh, mm = (int(x) for x in cfg.heartbeat_time.split(":"))
    start = datetime.now(IST)
    if (start.hour, start.minute) >= (hh, mm) and store.get_meta("heartbeat_date") is None:
        store.set_meta("heartbeat_date", start.strftime("%Y-%m-%d"))
    while True:
        tick["ts"] = time.time()
        await asyncio.sleep(30)
        tick["ts"] = time.time()
        for h in healths:
            msg = h.check()
            if msg:
                log.warning(msg.replace("\n", " | "))
                sender.enqueue(msg)
        now = datetime.now(IST)
        today = now.strftime("%Y-%m-%d")
        if (now.hour, now.minute) >= (hh, mm) and store.get_meta("heartbeat_date") != today:
            store.set_meta("heartbeat_date", today)
            since = time.time() - 86400
            read = store.count_events("read", since)
            fwd = store.count_events("forwarded", since)
            problems = [h.name for h in healths if h.is_failing()]
            status = "all sources OK" if not problems else "problems: " + ", ".join(problems)
            text = (f"💚 Watcher running ({status}).\n"
                    f"Last 24h: {read} messages read, {fwd} alerts sent.")
            if cfg.cvs_enabled:
                checks = store.count_events("cvs_check", since)
                errors = store.count_events("cvs_error", since)
                early = store.count_events("cvs_early", since)
                text += (f"\nCheckVisaSlots: {checks} checks ({checks / 24:.0f}/hour), "
                         f"{early} before a site refresh, {errors} errors.")
            sender.enqueue(text, silent=True)
            store.prune()


def record_start(store: Store, sender: AlertSender, dry_run: bool,
                 instance: str | None = None) -> None:
    """Startup message, but not on quick crash-restarts; warn once if crash-restarting repeatedly.

    `instance` is the container hostname. Docker keeps it when it restarts a crashed container,
    but a deliberate update (`docker compose up --force-recreate`) creates a new container with a
    new hostname, so updates never count as crashes.
    """
    instance = instance or os.getenv("WATCHER_INSTANCE_ID") or os.getenv("HOSTNAME") or socket.gethostname()
    now = time.time()
    try:
        entries = [e for e in json.loads(store.get_meta("starts") or "[]")
                   if isinstance(e, list) and len(e) == 2 and now - e[0] < 3600]
    except ValueError:
        entries = []
    same = [e for e in entries if e[1] == instance]  # earlier starts of THIS container
    quick = bool(same) and now - same[-1][0] < QUICK_RESTART_SECONDS
    entries.append([now, instance])
    store.set_meta("starts", json.dumps(entries[-20:]))
    crash_restarts = len(same)  # every earlier start of the same container = one restart
    if not quick:
        sender.enqueue("✅ Slot watcher started." + (" (dry run)" if dry_run else ""), silent=True)
    elif crash_restarts + 1 == RESTART_WARN_COUNT:
        sender.enqueue(f"⚠️ Slot watcher crashed and restarted {crash_restarts} times in the last "
                       "hour. It keeps running, but something may be wrong (check logs).")
    else:
        log.info("Quick crash-restart (%d in last hour); startup message suppressed", crash_restarts)


async def run(cfg: Config) -> None:
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    try:
        loop.add_signal_handler(signal.SIGTERM, current.cancel)
    except (NotImplementedError, RuntimeError):
        pass  # Windows does not expose Unix signal handlers
    setup_logging(cfg)
    log.info("Starting slot watcher (dry_run=%s, model=%s, groups=%s, cvs=%s)",
             cfg.dry_run, cfg.openai_model, cfg.tg_groups, cfg.cvs_enabled)
    store = Store(cfg.data_dir / "watcher.db")
    targets = build_targets(cfg)
    log.info("Alert targets: %s", [t.label for t in targets] or "NONE")
    sender = AlertSender(targets, dry_run=cfg.dry_run)
    ai = AIChecker(cfg.openai_api_key, cfg.openai_model, cfg.ai_timeout, cfg.ai_image_timeout,
                   cfg.openai_fallback_model)
    ai_health = SourceHealth("AI check (OpenAI)", cfg.source_fail_minutes, require_error=True)
    healths: list[SourceHealth] = [ai_health]
    watchdog = Watchdog()
    tick = {"ts": time.time()}
    watchdog.watch("event loop", lambda: tick["ts"])

    client = None
    if cfg.tg_enabled:
        if not (cfg.tg_api_id and cfg.tg_api_hash):
            log.error("TELEGRAM_API_ID / TELEGRAM_API_HASH missing in .env; Telegram disabled")
        else:
            client = make_client(cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash)
    feed = None
    if cfg.feed_bot_token and cfg.feed_chats:
        feed = AlertSender([TelegramBotTarget(cfg.feed_bot_token, c, min_gap=1.1, bot="feed-bot")
                            for c in cfg.feed_chats], dry_run=cfg.dry_run)
        log.info("Feed bot on: every message read is copied to %d chat(s), silently",
                 len(cfg.feed_chats))
    pipeline = Pipeline(cfg, store, ai, sender, client, ai_health, feed)
    # Status messages (started / daily check-in / warnings) never go to the alerts bot's recipients.
    status = build_status_sender(cfg, sender, feed)

    tasks = [asyncio.create_task(supervise("sender", sender.run), name="sender")]
    if feed is not None:
        tasks.append(asyncio.create_task(supervise("feed", feed.run), name="feed"))
    if status is not feed and status is not sender:
        tasks.append(asyncio.create_task(supervise("status", status.run), name="status"))
    if client is not None:
        tg_health = SourceHealth("Telegram", cfg.source_fail_minutes)
        healths.append(tg_health)
        poller = TelegramPoller(client, cfg.tg_groups, cfg.tg_poll_seconds, store,
                                pipeline.on_telegram, tg_health)
        tasks.append(asyncio.create_task(supervise("telegram", poller.run, tg_health), name="telegram"))
        watchdog.watch("telegram loop", poller.heartbeat)
        _secure_session_file(cfg)

    if cfg.cvs_enabled:
        cvs_health = SourceHealth("CheckVisaSlots", cfg.source_fail_minutes)
        healths.append(cvs_health)
        cvs = CvsPoller(cfg.cvs_url, cfg.cvs_poll_seconds, store, pipeline.on_cvs_changes, cvs_health,
                        on_status=lambda text: status.enqueue(text, silent=True),
                        on_check=(lambda text, audible: feed.enqueue(text, kind="cvs-check",
                                                                    silent=not audible)) if feed else None,
                        refresh_period=cfg.cvs_refresh_seconds,
                        check_summaries=cfg.cvs_feed_check_summaries,
                        sync_checks=cfg.cvs_sync_checks,
                        fetcher=BrowserFetcher(profile_dir=cfg.data_dir / "cvs-browser",
                                               headed=cfg.cvs_browser_headed,
                                               network_controls=cfg.cvs_network_controls_enabled))
        tasks.append(asyncio.create_task(supervise("cvs", cvs.run, cvs_health), name="cvs"))

    tasks.append(asyncio.create_task(
        supervise("housekeeping", lambda: housekeeping(cfg, store, status, healths, tick)),
        name="housekeeping"))
    record_start(store, status, cfg.dry_run)
    watchdog.start()

    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if client is not None:
            await client.disconnect()
        await sender.aclose()
        if feed is not None:
            await feed.aclose()
        if status is not feed and status is not sender:
            await status.aclose()


def build_status_sender(cfg: Config, alert_sender: AlertSender,
                        feed: AlertSender | None) -> AlertSender:
    """Where status messages go: the feed bot if configured; otherwise ONLY the first alert chat
    (the owner) via the alerts bot. Other alert recipients (e.g. family) only ever get slot alerts."""
    if feed is not None:
        return feed
    if cfg.tg_bot_token and cfg.bot_chats:
        return AlertSender([TelegramBotTarget(cfg.tg_bot_token, cfg.bot_chats[0])],
                           dry_run=cfg.dry_run)
    return alert_sender


def _secure_session_file(cfg: Config) -> None:
    p = str(cfg.tg_session)
    for path in (p, p + ".session"):
        if os.path.exists(path):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

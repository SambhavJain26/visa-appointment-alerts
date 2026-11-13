"""Source 1: public Telegram groups, read-only, by username, without joining.

Uses Telethon (MTProto user client). Only read calls are made: resolve username,
get history, download media. Nothing is ever sent, joined, or reacted to.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from telethon import TelegramClient, errors

log = logging.getLogger("watcher.telegram")

RESOLVE_RETRY_SECONDS = 600
POLL_TIMEOUT_SECONDS = 120  # a single group poll must finish within this, or it is abandoned
AUTH_RETRY_SECONDS = 300


@dataclass
class TgMessage:
    group: str  # username
    group_title: str
    id: int
    date: datetime
    text: str
    has_image: bool
    is_service: bool
    is_sticker: bool
    grouped_id: int | None
    raw: object = field(repr=False, default=None)
    # Sender: used in memory for duplicate detection and shown ONLY in the private feed bot.
    # Never written to logs, the database, or the alerts bot.
    sender_id: int | None = field(repr=False, default=None)
    sender_name: str | None = field(repr=False, default=None)
    sender_username: str | None = field(repr=False, default=None)
    media_id: int | None = None  # Telegram photo/document id (same when a screenshot is forwarded)

    @property
    def link(self) -> str:
        return f"https://t.me/{self.group}/{self.id}"


def _is_image(m) -> bool:
    if getattr(m, "photo", None):
        return True
    doc = getattr(m, "document", None)
    mime = getattr(doc, "mime_type", "") or ""
    return bool(doc) and mime.startswith("image/") and not getattr(m, "sticker", None)


def to_tg_message(m, username: str, title: str) -> TgMessage:
    return TgMessage(
        group=username,
        group_title=title,
        id=m.id,
        date=m.date,
        text=getattr(m, "message", None) or "",
        has_image=_is_image(m),
        is_service=getattr(m, "action", None) is not None,
        is_sticker=bool(getattr(m, "sticker", None)),
        grouped_id=getattr(m, "grouped_id", None),
        raw=m,
        sender_id=getattr(m, "sender_id", None),
        sender_name=_sender_name(m),
        sender_username=getattr(getattr(m, "sender", None), "username", None),
        media_id=_media_id(m),
    )


def _sender_name(m) -> str | None:
    """Display name from the user objects Telegram already returned with the history (no extra API call)."""
    s = getattr(m, "sender", None)
    if s is None:
        return None
    name = " ".join(x for x in (getattr(s, "first_name", None), getattr(s, "last_name", None)) if x)
    return name or getattr(s, "title", None)  # title: posted as a channel/anonymous admin


def _media_id(m) -> int | None:
    media = getattr(m, "photo", None) or getattr(m, "document", None)
    return getattr(media, "id", None)


def make_client(session, api_id: int, api_hash: str) -> TelegramClient:
    client = TelegramClient(
        str(session), api_id, api_hash,
        device_model="slot-watcher (read-only)",
        # Telethon sleeps through short flood waits itself; kept well under POLL_TIMEOUT_SECONDS so a
        # legitimate wait never trips our poll timeout. Longer waits raise FloodWaitError (handled).
        flood_sleep_threshold=60,
        receive_updates=False,  # we poll; no update stream needed
        connection_retries=10,
        retry_delay=5,
        request_retries=3,
        timeout=30,
    )
    return client


async def download_image(client: TelegramClient, msg: TgMessage, max_bytes: int) -> tuple[bytes, str] | None:
    m = msg.raw
    try:
        doc = getattr(m, "document", None)
        size = getattr(doc, "size", 0) or 0
        if size and size > max_bytes:
            log.info("Image too large (%d bytes) in %s; sending text only", size, msg.link)
            return None
        data = await client.download_media(m, file=bytes)
        if not data or len(data) > max_bytes:
            return None
        mime = getattr(doc, "mime_type", None) or "image/jpeg"
        return data, mime
    except errors.FloodWaitError as e:
        log.warning("FloodWait %ss on media download; skipping image", e.seconds)
        await asyncio.sleep(e.seconds + 1)
        return None
    except Exception as e:
        log.warning("Image download failed for %s: %s", msg.link, e)
        return None


class TelegramPoller:
    def __init__(self, client: TelegramClient, groups: list[str], interval: float,
                 store, on_message, health):
        self.client = client
        self.groups = groups
        self.interval = interval
        self.store = store
        self.on_message = on_message  # async callable(TgMessage)
        self.health = health
        self.entities: dict[str, object] = {}
        self.titles: dict[str, str] = {}
        self.unreadable: dict[str, tuple[float, str]] = {}  # username -> (retry_at, reason)
        self.last_cycle = time.time()  # watched by the watchdog
        self.busy_until = 0.0  # set while deliberately sleeping (FloodWait), so the watchdog waits

    def heartbeat(self) -> float:
        return max(self.last_cycle, self.busy_until)

    async def _long_sleep(self, seconds: float) -> None:
        self.busy_until = time.time() + seconds + 60
        try:
            await asyncio.sleep(seconds)
        finally:
            self.busy_until = 0.0
            self.last_cycle = time.time()

    async def ensure_authorized(self) -> None:
        """Wait (never crash) until connected and logged in."""
        while True:
            self.last_cycle = time.time()
            try:
                if not self.client.is_connected():
                    await asyncio.wait_for(self.client.connect(), 60)
                if await asyncio.wait_for(self.client.is_user_authorized(), 60):
                    return
                reason = "Telegram login expired or was removed. Run: python -m watcher login"
            except Exception as e:
                reason = f"cannot connect to Telegram: {type(e).__name__}: {e}"
            self.health.fail(reason)
            log.error("%s (retrying in %d min)", reason, AUTH_RETRY_SECONDS // 60)
            await self._long_sleep(AUTH_RETRY_SECONDS)

    async def resolve(self, username: str) -> bool:
        retry = self.unreadable.get(username)
        if retry and time.time() < retry[0]:
            return False
        try:
            ent = await asyncio.wait_for(self.client.get_entity(username), POLL_TIMEOUT_SECONDS)
            # Probe that history is readable without joining.
            latest = await asyncio.wait_for(self.client.get_messages(ent, limit=1),
                                            POLL_TIMEOUT_SECONDS)
        except errors.FloodWaitError as e:
            log.warning("FloodWait %ss resolving %s; sleeping", e.seconds, username)
            await self._long_sleep(e.seconds + 1)
            return False
        except (errors.ChannelPrivateError, errors.ChatAdminRequiredError,
                errors.UsernameNotOccupiedError, errors.UsernameInvalidError,
                errors.ChannelInvalidError, ValueError) as e:
            reason = f"{type(e).__name__}: {e}"
            self.unreadable[username] = (time.time() + RESOLVE_RETRY_SECONDS, reason)
            log.error("GROUP UNREADABLE @%s - %s (skipping; retry in %d min)",
                      username, reason, RESOLVE_RETRY_SECONDS // 60)
            return False
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            self.unreadable[username] = (time.time() + 60, reason)
            log.warning("Could not resolve @%s: %s (retry in 1 min)", username, reason)
            return False

        self.unreadable.pop(username, None)
        self.entities[username] = ent
        self.titles[username] = getattr(ent, "title", None) or username
        if self.store.get_cursor(username) is None:
            start = latest[0].id if latest else 0
            self.store.set_cursor(username, start)
            log.info("@%s (%s): first run, starting after message %d (no history alerts)",
                     username, self.titles[username], start)
        else:
            log.info("@%s (%s): resuming after message %d",
                     username, self.titles[username], self.store.get_cursor(username))
        return True

    async def poll_group(self, username: str) -> int:
        ent = self.entities[username]
        last_id = self.store.get_cursor(username) or 0
        new = []
        async for m in self.client.iter_messages(ent, min_id=last_id, reverse=True, limit=200):
            new.append(m)
        for m in new:
            msg = to_tg_message(m, username, self.titles[username])
            try:
                await self.on_message(msg)
            except Exception:
                log.exception("Error handling %s", msg.link)
            self.store.set_cursor(username, max(m.id, self.store.get_cursor(username) or 0))
        return len(new)

    async def run(self) -> None:
        await self.ensure_authorized()
        me = await asyncio.wait_for(self.client.get_me(), 60)
        log.info("Telegram logged in as id=%s (read-only polling)", getattr(me, "id", "?"))
        for g in self.groups:
            await self.resolve(g)
            await asyncio.sleep(2)

        stagger = self.interval / max(1, len(self.groups))
        while True:
            any_ok = False
            self.last_cycle = time.time()
            for g in self.groups:
                if not self.client.is_connected():
                    await self.ensure_authorized()
                if g not in self.entities and not await self.resolve(g):
                    await asyncio.sleep(stagger)
                    continue
                try:
                    n = await asyncio.wait_for(self.poll_group(g), POLL_TIMEOUT_SECONDS)
                    any_ok = True
                    if n:
                        log.debug("@%s: %d new message(s)", g, n)
                except errors.FloodWaitError as e:
                    log.warning("FloodWait %ss polling @%s; sleeping as requested", e.seconds, g)
                    self.health.fail(f"FloodWait {e.seconds}s")
                    await self._long_sleep(e.seconds + 1)
                except asyncio.TimeoutError:
                    self.health.fail(f"polling @{g} timed out")
                    log.warning("Polling @%s took over %ds; abandoned, reconnecting", g, POLL_TIMEOUT_SECONDS)
                    try:
                        await asyncio.wait_for(self.client.disconnect(), 30)
                    except Exception:
                        pass
                except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
                        errors.UserDeactivatedError, errors.AuthKeyDuplicatedError) as e:
                    self.health.fail(f"Telegram login broken ({type(e).__name__}). Run login again.")
                    log.error("Telegram auth broken (%s). Re-run login.", type(e).__name__)
                    await self._long_sleep(AUTH_RETRY_SECONDS)
                except (errors.ChannelPrivateError, errors.ChatAdminRequiredError) as e:
                    self.entities.pop(g, None)
                    self.unreadable[g] = (time.time() + RESOLVE_RETRY_SECONDS, type(e).__name__)
                    log.error("GROUP UNREADABLE @%s - %s", g, type(e).__name__)
                except (ConnectionError, OSError) as e:
                    self.health.fail(f"connection: {e}")
                    log.warning("Telegram connection issue: %s; reconnecting", e)
                    await asyncio.sleep(10)
                except Exception as e:
                    self.health.fail(f"{type(e).__name__}: {e}")
                    log.exception("Unexpected error polling @%s", g)
                await asyncio.sleep(stagger)
            if any_ok:
                self.health.ok()
            elif self.groups and len(self.unreadable) == len(self.groups):
                self.health.fail("no configured group is readable")

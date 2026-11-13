import asyncio
import json

import httpx

from watcher import alerts as alerts_mod
from watcher.alerts import AlertSender, Outgoing, TelegramBotTarget
from watcher.config import BotChat, _parse_bot_chats, _parse_recipients

TOKEN = "123456:SECRET-TOKEN"


def test_parse_bot_chats():
    chats = _parse_bot_chats("me|123456789, brother|987654321, junk, |")
    assert [c.chat_id for c in chats] == ["123456789", "987654321"]
    assert chats[0].name == "me"
    assert _parse_bot_chats("") == []


def test_incomplete_whatsapp_entries_ignored():
    assert _parse_recipients("me|+91|") == []
    assert _parse_recipients("me|+919876543210|callmebot_apikey")[0].apikey == "callmebot_apikey"


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _send(handler, item=None):
    async def go():
        async with _client(handler) as http:
            return await TelegramBotTarget(TOKEN, BotChat("42", "me")).send(
                http, item or Outgoing("<b>hi</b>", "slot", preview=False))
    return asyncio.run(go())


def test_telegram_target_payload():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "result": {}})

    ok, detail, retry, permanent = _send(handler)
    assert ok and retry is None and not permanent
    assert seen["url"].endswith(f"/bot{TOKEN}/sendMessage")
    b = seen["body"]
    assert b["chat_id"] == "42" and b["text"] == "<b>hi</b>" and b["parse_mode"] == "HTML"
    assert b["link_preview_options"] == {"is_disabled": True}
    assert b["disable_notification"] is False


def test_silent_feed_payload():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    _send(handler, Outgoing("x", "feed", preview=True, silent=True))
    assert seen["body"]["disable_notification"] is True
    assert seen["body"]["link_preview_options"] == {"is_disabled": False}


def test_telegram_rate_limit_and_token_hidden():
    def handler(req):
        return httpx.Response(429, json={"ok": False, "description": "Too Many Requests",
                                         "parameters": {"retry_after": 7}})

    ok, detail, retry, permanent = _send(handler)
    assert not ok and retry == 7 and not permanent and TOKEN not in detail


def test_telegram_blocked_is_permanent():
    def handler(req):
        return httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"})

    ok, detail, retry, permanent = _send(handler)
    assert not ok and permanent


def test_network_error_token_hidden():
    def handler(req):
        raise httpx.ConnectError(f"boom {req.url}")

    ok, detail, retry, permanent = _send(handler)
    assert not ok and not permanent and TOKEN not in detail


class FakeTarget:
    min_gap = 0

    def __init__(self, label, results):
        self.label = label
        self.results = list(results)
        self.sent = []

    async def send(self, http, item):
        self.sent.append(item.html)
        return self.results.pop(0) if self.results else (True, "ok", None, False)


def _no_sleep(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast_sleep(_):
        await real_sleep(0)
    monkeypatch.setattr(alerts_mod.asyncio, "sleep", fast_sleep)


def test_retry_then_success(monkeypatch):
    _no_sleep(monkeypatch)
    t = FakeTarget("t", [(False, "x", None, False), (True, "ok", None, False)])

    async def go():
        s = AlertSender([t], dry_run=False)
        ok = await s._send_with_retry(t, Outgoing("m", "slot"))
        await s.aclose()
        return ok
    assert asyncio.run(go()) is True and len(t.sent) == 2


def test_permanent_error_not_retried(monkeypatch):
    _no_sleep(monkeypatch)
    t = FakeTarget("t", [(False, "HTTP 403: blocked", None, True)])

    async def go():
        s = AlertSender([t], dry_run=False)
        ok = await s._send_with_retry(t, Outgoing("m", "slot"))
        await s.aclose()
        return ok
    assert asyncio.run(go()) is False and len(t.sent) == 1


def test_html_parse_error_falls_back_to_plain(monkeypatch):
    _no_sleep(monkeypatch)
    t = FakeTarget("t", [(False, "HTTP 400: Bad Request: can't parse entities", None, False)])

    async def go():
        s = AlertSender([t], dry_run=False)
        ok = await s._send_with_retry(t, Outgoing("<b>Slot</b> <i>x</i>", "slot"))
        await s.aclose()
        return ok
    assert asyncio.run(go()) is True
    assert t.sent[1] == "*Slot* _x_"


def test_failing_target_does_not_block_others(monkeypatch):
    _no_sleep(monkeypatch)
    bad = FakeTarget("bad", [(False, "down", None, False)] * 100)
    good = FakeTarget("good", [])

    async def go():
        s = AlertSender([bad, good], dry_run=False)
        runner = asyncio.create_task(s.run())
        s.enqueue_html("one")
        s.enqueue_html("two")
        for _ in range(200):
            await asyncio.sleep(0)
            if len(good.sent) == 2:
                break
        runner.cancel()
        await s.aclose()
    asyncio.run(go())
    assert good.sent == ["one", "two"]


def test_system_messages_are_escaped():
    async def go():
        s = AlertSender([FakeTarget("t", [])], dry_run=False)
        s.enqueue("Last error: <ConnectionError> & co")
        item = s._queues["t"].get_nowait()
        await s.aclose()
        return item.html
    assert asyncio.run(go()) == "Last error: &lt;ConnectionError&gt; &amp; co"


def test_photo_sent_with_caption():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = req.content
        return httpx.Response(200, json={"ok": True})

    item = Outgoing("<b>cap</b>", "feed", silent=True, photo=b"\x89PNGfake", photo_mime="image/png")
    ok, *_ = _send(handler, item)
    assert ok and seen["url"].endswith("/sendPhoto")
    assert b'name="caption"' in seen["body"] and b"<b>cap</b>" in seen["body"]
    assert b"screenshot.png" in seen["body"] and b"\x89PNGfake" in seen["body"]
    assert b'name="disable_notification"\r\n\r\ntrue' in seen["body"]


def test_long_caption_falls_back_to_text_message():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"ok": True})

    ok, *_ = _send(handler, Outgoing("x" * 1100, "feed", photo=b"img"))
    assert ok and seen["url"].endswith("/sendMessage")


def test_rejected_photo_resent_without_photo(monkeypatch):
    _no_sleep(monkeypatch)
    t = FakeTarget("t", [(False, "HTTP 400: Bad Request: IMAGE_PROCESS_FAILED", None, False)])
    got = []
    orig = t.send

    async def spy(http, item):
        got.append(item.photo)
        return await orig(http, item)
    t.send = spy

    async def go():
        s = AlertSender([t], dry_run=False)
        ok = await s._send_with_retry(t, Outgoing("m", "feed", photo=b"img"))
        await s.aclose()
        return ok
    assert asyncio.run(go()) is True
    assert got == [b"img", None]


def test_photo_queue_memory_guard():
    async def go():
        s = AlertSender([FakeTarget("t", [])], dry_run=False)
        for _ in range(alerts_mod.MAX_QUEUED_PHOTOS + 5):
            s.enqueue_html("m", "feed", photo=b"x" * 100)
        items = list(s._queues["t"]._queue)
        await s.aclose()
        return items
    items = asyncio.run(go())
    assert sum(1 for i in items if i.photo) == alerts_mod.MAX_QUEUED_PHOTOS
    assert len(items) == alerts_mod.MAX_QUEUED_PHOTOS + 5  # messages still sent, just without photo

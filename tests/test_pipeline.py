import asyncio
import dataclasses
import json
import time
from datetime import timedelta

from watcher.ai_check import AI_FAILED_SUMMARY, AIResult
from watcher.alerts import AlertSender
from watcher.app import Pipeline, record_start, supervise
from watcher.config import load_config
from watcher.db import Store
from watcher.health import SourceHealth
from watcher.telegram_poller import TgMessage
from watcher.timeutil import now_utc


class FakeAI:
    def __init__(self):
        self.calls = []

    async def check(self, text, images=None):
        self.calls.append(text)
        if "fail" in text:
            return AIResult(True, AI_FAILED_SUMMARY, failed=True)
        if "available" in text:
            return AIResult(True, "Chennai OFC open", appointment_type="biometrics",
                            location="Chennai", dates="20 Nov")
        return AIResult(False, "chatter")


def _msg(i, text, group="grp", age_min=1, grouped_id=None, sender=None, has_image=False,
         media_id=None):
    return TgMessage(group=group, group_title="G (No discussions)", id=i,
                     date=now_utc() - timedelta(minutes=age_min), text=text, has_image=has_image,
                     is_service=False, is_sticker=False, grouped_id=grouped_id,
                     sender_id=sender, media_id=media_id)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = dataclasses.replace(load_config(env_file=str(tmp_path / "none.env")), dry_run=False)
    store = Store(tmp_path / "t.db")
    sender = AlertSender([], dry_run=False)
    sent = []
    sender.enqueue_html = lambda h, kind="slot", preview=False, **kw: sent.append((h, kind, preview, kw.get("photo")))
    return cfg, store, sender, sent


def _drain(p):
    async def go():
        while p._tasks:
            await asyncio.sleep(0.01)
    return go()


def test_pipeline_end_to_end(tmp_path, monkeypatch):
    cfg, store, sender, sent = _setup(tmp_path, monkeypatch)
    ai = FakeAI()
    health = SourceHealth("AI", 15, require_error=True)

    async def go():
        p = Pipeline(cfg, store, ai, sender, ai_health=health)
        for m in [
            _msg(1, "NA 1 H1b All"),                       # filtered, no AI
            _msg(2, "CHN OFC Nov 20 available"),           # forwarded
            _msg(2, "CHN OFC Nov 20 available"),           # same id -> dup
            _msg(3, "CHN OFC Nov 20 available", "other"),  # same type/city/date within 2 min
            _msg(4, "anyone booked?"),                     # AI says no
            _msg(5, "HYD OFC slots? please fail"),          # AI failed + looks like a sighting -> forwarded
            _msg(6, "CHN OFC Nov 21 available", age_min=90),  # stale -> never alerted
        ]:
            await p.on_telegram(m)
            await _drain(p)
    asyncio.run(go())

    assert "NA 1 H1b All" not in ai.calls
    assert "CHN OFC Nov 21 available" not in ai.calls  # stale: no AI cost either
    assert len(sent) == 2
    assert sent[0][0].startswith("🚨 <b>BIOMETRICS (OFC) SLOT</b>")
    assert "📍 <b>Chennai</b>" in sent[0][0] and "👥 G" in sent[0][0]
    assert sent[1][0].startswith("⚠️ <b>CHECK THIS POST</b>")
    assert store.count_events("read", 0) == 7
    assert store.count_events("forwarded", 0) == 2
    assert health.last_error is not None  # last AI call failed


def test_album_alerts_once(tmp_path, monkeypatch):
    cfg, store, sender, sent = _setup(tmp_path, monkeypatch)

    async def go():
        p = Pipeline(cfg, store, FakeAI(), sender)
        await p.on_telegram(_msg(10, "HYD available Nov 3", grouped_id=77))
        await _drain(p)
        await p.on_telegram(_msg(11, "HYD available Nov 4", grouped_id=77))
        await _drain(p)
    asyncio.run(go())
    assert len(sent) == 1


def test_same_sender_cross_post_alerts_once(tmp_path, monkeypatch):
    cfg, store, sender, sent = _setup(tmp_path, monkeypatch)
    ai = FakeAI()

    async def go():
        p = Pipeline(cfg, store, ai, sender)
        for m in [
            # same person, same text, three groups (AI found no city -> summary dedupe can't help)
            _msg(20, "slots available now!!", "g1", sender=111),
            _msg(21, "Slots available now", "g2", sender=111),
            _msg(22, "slots  available now", "g3", sender=111),
            # different person, same text -> still checked
            _msg(23, "slots available now", "g2", sender=222),
            # same person, different text -> still checked
            _msg(24, "kolkata available too", "g1", sender=111),
            # same person forwarding the same screenshot (no text) to two groups
            _msg(25, "", "g1", sender=333, has_image=True, media_id=9),
            _msg(26, "", "g2", sender=333, has_image=True, media_id=9),
            # unknown sender -> never treated as duplicate
            _msg(27, "available", "g1"),
            _msg(28, "available", "g2"),
        ]:
            await p.on_telegram(m)
            await _drain(p)
    asyncio.run(go())
    texts = [c.strip().lower() for c in ai.calls]
    assert texts.count("slots available now!!") + texts.count("slots available now") == 2
    assert "kolkata available too" in texts
    assert texts.count("") == 1  # forwarded screenshot checked once
    assert texts.count("available") == 2


def test_feed_reports_every_message_with_decision(tmp_path, monkeypatch):
    cfg, store, sender, sent = _setup(tmp_path, monkeypatch)
    feed_items = []

    class FakeFeed:
        def enqueue_html(self, h, kind="feed", preview=False, silent=False, photo=None,
                         photo_mime="image/jpeg"):
            feed_items.append((h, kind, preview, silent))

    async def go():
        p = Pipeline(cfg, store, FakeAI(), sender, feed=FakeFeed())
        for m in [
            _msg(40, "H1 NA All"),                          # NA
            _msg(41, "CHN OFC Nov 20 available", sender=5),  # alert
            _msg(42, "CHN OFC Nov 20 available", "g2", sender=5),  # same sender dup
            _msg(43, "anyone booked? <b>", sender=6),       # AI no (and html must be escaped)
            _msg(44, "old available post", age_min=90),     # stale
            _msg(45, "", has_image=True, media_id=1),       # screenshot -> AI (fake: no)
        ]:
            await p.on_telegram(m)
            await _drain(p)
    asyncio.run(go())

    assert len(feed_items) == 6  # every message read is reported
    assert feed_items[1][0].split("\n")[1] == "👤 unknown sender · id 5"
    decisions = [h.split("\n")[3] for h, *_ in feed_items]
    assert decisions[0].startswith("⏭ Skipped · NA report")
    assert decisions[1].startswith("🚨 <b>ALERT SENT</b> · biometrics · Chennai · 20 Nov")
    assert "same person" in decisions[2]
    assert decisions[3].startswith("🤖 AI: no slot")
    assert "too old" in decisions[4]
    assert all(kind == "feed" and silent for _, kind, _, silent in feed_items)
    assert "&lt;b&gt;" in feed_items[3][0]
    assert feed_items[5][2] is True and "screenshot, no text" in feed_items[5][0]  # preview on
    assert len(sent) == 1  # the feed never adds alerts


def test_render_sender():
    from watcher.app import render_sender
    m = _msg(1, "x", sender=42)
    m.sender_name, m.sender_username = "Ravi <K>", "ravik"
    assert render_sender(m) == ('👤 Ravi &lt;K&gt; (<a href="https://t.me/ravik">@ravik</a>) · id 42')
    assert render_sender(_msg(2, "x")) == "👤 unknown sender"


def test_sender_never_in_alert_or_logs(tmp_path, monkeypatch, caplog):
    cfg, store, sender, sent = _setup(tmp_path, monkeypatch)

    async def go():
        p = Pipeline(cfg, store, FakeAI(), sender)
        m = _msg(50, "HYD OFC available 3 Dec", sender=987654321)
        m.sender_name, m.sender_username = "Secret Person", "secretuser"
        await p.on_telegram(m)
        await _drain(p)
    import logging
    with caplog.at_level(logging.DEBUG):
        asyncio.run(go())
    assert len(sent) == 1
    for blob in [sent[0][0], caplog.text]:
        assert "Secret Person" not in blob and "secretuser" not in blob and "987654321" not in blob


def test_same_sender_window_expires(tmp_path, monkeypatch):
    cfg, store, sender, sent = _setup(tmp_path, monkeypatch)
    cfg = dataclasses.replace(cfg, same_sender_window=0)
    ai = FakeAI()

    async def go():
        p = Pipeline(cfg, store, ai, sender)
        await p.on_telegram(_msg(30, "hyd available", "g1", sender=1))
        await _drain(p)
        await p.on_telegram(_msg(31, "hyd available", "g2", sender=1))
        await _drain(p)
    asyncio.run(go())
    assert len(ai.calls) == 2


def test_supervise_restarts_after_crash(monkeypatch):
    runs = {"n": 0}
    health = SourceHealth("X", 15)

    async def flaky():
        runs["n"] += 1
        if runs["n"] < 3:
            raise RuntimeError("boom")
        await asyncio.Event().wait()

    real_sleep = asyncio.sleep

    async def fast_sleep(_):
        await real_sleep(0)

    import watcher.app as app_mod
    monkeypatch.setattr(app_mod.asyncio, "sleep", fast_sleep)

    async def go():
        t = asyncio.create_task(supervise("x", flaky, health))
        for _ in range(50):
            await real_sleep(0)
        t.cancel()
    asyncio.run(go())
    assert runs["n"] == 3
    assert "boom" in (health.last_error or "")


def test_record_start_suppresses_quick_restarts(tmp_path):
    store = Store(tmp_path / "s.db")
    msgs = []

    class S:
        def enqueue(self, text, kind="system", silent=False):
            msgs.append(text)

    record_start(store, S(), False, "c1")    # first start -> message
    record_start(store, S(), False, "c1")    # crash-restart of same container -> silent
    record_start(store, S(), False, "c1")    # 2nd crash-restart (3 starts) -> warning
    assert msgs[0].startswith("✅ Slot watcher started")
    assert len(msgs) == 2 and "crashed and restarted 2 times" in msgs[1]
    # a start long after the previous one sends the normal message again
    store.set_meta("starts", json.dumps([[time.time() - 4000, "c1"]]))
    record_start(store, S(), False, "c1")
    assert msgs[-1].startswith("✅ Slot watcher started")


def test_deliberate_updates_are_not_crashes(tmp_path):
    store = Store(tmp_path / "s.db")
    msgs = []

    class S:
        def enqueue(self, text, kind="system", silent=False):
            msgs.append(text)

    # three updates in a few minutes = three new containers -> normal start messages, no warning
    for host in ("deploy1", "deploy2", "deploy3", "deploy4"):
        record_start(store, S(), False, host)
    assert len(msgs) == 4 and all(m.startswith("✅ Slot watcher started") for m in msgs)
    # old-format entries (plain timestamps) are ignored safely
    store.set_meta("starts", json.dumps([time.time(), time.time()]))
    record_start(store, S(), False, "x")
    assert msgs[-1].startswith("✅")


def test_health_require_error():
    h = SourceHealth("AI", 15, require_error=True)
    h.last_ok -= 7200  # quiet for 2 hours (no messages needed the AI)
    assert h.check() is None  # idle, no errors: not failing
    h.fail("timeout")
    assert h.check() is None  # ONE hiccup after a quiet period is not a failure
    h.fail("timeout")
    assert h.check() is None
    h.fail("quota")
    msg = h.check()
    assert msg and "failed 3 times in a row" in msg and "quota" in msg
    assert h.check() is None  # warned only once
    h.ok()
    assert "working again" in h.check()


def test_health_continuous_source():
    h = SourceHealth("Telegram", 15)
    assert h.check() is None
    h.fail("connection")
    assert h.check() is None  # within the 15 min grace
    h.last_ok -= 16 * 60
    assert "failing for 16 min" in h.check()


def test_status_messages_never_reach_family(tmp_path, monkeypatch):
    from watcher.app import build_status_sender
    from watcher.config import BotChat
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    base = load_config(env_file=str(tmp_path / "none.env"))
    cfg = dataclasses.replace(base, tg_bot_token="1:x",
                              bot_chats=[BotChat("111", "me"), BotChat("222", "brother")])
    alert_sender = AlertSender([], dry_run=True)

    async def go():
        # with a feed bot: status goes to the feed bot
        feed = AlertSender([], dry_run=True)
        assert build_status_sender(cfg, alert_sender, feed) is feed
        # without a feed bot: only the owner (first chat), never the brother
        s = build_status_sender(cfg, alert_sender, None)
        assert s is not alert_sender
        assert [t.chat.chat_id for t in s.targets] == ["111"]
        await s.aclose()
        await feed.aclose()
    asyncio.run(go())


def test_routine_status_is_silent(tmp_path):
    store = Store(tmp_path / "q.db")
    got = []

    class S:
        def enqueue(self, text, kind="system", silent=False):
            got.append((text, silent))

    record_start(store, S(), False, "c9")
    assert got == [("✅ Slot watcher started.", True)]


def test_feed_gets_the_same_in_memory_screenshot(tmp_path, monkeypatch):
    import watcher.app as app_mod
    cfg, store, sender, sent = _setup(tmp_path, monkeypatch)
    feed_items = []

    class FakeFeed:
        def enqueue_html(self, h, kind="feed", preview=False, silent=False, photo=None,
                         photo_mime="image/jpeg"):
            feed_items.append((photo, photo_mime, preview))

    async def fake_download(client, msg, max_bytes):
        return (b"PNGDATA", "image/png")
    monkeypatch.setattr(app_mod, "download_image", fake_download)

    class SeeAI(FakeAI):
        async def check(self, text, images=None):
            self.images = images
            return await super().check(text or "", images)

    ai = SeeAI()

    async def go():
        p = Pipeline(cfg, store, ai, sender, client=object(), feed=FakeFeed())
        await p.on_telegram(_msg(60, "", has_image=True, media_id=3))
        await _drain(p)
    asyncio.run(go())
    assert ai.images == [(b"PNGDATA", "image/png")]
    assert feed_items == [(b"PNGDATA", "image/png", False)]  # photo sent, no link preview needed


def test_image_uses_longer_ai_limit(monkeypatch):
    import watcher.ai_check as ai_mod
    seen = []

    async def fake_wait_for(coro, timeout):
        seen.append(timeout)
        coro.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(ai_mod.asyncio, "wait_for", fake_wait_for)
    ai = ai_mod.AIChecker("sk-test", "m", timeout=10, image_timeout=20)

    async def go():
        r1 = await ai.check("CHN available", [])
        r2 = await ai.check("", [(b"img", "image/jpeg")])
        return r1, r2
    r1, r2 = asyncio.run(go())
    assert seen == [10, 20]
    assert r1.failed and r2.failed  # timeouts still fail open

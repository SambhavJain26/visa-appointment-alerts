import asyncio
import dataclasses
from datetime import date, datetime, timedelta

from watcher.ai_check import AIResult
from watcher.alerts import AlertSender
from watcher.app import Pipeline
from watcher.config import load_config
from watcher.db import Store
from watcher.slotkey import first_date, normalize_city, slot_key
from watcher.telegram_poller import TgMessage
from watcher.timeutil import IST, now_utc

TODAY = date(2026, 11, 7)


def test_dates_written_differently_are_the_same():
    want = date(2026, 11, 30)
    for text in ["November 30, 2026", "30 Nov 2026", "Earliest 30 Nov 2026", "30 Nov, 26",
                 "Nov 30", "30th Nov", "Nov 30th 2026", "30 November"]:
        assert first_date(text, TODAY) == want, text


def test_first_date_details():
    assert first_date("October 28, 2026 (9:00 and 9:30)", TODAY) == date(2026, 10, 28)
    assert first_date("28 Oct 2026 · 9:00, 9:30", TODAY) == date(2026, 10, 28)
    assert first_date("Nov 13, 9", TODAY) == date(2026, 11, 13)      # "9" is a time, not a year
    assert first_date("20 Nov 10:30", TODAY) == date(2026, 11, 20)   # "10" is not a year
    assert first_date("24 May, 27", TODAY) == date(2027, 5, 24)
    assert first_date("Jan 5", TODAY) == date(2027, 1, 5)            # no year, already past
    assert first_date("", TODAY) is None and first_date("not mentioned", TODAY) is None


def test_cities():
    assert normalize_city("Delhi") == normalize_city("New Delhi") == "new delhi"
    assert normalize_city("CHENNAI VAC") == "chennai"
    assert normalize_city("Chennai, Hyderabad") is None   # several cities: no merge
    assert normalize_city("") is None


def test_slot_key():
    a = slot_key("biometrics", "Chennai", "November 30, 2026", TODAY)
    b = slot_key("biometrics", "CHENNAI", "30 Nov, 26", TODAY)
    assert a == b == "biometrics|chennai|2026-11-30"
    assert slot_key("interview", "Chennai", "30 Nov 2026", TODAY) != a   # different type
    assert slot_key("biometrics", "", "30 Nov 2026", TODAY) is None      # no city
    assert slot_key("biometrics", "Chennai", "", TODAY) is None          # no date


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = dataclasses.replace(load_config(env_file=str(tmp_path / "none.env")), dry_run=False)
    sender = AlertSender([], dry_run=False)
    sent = []
    sender.enqueue_html = (lambda h, kind="slot", preview=False, **kw:
                           sent.append((h, kw.get("photo"))))
    return cfg, Store(tmp_path / "k.db"), sender, sent


class AI:
    def __init__(self, res):
        self.res = res

    async def check(self, text, images=None):
        return self.res


def _tg(i, text, has_image=False):
    return TgMessage(group="g", group_title="G", id=i, date=now_utc() - timedelta(seconds=30),
                     text=text, has_image=has_image, is_service=False, is_sticker=False,
                     grouped_id=None)


def _cvs_row(earliest="30 Nov, 26"):
    t = datetime.now(IST).strftime("%d %b %Y, %I:%M %p")
    return {"location": "CHENNAI VAC", "earliest_date": earliest, "slots_on_earliest": "N/A",
            "total_dates": "1", "last_seen": t}


def _drain(p):
    async def go():
        while p._tasks:
            await asyncio.sleep(0.01)
    return go()


def test_cvs_then_telegram_same_slot_is_one_alert(tmp_path, monkeypatch):
    cfg, store, sender, sent = _setup(tmp_path, monkeypatch)
    same = AI(AIResult(True, "Chennai OFC open", appointment_type="biometrics",
                       location="Chennai", dates="November 30, 2026"))

    async def go():
        p = Pipeline(cfg, store, same, sender)
        p.on_cvs_changes([_cvs_row()])                            # CheckVisaSlots first
        await p.on_telegram(_tg(1, "CHN OFC Nov 30 available"))  # same slot on Telegram
        await _drain(p)
        p.ai = AI(AIResult(True, "x", appointment_type="biometrics", location="",
                           dates="30 Nov"))
        await p.on_telegram(_tg(2, "slot seen 30 nov"))           # no city -> still sent
        await _drain(p)
    asyncio.run(go())
    assert len(sent) == 2  # CVS alert + the no-city post; the Telegram duplicate was merged


def test_same_slot_after_window_is_sent_again(tmp_path, monkeypatch):
    cfg, store, sender, sent = _setup(tmp_path, monkeypatch)
    cfg = dataclasses.replace(cfg, dup_summary_window=0)
    ai = AI(AIResult(True, "x", appointment_type="biometrics", location="Chennai",
                     dates="30 Nov 2026"))

    async def go():
        p = Pipeline(cfg, store, ai, sender)
        p.on_cvs_changes([_cvs_row()])
        await p.on_telegram(_tg(5, "CHN OFC 30 Nov available"))
        await _drain(p)
    asyncio.run(go())
    assert len(sent) == 2


def test_alerts_bot_gets_the_screenshot(tmp_path, monkeypatch):
    import watcher.app as app_mod
    cfg, store, sender, sent = _setup(tmp_path, monkeypatch)

    async def fake_download(client, msg, max_bytes):
        return (b"JPEGDATA", "image/jpeg")
    monkeypatch.setattr(app_mod, "download_image", fake_download)
    ai = AI(AIResult(True, "Calendar with open dates", appointment_type="biometrics",
                     location="Hyderabad", dates="13 Nov 2026"))

    async def go():
        p = Pipeline(cfg, store, ai, sender, client=object())
        await p.on_telegram(_tg(9, "", has_image=True))
        await _drain(p)
    asyncio.run(go())
    assert len(sent) == 1 and sent[0][1] == b"JPEGDATA"

import asyncio
import dataclasses
from datetime import datetime, timedelta

import pytest

from watcher.alerts import AlertSender
from watcher.app import Pipeline
from watcher.config import load_config
from watcher.cvs_poller import (BlockedError, CvsPoller, parse_cvs, parse_last_seen, row_fields)
from watcher.db import Store
from watcher.health import SourceHealth
from watcher.timeutil import IST

HEAD = ("<tr><th>Visa Location</th><th>Visa Type</th><th>Earliest Date</th>"
        "<th>Slots on Earliest Date</th><th>Total Dates Available</th>"
        "<th>Last Seen At</th><th>Relative Time</th></tr>")


def page(rows, tz=IST):
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    gen = datetime.now(tz).strftime("%d %b %Y, %I:%M %p")
    return (f"<html><head><title>Latest US Visa Availability</title></head><body>"
            f"<p>The current info is generated at {gen}</p><table>{HEAD}{body}</table></body></html>")


def ist(minutes_ago):
    return (datetime.now(IST) - timedelta(minutes=minutes_ago)).strftime("%d %b %Y, %I:%M %p")


def row(loc, seen, earliest="30 Nov, 26", slots="6", total="1"):
    return [loc, "H-1B (Regular)", earliest, slots, total, seen, "just now"]


class FakeFetcher:
    def __init__(self, pages):
        self.pages = list(pages)

    async def fetch(self, url):
        p = self.pages.pop(0)
        if isinstance(p, Exception):
            raise p
        return p


def test_parse_last_seen_and_pretty_dates():
    dt = parse_last_seen("06 Nov 2026, 01:31 PM")
    assert dt == datetime(2026, 11, 6, 13, 31, tzinfo=IST)
    assert parse_last_seen("garbage") is None and parse_last_seen(None) is None
    f = row_fields({"location": "CHENNAI VAC", "earliest_date": "30 Nov, 26",
                    "slots_on_earliest": "6", "total_dates": "1"})
    assert f["dates"] == "Earliest 30 Nov 2026"
    assert f["appointment_type"] == "biometrics" and f["location"] == "Chennai"
    assert row_fields({"location": "NEW DELHI", "earliest_date": "N/A"})["dates"] == "Earliest N/A"


def test_empty_table_is_an_error_not_a_reset():
    with pytest.raises(ValueError):
        parse_cvs(page([]))


def test_poll_once_first_fetch_then_change(tmp_path):
    store = Store(tmp_path / "c.db")
    changes, notes = [], []
    seen1 = ist(60)
    p1 = page([row("CHENNAI VAC", seen1), row("HYDERABAD", seen1)])
    p2 = page([row("CHENNAI VAC", ist(1)), row("HYDERABAD", seen1)])
    p3 = page([row("HYDERABAD", seen1)])  # a row missing: must NOT count as a change later
    p4 = page([row("CHENNAI VAC", ist(1)), row("HYDERABAD", seen1)])
    poller = CvsPoller("u", 120, store, changes.extend, SourceHealth("CVS", 15),
                       on_status=notes.append, fetcher=FakeFetcher([p1, p2, p3, p4]))

    async def go():
        assert await poller.poll_once() == []          # first fetch: store only
        assert [r["location"] for r in await poller.poll_once()] == ["CHENNAI VAC"]
        assert await poller.poll_once() == []          # partial page: no false alerts
        assert await poller.poll_once() == []          # row back, same last_seen: no alert
    asyncio.run(go())
    assert len(changes) == 1
    assert notes and "CheckVisaSlots connected: reading 2 locations" in notes[0]


def test_blocked_is_reported(tmp_path):
    store = Store(tmp_path / "c.db")
    poller = CvsPoller("u", 120, store, lambda r: None, SourceHealth("CVS", 15),
                       fetcher=FakeFetcher([BlockedError("checkpoint")]))
    with pytest.raises(BlockedError):
        asyncio.run(poller.poll_once())


def _pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = dataclasses.replace(load_config(env_file=str(tmp_path / "none.env")), dry_run=False)
    sender = AlertSender([], dry_run=False)
    sent, feed = [], []
    sender.enqueue_html = lambda h, kind="slot", preview=False, **kw: sent.append(h)

    class F:
        def enqueue_html(self, h, kind="feed", preview=False, silent=False, **kw):
            feed.append((h, silent))
    return Pipeline(cfg, Store(tmp_path / "p.db"), None, sender, feed=F()), sent, feed


def test_cvs_change_alerts_with_sighting_time(tmp_path, monkeypatch):
    p, sent, feed = _pipeline(tmp_path, monkeypatch)
    r = {"location": "CHENNAI VAC", "earliest_date": "30 Nov, 26", "slots_on_earliest": "6",
         "total_dates": "1", "last_seen": ist(2)}
    p.on_cvs_changes([r])
    assert len(sent) == 1
    a = sent[0]
    assert a.startswith("🚨 <b>BIOMETRICS (OFC) SLOT</b>")
    assert "📍 <b>Chennai</b>" in a and "📅 Earliest 30 Nov 2026" in a
    assert "🎟 6 slots on that date · 1 date open" in a
    assert "👥 CheckVisaSlots website" in a and "Open CheckVisaSlots" in a
    assert feed and "ALERT SENT" in feed[0][0] and feed[0][1] is True
    p.on_cvs_changes([r])  # same report again -> no second alert
    assert len(sent) == 1


def test_cvs_stale_report_not_alerted(tmp_path, monkeypatch):
    p, sent, feed = _pipeline(tmp_path, monkeypatch)
    p.on_cvs_changes([{"location": "HYDERABAD", "earliest_date": "13 Nov, 26",
                       "slots_on_earliest": "1", "total_dates": "4", "last_seen": ist(180)}])
    assert sent == []
    assert "too old" in feed[0][0]


@pytest.mark.parametrize("minutes_old,should_alert", [(4, True), (5, True), (6, False), (15, False)])
def test_cvs_five_minute_freshness_does_not_change_telegram_cutoff(tmp_path, monkeypatch,
                                                               minutes_old, should_alert):
    from datetime import timezone
    import watcher.app as app
    monkeypatch.setenv("CVS_MAX_ALERT_AGE_MINUTES", "5")
    monkeypatch.setenv("MAX_ALERT_AGE_MINUTES", "30")
    p, sent, feed = _pipeline(tmp_path, monkeypatch)
    now = datetime(2026, 11, 7, 14, 0, tzinfo=IST)
    monkeypatch.setattr(app, "now_utc", lambda: now.astimezone(timezone.utc))
    r = {"location": "CHENNAI VAC", "earliest_date": "12 Nov, 27",
         "last_seen": (now - timedelta(minutes=minutes_old)).strftime("%d %b %Y, %I:%M %p")}
    p.on_cvs_changes([r])
    assert bool(sent) is should_alert
    assert p.cfg.max_alert_age_minutes == 30
    assert feed
    if not should_alert:
        assert "too old" in feed[0][0]


def test_utc_rendered_page_is_converted_to_ist():
    from datetime import timezone
    seen_ist = datetime(2026, 11, 7, 0, 0, tzinfo=IST)
    seen_utc = seen_ist.astimezone(timezone.utc)
    fmt = "%d %b %Y, %I:%M %p"
    ist_page = page([row("CHENNAI VAC", seen_ist.strftime(fmt))], tz=IST)
    utc_page = page([row("CHENNAI VAC", seen_utc.strftime(fmt))], tz=timezone.utc)
    a, b = parse_cvs(ist_page), parse_cvs(utc_page)
    assert a["CHENNAI VAC"]["last_seen"] == b["CHENNAI VAC"]["last_seen"] == "07 Nov 2026, 12:00 AM"


def test_first_paint_utc_then_ist_is_not_a_change(tmp_path):
    from datetime import timezone
    store = Store(tmp_path / "z.db")
    fmt = "%d %b %Y, %I:%M %p"
    t = datetime.now(IST) - timedelta(minutes=3)
    p_utc = page([row("CHENNAI VAC", t.astimezone(timezone.utc).strftime(fmt))], tz=timezone.utc)
    p_ist = page([row("CHENNAI VAC", t.strftime(fmt))], tz=IST)
    changes = []
    poller = CvsPoller("u", 120, store, changes.extend, SourceHealth("CVS", 15),
                       fetcher=FakeFetcher([p_utc, p_ist]))

    async def go():
        await poller.poll_once()
        await poller.poll_once()
    asyncio.run(go())
    assert changes == []  # the bug seen live: must not look like a new report


def test_unknown_page_time_is_rejected():
    from datetime import timezone
    from watcher.cvs_poller import page_utc_offset_minutes
    with pytest.raises(ValueError):
        parse_cvs("<html><head><title>x</title></head><body><table>" + HEAD + "</table></body></html>")
    weird = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%d %b %Y, %I:%M %p")
    with pytest.raises(ValueError):
        page_utc_offset_minutes(f"generated at {weird}")


def test_min_gap_defaults_to_synced_and_never_below_25_seconds(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    none = str(tmp_path / "none.env")
    monkeypatch.setenv("CVS_POLL_SECONDS", "25")     # old fixed-rhythm setting: no longer used
    cfg = load_config(env_file=none)
    assert cfg.cvs_poll_seconds == 115 and cfg.cvs_refresh_seconds == 120
    monkeypatch.setenv("CVS_MIN_GAP_SECONDS", "5")   # clamp to minimum interval
    assert load_config(env_file=none).cvs_poll_seconds == 25
    monkeypatch.setenv("CVS_MIN_GAP_SECONDS", "200")
    assert load_config(env_file=none).cvs_poll_seconds == 200


def _fake_site(period=120.0, phase=37.0):
    """Refresh moments of a site that rebuilds every `period` s; 'generated at' = that minute."""
    def latest(t):
        n = (t - phase) // period
        return phase + n * period
    return latest


def _drive(sync, latest, hours=6.0, t0=1_000_020.0):
    """Run RefreshSync against the fake site; returns (starts, results, delay of each refresh)."""
    t, starts, results = t0, [], []
    while t < t0 + hours * 3600:
        render, end = t + 1.5, t + 4.0
        r = latest(render)
        results.append(sync.observe(r // 60 * 60, t, end))
        starts.append(t)
        t = sync.next_start(t, end)
    delays = []
    for s0, s1 in zip(starts, starts[1:]):
        r = latest(s1 + 1.5)
        if r > s0 + 1.5:  # a new version appeared between these checks
            delays.append(s1 + 1.5 - r)
    return starts, results, delays


def test_refresh_sync_locks_on_to_site_refresh():
    from watcher.cvs_poller import RefreshSync
    starts, results, delays = _drive(RefreshSync(), _fake_site())
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= 115                          # never closer than the floor
    per_hour = len(starts) / 6
    assert 28 <= per_hour <= 32                      # ~one check per refresh, not 144/hour
    settled = sorted(delays[30:])                    # after the first hour
    assert settled[len(settled) // 2] <= 25          # typically ~10-20 s after the refresh
    assert settled[int(len(settled) * 0.9)] <= 45
    assert results.count("early") / len(results) < 0.06


def test_refresh_sync_follows_a_site_cycling_faster_or_slower():
    from watcher.cvs_poller import RefreshSync
    for period in (117.0, 123.0):
        sync = RefreshSync()
        starts, results, delays = _drive(sync, _fake_site(period=period), hours=8)
        settled = sorted(delays[60:])
        assert settled[len(settled) // 2] <= 25, period
        assert abs(sync.period - period) < 1.5, (period, sync.period)
        assert min(b - a for a, b in zip(starts, starts[1:])) >= 115


def test_refresh_sync_arriving_early_aims_later_and_keeps_floor():
    from watcher.cvs_poller import RefreshSync
    s = RefreshSync()
    assert s.observe(600.0, 650.0, 653.0) == "first"
    lead = s.lead
    nxt = s.next_start(650.0, 653.0)
    assert nxt >= 650.0 + 115
    # we arrive and the page still shows the same version: the refresh hasn't happened yet
    assert s.observe(600.0, nxt, nxt + 3) == "early"
    assert s.lead == lead + s.early_step
    after = s.next_start(nxt, nxt + 3)
    assert after >= nxt + 115                         # no quick retry, whatever happened
    assert s.observe(720.0, after, after + 3) == "fresh"


def test_run_synced_rhythm_floor_and_counts(tmp_path, monkeypatch):
    """The real loop: checks land shortly after each site refresh, never <115 s apart; an
    early arrival and an error are counted; after the error the next check is ~2 min later."""
    import watcher.cvs_poller as mod
    from datetime import timezone
    store = Store(tmp_path / "v.db")
    seen = ist(60)
    base = (datetime.now(timezone.utc).timestamp() // 120) * 120
    clock = {"wall": base + 50.0, "mono": 1000.0}
    latest = _fake_site(phase=base + 7.0)
    starts, sleeps = [], []
    fails = {7}  # the 7th fetch fails

    def page_generated(gen_epoch):
        gen = datetime.fromtimestamp(gen_epoch, IST).strftime("%d %b %Y, %I:%M %p")
        body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
                       for r in [row("CHENNAI VAC", seen)])
        return (f"<html><head><title>Latest US Visa Availability</title></head><body>"
                f"<p>The current info is generated at {gen}</p><table>{HEAD}{body}</table>"
                f"</body></html>")

    def advance(sec):
        clock["wall"] += sec
        clock["mono"] += sec

    class SiteFetcher:
        async def fetch(self, url):
            starts.append(clock["wall"])
            advance(1.5)
            r = latest(clock["wall"])
            advance(2.5)
            if len(starts) in fails:
                raise BlockedError("x")
            return page_generated(r // 60 * 60)

    class Stop(Exception):
        pass

    async def fake_sleep(sec):
        sleeps.append(sec)
        advance(sec)
        if len(starts) >= 8:
            raise Stop()

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(mod.time, "time", lambda: clock["wall"])
    poller = CvsPoller("u", 115, store, lambda r: None, SourceHealth("CVS", 15),
                       fetcher=SiteFetcher())
    with pytest.raises(Stop):
        asyncio.run(poller.run())
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g >= 115 for g in gaps)               # never faster than about 2 minutes
    assert sleeps[7] + 4.0 == pytest.approx(120)     # after the error (7th fetch, took 4 s): 2 min start-to-start, no backoff
    assert store.count_events("cvs_check", 0) == 7 and store.count_events("cvs_error", 0) == 1
    # each successful check after the first lands within a minute after a site refresh
    for t in starts[1:6]:
        assert 0 < (t + 1.5) - latest(t + 1.5) <= 60


def test_slots_wording():
    f = row_fields({"location": "CHENNAI VAC", "earliest_date": "30 Nov, 26",
                    "slots_on_earliest": "N/A", "total_dates": "1"})
    assert f["extra"] == "Number of slots not reported · 1 date open"
    f = row_fields({"location": "HYDERABAD VAC", "earliest_date": "24 May, 27",
                    "slots_on_earliest": "6", "total_dates": "11"})
    assert f["extra"] == "6 slots on that date · 11 dates open"
    f = row_fields({"location": "X", "slots_on_earliest": "1", "total_dates": "N/A"})
    assert f["extra"] == "1 slot on that date"


def test_fixed_trial_uses_five_minute_gaps_including_errors(tmp_path, monkeypatch):
    import watcher.cvs_poller as mod
    from datetime import timezone

    clock = {"now": datetime.now(timezone.utc).timestamp()}
    starts = []

    class Stop(Exception):
        pass

    class Fetcher:
        async def fetch(self, url):
            starts.append(clock["now"])
            clock["now"] += 3
            if len(starts) == 2:
                raise ValueError("temporary parser failure")
            return page([row("CHENNAI VAC", ist(60))])

    async def sleep(seconds):
        clock["now"] += seconds
        if len(starts) >= 3:
            raise Stop()

    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(mod.asyncio, "sleep", sleep)
    p = CvsPoller("fixture", 300, Store(tmp_path / "v.db"), lambda rows: None,
                  SourceHealth("CVS", 15), fetcher=Fetcher(), sync_checks=False)
    with pytest.raises(Stop):
        asyncio.run(p.run())
    assert starts[1] - starts[0] == 300
    assert starts[2] - starts[1] >= 300


def test_persistent_fetcher_restarts_after_errors_and_max_uses():
    from watcher.cvs_poller import BrowserFetcher
    f = BrowserFetcher(max_uses=2)
    events = []

    async def fake_fetch(url):
        events.append("fetch")
        if url == "bad":
            raise ValueError("boom")
        return "<html/>"

    async def fake_close():
        events.append("close")
    f._fetch, f.close = fake_fetch, fake_close

    async def go():
        await f.fetch("ok")
        await f.fetch("ok")          # 2nd use -> fresh browser next time
        with pytest.raises(ValueError):
            await f.fetch("bad")     # error -> closed
    asyncio.run(go())
    assert events == ["fetch", "fetch", "close", "fetch", "close"]

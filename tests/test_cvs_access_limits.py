import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from watcher.cvs_poller import BlockedError, CvsPoller, retry_after_seconds
from watcher.db import Store
from watcher.health import SourceHealth


def test_retry_after_accepts_seconds_and_http_date():
    assert retry_after_seconds("900") == 900
    assert retry_after_seconds(None) == 0
    assert retry_after_seconds("invalid") == 0
    future = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=10))
    assert 598 < retry_after_seconds(future) <= 600


def test_every_failure_retries_on_the_two_minute_rhythm_without_pausing(tmp_path, monkeypatch):
    """No backoff, no Retry-After, no pause: rate limits, denials and errors all retry ~2 min later."""
    from watcher import cvs_poller as mod
    failures = [BlockedError("rate-limit page", status=429, retry_after=3600),
                BlockedError("denied", status=403, retry_after=1200),
                BlockedError("denied", status=403),
                BlockedError("denied", status=403),
                ValueError("table did not appear")]
    calls, waits, notices = [], [], []

    class Done(BaseException):  # the retry loop catches every Exception; this ends the test
        pass

    class Fetcher:
        async def fetch(self, url):
            calls.append(url)
            if not failures:
                raise Done()
            raise failures.pop(0)

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(mod.asyncio, "sleep", sleep)
    store = Store(tmp_path / "v.db")
    poller = CvsPoller("status-page", 115, store, lambda rows: None,
                       SourceHealth("CVS", 15), fetcher=Fetcher(), refresh_period=120,
                       on_check=lambda text, audible: notices.append((text, audible)))
    with pytest.raises(Done):
        asyncio.run(poller.run())
    assert len(calls) == 6  # three 403s in a row no longer pause checks
    assert all(110 < w <= 120 for w in waits[1:])  # start-to-start ~2 min, whatever Retry-After says
    assert store.get_meta("cvs_rate_limited_until") is None
    assert not any("paused" in text for text, _ in notices)
    # first failure of each kind is audible; identical repeats stay silent
    assert [audible for _, audible in notices] == [True, True, False, False, True]


def test_old_saved_rate_limit_deadline_no_longer_delays_startup(tmp_path, monkeypatch):
    from watcher import cvs_poller as mod
    monkeypatch.setattr(mod.time, "time", lambda: 1000)
    store = Store(tmp_path / "v.db")
    store.set_meta("cvs_rate_limited_until", "9000")
    waits = []

    class Stop(Exception):
        pass

    async def sleep(seconds):
        waits.append(seconds)
        raise Stop()

    monkeypatch.setattr(mod.asyncio, "sleep", sleep)
    poller = CvsPoller("status-page", 115, store, lambda rows: None, SourceHealth("CVS", 15),
                       fetcher=object())
    with pytest.raises(Stop):
        asyncio.run(poller.run())
    assert waits[0] <= 20  # just the usual 5-20 s start-up jitter

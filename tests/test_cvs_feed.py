import asyncio
from datetime import datetime, timedelta

from watcher.cvs_poller import BlockedError, CvsPoller
from watcher.db import Store
from watcher.health import SourceHealth
from watcher.timeutil import IST


def make_poller(tmp_path, callback):
    now = datetime.now(IST)
    generated = now.strftime("%d %b %Y, %I:%M %p")
    older = (now - timedelta(hours=2)).strftime("%d %b %Y, %I:%M %p")
    newer = (now - timedelta(minutes=10)).strftime("%d %b %Y, %I:%M %p")
    html = (f"<p>The current info is generated at {generated}</p><table>"
            "<tr><th>Visa Location</th><th>Earliest Date</th><th>Last Seen At</th></tr>"
            f"<tr><td>HYDERABAD</td><td>10 Nov, 26</td><td>{older}</td></tr>"
            f"<tr><td>CHENNAI VAC</td><td>30 Nov, 26</td><td>{newer}</td></tr></table>")

    class Fetcher:
        async def fetch(self, url):
            return html

    return CvsPoller("local-fixture", 115, Store(tmp_path / "v.db"), lambda rows: None,
                     SourceHealth("CVS", 15), fetcher=Fetcher(), on_check=callback,
                     check_summaries=True)


def test_every_check_reports_latest_sighting_not_earliest_calendar_date(tmp_path):
    notices = []
    p = make_poller(tmp_path, lambda text, audible: notices.append((text, audible)))

    async def go():
        await p.poll_once()
        await p.poll_once()

    asyncio.run(go())
    assert len(notices) == 2
    first, second = notices
    assert not first[1] and not second[1]
    assert "Latest report: Chennai OFC/biometrics" in first[0]
    assert "Earliest 30 Nov 2026" in first[0]
    assert "Report last seen:" in first[0] and "Page updated:" in first[0]
    assert "2 locations" in first[0] and "Baseline saved" in first[0]
    assert "No new reports" in second[0]


def test_immediate_failure_repeat_and_recovery(tmp_path):
    notices = []
    p = make_poller(tmp_path, lambda text, audible: notices.append((text, audible)))
    p._notify_failure(BlockedError("HTTP 403; request_id=one", status=403), 300)
    p._notify_failure(BlockedError("HTTP 403; request_id=two", status=403), 600)
    p._notify_failure(BlockedError("HTTP 429", status=429), 1200)
    p._notify_failure(TimeoutError("page timed out"), 120)
    p._notify_failure(ValueError("table missing"), 240)
    assert [audible for _, audible in notices] == [True, False, True, True, True]
    assert "300 seconds" in notices[0][0]
    asyncio.run(p.poll_once())
    assert "recovered" in notices[-1][0] and notices[-1][1]
    asyncio.run(p.poll_once())
    assert not notices[-1][1]


def test_feed_failure_does_not_break_a_successful_check(tmp_path):
    def broken(text, audible):
        raise RuntimeError("feed unavailable")

    p = make_poller(tmp_path, broken)
    asyncio.run(p.poll_once())
    assert len(p.store.get_cvs_snapshot()) == 2
    assert p.health.consecutive_failures == 0


def test_browser_failure_message_is_actionable_without_chrome_command_dump(tmp_path):
    notices = []
    p = make_poller(tmp_path, lambda text, audible: notices.append(text))

    class TargetClosedError(Exception):
        pass

    p._notify_failure(TargetClosedError("Browser launch failed\nBrowser logs:\n"
                                      "--many-internal-flags\n"
                                      "ERROR: The profile appears to be in use"), 120)
    assert "TargetClosedError" in notices[0]
    assert "The profile appears to be in use" in notices[0]
    assert "--many-internal-flags" not in notices[0]


def test_routine_summaries_off_keeps_failures_recovery_and_changed_reports(tmp_path):
    notices, reports = [], []
    p = make_poller(tmp_path, lambda text, audible: notices.append((text, audible)))
    p.check_summaries = False
    p.on_change = reports.extend

    async def go():
        await p.poll_once()
        await p.poll_once()
        assert notices == []
        old = p.store.get_cvs_snapshot()
        old["CHENNAI VAC"]["last_seen"] = "01 Nov 2026, 12:00 AM"
        p.store.save_cvs_snapshot(old)
        await p.poll_once()
        assert len(reports) == 1 and reports[0]["location"] == "CHENNAI VAC"
        assert notices == []
        p._notify_failure(BlockedError("HTTP 403", status=403), 300)
        assert notices[-1][1] and "403" in notices[-1][0]
        await p.poll_once()
        assert notices[-1][1] and "recovered" in notices[-1][0]
        await p.poll_once()
        assert len(notices) == 2

    asyncio.run(go())


def test_browser_handles_country_button_that_appears_after_navigation(tmp_path):
    from watcher.cvs_poller import BrowserFetcher
    from types import SimpleNamespace

    state = {"hydrated": False, "india_selected": False}

    class Locator:
        def __init__(self, kinds):
            self.kinds = set(kinds)

        def or_(self, other):
            return Locator(self.kinds | other.kinds)

        @property
        def first(self):
            return self

        async def wait_for(self, **kwargs):
            assert "button" in self.kinds  # current site uses a button, not a checkbox
            state["hydrated"] = True

        async def is_visible(self):
            return state["hydrated"] and "button" in self.kinds

        async def click(self):
            assert state["hydrated"] and "button" in self.kinds
            state["india_selected"] = True

    class Page:
        async def goto(self, *args, **kwargs):
            class Response:
                status = 200

                async def all_headers(self):
                    return {}
            return Response()

        def get_by_role(self, role, **kwargs):
            return Locator([role])

        def locator(self, selector):
            return Locator(["table"])

        async def wait_for_selector(self, *args, **kwargs):
            assert state["india_selected"]

        async def wait_for_load_state(self, *args, **kwargs):
            pass

        async def content(self):
            return "<table>availability</table>"

    f = BrowserFetcher(profile_dir=tmp_path)

    async def ensure():
        return Page()

    f._ensure_page = ensure
    assert asyncio.run(f._fetch("fixture")) == "<table>availability</table>"


def test_http_200_rate_limit_page_is_not_treated_as_parser_failure(tmp_path):
    from watcher.cvs_poller import BrowserFetcher
    import pytest

    class Response:
        status = 200

        async def all_headers(self):
            return {"x-matched-path": "/latest-us-visa-availability/rate-limited",
                    "x-vercel-id": "test-id"}

    class Page:
        async def goto(self, *args, **kwargs):
            return Response()

    f = BrowserFetcher(profile_dir=tmp_path)

    async def ensure():
        return Page()

    f._ensure_page = ensure
    with pytest.raises(BlockedError) as exc:
        asyncio.run(f._fetch("fixture"))
    assert exc.value.status == 429
    assert "HTTP 200" in str(exc.value) and "test-id" in str(exc.value)

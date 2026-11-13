import asyncio
from types import SimpleNamespace, ModuleType
import sys
from unittest.mock import AsyncMock

import pytest

from watcher.cvs_poller import BrowserFetcher, CvsPoller
from watcher.db import Store
from watcher.health import SourceHealth


def test_baseline_browser_has_no_network_interception(tmp_path, monkeypatch):
    api = ModuleType("playwright.async_api")
    pkg = ModuleType("playwright")
    pkg.async_api = api
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", api)
    page = object()
    context = SimpleNamespace(pages=[page], new_cdp_session=AsyncMock(), on=AsyncMock())
    chromium = SimpleNamespace(launch_persistent_context=AsyncMock(return_value=context))
    pw = SimpleNamespace(chromium=chromium)
    api.async_playwright = lambda: SimpleNamespace(start=AsyncMock(return_value=pw))
    f = BrowserFetcher(profile_dir=tmp_path, headed=True, network_controls=False)
    assert asyncio.run(f._ensure_page()) is page
    context.new_cdp_session.assert_not_called()
    context.on.assert_not_called()
    opts = chromium.launch_persistent_context.call_args.kwargs
    assert opts["headless"] is False and opts["channel"] == "chromium"
    assert "user_agent" not in opts and f._network is None


def test_requested_cooldown_survives_restart_and_preserves_longer_deadline(tmp_path, monkeypatch):
    import watcher.cvs_poller as mod
    monkeypatch.setattr(mod.time, "time", lambda: 1000)
    store = Store(tmp_path / "v.db")
    store.set_meta("cvs_manual_cooldown_until", "4600")
    store.set_meta("cvs_next_check_not_before", "5000")
    notices, waits = [], []

    class Stop(Exception):
        pass

    async def sleep(seconds):
        waits.append(seconds)
        raise Stop()

    monkeypatch.setattr(mod.asyncio, "sleep", sleep)
    h = SourceHealth("CVS", 15)
    p = CvsPoller("fixture", 115, store, lambda rows: None, h, fetcher=object(),
                  on_status=notices.append)
    with pytest.raises(Stop):
        asyncio.run(p.run())
    assert waits == [4000]
    assert h.deferred_until == 5000 and not h.is_failing()
    assert "requested cooldown" in notices[0]


def test_intentional_cooldown_has_normal_failure_grace_after_resume(monkeypatch):
    import watcher.health as mod
    clock = {"now": 1000}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])
    h = SourceHealth("CVS", 15)
    h.defer_checks_until(4600)
    clock["now"] = 4500
    assert h.check() is None
    clock["now"] = 4601
    assert h.check() is None
    h.fail("real error after resume")
    clock["now"] = 5501
    assert "real error after resume" in h.check()

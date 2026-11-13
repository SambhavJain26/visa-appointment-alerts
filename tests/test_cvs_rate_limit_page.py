"""The site's soft rate limit: a frozen visa-type index served at the H-1B URL, often without the
x-matched-path header. It must be recognised and reported as rate limiting, not a parser error.
After each successful check the tab is parked on about:blank."""
import asyncio

import pytest

from watcher.cvs_poller import BlockedError, BrowserFetcher, rate_limit_marker

# Shapes copied from the real page saved on 8 Nov 2026 (Next.js route data, status line).
ROUTE_DATA = r'<script>self.__next_f.push([1,"0:{\"c\":[\"\",\"latest-us-visa-availability\",\"rate-limited\"]}"])</script>'
STALE_STATUS = "Status: The current info is generated <b><span>8 mos ago</span></b> i.e., at Mar 07, 2026"
# Every page's shared layout script mentions the plain path; that alone is not the rate-limit page.
LAYOUT_SCRIPT = '<script>!b.startsWith("/latest-us-visa-availability/rate-limited")</script>'
REAL_STATUS = "Status: The current info is generated <b><span>2 mins ago</span></b>"


def test_rate_limit_page_markers():
    assert rate_limit_marker(ROUTE_DATA) == "page route is rate-limited"
    assert rate_limit_marker(STALE_STATUS) == "page data is months old"
    assert rate_limit_marker(LAYOUT_SCRIPT + REAL_STATUS + "<table></table>") is None


class Response:
    status = 200

    def __init__(self, headers=None):
        self.headers = headers or {}

    async def all_headers(self):
        return self.headers


class FakePage:
    def __init__(self, html, headers=None):
        self.html, self.headers, self.visited = html, headers, []

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        return Response(self.headers)

    async def content(self):
        return self.html


def _fetcher(tmp_path, page):
    f = BrowserFetcher(profile_dir=tmp_path / "profile")

    async def ensure():
        f._page = page
        return page

    f._ensure_page = ensure
    return f


@pytest.mark.parametrize("html", [ROUTE_DATA, STALE_STATUS])
def test_rate_limit_page_without_header_is_blocked_immediately(tmp_path, html):
    page = FakePage(LAYOUT_SCRIPT + html, headers={"x-vercel-id": "req-1"})
    f = _fetcher(tmp_path, page)
    with pytest.raises(BlockedError) as exc:
        asyncio.run(f._fetch("https://checkvisaslots.com/latest-us-visa-availability/h-1b-regular/"))
    assert exc.value.status == 429
    assert "rate-limit page" in str(exc.value) and "req-1" in str(exc.value)
    # Recognised straight away: no waiting for the table or clicking the country selector.
    assert page.visited == ["https://checkvisaslots.com/latest-us-visa-availability/h-1b-regular/"]


def test_tab_is_parked_on_blank_page_after_a_check(tmp_path):
    page = FakePage("<table>rows</table>")
    f = _fetcher(tmp_path, page)

    async def fetch(url):
        await (await f._ensure_page()).goto(url)
        return "<table>rows</table>"

    f._fetch = fetch
    assert asyncio.run(f.fetch("https://checkvisaslots.com/h1b")) == "<table>rows</table>"
    assert page.visited == ["https://checkvisaslots.com/h1b", "about:blank"]


def test_failed_blanking_still_returns_the_table_and_closes_browser(tmp_path):
    class BrokenBlank(FakePage):
        async def goto(self, url, **kwargs):
            if url == "about:blank":
                raise RuntimeError("page crashed")
            return await super().goto(url)

    page = BrokenBlank("<table>rows</table>")
    f = _fetcher(tmp_path, page)
    closed = []

    async def fetch(url):
        await f._ensure_page()
        return "<table>rows</table>"

    async def close():
        closed.append(True)

    f._fetch, f.close = fetch, close
    assert asyncio.run(f.fetch("https://checkvisaslots.com/h1b")) == "<table>rows</table>"
    assert closed

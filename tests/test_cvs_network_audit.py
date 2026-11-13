from types import SimpleNamespace
import json

from watcher.cvs_poller import BrowserFetcher
from watcher.browser_network import BrowserNetwork
import asyncio


def test_network_observer_counts_prefetch_without_recording_secrets(tmp_path):
    f = BrowserFetcher(profile_dir=tmp_path)
    f._audit_request(SimpleNamespace(
        url="https://checkvisaslots.com/latest-us-visa-availability/?token=secret-query",
        resource_type="fetch",
        headers={"next-router-prefetch": "1", "cookie": "secret-cookie"}))
    assert f._request_types == {"fetch": 1}
    assert f._request_hosts == {"checkvisaslots.com": 1}
    assert f._site_paths == {"/latest-us-visa-availability/": 1}
    assert f._prefetches == 1
    recorded = json.dumps([f._request_types, f._request_hosts, f._site_paths])
    assert "secret" not in recorded and "token" not in recorded


def test_only_speculative_same_site_requests_are_blocked():
    sent = []

    class Session:
        async def send(self, method, args):
            sent.append((method, args))

    p = BrowserNetwork(Session())

    async def go():
        for i, (host, headers) in enumerate([
            ("checkvisaslots.com", {"Next-Router-Prefetch": "1", "RSC": "1"}),
            ("checkvisaslots.com", {"RSC": "1"}),
            ("checkvisaslots.com", {}),
            ("another.example", {"Purpose": "prefetch"}),
        ]):
            await p.intercept({"requestId": str(i), "networkId": str(i),
                               "request": {"url": "https://" + host + "/page", "headers": headers}})

    asyncio.run(go())
    assert [method for method, _ in sent] == ["Fetch.failRequest"] + ["Fetch.continueRequest"] * 3
    assert p.blocked == 1


def test_cache_hits_and_revalidations_are_not_misreported_as_downloads():
    p = BrowserNetwork(None)
    for key in ["cached", "revalidated", "network"]:
        p.request({"requestId": key, "request": {"url": "https://checkvisaslots.com/file"}})
        p.response({"requestId": key, "response": {"fromDiskCache": key != "network"}})
    p.extra({"requestId": "revalidated", "statusCode": 304})
    p.finished({"requestId": "cached", "encodedDataLength": 900})
    p.finished({"requestId": "revalidated", "encodedDataLength": 100})
    p.finished({"requestId": "network", "encodedDataLength": 1000})
    report = p.snapshot()
    assert report["cache_only_responses"] == 1
    assert report["network_responses"] == 2
    assert report["network_revalidations"] == 1
    assert report["encoded_network_bytes"] == 1100
    assert p.snapshot() == {"prefetches_prevented": 0}

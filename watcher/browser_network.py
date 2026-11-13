"""Limit speculative traffic and observe Chromium's cache without disabling it."""
import logging
from collections import Counter
from urllib.parse import urlsplit

log = logging.getLogger("watcher.cvs")


class BrowserNetwork:
    def __init__(self, session, site_host="checkvisaslots.com"):
        self.session = session
        self.site_host = site_host
        self.records = {}
        self.blocked = 0

    async def start(self):
        s = self.session
        s.on("Network.requestWillBeSent", self.request)
        s.on("Network.requestServedFromCache", self.cache)
        s.on("Network.responseReceived", self.response)
        s.on("Network.responseReceivedExtraInfo", self.extra)
        s.on("Network.loadingFinished", self.finished)
        s.on("Network.loadingFailed", self.failed)
        s.on("Fetch.requestPaused", self.intercept)
        await s.send("Network.enable")
        # Fetch interception keeps Chromium's HTTP cache enabled. Do not use Playwright
        # route(), which disables caching, or Network.setCacheDisabled(true).
        await s.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})

    def record(self, event):
        return self.records.setdefault(event["requestId"], {})

    def request(self, event):
        self.record(event)["host"] = urlsplit(event["request"]["url"]).hostname

    def cache(self, event):
        self.record(event)["cached"] = True

    def response(self, event):
        r = event["response"]
        record = self.record(event)
        record["response"] = True
        record["cached"] = record.get("cached", False) or r.get("fromDiskCache", False) or r.get("fromPrefetchCache", False)
        record["service_worker"] = r.get("fromServiceWorker", False)

    def extra(self, event):
        self.record(event)["wire_status"] = event["statusCode"]

    def finished(self, event):
        self.record(event).update(done=True, encoded_bytes=event.get("encodedDataLength", 0))

    def failed(self, event):
        self.record(event).update(done=True, failed=True)

    async def intercept(self, event):
        request = event["request"]
        headers = {k.lower(): str(v).lower() for k, v in request.get("headers", {}).items()}
        speculative = (headers.get("next-router-prefetch") == "1" or
                       bool(headers.get("next-router-segment-prefetch")) or
                       "prefetch" in headers.get("purpose", "") or
                       "prefetch" in headers.get("sec-purpose", ""))
        block = urlsplit(request["url"]).hostname == self.site_host and speculative
        try:
            if block:
                self.blocked += 1
                if event.get("networkId"):
                    self.records.setdefault(event["networkId"], {})["blocked"] = True
                await self.session.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "Aborted"})
            else:
                await self.session.send("Fetch.continueRequest", {"requestId": event["requestId"]})
        except Exception:
            log.debug("Request interception ended during browser shutdown", exc_info=True)

    def snapshot(self):
        counts = Counter()
        for r in self.records.values():
            if not r.get("done"):
                continue
            if r.get("blocked"):
                counts["blocked_prefetch_requests"] += 1
            elif r.get("service_worker"):
                counts["service_worker_responses"] += 1
            elif r.get("wire_status") == 304:
                counts["network_responses"] += 1
                counts["network_revalidations"] += 1
            elif r.get("cached"):
                counts["cache_only_responses"] += 1
            elif r.get("response"):
                counts["network_responses"] += 1
            elif r.get("failed"):
                counts["failed_without_response"] += 1
            if not r.get("cached") or r.get("wire_status") == 304:
                counts["encoded_network_bytes"] += r.get("encoded_bytes", 0)
        counts["prefetches_prevented"] = self.blocked
        self.blocked = 0
        self.records = {k: v for k, v in self.records.items() if not v.get("done")}
        return dict(counts)

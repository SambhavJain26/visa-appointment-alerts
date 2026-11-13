# CheckVisaSlots browser checks - 7 November 2026

## Latest check: the VPS check succeeds (~02:42 IST)

One isolated fetch from the VPS using the
updated BrowserFetcher returned **HTTP 200** and parsed **8 location rows**.
Configuration: full headed Chromium under Xvfb, native browser identity, a dedicated
persistent profile, and normal resource loading. No User-Agent rewriting or resource
interception was used. The profile is under the data directory.

Request ID: `<request id>`.
Page generated at: `2026-11-06T21:10:00+00:00` (7 Nov, 02:40 IST).

This was a single read-only check, not a polling trial. It does not establish why the
earlier requests were denied or demonstrate lasting access. The running Telegram
container was not modified, no alerts were sent, and CVS remains disabled there.

## Earlier checks

Testing with standard browser identification for the H-1B Regular page was evaluated at
a maximum frequency of once per 25 seconds from the VPS.

Two isolated read-only page fetches were made from that VPS, 42 seconds apart.
Neither ran the alert pipeline or updated the watcher database.

| Check | Time (IST) | Result |
|---|---|---|
| Full Chromium, new headless mode (`channel="chromium"`) | 01:35:04 | HTTP 403 |
| Full Chromium, headed mode with Xvfb | 01:35:46 | HTTP 403 |

Both used the installed Chromium browser, the standard Chrome User-Agent below,
India timezone and locale, and the existing image/font/media filtering.

```text
Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36
```

Both responses had `server: Vercel`, `x-vercel-mitigated: deny`, title
`403: Forbidden`, and body `This request was blocked`. Neither issued cookies
or local storage, so there was no browser session state to reuse in a follow-up check.

Request identifiers for CheckVisaSlots support:

```text
new headless: <request id>
headed: <request id>
```

These results establish an explicit Vercel denial, but do not establish which rule
caused it or whether it targets the IP, network, browser signals, or another condition.
Logged request IDs for diagnosis against the H-1B Regular page:

```text
https://checkvisaslots.com/latest-us-visa-availability/h-1b-regular/
```

CVS remains disabled. The production browser configuration was not changed by these
diagnostics; Telegram continues under the existing deployment. No message was sent to
the site or its support team.

# CheckVisaSlots rate-limit investigation - 7 November 2026

## Established from existing VPS evidence

- Last successful pre-failure check: 11:49:32 IST. First failure: 11:52:16.
  The quiet-feed replacement container started at 11:52:20, after that failure.
- Successful polling was 29-30 checks/hour, consistent with the adaptive ~2-minute schedule.
- The failing H-1B URL returned HTTP 200 with
  `x-matched-path: /latest-us-visa-availability/rate-limited`.
- Cached response headers include `cache-control: public, max-age=0, must-revalidate`
  and `x-vercel-cache: HIT`. The 12:31:58 retry had a new Vercel request ID:
  `<request id>`.
- The captured response is a stale generic visa-type index, not an H-1B table covered
  by a popup. The country selector could be dismissed, but no table appeared afterward.
- There is no service-worker store in the dedicated profile and no non-analytics site
  cookies in its cookie database. These facts do not prove how the site keys its limiter.
- Exact Chromium disk-cache keys show Next.js RSC (`_rsc`) responses for `/` (14 cache-key
  variants) and `/latest-us-visa-availability/` (7 variants), in addition to the H-1B page.
  There are 61 site cache keys total, including static assets. Cache-key counts are NOT
  request totals and do not establish how often these routes were requested.

## Interpretation and remaining uncertainty

The immediate cause is server-side routing to a rate-limit response, not the promotional
popup or a parser inventing a failure. The original rate-limited fetch predates the
quiet-feed restart, so the summary toggle did not initiate that fetch.

A cumulative IP/session/traffic quota is plausible. Automatic prefetch requests are a
concrete extra source of traffic beyond the scheduled page navigations, but we cannot
establish the limiter's key, threshold, counting window, reset deadline, or whether it
counts those prefetches from client evidence alone. No numeric quota should be claimed.

Thirty minutes is our conservative retry backoff, NOT a site-provided unblock time.

## Diagnostic change

Deployed passive network counting on the existing browser context: request resource types,
hosts, same-site paths and prefetch indicators, plus selected response/cache/rate-limit
headers. Query values, cookies, authorization and other request headers are not logged.
No traffic routing, browser identity, profile, cadence or site-request scope was changed
by this diagnostic patch. It generates no additional website requests. Existing backoff
remains persisted, with the next scheduled attempt at ~13:02 IST.

Validation: existing 143 tests passed; the additional network-audit privacy/counting test
passed separately (144 tests total). Production Telegram resumed after deployment.

## Online research and later measurements - 7 November 2026

No verified, CheckVisaSlots-specific client-side remedy or published public-page quota was
found in targeted searches. Avoid treating unrelated visa-portal rate-limit anecdotes as evidence.

The 13:47:44 IST attempt had 1 network response, 16 cache-only responses, 0 prevented
prefetches, and an HTTP-200 response mapped to the rate-limited route. This weakens the
hypothesis that current background prefetch volume alone explains the block. It does not
rule out a previously accumulated quota, IP/network reputation, or other site-side criteria.

Primary references:
- https://vercel.com/docs/vercel-firewall/vercel-waf/custom-rules - persistent blocks can be
  tied to the triggering client's IP and configured duration. This is a supported Vercel
  capability, NOT proof that CheckVisaSlots uses this particular WAF mechanism.
- https://vercel.com/docs/routing-middleware - routing logic runs before cache lookup and
  can rewrite a URL to another page. Therefore cache HIT does not itself prove a stale
  browser cache or identify why a rate-limit rewrite was selected.
- https://www.rfc-editor.org/rfc/rfc6585.html#section-4 - HTTP 429 supports Retry-After,
  leaves counting/identification policy to the server, and must not be cached. Our observed
  response is HTTP 200, so its unknown cooldown cannot be inferred from that standard.
- https://vercel.com/docs/vercel-firewall/firewall-observability - the owner can inspect
  applied rules and traffic by IP, path, ASN and fingerprint; app-level middleware may
  additionally require their application logs.
- https://playwright.dev/python/docs/api/class-page#page-route - Playwright routing disables
  HTTP caching. The implemented CDP prefetch filter was explicitly verified to preserve cache.

Practical next step: a longer uninterrupted cooldown with one scheduled diagnostic attempt
is a possible experiment, not a known fix. Reliable resolution requires identifying the
matching rate limit rule, quota, and reset window, while respecting server load.
Observability would benefit from standard 429 / Retry-After response headers, but this
is not by itself evidence that response caching caused or will resolve the restriction.
No deployment settings were changed during this research, and no manual availability fetches
were made. Do not promise that random timing, headed mode or a fresh IP resolves it.

# Operations notes (November 2026)

These are dated notes from running the watcher. They record what was tried and why.

## Website reader: access history

- CheckVisaSlots serves a Vercel Security Checkpoint to plain HTTP clients, so the reader uses full Chromium
  (Playwright) with its native identity, a persistent profile and normal page resources.
- Read-only access targets one public status page with standard browser identification.
- Early checks were denied with HTTP 403 ([cvs-access-checks.md](cvs-access-checks.md)). After the browser was
  updated, checks succeeded from the VPS.
- A later rate-limit page was investigated in [cvs-rate-limit-investigation.md](cvs-rate-limit-investigation.md).
- One browser stays open between checks and restarts after 180 successes (~6 hours) or an error,
  keeping the profile. Times are normalised to IST using the page's own "generated at" time.
- A separate website-only deployment on a second server was tried and then stopped. Both sources now run in one
  deployment.

## Owner feed behaviour

- Routine success summaries are off (`CVS_FEED_CHECK_SUMMARIES=false`) to keep the owner feed quiet. Turn them on
  only when diagnosing freshness.
- Failed checks immediately report their error and next retry delay to the owner feed. The first failure, or a
  different error, notifies with sound; identical repeats are silent. Recovery also notifies.
- These technical messages never go to family alert chats.
- HTTP-200 rate-limit pages (recognised by header or page content) are reported as rate limiting and retried
  ~2 minutes later like any other failure.

## Five-minute traffic-reduction trial (7 November)

- `CVS_SYNC_CHECKS=false` with `CVS_MIN_GAP_SECONDS=300` gave fixed five-minute checks instead of catching every
  two-minute site update.
- Only same-site requests marked as speculative prefetch were aborted through Chromium's Fetch protocol. Normal
  navigation, scripts, fonts, images and required data requests continued.
- A network audit separated network responses, HTTP-304 revalidations, local cache hits and prevented prefetches.

## Baseline restored (7 November, ~14:00 IST)

- The trial was turned off. Production returned to the adaptive ~2-minute rhythm (120-second refresh, 115-second
  minimum) with `CVS_NETWORK_CONTROLS_ENABLED=false`, so no request interception runs.
- Website alerts are limited to reports at most five minutes old (`CVS_MAX_ALERT_AGE_MINUTES=5`); older changed
  reports go only to the owner feed. Telegram keeps its 30-minute cutoff.

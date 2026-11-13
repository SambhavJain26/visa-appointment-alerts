# Case Study: Visa Appointment Monitoring & Real-Time Alert Engine

> *How a read-only watcher helped a family member book an H-1B visa appointment: the problem, the design, and what
> was hard.*

---

## Project Summary

- **Project Title**: Visa Appointment Monitoring & Real-Time Alert Engine
- **Subtitle**: A 24/7 read-only watcher that reads public Telegram groups and the CheckVisaSlots status page,
  checks posts with rules and an OpenAI vision model, and sends Telegram alerts when slots open.
- **Role**: Sole developer
- **Author**: Sambhav Jain
- **Timeline**: About two weeks, then running 24/7
- **Stack & Technologies**:
  - **Language & Runtime**: Python 3.12, asyncio, Docker Compose
  - **Ingestion**: Telethon (Telegram MTProto user client, read-only), Playwright Chromium (persistent profile)
  - **Verification**: OpenAI vision model with a strict JSON schema response
  - **State**: SQLite in WAL mode (group cursors, page snapshot, alerted keys)
  - **Alerting**: Telegram Bot API over HTTPX with per-recipient queues; optional CallMeBot WhatsApp
  - **Quality**: pytest (160 tests, including read-only safety checks), GitHub Actions CI
- **Project Type**: Personal project for a family member

---

## Outcome

**The watcher's alerts led to a booked H-1B appointment for the family member it was built for.** It runs 24/7 on a
VPS, and nobody has to watch the groups or the website by hand.

What the design guarantees, by construction and by tests:
* **Never touches the visa portal and never books**, so the applicant's account is never put at risk by the tool.
* **Never posts, joins or reacts on Telegram** with the reading account.
* **Fails open:** a model outage can't silently hide a real opening.

---

## The Problem

### 1. Slots are scarce and unpredictable
US visa appointments in India (New Delhi, Mumbai, Chennai, Hyderabad and Kolkata) open rarely, at no fixed time,
and are taken quickly. Missing a short opening can mean waiting a long time for the next one.

### 2. Booking bots are risky
Some applicants use tools that automate the official portal. Automating the portal can get the applicant's account
suspended, and handing credentials to third-party booking services is a security risk. This project deliberately
only alerts; a person books by hand.

### 3. Community groups are noisy
Applicants report openings in public Telegram groups, but most messages are questions, "NA" (not available) reports,
stickers and chat. Many real sightings are screenshots of the booking calendar with no text, which a text-only
filter would miss.

---

## The Solution

The watcher reads the community sources the applicant was already relying on, and does the watching for them:

```
[ Public Telegram groups ] ──► Telethon poller (~25 s) ──► Rule filter ──► OpenAI vision check ──┐
                                                                                                  ▼
[ CheckVisaSlots page (off by default) ] ──► Playwright reader (~2 min) ──► changed row ──► Deduplication
                                                                                                  │
                                                                                                  ▼
                                                                           Per-recipient alert queues
                                                                           ├──► Telegram alert bot
                                                                           └──► WhatsApp (optional)
```

---

## Step-by-Step Walkthrough

### Phase 1: Ingestion
* **Telegram:** an asynchronous Telethon user client polls each configured public group about every 25 seconds,
  staggered across groups. It keeps one cursor per group in SQLite (`tg_cursor.last_id`), so after a restart it
  resumes where it left off. Posts older than 30 minutes, such as a backlog after downtime, are never alerted.
* **CheckVisaSlots (optional):** a Playwright Chromium reader with a persistent profile reads the public H-1B table
  about every 2 minutes (never two checks less than 115 seconds apart) and alerts when a row's "Last Seen At"
  changes. These changes skip the AI check (disabled by default).

### Phase 2: Rule filter, then the vision model
1. **Rule filter (`na_filter.py`):** drops pure "NA" reports, stickers, service messages and empty messages. Each
   decision is written to `decisions.log`. Only posts that might be sightings reach the model.
2. **Vision check (`ai_check.py`):** images are downloaded into memory (`download_media(file=bytes)`), never to
   disk, and sent with the text to a small OpenAI vision model using a **strict JSON schema**. The model returns
   whether slots are available, the appointment type, the city, the dates and a short summary.
3. **Fail open:** if the model times out, errors or returns bad output, a post that still looks like a sighting is
   alerted anyway with the header "CHECK THIS POST · automatic check failed". A fallback model is used
   automatically if the main one is unavailable.

### Phase 3: Deduplication
The same opening is often posted many times. Before an alert is queued, the watcher skips:
* a message that was already alerted (keys in SQLite),
* other images from the same album,
* the same person posting the same text or screenshot in several groups within 5 minutes,
* the same slot reported again within 2 minutes, matched by a fingerprint from `slotkey.py`:
```python
slot_key = f"{appointment_type}|{city}|{first_date}"   # e.g. "biometrics|chennai|2026-11-24"
```
City aliases (for example "CHN", "Madras" and "Chennai") are normalised. If the city or date can't be pinned down,
there is no fingerprint and the alert is always sent: a duplicate is better than a miss.

### Phase 4: Delivery and 24/7 reliability
* **Per-recipient queues** (`alerts.py`): one failing chat never delays the others. Sends are spaced 1.5 seconds
  apart per chat, Telegram's `retry_after` is respected, transient errors are retried with backoff (5 s up to
  10 min), and if Telegram rejects the HTML the alert is resent as plain text.
* **Supervised loops:** each loop restarts with backoff after a crash; Docker restarts the process if it dies.
* **Watchdog:** a separate thread in `app.py` exits the process if the Telegram loop or the event loop stops for more
  than 10 minutes, and Docker restarts it.
* **Health warnings** (`health.py`): one warning when Telegram, the AI check or CheckVisaSlots has been failing,
  and one when it recovers. A daily check-in confirms the watcher is alive. These go only to the owner, never to
  the family alert chats.

---

## What an Alert Looks Like

The format comes from `render_alert()` in `alerts.py` (the values below are illustrative):

```text
🚨 BIOMETRICS (OFC) SLOT

📍 Chennai
📅 24 Nov

💬 Chennai OFC available 24 Nov

👥 <group name>
🕒 Posted 6 Nov, 2:02 PM IST · 3:32 AM EST
🔗 Open post
```

When a screenshot is the evidence, the screenshot is sent as a photo with the alert as its caption.

### Decision audit trail (`decisions.log`)
Every post gets one line, so any alert (or missed alert) can be traced:
* `SKIP filter [...]`: dropped by the rule filter
* `SKIP ai [...]`: the model said there was no availability
* `SKIP dup-*`: a duplicate (`dup-id`, `dup-album`, `dup-sender`, `dup-summary`)
* `FORWARD`: alert sent

---

## Key Engineering Challenges

### 1. Telegram rate limits without losing messages
* **Challenge:** Telegram returns `FloodWait` errors when a client reads too often, and a crash mid-read must not
  lose or repeat messages.
* **Solution:** the poller staggers group reads, honours each `FloodWait` for exactly the requested time (flood-waits
  don't count as freezes for the watchdog), and stores the last processed message ID per group in SQLite.

### 2. Screenshots and model outages
* **Challenge:** many real sightings are calendar screenshots with no text, but depending on a model means an
  outage could hide a real opening.
* **Solution:** screenshots go to a vision model with a strict JSON schema, and the pipeline fails open: when the
  model can't answer, plausible sightings are still alerted with a "please look" header.

### 3. Resilient and rate-aware web polling
* **Challenge:** CheckVisaSlots serves a bot checkpoint to plain HTTP clients, and early browser checks were denied
  (HTTP 403); later a rate-limit page appeared.
* **Solution:** The reader uses full Chromium with a persistent browser profile, native request headers, and
  conservative polling intervals (synced to the 2-minute site refresh cycle, with a 25-second floor). Both problems
  were investigated from response headers and request IDs, and documented in [`docs/`](docs/) with clear root-cause analysis.

### 4. Silent hangs
* **Challenge:** an asyncio process can hang without crashing, so Docker never restarts it.
* **Solution:** a watchdog thread outside the event loop checks that the loops are still ticking and calls
  `os._exit` after 10 minutes without progress, so Docker starts a fresh process.

---

## Technical Decisions & Trade-offs

| Decision | Chosen | Alternative | Rationale |
| :--- | :--- | :--- | :--- |
| **Scope** | **Read-only alerts** | Booking bot | Automating the portal risks the applicant's account. Alerts let a person book by hand. |
| **Reading Telegram** | **Telethon user client** | Telegram Bot API | A bot can only read groups it has been added to; a user client can read public groups by username. |
| **Filtering** | **Rules, then a vision model** | Model only | Most posts are obvious noise; the rules drop them without an API call. |
| **Model failure** | **Fail open** | Fail closed | A false alarm costs a glance; a missed opening costs the appointment. |
| **Storage** | **SQLite (WAL)** | Redis / PostgreSQL | One small service on one server doesn't need a database server. |

---

## What's Next

* **More visa types and consulates:** make the groups, page and fingerprint rules configurable per visa type.
* **More alert channels:** add other outputs (for example email or a webhook) alongside Telegram.
* The read-only rule stays: the watcher will never log in to or automate the official portal.

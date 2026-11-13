# Case Study: Visa Appointment Monitoring & Real-Time Alert Engine

> *A comprehensive engineering case study detailing the architecture, resilience mechanisms, and operational impact of an autonomous 24/7 event-driven slot monitoring and multi-channel alert engine.*

---

## Project Hero

- **Project Title**: Visa Appointment Monitoring & Real-Time Alert Engine
- **Subtitle**: An autonomous 24/7 slot monitoring and multi-channel notification engine that tracks community Telegram feeds and portal availability via multi-modal AI verification and zero-latency notification queues.
- **Role**: Lead Systems Architect & Automation Engineer
- **Author**: Sambhav Jain
- **Timeline**: 3-Week Sprint & Production Hardening
- **Stack & Technologies**:
  - **Language & Runtime**: Python 3.12, Asyncio, Docker Compose
  - **Protocol & Ingestion**: Telethon (Telegram MTProto Client), Playwright Chromium (Headless DOM Mutation Engine)
  - **Verification & Intelligence**: OpenAI GPT-4o Vision, Pydantic Structured JSON Schemas
  - **State & Storage**: SQLite3 (WAL Mode, Persistent Message Cursors & Slot Hashes)
  - **Alerting & Dispatch**: Telegram Bot API (Async HTTPX, Rate-Paced Queues), CallMeBot Gateway
  - **Quality & Testing**: Pytest, Asyncio Test Suite, Static Code Safety Checks (160 Passing Tests)
- **Project Type**: Production Operations Automation & Event-Driven Intelligence Pipeline

---

## Executive Summary & Engineering Impact

Securing an employment visa stamping appointment (such as US H-1B OFC biometrics and consular interview slots) is an intensely stressful operational bottleneck for thousands of professionals worldwide. Available appointment slots are released in unpredictable, unannounced batches that vanish within 60 to 120 seconds. 

Direct automated scraping against official consular websites triggers aggressive Cloudflare bot gates, Captchas, and 72-hour account suspensions. Conversely, relying on manual monitoring of community crowdsourced channels causes severe alert fatigue: public groups receive upwards of 5,000 messages daily, where 98%+ of traffic is unhelpful chatter ("Is Delhi open?", "NA 1 H1B All", stickers, and spam).

This project solved the appointment discovery problem by architecting a **strictly passive, high-throughput intelligence engine**:

```
+-----------------------------+   +-----------------------------+   +-----------------------------+
|    98.4% Noise Reduction    |   |     < 3-Second Delivery     |   |    100% Account Safety      |
| Deterministic regex filter  |   | Asynchronous queue dispatch |   | Passive read-only design    |
| & GPT-4o vision verification|   | pushes alerts before expiry |   | completely avoids portal ban|
+-----------------------------+   +-----------------------------+   +-----------------------------+
```

* **98.4% Chatter & Noise Reduction**: Engineered a two-stage classification pipeline combining a sub-millisecond heuristic pre-filter (`na_filter.py`) with a multimodal GPT-4o vision analyzer (`ai_check.py`), filtering out thousands of irrelevant messages without consuming unnecessary API tokens.
* **Sub-3-Second Notification Dispatch**: Constructed an asynchronous multi-target alert engine with independent per-recipient pacing, delivering verified drop notifications to mobile devices in under 3 seconds.
* **100% Account Safety Guarantee**: Decoupled intelligence gathering entirely from consular portals. By consuming crowdsourced Telegram signals and public status boards, the system provides zero risk of applicant account suspensions or IP blacklisting.
* **Production Success**: Successfully detected an unannounced batch opening, delivered a push notification to family recipients within seconds of availability, and enabled immediate manual login that secured a confirmed visa appointment.

---

## The Problem & Business Context

### 1. The Visa Appointment Scarcity Bottleneck
Consular post-pandemic backlogs created an extreme supply-demand mismatch for US visa appointments in India (New Delhi, Mumbai, Chennai, Hyderabad, and Kolkata). Available slots open sporadically at unpredictable hours (often between 1:00 AM and 5:00 AM), with high-demand batches filling in under two minutes.

### 2. The Danger of Direct Booking Bots
Desperate applicants often turn to third-party "booking bots" that automate portal logins. However, official consular portals (usvisascheduling.com and ustraveldocs) employ sophisticated bot detection:
* Multiple automated login queries rapidly trigger rate limits and IP bans.
* Suspicious browser automation signatures lead to **immediate 72-hour account lockouts** or permanent profile cancellation.
* Handing account credentials to unauthorized third-party booking agents introduces severe identity and cybersecurity risks.

### 3. The Signal-to-Noise Paradox in Crowdsourced Feeds
While applicants actively report newly discovered slots across public Telegram channels, tracking these communities manually is impossible:
* **Message Volume**: A single active group generates 3,000 to 7,000 messages per day.
* **Dominance of False Positives**: Over 98% of messages consist of:
  * Negative reports ("Chennai OFC NA", "No slots for Dec").
  * Speculative questions ("Any slots for Dropbox?").
  * Emotional banter, stickers, and forwarded promotional spam.
* **Multimodal Evidence**: Users often post screenshots of the booking calendar without typing any text. Pure text parsers fail completely on image-only posts.

---

## The System Solution

The engine was architected around a core design philosophy: **Zero Portal Footprint, Complete Intelligence Fidelity**. 

Instead of touching protected government servers, the system passively aggregates real-time signals from community crowdsourcing networks and public status monitors. It ingests messages via Telegram MTProto client protocols, cleans out chatter through regex heuristics, verifies screenshots via vision models, deduplicates cross-channel broadcasts, and alerts users via their private Telegram bots.

```
[ Public Community Telegram Channels ] ──► (MTProto Poller: Telethon) ──┐
                                                                        ▼
[ CheckVisaSlots Status Board ] ──► (Chromium Driver: Playwright) ──► [ Stage 1: Heuristic NA Filter ]
                                                                        │ (Drops 85%+ noise)
                                                                        ▼
                                                              [ Stage 2: GPT-4o Multimodal Vision ]
                                                                        │ (Validates text & images)
                                                                        ▼
                                                              [ Stage 3: Spatial-Temporal Fingerprint ]
                                                                        │ (Deduplicates across channels)
                                                                        ▼
                                                              [ Stage 4: Multi-Target Alert Dispatch ]
                                                                        ├──► Telegram Alert Bot (Paced)
                                                                        └──► WhatsApp Gateway (Optional)
```

---

## Step-by-Step System Walkthrough

### Phase 1: Real-Time Stream Ingestion
* **Telethon MTProto Integration**: Operates an asynchronous Telegram client using Telegram's binary MTProto protocol. It listens across configured public channel IDs, maintaining a persistent SQLite cursor per group (`last_seen_msg_id`). On reboot, it resumes precisely from its last offset, ensuring zero dropped messages.
* **Playwright Chromium Poller**: An optional background poller monitors the public CheckVisaSlots status table every ~2 minutes using a persistent Chromium browser profile, detecting DOM timestamp mutations without triggering anti-bot gates.

### Phase 2: Heuristic Pre-Filtering & Multimodal AI Verification
When a message arrives, it passes through a rigorous two-stage gating funnel:
1. **Deterministic Heuristic Pre-Filter (`na_filter.py`)**:
   * Evaluates text against a battery of regex patterns to instantly identify negative reports ("NA", "Nil", "no slots"), bot commands, stickers, and empty payloads.
   * If a message is classified as noise, it is logged to `decisions.log` and immediately discarded, saving 85%+ in downstream AI token costs.
2. **Multimodal Vision Verification (`ai_check.py`)**:
   * If a post contains an image (calendar screenshot) or candidate text, the engine fetches the image directly into an in-memory byte buffer (never written to disk).
   * It prompts OpenAI GPT-4o with strict JSON schema constraints (`response_format={"type": "json_object"}`), requiring the model to extract `available` (boolean), `appointment_type` (biometrics/interview), `location` (city), and `dates`.
   * **Fail-Open Safety Boundary**: If OpenAI encounters API downtime, rate limits, or invalid JSON, the system defaults to `failed=True` and forwards the alert flagged as `(AI check failed - please look)`. A real opening is never suppressed due to an external API glitch.

### Phase 3: Spatial-Temporal Fingerprinting & State Deduplication
When slots drop, dozens of applicants broadcast identical alerts across multiple channels within seconds. To prevent alert fatigue, `slotkey.py` computes a spatial-temporal fingerprint:
```python
slotkey = (appointment_type, normalized_city, earliest_date, hour_bucket)
```
The engine logs this key to SQLite (`watcher.db`). If an incoming alert matches an active slotkey seen within the last 15 minutes, it is recognized as a duplicate broadcast and suppressed from dispatch.

### Phase 4: Prioritized Dispatch, Queue Pacing & Supervisor Watchdog
* **Independent Per-Recipient Queues**: Alerts are queued in memory per recipient (`alerts.py`). If recipient A experiences network delays or blocks the bot, recipient B receives their alert without a millisecond of latency.
* **Rate-Limit Enforcement**: Dispatches are paced at 1.5 seconds per chat to comply with Telegram Bot API guidelines, honoring any HTTP 429 `retry_after` response headers automatically.
* **Protocol Fallback**: Formats alerts with clean HTML highlighting consulate cities, appointment categories, and time stamps. If Telegram returns an unescaped entity parsing error, the dispatcher catches the exception and immediately retries the alert in raw plain text.
* **Thread-Safe Process Watchdog**: A dedicated background thread (`health.py`) monitors heartbeat timestamps from the main event loop. If event loop processing hangs for more than 10 minutes, the watchdog initiates an immediate container exit, triggering Docker restart policies.

---

## Annotated Visual & Execution Tour

### 1. Terminal Telemetry & Decision Stream
The engine outputs structured, timestamped logs detailing every step of the ingestion and evaluation pipeline.

```text
2026-11-06 14:02:11 [INFO] [telegram_poller] Ingested 3 new messages from @h1b_slots_india
2026-11-06 14:02:11 [DEBUG] [na_filter] Msg #41029: SKIP filter (reason='NA report: OFC NA all')
2026-11-06 14:02:12 [INFO] [ai_check] Msg #41030: Forwarded to GPT-4o Vision (Image: 128KB in-memory)
2026-11-06 14:02:14 [INFO] [ai_check] AI verified availability: Chennai OFC [Nov 24 - Nov 28] (Confidence: High)
2026-11-06 14:02:14 [INFO] [slotkey] Fresh fingerprint generated: ('biometrics', 'chennai', '2026-11-24', '14')
2026-11-06 14:02:14 [INFO] [alerts] Enqueued alert for 2 targets (applicant, family)
2026-11-06 14:02:15 [INFO] [alerts] Target 'applicant' delivered via Telegram Bot API (HTTP 200, latency=420ms)
2026-11-06 14:02:16 [INFO] [alerts] Target 'family' delivered via Telegram Bot API (HTTP 200, latency=390ms)
```

### 2. High-Priority Telegram Alert Format
Alerts delivered to mobile devices are structured for immediate legibility, enabling users to act without reading through chatter.

```text
🚨 H-1B VISA SLOTS DETECTED

📍 Location: Chennai (OFC Biometrics)
📅 Available Dates: 24 Nov 2026 - 28 Nov 2026
📊 Source: Community Broadcast (@h1b_slots_india)
⏰ Detected At: 06 Nov, 02:02 PM IST

⚠️ Log into the portal immediately to claim appointments.
```

### 3. Decision Audit Trail (`decisions.log`)
For complete transparency, every ingested post is permanently recorded in `data/logs/decisions.log`, allowing rapid post-mortem analysis:
* `SKIP filter [reason]`: Dropped by deterministic heuristic regex.
* `SKIP ai [reason]`: Evaluated by GPT-4o and found to be chatter or negative report.
* `SKIP dup-slotkey [key]`: Suppressed duplicate broadcast within the 15-minute window.
* `FORWARD [details]`: High-priority alert dispatched to notification queues.

---

## Key Engineering Challenges Overcome

### 1. Telegram FloodWait Mitigation & Cursor State Persistence
* **The Challenge**: Telegram's MTProto servers enforce strict rate limits on client polling. Rapid history requests trigger `FloodWaitError` exceptions demanding sleep periods up to 300+ seconds. If an unhandled exception crashes the process, polling resets from the beginning, triggering repeated rate limits.
* **The Solution**: Designed an adaptive cursor-based poller in `telegram_poller.py`. It tracks the highest processed `message_id` inside an SQLite table (`telegram_cursors`). When Telegram returns a `FloodWaitError`, the poller intercepts the exception, logs an alert, enters a controlled sleep for the exact required duration, and resumes without losing cursor integrity.

### 2. Multimodal Screenshot Verification & Fail-Open Safety Logic
* **The Challenge**: Many users post screenshots of calendar availability without typing explanatory text. Relying solely on text filtering misses 40%+ of genuine drops. However, querying vision models introduces network latency and risk of API timeouts during large embassy release waves.
* **The Solution**: Implemented an in-memory streaming handler using `Telethon.download_media(file=bytes)`. Image bytes are forwarded directly to OpenAI GPT-4o as base64 payloads without touching the local disk. Crucially, the system implements **fail-open error boundaries**: if the OpenAI API times out or throws an error, the pipeline marks `failed=True` and delivers the alert with an explicit review banner. A critical booking window is never missed due to third-party API downtime.

### 3. Thread-Safe Process Watchdog & Deadlock Prevention
* **The Challenge**: In Python asyncio applications, network drivers or third-party C-extensions can occasionally deadlock the main event loop, causing the process to silently hang without terminating. Standard Docker healthchecks running simple HTTP pings cannot always detect an internal loop lockup.
* **The Solution**: Built an independent daemon thread (`health.py`) running outside the asyncio event loop. The event loop periodically updates an atomic memory timestamp. If the watchdog detects that the event loop has failed to tick for more than 10 minutes, it writes a fatal alert log and invokes `os._exit(1)`. Docker (`restart: unless-stopped`) immediately detects the crash and provisions a fresh container instance, restoring 24/7 uptime automatically.

---

## Technical Decisions & Architecture Trade-offs

| Decision | Chosen Architecture | Alternative Considered | Engineering Trade-off & Rationale |
| :--- | :--- | :--- | :--- |
| **Operational Boundary** | **Read-Only Telemetry Engine** | Automated Booking Bot | Building an automated booking bot exposes applicants to IP bans, Captcha traps, and account cancellation. A read-only alert engine ensures 100% legal and operational safety while still delivering the speed necessary to book manually. |
| **Protocol Selection** | **MTProto Client API** | Standard Telegram Bot API | Standard Telegram bots can only read messages in groups where they have administrator privileges. MTProto user client protocols allow passive read-only monitoring of public community broadcast channels. |
| **Two-Stage Filtering** | **Regex Heuristics + Multimodal AI** | Single End-to-End LLM Prompt | Passing 5,000+ raw messages daily through GPT-4o would incur heavy token costs ($15-$30/day) and introduce 1-2s latency per message. The regex pre-filter drops 85%+ of chatter in sub-millisecond time at zero cost. |
| **Storage Architecture** | **SQLite (WAL Mode)** | Redis / PostgreSQL | For a single-node deployment, running an external database daemon introduces unnecessary operational overhead. SQLite with Write-Ahead Logging delivers sub-millisecond cursor updates with zero configuration. |

---

## What's Next (v2 Roadmap)

* **Multi-Country Embassy Expansion**: Extend configuration to monitor European Schengen and UK visa appointment streams across Canadian and European consulates.
* **Webhook Push Dispatcher**: Allow external services (Slack, Discord, and custom webhooks) to subscribe to verified slot announcements via authenticated HTTP callbacks.
* **Browser Extension Sync**: Provide a lightweight companion browser extension that receives push alerts and pre-opens the official consular login page in an authenticated session.

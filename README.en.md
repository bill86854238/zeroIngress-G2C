# zeroIngress-G2C

[繁體中文](README.md)

Automatically scans Gmail for travel, transport, and event emails, extracts itinerary details using AI, and writes events to Google Calendar.

Two versions are available:

| | v2 Google Apps Script | v1 Local Python |
|---|---|---|
| Runs | Automatically every hour in the cloud | Manually on your machine |
| LLM | Swappable (see below) | Ollama (local model) |
| Needs local machine | No | No (manual trigger) |
| Privacy | Email data sent to cloud LLM | Data stays on your machine |
| Setup difficulty | Easy (paste one script) | Requires Python + Ollama |

> **v1** is tagged as `v1.0-local-ollama`. Switch to that tag if privacy is a priority.

---

## v2 LLM options

Switch between providers in the `CONFIG` block of `gas/Code.gs`:

### Option A: Gemini (Google)

- **Free tier**: `gemini-2.5-flash`, 20 requests/day — not enough for a 45-email backfill
- **Paid**: Buy NT$1,000 credits (one-time, not a subscription), unlocks 1,000 RPM — lasts years at this usage level (~NT$0.3/run)
- Script Properties key: `GEMINI_API_KEY`

### Option B: Groq (current)

```js
GROQ_MODEL: "llama-3.3-70b-versatile",
```

- **Free tier limit**: 100,000 tokens/day (~50–80 emails) — fine for daily trickle, hits the wall on large backfills
- Script Properties key: `GROQ_API_KEY` — get one at [console.groq.com/keys](https://console.groq.com/keys)

> **Practical advice**: Groq free tier works well for day-to-day use (1–2 new emails per hour). For a one-time backfill of many old emails, consider buying Gemini credits temporarily.

---

## Features

- Gmail filtering by travel, accommodation, and event keywords
- Automatic outbound/return flight splitting
- Multi-day all-day events (hotel check-in → check-out)
- Duplicate detection (same flight number + date)
- Suspicious parse results (e.g. hallucinated flight info) are silently skipped
- Incremental sync: processed email IDs are tracked and skipped on the next run

---

## v2: Google Apps Script (recommended)

No local environment needed. Set it up once and it runs automatically every hour.

### Requirements

- A Google account with Gmail and Google Calendar
- A Gemini API key (free tier is sufficient): create one at [Google AI Studio](https://aistudio.google.com/apikey)

### Setup

1. Go to [Google Apps Script](https://script.google.com) and click **New project**
2. Paste the entire contents of `gas/Code.gs` into the editor (replace the default `myFunction`)
3. Open **Project Settings** → **Script Properties** → add:
   - Key: `GEMINI_API_KEY`  Value: your Gemini API key
4. In the function dropdown, select `setupTrigger` and click Run (⏵). Authorise Gmail + Calendar access when prompted
5. Done. `syncEmails` will now run automatically every hour

### Manual run

Select `syncEmails` in the function dropdown and click Run to trigger a sync immediately.

### Viewing logs

Go to **Executions** in the left sidebar of the GAS editor to see Logger output from each run.

### Resetting processed records

Run `resetProcessed()` to clear the processed-email log. The next run will re-scan all emails.

---

## v1: Local Python version

All data is processed locally — no third-party cloud LLM involved.

> Switch to the stable tag: `git checkout v1.0-local-ollama`

### Requirements

- Python 3.11+
- [Ollama](https://ollama.com) 0.30.4+, with a model that supports structured output (recommended: `qwen3.6:35b-a3b`)
- Google Cloud OAuth credentials with Gmail API + Google Calendar API enabled

### Installation

```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 \
            google-api-python-client pydantic python-dotenv \
            beautifulsoup4 python-dateutil requests
```

### Configuration

```bash
cp .env.example .env
```

Fill in `.env`:

```
MY_EMAIL=your_gmail_address@gmail.com
```

### Usage

```bash
# HTML preview — opens in browser, no write
python sync_now.py --preview

# Write events to calendar
python sync_now.py --live

# Reset processed log and reprocess all emails
python sync_now.py --reset
```

---

## Calendar

The first write creates a dedicated calendar named **G2C AI Sync**. To start fresh, delete that calendar in Google Calendar — your other calendars are not affected.

---

## Customising keywords

**v2**: Edit the `KEYWORDS` array in `gas/Code.gs`.

**v1**: Edit the `KEYWORDS` list in `sync_now.py`.

Defaults cover travel (flights, hotels, car rentals, airport transfers), event tickets (concerts, exhibitions), medical appointments, and restaurant reservations.

---

## Filtering logic

**Keyword matching** — both subject and body are checked.

**Automatically excluded:**
- Cancellation notices
- Boarding reminders (no complete itinerary data)
- Emails starting with Re: / Fw:
- Bank statements and other non-itinerary mail
- Emails you sent yourself

---

## Security notes

- **v2**: The Gemini API key is stored in GAS Script Properties — it never appears in the source code
- **v1**: `token.json` and `client_secret_*.json` contain your Google account credentials — do not commit them to a public repo

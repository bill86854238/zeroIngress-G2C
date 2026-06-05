# zeroIngress-G2C

Automatically parse Gmail messages and sync events to Google Calendar using a local Ollama LLM. All data is processed on your machine — no third-party cloud services involved.

Designed for travel itineraries (flights, hotels, car rentals, airport transfers) out of the box. Extend the keyword list to cover concerts, medical appointments, restaurant reservations, or any email that contains time-based information.

---

## Features

- Gmail filtering by configurable keyword list
- Automatic outbound/return flight splitting
- Multi-day all-day events (hotel check-in → check-out)
- Duplicate detection (same flight number + date)
- Suspicious parse results flagged in orange — not written to calendar
- Incremental sync: processed email IDs are logged and skipped on the next run
- Optional Brave Search API enrichment for hotel check-in times and addresses

---

## Requirements

### Local environment

- Python 3.11+
- [Ollama](https://ollama.com) 0.30.4+, with a model that supports structured output pulled (recommended: `qwen3:30b-a3b` or `gemma3:12b`)

### Google Cloud

1. Create or select a project in [Google Cloud Console](https://console.cloud.google.com/)
2. Enable the **Gmail API** and **Google Calendar API**
3. Create an OAuth 2.0 credential (application type: **Desktop app**)
4. Download the credential file — it will be named `client_secret_*.json` — and place it in the project directory

---

## Installation

```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 \
            google-api-python-client pydantic python-dotenv \
            beautifulsoup4 python-dateutil requests
```

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```
MY_EMAIL=your_gmail@gmail.com
BRAVE_API_KEY=your_brave_search_api_key
```

- `MY_EMAIL` — your Gmail address, used to filter out emails you sent yourself
- `BRAVE_API_KEY` — optional. Get a free key at [Brave Search API](https://brave.com/search/api/) (2,000 requests/month on the free plan). Brave enrichment is disabled when this is blank or when `--enrich` is not passed

---

## Usage

```bash
# Dry run — parse emails but do not write to calendar
python sync_now.py

# Output an HTML preview to preview.html and open it in the browser
python sync_now.py --preview

# Incremental write — skip already-processed emails
python sync_now.py --live

# Same as above, plus Brave Search enrichment for hotel info
python sync_now.py --live --enrich

# Reset processed-email log and reprocess everything
python sync_now.py --live --reset
```

The **first run** will open a browser for Google OAuth consent. After authorising, `token.json` is saved automatically and subsequent runs will not require re-authorisation.

---

## Calendar

The first write creates a dedicated calendar named **G2C AI Sync**. To start fresh, delete the calendar in Google Calendar — your other calendars are not affected.

---

## Customising keywords

Edit the `KEYWORDS` list in `sync_now.py`:

```python
KEYWORDS = [
    "flight", "hotel", "booking",       # default travel keywords
    "concert", "appointment", "reservation",  # add your own
]
```

---

## Filtering logic

**Keyword matching** — subject and body are both checked.

**Automatically excluded:**
- Cancellation notices
- Boarding reminders (no complete itinerary data)
- Emails starting with Re: / Fw:
- Bank statements and other non-itinerary mail
- Emails you sent yourself

---

## Security notes

- `token.json` and `client_secret_*.json` contain your Google account credentials — **do not commit them to a public repo**
- `.env` is gitignored — use `.env.example` as a reference and never commit your actual `.env`
- `processed.log` tracks processed email IDs; deleting it causes all emails to be reprocessed on the next run
- LLM parsing is not perfect — use `--preview` to review results before running `--live`

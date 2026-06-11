"""
sync_now.py — Gmail to Google Calendar sync (dry-run mode by default)
Usage: python sync_now.py [--live]
"""

import io
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import requests
import caldav
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from icalendar import Calendar as iCalendar, Event as iEvent
from pydantic import BaseModel, ValidationError

load_dotenv()

# ── Constants ─────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]

CREDENTIALS_FILE = next(
    (f for f in os.listdir(".") if f.startswith("client_secret_") and f.endswith(".json")),
    None,
)
TOKEN_FILE = "token.json"
PROCESSED_LOG = "processed.log"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen3.6:35b-a3b"
CALENDAR_NAME = "G2C AI Sync"
TAIPEI_TZ = timezone(timedelta(hours=8))
BASE_YEAR = 2026
MAX_BODY_CHARS = 3000
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"

CALDAV_URL      = os.getenv("CALDAV_URL", "")
CALDAV_USERNAME = os.getenv("CALDAV_USERNAME", "")
CALDAV_PASSWORD = os.getenv("CALDAV_PASSWORD", "")
CALDAV_CALENDAR = os.getenv("CALDAV_CALENDAR", "My Calendar")

KEYWORDS = [
    # 交通
    "高鐵", "台鐵", "train", "hsr",
    "機票", "航班", "flight", "airline", "boarding",
    "租車", "car rental",
    # 住宿
    "飯店", "hotel", "旅館", "hostel", "check-in", "check in",
    "訂房", "booking", "reservation",
    # 行程
    "itinerary", "行程",
    # 活動票券
    "演唱會", "concert", "展覽", "exhibition", "票券", "ticket",
    "入場券", "門票",
    # 醫療預約
    "看診", "門診", "掛號", "預約", "appointment",
    # 餐廳訂位
    "訂位", "餐廳預約",
]

BLOCKED_SENDERS = [
    "cathaybk.com.tw",
    "skyinfo.jal.com",      # JAL check-in reminder, no itinerary data
    "skyinfo.ana.co.jp",    # ANA check-in reminder
    "mh1.evaair.com",       # EVA Air seat purchase confirmation, not itinerary
]

CHECKIN_NOTICE_KEYWORDS = [
    "online check in result",
    "check-in result",
    "ご搭乗のご案内",
    "boarding reminder",
    "check in reminder",
]

# Subject keywords that indicate duplicate/redundant emails when paired with specific senders
DUPLICATE_SUBJECT_SENDER = [
    ("evaair.com", "electronic ticket"),   # EVA Air e-ticket receipt duplicates purchase confirmation
    ("evaair.com", "emd receipt"),
    ("amadeus.com", "emd receipt"),
]

MY_EMAIL = os.getenv("MY_EMAIL", "")

CANCEL_KEYWORDS = ["已取消", "cancelled", "canceled", "キャンセル", "取消", "cancellation"]

# Subject prefixes that indicate hotel/vendor reply threads — direct notifications are more reliable
SKIP_SUBJECT_PREFIXES = [
    "re:",
    "re：",
    "fw:",
    "fwd:",
]

# Maps signal keywords (in subject/sender) to expected title keywords
# If signal found but none of the expected keywords appear in title → suspicious
AIRLINE_SIGNALS = [
    (["JAL", "Japan Airlines", "jal.com", "skyinfo.jal"], ["日本航空", "JAL", "JL"]),
    (["EVA AIR", "evaair", "長榮"], ["長榮", "EVA", "BR"]),
    (["星宇", "STARLUX", "starlux"], ["星宇", "STARLUX", "JX"]),
    (["ANA", "ana.co.jp"], ["全日空", "ANA", "NH"]),
    (["CATHAY", "cathay", "國泰航空"], ["國泰", "CATHAY", "CX"]),
]

# ── Pydantic schema ────────────────────────────────────────────────────────────

class CalendarEvent(BaseModel):
    title: str
    start: str        # ISO 8601 with +08:00
    end: str          # ISO 8601 with +08:00
    location: str = ""
    description: str = ""
    is_all_day: bool = False  # True when no specific time is known


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_credentials() -> Credentials:
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception:
            pass  # fall through to full re-auth

    if not CREDENTIALS_FILE:
        print("[ERROR] credentials.json not found in current directory.")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    _save_token(creds)
    return creds


def _load_processed() -> set:
    if not os.path.exists(PROCESSED_LOG):
        return set()
    with open(PROCESSED_LOG, "r") as f:
        return set(line.strip() for line in f if line.strip())


def _save_processed(msg_id: str):
    with open(PROCESSED_LOG, "a") as f:
        f.write(msg_id + "\n")


def _save_token(creds: Credentials):
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())


# ── Gmail ─────────────────────────────────────────────────────────────────────

def fetch_candidate_emails(service) -> list[dict]:
    """Return all emails matching travel keywords from the last 90 days."""
    keyword_query = " OR ".join(f'"{kw}"' for kw in KEYWORDS)
    query = f"({keyword_query}) newer_than:90d"

    messages = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    if not messages:
        return []

    print(f"[Fetching] Downloading {len(messages)} matched message(s)...")
    emails = []
    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="full"
        ).execute()
        parsed = _parse_message(msg)

        # Skip emails sent by myself
        if MY_EMAIL in parsed["sender"]:
            continue

        # Skip blocked senders (e.g. bank notifications)
        if any(domain in parsed["sender"] for domain in BLOCKED_SENDERS):
            continue

        # Skip cancellation emails
        subject_lower = parsed["subject"].lower()
        body_lower = parsed["body"][:500].lower()
        if any(kw.lower() in subject_lower or kw.lower() in body_lower for kw in CANCEL_KEYWORDS):
            continue

        # Skip check-in reminder emails (no itinerary data, booking confirmation has the details)
        if any(kw.lower() in subject_lower for kw in CHECKIN_NOTICE_KEYWORDS):
            continue

        # Skip reply/forward thread emails — direct vendor notifications are more reliable
        if any(subject_lower.startswith(prefix) for prefix in SKIP_SUBJECT_PREFIXES):
            continue

        # Skip sender+subject combinations that are known duplicates
        sender_lower = parsed["sender"].lower()
        if any(domain in sender_lower and kw in subject_lower
               for domain, kw in DUPLICATE_SUBJECT_SENDER):
            continue

        emails.append(parsed)

    return emails


def _parse_message(msg: dict) -> dict:
    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
    subject = headers.get("Subject", "(no subject)")
    sender = headers.get("From", "")
    date_str = headers.get("Date", "")
    body = _extract_body(msg["payload"])

    return {
        "id": msg["id"],
        "subject": subject,
        "sender": sender,
        "date": date_str,
        "body": _clean_body(body),
    }


def _extract_body(payload: dict) -> str:
    import base64

    def _decode(data: str) -> str:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")

    mime = payload.get("mimeType", "")

    if "parts" in payload:
        # prefer text/plain, fallback to text/html
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return _decode(data)
        for part in payload["parts"]:
            if part.get("mimeType") == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    return _decode(data)
        # recurse into nested multipart
        for part in payload["parts"]:
            result = _extract_body(part)
            if result:
                return result
    else:
        data = payload.get("body", {}).get("data", "")
        if data:
            return _decode(data)

    return ""


def _clean_body(raw: str) -> str:
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # Cut off at payment/footer sections
    cutoff_patterns = [
        r"付款明細", r"價格摘要", r"Payment Summary", r"Price Summary",
        r"稅金\n", r"Tax\n", r"票價.*TWD", r"合計.*TWD",
        r"(?i)unsubscribe", r"(?i)copyright.*all rights reserved",
        r"本メールの記事を許可無く",
    ]
    for pattern in cutoff_patterns:
        m = re.search(pattern, text)
        if m and m.start() > 300:
            text = text[:m.start()].strip()
            break

    return text[:MAX_BODY_CHARS]


# ── Ollama LLM ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are a travel itinerary extraction assistant.
Today's base year is {BASE_YEAR}. All relative dates (e.g. "this Friday", "next Monday", "06/22")
must be interpreted relative to {BASE_YEAR}.

Extract calendar events from the email. Output ONLY valid JSON with these exact keys:

- title: descriptive event name. Follow these rules:
  * Flights: airline name + flight number + route. e.g. "長榮航空 BR801 — 桃園T2 → 札幌新千歲"
  * Car rentals: vendor + pickup location. e.g. "Times 租車 — 旭川站前"
  * Hotels: hotel name + 入住. e.g. "Dormy Inn PREMIUM Kushiro 入住"
  * Transfers: service + route. e.g. "Klook 送機 — 樹林→桃園T1"
  * Always use Traditional Chinese where possible.

- start / end: ISO 8601 datetime using the LOCAL timezone of the event location.
  * Taiwan: +08:00 | Japan (all cities/airports): +09:00 | Korea: +09:00
  * Thailand / Vietnam / Cambodia / Laos: +07:00 | Singapore / Malaysia / Philippines: +08:00
  * Indonesia (Bali/Lombok): +08:00 | Indonesia (Java/Sumatra): +07:00
  * For FLIGHTS: start uses departure airport local timezone; end uses arrival airport local timezone.
    Example Taipei(TPE/TSA)→Sapporo(CTS): start "{BASE_YEAR}-06-22T10:30:00+08:00", end "{BASE_YEAR}-06-22T14:40:00+09:00"
  * For HOTELS: start = check-in date + check-in time from email (in hotel's local timezone).
                If check-in time not stated in email, use T15:00:00 WITH the hotel's local timezone offset (e.g., T15:00:00+09:00 for Japan, T15:00:00+08:00 for Taiwan).
                end = check-out date + check-out time from email (in hotel's local timezone).
                If check-out time not stated in email, use T12:00:00 WITH the hotel's local timezone offset (e.g., T12:00:00+09:00 for Japan, T12:00:00+08:00 for Taiwan).
  * For other events with no known time: use T00:00:00 WITH the event's local timezone offset (e.g., T00:00:00+09:00 for Japan).
  * CRITICAL: Every datetime string MUST end with a timezone offset (e.g., +09:00). NEVER output a bare datetime like T15:00:00 without an offset.

- location:
  * Flights: "出發航廈 → 目的地機場" — must match the actual direction of this flight leg.
  * Hotels: hotel name only (do NOT add street address — name gives better Google Maps results).
  * Car rentals: pickup location name.
  * Transfers: pickup address → drop-off address.

- description: Traditional Chinese only. Include ONLY information explicitly present in the email; omit any field not found.
  * Flights: 航班號、座位、訂位代號
  * Hotels — only include lines where data is actually in the email:
    入住：[date + time]（if using default T15:00:00, append「時間預設，請確認」）
    退房：[date + time]（if using default T12:00:00, append「時間預設，請確認」）
    訂房號碼：[if in email]
    房型：[if in email]
    餐飲：[if in email, e.g. 含早餐 / 一泊二食 / 含早晚餐]
    [any other relevant details present in the email]
  * Car rentals: 車型、取還車地點與時間、訂單號
  * Transfers: 接送地址、車型、訂單號

IMPORTANT rules:
- EMD receipts, ancillary fee receipts, meal pre-orders, Wi-Fi purchases, check-in reminders → {{"events": []}}
- Cancellation notices → {{"events": []}}
- Flights: extract airline + flight number ONLY from the itinerary table (e.g. "Flight Number:", "運航便:", "航班號:"). Never from signatures or unrelated text.
- Hotels: extract ONLY check-in dates explicitly stated. Do NOT invent stays not in the email.
- Hotels are NEVER all-day events — always use timed start/end (default T15:00:00 / T12:00:00 if times unknown).
- Each event must be a distinct real travel segment. No duplicates or fabricated events.

Output format: always return a JSON object with an "events" array.
- Single event: {{"events": [{{...}}]}}
- Round-trip flights: {{"events": [{{outbound leg...}}, {{return leg...}}]}} — each leg has its own correct direction in location field
- No travel event: {{"events": []}}
"""

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "title":       {"type": "string"},
        "start":       {"type": "string"},
        "end":         {"type": "string"},
        "location":    {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["title", "start", "end", "location", "description"],
}

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": EVENT_SCHEMA,
            "minItems": 0,
            "maxItems": 4,
        }
    },
    "required": ["events"],
}


def parse_email_with_llm(email: dict) -> list[CalendarEvent]:
    user_content = (
        f"Subject: {email['subject']}\n"
        f"From: {email['sender']}\n"
        f"Date: {email['date']}\n\n"
        f"{email['body']}"
    )

    payload = {
        "model": OLLAMA_MODEL,
        "system": SYSTEM_PROMPT,
        "prompt": user_content,
        "stream": False,
        "format": JSON_SCHEMA,
        "options": {
            "temperature": 0,
            "top_p": 1,
            "repeat_penalty": 1.0,
        },
        "think": False,
    }

    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
        resp.raise_for_status()
        raw_json = resp.json()["response"]
        wrapper = json.loads(raw_json)
        items = wrapper.get("events", [])
    except Exception as e:
        raise RuntimeError(f"Ollama request/parse failed: {e}")

    results = []
    for data in items:
        if not data.get("title") or not data.get("start"):
            continue
        try:
            data["start"] = _normalise_dt(data["start"])
            data["end"] = _normalise_dt(data["end"]) if data.get("end") else _add_one_hour(data["start"])
            data["is_all_day"] = _is_midnight(data["start"]) and _is_midnight(data["end"])
            # 飯店事件不允許全天：若 LLM 違反指令輸出 T00:00:00，強制改回預設時間
            if data["is_all_day"]:
                hotel_signals = ["入住", "hotel", "飯店", "旅館", "hostel", "inn"]
                if any(kw in (data.get("title") or "").lower() for kw in hotel_signals):
                    data["is_all_day"] = False
                    if "T00:00:00" in data["start"]:
                        data["start"] = data["start"].replace("T00:00:00", "T15:00:00", 1)
                    if "T00:00:00" in data["end"]:
                        data["end"] = data["end"].replace("T00:00:00", "T12:00:00", 1)
            if data["is_all_day"] and data["start"][:10] == data["end"][:10]:
                d = dateutil_parser.parse(data["end"])
                data["end"] = (d + timedelta(days=1)).isoformat()
            if not data["is_all_day"]:
                s = dateutil_parser.parse(data["start"])
                e = dateutil_parser.parse(data["end"])
                if e <= s:
                    data["end"] = (s + timedelta(hours=1)).isoformat()
        except Exception as ex:
            raise RuntimeError(f"Datetime normalisation failed: {ex}")
        try:
            results.append(CalendarEvent(**data))
        except ValidationError as ex:
            raise RuntimeError(f"Schema validation failed: {ex}")

    return results


def _check_suspicious(email: dict, event: CalendarEvent) -> str | None:
    """Return a warning message if airline in subject/sender doesn't match title, else None."""
    haystack = (email["subject"] + " " + email["sender"]).lower()
    for signals, expected_keywords in AIRLINE_SIGNALS:
        if any(s.lower() in haystack for s in signals):
            if not any(k.lower() in event.title.lower() for k in expected_keywords):
                matched_signal = next(s for s in signals if s.lower() in haystack)
                return f"Subject/sender contains '{matched_signal}' but title is '{event.title}' — possible hallucination"
    return None


def _normalise_dt(dt_str: str) -> str:
    dt = dateutil_parser.parse(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI_TZ)
    return dt.isoformat()


def _add_one_hour(dt_str: str) -> str:
    dt = dateutil_parser.parse(dt_str)
    return (dt + timedelta(hours=1)).isoformat()


def _is_midnight(dt_str: str) -> bool:
    dt = dateutil_parser.parse(dt_str)
    return dt.hour == 0 and dt.minute == 0 and dt.second == 0


def _is_hotel_event(event: "CalendarEvent") -> bool:
    hotel_keywords = ["入住", "hotel", "inn", "hostel", "villa", "resort", "飯店", "旅館", "ホテル", "dormy", "route inn"]
    return any(k.lower() in event.title.lower() for k in hotel_keywords)


def brave_enrich(event: "CalendarEvent") -> str:
    """Query Brave Search for hotel check-in time and address, return enrichment string."""
    if not BRAVE_API_KEY or not _is_hotel_event(event):
        return ""

    query = f"{event.title} check-in time address phone"
    if event.location:
        query = f"{event.title} {event.location} check-in time address"

    try:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": BRAVE_API_KEY,
        }
        resp = requests.get(BRAVE_SEARCH_URL, headers=headers,
                            params={"q": query, "count": 3}, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("web", {}).get("results", [])
        if not results:
            return ""

        # Pass top snippets to LLM to extract structured info
        snippets = "\n".join(
            f"- {r.get('title', '')}: {r.get('description', '')[:200]}"
            for r in results
        )

        llm_payload = {
            "model": OLLAMA_MODEL,
            "system": "You are a hotel information extractor. From the search snippets, extract check-in time, check-out time, address, and phone number if available. Reply in Traditional Chinese, 2-3 lines max. If info not found, reply with empty string.",
            "prompt": f"Hotel: {event.title}\n\nSearch results:\n{snippets}",
            "stream": False,
            "think": False,
            "options": {"temperature": 0},
        }
        llm_resp = requests.post(OLLAMA_URL, json=llm_payload, timeout=60)
        llm_resp.raise_for_status()
        enrichment = llm_resp.json().get("response", "").strip()
        return enrichment

    except Exception as e:
        print(f"  [Brave] enrichment failed: {e}")
        return ""


def _flight_number(title: str) -> str | None:
    """Extract flight number like BR166, JL98, JX838 from title."""
    m = re.search(r'\b([A-Z]{2})\s*(\d{2,4})\b', title)
    return f"{m.group(1)}{m.group(2)}" if m else None


def _dedup_events(events_data: list[dict]) -> list[dict]:
    """Remove duplicate events based on start date + flight number (for flights) or start+end+title prefix (for others)."""
    seen = set()
    result = []
    for item in events_data:
        event = item.get("event")
        if event:
            fn = _flight_number(event.title)
            if fn:
                # For flights: deduplicate by flight number + departure date
                key = (fn, event.start[:10])
            else:
                # For hotels/others: deduplicate by start+end dates + first 8 chars of title
                key = (event.start[:10], event.end[:10], event.title[:8].strip())
            if key in seen:
                item = {**item, "status": "skip", "reason": f"Duplicate of earlier entry", "event": event}
            else:
                seen.add(key)
        result.append(item)
    return result


# ── Calendar ──────────────────────────────────────────────────────────────────

OFFSET_TO_TIMEZONE = {
    "+00:00": "UTC",
    "+01:00": "Europe/Paris",
    "+02:00": "Europe/Helsinki",
    "+03:00": "Asia/Riyadh",
    "+04:00": "Asia/Dubai",
    "+05:00": "Asia/Karachi",
    "+05:30": "Asia/Kolkata",
    "+05:45": "Asia/Kathmandu",
    "+06:00": "Asia/Dhaka",
    "+06:30": "Asia/Rangoon",
    "+07:00": "Asia/Bangkok",
    "+08:00": "Asia/Taipei",
    "+09:00": "Asia/Tokyo",
    "+09:30": "Australia/Darwin",
    "+10:00": "Australia/Sydney",
    "+11:00": "Pacific/Noumea",
    "+12:00": "Pacific/Auckland",
    "-05:00": "America/New_York",
    "-06:00": "America/Chicago",
    "-07:00": "America/Denver",
    "-08:00": "America/Los_Angeles",
}


def _timezone_from_offset(iso_str: str) -> str:
    m = re.search(r'([+-]\d{2}:\d{2})$', iso_str)
    return OFFSET_TO_TIMEZONE.get(m.group(1), "Asia/Taipei") if m else "Asia/Taipei"


def get_or_create_calendar(service) -> str:
    calendars = service.calendarList().list().execute().get("items", [])
    for cal in calendars:
        if cal.get("summary") == CALENDAR_NAME:
            return cal["id"]

    new_cal = service.calendars().insert(body={"summary": CALENDAR_NAME}).execute()
    print(f"[Calendar] Created new calendar: {CALENDAR_NAME}")
    return new_cal["id"]


def get_caldav_calendar():
    client = caldav.DAVClient(
        url=CALDAV_URL,
        username=CALDAV_USERNAME,
        password=CALDAV_PASSWORD,
    )
    principal = client.principal()
    for cal in principal.calendars():
        if cal.get_display_name() == CALDAV_CALENDAR:
            return cal
    raise RuntimeError(f"CalDAV calendar '{CALDAV_CALENDAR}' not found on {CALDAV_URL}")


def insert_event_caldav(cal: "caldav.Calendar", event: CalendarEvent):
    import uuid
    ical = iCalendar()
    ical.add("prodid", "-//G2C AI Sync//EN")
    ical.add("version", "2.0")

    ievent = iEvent()
    ievent.add("uid", str(uuid.uuid4()))
    ievent.add("summary", event.title)
    ievent.add("location", event.location)
    ievent.add("description", event.description)

    if event.is_all_day:
        from datetime import date as date_type
        start_date = dateutil_parser.parse(event.start).date()
        end_date   = dateutil_parser.parse(event.end).date()
        ievent.add("dtstart", start_date)
        ievent.add("dtend",   end_date)
    else:
        ievent.add("dtstart", dateutil_parser.parse(event.start))
        ievent.add("dtend",   dateutil_parser.parse(event.end))

    ievent.add("dtstamp", datetime.now(timezone.utc))
    ical.add_component(ievent)
    cal.save_event(ical.to_ical())


def insert_event(service, calendar_id: str, event: CalendarEvent):
    if event.is_all_day:
        start_val = {"date": event.start[:10]}
        # Google Calendar all-day end is exclusive, so add one extra day
        end_dt = dateutil_parser.parse(event.end) + timedelta(days=1)
        end_val = {"date": end_dt.strftime("%Y-%m-%d")}
    else:
        start_val = {"dateTime": event.start, "timeZone": _timezone_from_offset(event.start)}
        end_val   = {"dateTime": event.end,   "timeZone": _timezone_from_offset(event.end)}

    body = {
        "summary": event.title,
        "location": event.location,
        "description": event.description,
        "start": start_val,
        "end":   end_val,
    }
    service.events().insert(calendarId=calendar_id, body=body).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def generate_preview_html(events_data: list[dict], output_path: str = "preview.html"):
    rows = ""
    for idx, item in enumerate(events_data, 1):
        status = item["status"]
        event = item.get("event")
        subject = item["subject"]
        color = {"ok": "#d4edda", "skip": "#fff3cd", "fail": "#f8d7da", "suspicious": "#ffe5b4"}.get(status, "#fff")
        if event:
            time_tag = "🗓 全天" if event.is_all_day else event.start[11:16]
            warning_html = f'<br><span style="color:#c0392b;font-size:12px">⚠ {item.get("reason","")}</span>' if status == "suspicious" else ""
            rows += f"""
            <tr style="background:{color}">
                <td style="text-align:center">{idx}</td>
                <td>{subject}</td>
                <td>{event.title}{warning_html}</td>
                <td>{event.start[:10]} {time_tag}</td>
                <td>{event.end[:10]}</td>
                <td>{event.location}</td>
                <td>{event.description[:100]}</td>
                <td><b>{status.upper()}</b></td>
            </tr>"""
        else:
            rows += f"""
            <tr style="background:{color}">
                <td style="text-align:center">{idx}</td>
                <td>{subject}</td>
                <td colspan="5">{item.get('reason', '')}</td>
                <td><b>{status.upper()}</b></td>
            </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<title>G2C Sync Preview</title>
<style>
  body {{ font-family: sans-serif; padding: 24px; }}
  h1 {{ color: #333; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  th {{ background: #4285f4; color: white; padding: 8px 12px; text-align: left; }}
  td {{ border: 1px solid #ddd; padding: 8px 12px; vertical-align: top; }}
  tr:hover {{ filter: brightness(0.97); }}
</style>
</head>
<body>
<h1>G2C AI Sync — Preview</h1>
<p>共 {len(events_data)} 封郵件，確認無誤後執行 <code>python sync_now.py --live</code> 寫入行事曆。</p>
<table>
  <thead>
    <tr>
      <th>#</th><th>郵件主旨</th><th>標題</th><th>開始</th><th>結束</th><th>地點</th><th>描述</th><th>狀態</th>
    </tr>
  </thead>
  <tbody>{rows}
  </tbody>
</table>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path


def main():
    preview = "--preview" in sys.argv
    live    = not preview
    enrich  = "--enrich" in sys.argv
    reset   = "--reset" in sys.argv  # clear processed.log and reprocess all
    target  = "caldav" if "--target=caldav" in sys.argv else os.getenv("SYNC_TARGET", "google")

    if live:
        print("=" * 60)
        if target == "caldav":
            print("  LIVE MODE — events will be written to Synology CalDAV")
        else:
            print("  LIVE MODE — events will be written to Google Calendar")
        if enrich:
            print("  + Brave enrichment ON")
        print("=" * 60)
    elif preview:
        print("[Preview] Will generate preview.html after processing...")
    else:
        print("=" * 60)
        print("  預設直接寫入行事曆")
        print("  --preview          輸出 HTML 預覽（不寫入）")
        print("  --target=caldav    覆蓋寫入目標為 Synology CalDAV")
        print("  --enrich           用 Brave Search 補充飯店資訊")
        print("  --reset            清除處理記錄，重新處理全部信件")
        print("=" * 60)

    if reset and os.path.exists(PROCESSED_LOG):
        os.remove(PROCESSED_LOG)
        print("[Reset] Cleared processed.log")

    processed_ids = _load_processed()

    creds = get_credentials()
    gmail_svc = build("gmail", "v1", credentials=creds)
    cal_svc   = build("calendar", "v3", credentials=creds)

    print("\n[Fetching] Scanning Gmail for travel-related emails...")
    emails = fetch_candidate_emails(gmail_svc)

    # Skip already-processed emails (unless reset)
    if processed_ids:
        before = len(emails)
        emails = [e for e in emails if e["id"] not in processed_ids]
        skipped = before - len(emails)
        if skipped:
            print(f"[Skip] {skipped} already-processed email(s)")

    if not emails:
        print("[Done] No new emails to process.")
        return

    print(f"[Found] {len(emails)} new candidate email(s)\n")

    calendar_id  = None
    caldav_cal   = None
    if live:
        if target == "caldav":
            caldav_cal = get_caldav_calendar()
            print(f"[Calendar] Using CalDAV: {CALDAV_CALENDAR} @ {CALDAV_URL}")
        else:
            calendar_id = get_or_create_calendar(cal_svc)

    results = {"success": 0, "skipped": 0, "failed": 0}
    failed_subjects = []
    events_data = []

    for i, email in enumerate(emails, 1):
        print(f"[{i}/{len(emails)}] Processing: {email['subject'][:60]}")

        try:
            parsed_events = parse_email_with_llm(email)
        except Exception as e:
            print(f"  [WARN] LLM parse error — {e}")
            results["failed"] += 1
            failed_subjects.append(email["subject"][:60])
            events_data.append({"status": "fail", "subject": email["subject"], "msg_id": email["id"], "reason": str(e)})
            continue

        if not parsed_events:
            print("  [SKIP] No travel event detected")
            results["skipped"] += 1
            events_data.append({"status": "skip", "subject": email["subject"], "msg_id": email["id"], "reason": "No travel event detected"})
            if live:
                _save_processed(email["id"])
            continue

        for event in parsed_events:
            # Brave enrichment for hotel events (only when --enrich flag is set)
            if enrich and _is_hotel_event(event) and BRAVE_API_KEY:
                enrichment = brave_enrich(event)
                if enrichment:
                    event = event.model_copy(update={
                        "description": (event.description + "\n\n" + enrichment).strip()
                    })
                    print(f"  [Brave] enriched: {event.title}")

            tag = "全天" if event.is_all_day else event.start[11:16]
            warning = _check_suspicious(email, event)
            status = "suspicious" if warning else "ok"
            events_data.append({"status": status, "subject": email["subject"], "msg_id": email["id"], "event": event, "reason": warning or ""})
            flag = " [!]" if warning else ""
            print(f"  [DRY-RUN] {event.title} | {event.start[:10]} {tag}{flag}")

    # Dedup before preview/insert
    events_data = _dedup_events(events_data)

    # Recount after dedup
    results = {"success": 0, "skipped": 0, "failed": 0, "suspicious": 0}
    failed_subjects = []
    for item in events_data:
        if item["status"] == "ok":
            results["success"] += 1
        elif item["status"] == "suspicious":
            results["suspicious"] += 1
        elif item["status"] == "skip":
            results["skipped"] += 1
        else:
            results["failed"] += 1
            failed_subjects.append(item["subject"][:60])

    if live:
        print()
        for item in events_data:
            if item["status"] == "suspicious":
                print(f"  [SKIP-SUSPICIOUS] {item['event'].title} — {item['reason']}")
                _save_processed(item["msg_id"])
                continue
            if item["status"] == "skip" and item.get("msg_id"):
                _save_processed(item["msg_id"])
                continue
            if item["status"] != "ok":
                continue
            event = item["event"]
            try:
                if target == "caldav":
                    insert_event_caldav(caldav_cal, event)
                else:
                    insert_event(cal_svc, calendar_id, event)
                print(f"  [OK] Inserted: {event.title}")
                _save_processed(item["msg_id"])
            except Exception as e:
                print(f"  [ERROR] Calendar insert failed — {e}")
                results["success"] -= 1
                results["failed"] += 1
                failed_subjects.append(item["subject"][:60])
                item["status"] = "fail"
                item["reason"] = str(e)

    if preview:
        path = generate_preview_html(events_data)
        print(f"\n[Preview] HTML saved to: {path}")
        import webbrowser
        webbrowser.open(f"file://{os.path.abspath(path)}")

    # Summary table
    print()
    print("=" * 60)
    print(f"  {'SUMMARY':^56}")
    print("=" * 60)
    print(f"  Total processed   : {len(emails)}")
    print(f"  Synced/preview    : {results['success']}")
    print(f"  Suspicious (check): {results['suspicious']}")
    print(f"  Skipped (no event): {results['skipped']}")
    print(f"  Failed            : {results['failed']}")
    if failed_subjects:
        print()
        print("  Failed items:")
        for s in failed_subjects:
            print(f"    - {s}")
    print("=" * 60)


if __name__ == "__main__":
    main()

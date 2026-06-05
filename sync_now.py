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

Extract ONE primary calendar event from the email. Output ONLY valid JSON with these exact keys:

- title: descriptive event name. Follow these rules:
  * For flights: include airline name, flight number, and route. Format: "長榮航空 BR801 — 桃園T2 → 札幌新千歲"
  * For car rentals: include vendor and pickup location. Format: "Times 租車 — 旭川站前"
  * For hotels: include hotel name. Format: "Dormy Inn PREMIUM Kushiro 入住"
  * For transfers/shuttles: include service and route. Format: "Klook 送機 — 樹林→桃園T1"
  * Always use Traditional Chinese where possible.

- start: ISO 8601 datetime with timezone +08:00 (e.g. "{BASE_YEAR}-06-22T10:30:00+08:00").
  * For flights: use departure time.
  * If only a date is known with no time, use T00:00:00+08:00.

- end: ISO 8601 datetime with timezone +08:00.
  * For flights: use arrival time at destination. If not stated, estimate based on typical route duration.
  * For hotels: use the CHECKOUT date (not check-in date) at T00:00:00+08:00. The end date MUST be different from start date. e.g. check-in 6/22, checkout 6/24 → start: 2026-06-22T00:00:00+08:00, end: 2026-06-24T00:00:00+08:00.
  * For car rentals: use the return date at the return time.
  * If only a date is known, use T00:00:00+08:00.

- location: specific and useful location string.
  * For flights: "出發航廈 → 目的地機場" — MUST match the actual direction of THIS flight leg (e.g. outbound: "桃園T2 → 新千歲", return: "新千歲 → 桃園T2")
  * For hotels: city name or hotel address
  * For car rentals: pickup location name/address
  * For transfers: pickup address → drop-off address

- description: concise summary in Traditional Chinese only (no other languages). 2~4 lines max:
  * For flights: 航班號、座位、訂位代號
  * For hotels: 入住/退房日期、房型、訂單號
  * For car rentals: 車型、取還車地點與時間、訂單號
  * For transfers: 接送地址、車型、訂單號

IMPORTANT rules:
- If the email is an EMD receipt, ancillary fee receipt, meal pre-order, Wi-Fi purchase, or check-in reminder without full itinerary details, output empty events array.
- If the email is a cancellation notice, output empty events array.
- For flights: extract airline name and flight number ONLY from the flight itinerary table (e.g. "Flight Number:", "運航便:", "航班號:"). Never use airline/flight info from signatures, headers, or unrelated text.
- For hotels: only extract check-in dates that are explicitly stated. Do NOT invent additional hotel stays not mentioned in the email.
- Each event in the array must be a distinct, real travel segment from the email. Do not duplicate or fabricate events.

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


WEATHER_CONDITION_MAP = {
    0: "晴天", 1: "大致晴天", 2: "部分多雲", 3: "陰天",
    45: "霧", 48: "霧淞",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    80: "小陣雨", 81: "中陣雨", 82: "大陣雨",
    95: "雷雨", 96: "雷雨夾冰雹", 99: "強雷雨夾冰雹",
}


WEATHER_PENDING_FILE = "weather_pending.json"


def _load_weather_pending() -> list[dict]:
    if not os.path.exists(WEATHER_PENDING_FILE):
        return []
    try:
        with open(WEATHER_PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_weather_pending(records: list[dict]):
    with open(WEATHER_PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def _add_weather_pending(record: dict):
    records = _load_weather_pending()
    # Avoid duplicate by cal_event_id
    if not any(r.get("cal_event_id") == record.get("cal_event_id") for r in records):
        records.append(record)
        _save_weather_pending(records)


def fetch_weather(location: str, date_str: str) -> str | None:
    """Return weather summary string, empty string on API failure, or None if date is out of range (should be retried later)."""
    if not location:
        return ""

    # Only fetch weather for dates within the 16-day forecast window
    try:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now(TAIPEI_TZ).date()
        delta = (event_date - today).days
        if delta < 0 or delta > 15:
            return None  # Signal: out of range, caller should record as pending
    except ValueError:
        return ""

    # Extract a city-level query from location (strip "→" routes, take destination)
    city_query = location
    if "→" in location:
        city_query = location.split("→")[-1].strip()
    # Strip airport terminal codes like "T1", "T2"
    city_query = re.sub(r'\bT\d\b', '', city_query).strip()

    try:
        geo_resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city_query, "count": 1, "language": "zh", "format": "json"},
            timeout=8,
        )
        geo_resp.raise_for_status()
        results = geo_resp.json().get("results")
        if not results:
            return ""
        lat = results[0]["latitude"]
        lon = results[0]["longitude"]
        resolved_name = results[0].get("name", city_query)
    except Exception:
        return ""

    try:
        wx_resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum",
                "timezone": "Asia/Taipei",
                "start_date": date_str,
                "end_date": date_str,
                "forecast_days": 16,
            },
            timeout=8,
        )
        wx_resp.raise_for_status()
        daily = wx_resp.json().get("daily", {})
        codes = daily.get("weathercode", [])
        t_max = daily.get("temperature_2m_max", [])
        t_min = daily.get("temperature_2m_min", [])
        precip = daily.get("precipitation_sum", [])
        if not codes:
            return ""
        condition = WEATHER_CONDITION_MAP.get(int(codes[0]), f"天氣代碼 {codes[0]}")
        hi = f"{t_max[0]:.0f}" if t_max else "?"
        lo = f"{t_min[0]:.0f}" if t_min else "?"
        rain = f"{precip[0]:.1f}" if precip else "?"
        return f"🌤 {resolved_name} 天氣：{condition}，{lo}～{hi}°C，降雨 {rain}mm"
    except Exception:
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


def insert_event_caldav(cal: "caldav.Calendar", event: CalendarEvent) -> str:
    """Insert event and return the UID (used for weather pending patch)."""
    import uuid
    uid = str(uuid.uuid4())
    ical = iCalendar()
    ical.add("prodid", "-//G2C AI Sync//EN")
    ical.add("version", "2.0")

    ievent = iEvent()
    ievent.add("uid", uid)
    ievent.add("summary", event.title)
    ievent.add("location", event.location)
    ievent.add("description", event.description)

    if event.is_all_day:
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
    return uid


def patch_event_caldav(cal: "caldav.Calendar", uid: str, new_description: str):
    """Patch description of an existing CalDAV event by UID."""
    for ev in cal.events():
        cal_obj = iCalendar.from_ical(ev.data)
        for component in cal_obj.walk():
            if component.name == "VEVENT" and str(component.get("uid", "")) == uid:
                component["description"] = new_description
                ev.data = cal_obj.to_ical()
                ev.save()
                return


def insert_event(service, calendar_id: str, event: CalendarEvent) -> str:
    """Insert event and return the Google Calendar event id."""
    if event.is_all_day:
        start_val = {"date": event.start[:10]}
        # Google Calendar all-day end is exclusive, so add one extra day
        end_dt = dateutil_parser.parse(event.end) + timedelta(days=1)
        end_val = {"date": end_dt.strftime("%Y-%m-%d")}
    else:
        start_val = {"dateTime": event.start, "timeZone": "Asia/Taipei"}
        end_val   = {"dateTime": event.end,   "timeZone": "Asia/Taipei"}

    body = {
        "summary": event.title,
        "location": event.location,
        "description": event.description,
        "start": start_val,
        "end":   end_val,
    }
    result = service.events().insert(calendarId=calendar_id, body=body).execute()
    return result["id"]


def patch_event_google(service, calendar_id: str, event_id: str, new_description: str):
    """Patch description of an existing Google Calendar event."""
    service.events().patch(
        calendarId=calendar_id,
        eventId=event_id,
        body={"description": new_description},
    ).execute()


# ── Weather Pending Retry ────────────────────────────────────────────────────

def retry_pending_weather(cal_svc=None, caldav_cal=None, calendar_id: str = ""):
    """Check weather_pending.json and patch calendar events whose dates are now within forecast range."""
    records = _load_weather_pending()
    if not records:
        return

    remaining = []
    patched = 0
    for rec in records:
        weather = fetch_weather(rec["location"], rec["date"])
        if weather is None:
            # Still out of range
            remaining.append(rec)
            continue
        if not weather:
            # API failed, keep retrying next time
            remaining.append(rec)
            continue

        new_desc = (rec.get("description_base", "") + "\n\n" + weather).strip()
        target = rec.get("target", "google")
        try:
            if target == "caldav" and caldav_cal:
                patch_event_caldav(caldav_cal, rec["cal_event_id"], new_desc)
            elif target != "caldav" and cal_svc:
                cal_id = rec.get("calendar_id") or calendar_id
                patch_event_google(cal_svc, cal_id, rec["cal_event_id"], new_desc)
            else:
                remaining.append(rec)
                continue
            print(f"  [Weather-Patch] {rec['title']}：{weather}")
            patched += 1
        except Exception as e:
            print(f"  [Weather-Patch] 失敗 {rec['title']} — {e}")
            remaining.append(rec)

    _save_weather_pending(remaining)
    if patched:
        print(f"[Weather-Pending] 補查完成，已更新 {patched} 個事件，剩餘 {len(remaining)} 個待補")


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

    # Retry weather for previously pending events (live only — needs real calendar access)
    if live and os.path.exists(WEATHER_PENDING_FILE):
        pending_cal_id = None
        pending_caldav = None
        if target == "caldav" and CALDAV_URL:
            try:
                pending_caldav = get_caldav_calendar()
            except Exception:
                pass
        else:
            try:
                pending_cal_id = get_or_create_calendar(cal_svc)
            except Exception:
                pass
        retry_pending_weather(cal_svc=cal_svc, caldav_cal=pending_caldav, calendar_id=pending_cal_id or "")

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

            # Weather enrichment for any event with a location (free, no API key)
            weather_pending_needed = False
            if event.location:
                weather = fetch_weather(event.location, event.start[:10])
                if weather is None:
                    weather_pending_needed = True
                    print(f"  [Weather] {event.start[:10]} 超出預報範圍，將於日期接近時自動補查")
                elif weather:
                    event = event.model_copy(update={
                        "description": (event.description + "\n\n" + weather).strip()
                    })
                    print(f"  [Weather] {weather}")

            tag = "全天" if event.is_all_day else event.start[11:16]
            warning = _check_suspicious(email, event)
            status = "suspicious" if warning else "ok"
            events_data.append({"status": status, "subject": email["subject"], "msg_id": email["id"], "event": event, "reason": warning or "", "weather_pending": weather_pending_needed})
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
                    cal_event_id = insert_event_caldav(caldav_cal, event)
                else:
                    cal_event_id = insert_event(cal_svc, calendar_id, event)
                print(f"  [OK] Inserted: {event.title}")
                _save_processed(item["msg_id"])
                if item.get("weather_pending") and event.location:
                    _add_weather_pending({
                        "title": event.title,
                        "location": event.location,
                        "date": event.start[:10],
                        "target": target,
                        "cal_event_id": cal_event_id,
                        "calendar_id": calendar_id if target != "caldav" else None,
                        "description_base": event.description,
                    })
                    print(f"  [Weather-Pending] 記錄至 weather_pending.json，待日期接近自動補查")
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

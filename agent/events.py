import os
import re
import json
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from groq import Groq

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# ── Security constants ─────────────────────────────────────────────────────────

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"forget\s+(all\s+)?(previous|prior|above|your)\s+instructions",
    r"you\s+are\s+now\s+(a\s+)?(new|different)",
    r"system\s*prompt\s*:",
    r"override\s+(all\s+)?instructions",
    r"jailbreak",
    r"do\s+not\s+follow",
    r"disregard\s+(all\s+)?(previous|prior)",
    r"new\s+instructions?\s*:",
    r"<\s*system\s*>",
    r"prompt\s*injection",
]

ALLOWED_URL_DOMAINS = ["lu.ma", "luma.com", "partiful.com", "tnt.so", "eventbrite.com"]

# ── Field length caps (prevent token-stuffing) ─────────────────────────────────

MAX_NAME_CHARS         = 200
MAX_ORGANIZER_DESC     = 300
MAX_HOST_BIO_CHARS     = 150
MAX_GUEST_BIO_CHARS    = 150
MAX_CLASSIFY_BLOCK     = 4000
MAX_EXTRACT_BLOCK      = 6000

# ── Keyword lists for PRE-FILTER ───────────────────────────────────────────────

LIFESTYLE_KEYWORDS = [
    "yoga", "sauna", "hike", "hiking", "hockey", "cooking",
    "meditation", "drawing", "craft", "wellness", "fitness",
    "gallery", "museum", "triathlon", "pilates", "soccer", "football",
    "martial arts", "surfskate", "sober party", "offline club", "paint night",
]

STARTUP_KEYWORDS = [
    "startup", "founder", "investor", "vc", "venture", "capital", "pitch",
    "demo", "hackathon", "accelerator", "operator", "ai", "tech", "builder",
    "engineer", "product", "seed", "raise", "funding", "innovation",
    "incubator", "entrepreneur", "saas", "software",
]

# ── IMS context embedded in LLM prompts ───────────────────────────────────────

IMS_CONTEXT = """
Imaginary Space (IMS) is a technical execution partner for early-stage startups.
They build MVPs and v1 products for founders who have funding but need to move fast.
IMS sellers attend Boston ecosystem events to build relationships with pre-seed and
seed stage founders before those founders know they need IMS.

Score events HIGHER when they attract:
- Pre-seed to Seed stage founders ($250K–$5M raised)
- Technical founders in AI, SaaS, or developer-facing products
- VC partners, angels, and accelerator cohort participants (YC, Techstars, MassChallenge, The Engine, TNT)

Score events LOWER when they attract:
- Series B+ company employees
- Non-technical founders in CPG, retail, or media
- Academics without commercialization intent
- General networking with no startup density
""".strip()

# ── Schema validation patterns ─────────────────────────────────────────────────

TIME_RE = re.compile(r"^\d{1,2}(:\d{2})?(am|pm)$")
DAY_RE  = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]+ \d{1,2}$")

# ── Paths ──────────────────────────────────────────────────────────────────────

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
MEMORY_PATH = os.path.join(os.path.dirname(__file__), "memory.json")

# ── Luma ───────────────────────────────────────────────────────────────────────

LUMA_API = "https://api.lu.ma/discover/get-paginated-events"
TNT_URL  = "https://www.tnt.so/events"

# ── Utilities ──────────────────────────────────────────────────────────────────

def sanitize(text: str) -> str:
    if not text:
        return ""
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, str(text), re.IGNORECASE):
            print(f"[SECURITY] Injection pattern detected, redacting field.")
            return "[REDACTED]"
    return str(text)[:500]


_groq_client = None

def _get_groq() -> Groq:
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _groq_client


def call_groq(system: str, user: str) -> str:
    client = _get_groq()
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
    )
    content = response.choices[0].message.content
    return (content or "").strip()


def parse_json_response(text: str, fallback):
    try:
        cleaned = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
        match = re.search(r"[\[{]", cleaned)
        if match:
            cleaned = cleaned[match.start():]
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        print(f"[WARN] Failed to parse JSON: {text[:200]}")
        return fallback


# ── Config + memory ────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    print(f"[CONFIG] geo={config['geo_latitude']},{config['geo_longitude']} "
          f"radius={config['geo_radius_km']}km window={config['window_days']}d")
    return config


def load_memory() -> dict:
    if not os.path.exists(MEMORY_PATH):
        return {"processed_urls": [], "last_run": None}
    with open(MEMORY_PATH) as f:
        return json.load(f)


def save_memory(memory: dict) -> None:
    with open(MEMORY_PATH, "w") as f:
        json.dump(memory, f, indent=2)


def trim_memory(memory: dict) -> dict:
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    memory["processed_urls"] = [
        e for e in memory.get("processed_urls", [])
        if e.get("date_seen", "9999-99-99") >= cutoff
    ]
    return memory


def _update_memory(memory: dict, extracted: list) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = {e["url"] for e in memory["processed_urls"]}
    for event in extracted:
        url = event.get("url", "")
        if url and url not in existing:
            memory["processed_urls"].append({"url": url, "date_seen": today})
            existing.add(url)


# ── Discord ────────────────────────────────────────────────────────────────────

def post_to_discord(message: str) -> None:
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    for chunk in _split_message(message):
        resp = requests.post(webhook_url, json={"content": chunk}, timeout=10)
        resp.raise_for_status()
        print(f"[DISCORD] Posted {len(chunk)} chars")


def _split_message(message: str, max_len: int = 1900) -> list:
    if len(message) <= max_len:
        return [message]
    chunks = []
    current_blocks = []
    current_len = 0
    for block in message.split("\n\n"):
        block_len = len(block) + 2
        if current_len + block_len > max_len and current_blocks:
            chunks.append("\n\n".join(current_blocks))
            current_blocks = [block]
            current_len = block_len
        else:
            current_blocks.append(block)
            current_len += block_len
    if current_blocks:
        chunks.append("\n\n".join(current_blocks))
    return chunks


# ── FETCH — Luma ───────────────────────────────────────────────────────────────

def fetch_luma_events(config: dict) -> list:
    events_out = []
    cursor = None
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    window_end = now_utc + timedelta(days=config["window_days"])

    while True:
        params = {
            "geo_latitude": config["geo_latitude"],
            "geo_longitude": config["geo_longitude"],
            "geo_radius_km": config["geo_radius_km"],
            "pagination_limit": 50,
        }
        if cursor:
            params["pagination_cursor"] = cursor

        try:
            resp = requests.get(LUMA_API, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[LUMA] Fetch error: {e}")
            break

        entries = data.get("entries", [])
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
        stop_paginating = False

        for entry in entries:
            start_at = entry.get("event", {}).get("start_at", "")
            try:
                start_dt = datetime.fromisoformat(
                    start_at.replace("Z", "+00:00")
                ).replace(tzinfo=None)
                if start_dt > window_end:
                    stop_paginating = True
                    break
            except (ValueError, AttributeError):
                pass
            events_out.append(entry)

        if not has_more or not cursor or stop_paginating:
            break

    print(f"[LUMA] Fetched {len(events_out)} raw entries")
    return events_out


# ── FETCH — TNT ────────────────────────────────────────────────────────────────

def fetch_tnt_events(config: dict) -> list:
    if not config.get("tnt_enabled", True):
        return []
    try:
        resp = requests.get(
            TNT_URL, timeout=10,
            headers={"User-Agent": "startup-intel/1.0"},
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[TNT] Fetch error: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not any(d in href for d in ALLOWED_URL_DOMAINS):
            continue
        if href in seen_urls:
            continue
        name = a.get_text(strip=True)
        if not name or len(name) < 5:
            continue
        parent = a.find_parent()
        date_text = parent.get_text(separator=" ", strip=True)[:200] if parent else ""

        results.append({
            "name": name[:MAX_NAME_CHARS],
            "url": href,
            "date_text": date_text,
            "source": "tnt",
        })
        seen_urls.add(href)

    print(f"[TNT] Scraped {len(results)} raw entries")
    return results


# ── NORMALIZE ──────────────────────────────────────────────────────────────────

def normalize_luma_event(entry: dict):
    event = entry.get("event", {})
    name = sanitize(event.get("name", ""))
    if not name or name == "[REDACTED]":
        return None

    url_slug = event.get("url", "")
    if not url_slug:
        return None
    full_url = url_slug if url_slug.startswith("http") else f"https://lu.ma/{url_slug}"

    calendar = entry.get("calendar") or {}
    ticket_info = entry.get("ticket_info") or {}
    hosts = entry.get("hosts") or []
    featured_guests = entry.get("featured_guests") or []

    host_names = [
        sanitize(h.get("name", ""))[:MAX_HOST_BIO_CHARS]
        for h in hosts[:3]
    ]
    guest_bios = [
        sanitize(g.get("bio_short", ""))[:MAX_GUEST_BIO_CHARS]
        for g in featured_guests[:3]
    ]

    return {
        "name": name,
        "start_at": event.get("start_at", ""),
        "timezone": event.get("timezone", "America/New_York"),
        "city": (event.get("geo_address_info") or {}).get("city_state", ""),
        "url": full_url,
        "source": "luma",
        "location_type": event.get("location_type", "offline"),
        "organizer_name": sanitize(calendar.get("name", "")),
        "organizer_desc": sanitize(calendar.get("description_short", ""))[:MAX_ORGANIZER_DESC],
        "guest_count": int(entry.get("guest_count") or 0),
        "require_approval": bool(ticket_info.get("require_approval")),
        "verified": bool(calendar.get("verified_at")),
        "luma_plus": bool(calendar.get("luma_plus_active")),
        "host_names": [h for h in host_names if h and h != "[REDACTED]"],
        "guest_bios": [b for b in guest_bios if b and b != "[REDACTED]"],
    }


def normalize_tnt_event(entry: dict):
    name = (entry.get("name") or "").strip()
    url = (entry.get("url") or "").strip()
    if not name or not url:
        return None

    start_at = _parse_tnt_date(entry.get("date_text", ""))

    return {
        "name": name[:MAX_NAME_CHARS],
        "start_at": start_at,
        "timezone": "America/New_York",
        "city": "Boston, MA",
        "url": url,
        "source": "tnt",
        "location_type": "offline",
        "organizer_name": "TNT",
        "organizer_desc": "",
        "guest_count": 0,
        "require_approval": False,
        "verified": True,
        "luma_plus": False,
        "host_names": [],
        "guest_bios": [],
    }


def _parse_tnt_date(date_text: str) -> str:
    """Best-effort parse of free-form date text from TNT HTML. Returns ISO UTC string or ''."""
    if not date_text:
        return ""
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{1,2})(?:\s+(\d{4}))?",
                  date_text, re.IGNORECASE)
    if not m:
        return ""
    month = months[m.group(1).lower()[:3]]
    day = int(m.group(2))
    year = int(m.group(3)) if m.group(3) else datetime.now(timezone.utc).year
    try:
        dt = datetime(year, month, day, 18, 0, tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return ""


# ── PRE-FILTER ────────────────────────────────────────────────────────────────

def pre_filter(event_list: list, memory: dict, config: dict) -> list:
    now_utc = datetime.now(timezone.utc)
    window_end = now_utc + timedelta(days=config["window_days"])
    processed_urls = {e["url"] for e in memory.get("processed_urls", [])}

    extra_allow = [k.lower() for k in config.get("extra_keyword_allow", [])]
    extra_deny  = [k.lower() for k in config.get("extra_keyword_deny", [])]

    results = []
    for event in event_list:
        name_lower = event["name"].lower()
        org_lower  = (event.get("organizer_name", "") + " " +
                      event.get("organizer_desc", "")).lower()
        source     = event.get("source", "luma")

        # Date gate
        start_at = event.get("start_at", "")
        if start_at:
            try:
                start_dt = datetime.fromisoformat(
                    start_at.replace("Z", "+00:00")
                )
                if start_dt <= now_utc or start_dt > window_end:
                    continue
            except ValueError:
                if source != "tnt":
                    continue

        # Location gate (Luma only)
        if source == "luma" and event.get("location_type") == "online":
            continue

        # Duplicate URL drop
        if event.get("url") in processed_urls:
            continue

        if source != "tnt":
            # Lifestyle keyword drop (word boundary matching)
            lifestyle_hit = False
            for kw in LIFESTYLE_KEYWORDS + extra_deny:
                pattern = r"\b" + re.escape(kw) + r"\b"
                if re.search(pattern, name_lower, re.IGNORECASE):
                    lifestyle_hit = True
                    break
            if lifestyle_hit:
                continue

            # Startup keyword gate
            all_keywords = STARTUP_KEYWORDS + extra_allow
            searchable = name_lower + " " + org_lower
            if not any(kw in searchable for kw in all_keywords):
                continue

        results.append(event)

    return results


# ── CLASSIFY ───────────────────────────────────────────────────────────────────

_CLASSIFY_SYSTEM = f"""You are a classifier for a Boston startup ecosystem event digest.

{IMS_CONTEXT}

You will receive a numbered list of events in the format: [N] EventName | OrganizerName

Return a JSON array of integer IDs (0-indexed) for events that should be included.
Include an event if it clearly involves founders, investors, operators, or technically
ambitious builders gathering in Boston.

Reject: generic tech meetups with no startup focus, academic seminars without commercialization
angle, wellness/lifestyle events, crypto/NFT without equity/VC angle.

Return ONLY a JSON array like [0, 3, 5] or [] if none qualify. No other text."""


def classify_events(event_list: list) -> list:
    if not event_list:
        return []

    lines = []
    for i, event in enumerate(event_list):
        name = event.get("name", "")[:100]
        org  = event.get("organizer_name", "")[:60]
        lines.append(f"[{i}] {name} | {org}")

    block = "\n".join(lines)
    if len(block) > MAX_CLASSIFY_BLOCK:
        block = block[:MAX_CLASSIFY_BLOCK]

    raw = call_groq(_CLASSIFY_SYSTEM, block)
    approved_ids = parse_json_response(raw, fallback=list(range(len(event_list))))

    if not isinstance(approved_ids, list):
        approved_ids = list(range(len(event_list)))

    valid_ids = [i for i in approved_ids if isinstance(i, int) and 0 <= i < len(event_list)]
    result = [event_list[i] for i in valid_ids]
    print(f"[CLASSIFY] {len(result)}/{len(event_list)} events approved")
    return result


# ── EXTRACT ────────────────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = f"""You are extracting structured data for a Boston startup ecosystem event digest.

{IMS_CONTEXT}

For each event in the input JSON array, return a JSON array with one object per event:
{{
  "source_id": <integer — the index of the event in the input array>,
  "short_name": "<2-5 word natural shortening of the actual event name>",
  "time_str": "<e.g. 6:30pm or 9am>",
  "day_str": "<e.g. Wed May 13>",
  "signal": "HIGH" or "MEDIUM",
  "url": "<copy url exactly from source — never construct or guess>"
}}

Rules:
- short_name must be a natural shortening of the actual name, not an invented label
- time_str must match pattern: digits + optional :mm + am/pm (e.g. "6pm", "6:30pm", "10am")
- day_str must match pattern: 3-letter day + month name + day number (e.g. "Wed May 13")
- url must be copied exactly from the source — never construct or infer
- signal HIGH: VC/accelerator organizer, require_approval=true with verified organizer, named investors in guest bios
- signal MEDIUM: AI/tech focus, decent guest count, recognized organization
- Return ONLY the JSON array, no other text."""


def extract_events(event_list: list) -> list:
    if not event_list:
        return []

    input_data = []
    for i, event in enumerate(event_list):
        input_data.append({
            "index": i,
            "name": event.get("name", "")[:MAX_NAME_CHARS],
            "organizer_name": event.get("organizer_name", ""),
            "organizer_desc": event.get("organizer_desc", "")[:MAX_ORGANIZER_DESC],
            "guest_count": event.get("guest_count", 0),
            "require_approval": event.get("require_approval", False),
            "verified": event.get("verified", False),
            "luma_plus": event.get("luma_plus", False),
            "host_names": event.get("host_names", []),
            "guest_bios": event.get("guest_bios", []),
            "url": event.get("url", ""),
            "start_at": event.get("start_at", ""),
        })

    user_content = json.dumps(input_data)
    if len(user_content) > MAX_EXTRACT_BLOCK:
        user_content = user_content[:MAX_EXTRACT_BLOCK]

    raw = call_groq(_EXTRACT_SYSTEM, user_content)
    extracted = parse_json_response(raw, fallback=[])

    if not isinstance(extracted, list):
        extracted = []

    remapped = []
    for item in extracted:
        sid = item.get("source_id")
        if isinstance(sid, int) and 0 <= sid < len(event_list):
            item["url"] = event_list[sid]["url"]  # always use Python-resolved URL
        remapped.append(item)

    print(f"[EXTRACT] {len(remapped)} events extracted")
    return remapped


def validate_extracted_events(extracted: list, source_events: list) -> list:
    valid = []
    max_id = len(source_events) - 1
    for event in extracted:
        sid = event.get("source_id")
        if not isinstance(sid, int) or sid < 0 or sid > max_id:
            print(f"[VALIDATE] Bad source_id={sid}, dropping")
            continue
        short_name = event.get("short_name", "")
        if not short_name or "\x00" in short_name:
            print(f"[VALIDATE] Bad short_name, dropping")
            continue
        if not TIME_RE.match(event.get("time_str", "")):
            print(f"[VALIDATE] Bad time_str={event.get('time_str')}, dropping")
            continue
        if not DAY_RE.match(event.get("day_str", "")):
            print(f"[VALIDATE] Bad day_str={event.get('day_str')}, dropping")
            continue
        if event.get("signal") not in ("HIGH", "MEDIUM"):
            print(f"[VALIDATE] Bad signal={event.get('signal')}, dropping")
            continue
        url = event.get("url", "")
        if not url or not any(d in url for d in ALLOWED_URL_DOMAINS):
            print(f"[VALIDATE] URL not in whitelist: {url}, dropping")
            continue
        valid.append(event)
    return valid


# ── DEDUPE ─────────────────────────────────────────────────────────────────────

def dedupe_events(event_list: list) -> list:
    seen = set()
    result = []
    for event in event_list:
        url = event.get("url", "")
        if url and url not in seen:
            seen.add(url)
            result.append(event)
    return result


# ── FORMAT ─────────────────────────────────────────────────────────────────────

_MONTH_NUMS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _day_sort_key(event: dict) -> tuple:
    signal_order = 0 if event.get("signal") == "HIGH" else 1
    day_str = event.get("day_str", "")
    m = re.match(r"\w+ (\w+) (\d+)", day_str)
    if m:
        month = _MONTH_NUMS.get(m.group(1), 99)
        day = int(m.group(2))
        return (signal_order, month, day)
    return (signal_order, 99, 99)


def format_message(event_list: list, intro_line: str = "") -> str:
    sorted_events = sorted(event_list, key=_day_sort_key)
    blocks = []
    if intro_line:
        blocks.append(intro_line)
    for event in sorted_events:
        block = f"{event['short_name']}\n{event['time_str']} {event['day_str']}\n{event['url']}"
        blocks.append(block)
    return "\n\n".join(blocks)

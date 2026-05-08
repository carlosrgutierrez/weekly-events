# Boston Ecosystem Events Bot

A weekly Discord bot that finds Boston startup ecosystem events, scores them for relevance to IMS (Imaginary Space), and posts the best ones every Monday morning.

**Live repo:** `https://github.com/drozrzd/weekly-events` (running on Carlos's GitHub)

---

## What It Does

Every Monday at 8am EDT, GitHub runs this bot automatically. It:

1. Fetches events from Luma, TNT, and Eventbrite (if token set)
2. Filters out past events, online-only events, lifestyle events, and events after 6:30pm
3. Scores each event 1–10 based on IMS relevance
4. Posts events scoring 5 or higher to Discord
5. Saves posted URLs to memory so events are never repeated

---

## Files

```
agent/
  events.py         The entire pipeline. All logic lives here.
  config.json       Runtime settings. Edit this to tune the bot without touching code.
  memory.json       Tracks posted URLs so events are never repeated. Auto-updated by CI.
  .env              Local secrets (API keys). Never committed to GitHub.
  requirements.txt  Python dependencies.

.github/workflows/
  weekly-events.yml The schedule and CI steps.

tests/
  test_events.py    55 unit tests. Run before pushing any change.
```

---

## Configuration (config.json)

Edit this file directly on GitHub — no code changes needed.

```json
{
  "city": "Boston",
  "geo_latitude": 42.3601,
  "geo_longitude": -71.0589,
  "geo_radius_km": 30,
  "window_days": 7,
  "max_events": 15,
  "min_score": 5,
  "tnt_enabled": true,
  "extra_keyword_allow": [],
  "extra_keyword_deny": []
}
```

| Setting | What it does |
|---|---|
| `geo_radius_km` | Search radius from downtown Boston (km) |
| `window_days` | How many days ahead to look for events |
| `max_events` | Maximum events to post in one message |
| `min_score` | Minimum 1–10 score to include an event. Lower = more events. |
| `tnt_enabled` | Set to `false` to disable TNT scraping |
| `extra_keyword_allow` | Words that always let an event pass. Example: `["climate", "biotech"]` |
| `extra_keyword_deny` | Words that always block an event. Example: `["party", "happy hour"]` |

---

## Secrets (GitHub → Settings → Secrets → Actions)

| Secret | Required | What it is |
|---|---|---|
| `GROQ_API_KEY` | Yes | Groq API key for LLM scoring |
| `DISCORD_WEBHOOK_URL` | Yes | Discord webhook to post messages |
| `EVENTBRITE_TOKEN` | Optional | Enables Eventbrite as a third source |
| `OLLAMA_MODEL` | Optional | Ollama model name for fallback (default: `llama3.1`) |

---

## Schedule

Runs every **Monday at 12pm UTC (8am EDT / 7am EST)**.

Scheduled 1 hour early because GitHub Actions can be 15–60 minutes late on the free tier.

To change the schedule, edit `.github/workflows/weekly-events.yml`:

```yaml
- cron: "0 12 * * 1"   # MINUTE HOUR * * DAY
```

| DAY value | Day |
|---|---|
| 0 | Sunday |
| 1 | Monday |
| 2 | Tuesday |
| 3 | Wednesday |
| 4 | Thursday |
| 5 | Friday |
| 6 | Saturday |

**HOUR is in UTC.** Add 4 hours to get EDT (summer), add 5 for EST (winter).

Examples:
- Monday 8am EDT → `"0 12 * * 1"`
- Wednesday 8am EDT → `"0 12 * * 3"`
- Monday + Thursday 8am EDT → `"0 12 * * 1,4"`

---

## Memory

`agent/memory.json` tracks every URL posted so events are never repeated. It auto-cleans entries older than 7 days.

**To reset the memory** (start fresh, re-post all current events):

1. Go to `https://github.com/drozrzd/weekly-events/blob/main/agent/memory.json`
2. Click the pencil icon (Edit)
3. Replace the content with: `{ "processed_urls": [], "last_run": null }`
4. Click Commit changes

---

## How Scoring Works

The bot uses Groq (llama-3.3-70b) to score each event 1–10 based on IMS context.

**IMS Context embedded in every prompt:**

> Imaginary Space (IMS) is a technical execution partner for early-stage startups.
> They build MVPs and v1 products for founders who have funding but need to move fast.
> IMS sellers attend Boston ecosystem events to build relationships with pre-seed and
> seed stage founders before those founders know they need IMS.

**Score HIGHER for:**
- Pre-seed to Seed stage founders ($100K–$5M raised)
- Technical founders in AI, SaaS, or developer-facing products
- VC partners, angels, YC/Techstars/MassChallenge/The Engine/TNT participants

**Score LOWER for:**
- Series B+ company employees
- Non-technical founders in CPG, retail, or media
- Academics without commercialization intent
- General networking with no startup density

**Scoring rubric:**
- 9–10: VC/accelerator organizer, exclusive event, named investors in bios
- 7–8: AI/tech/startup focus, recognized organizer, YC/Techstars/MassChallenge connection
- 5–6: General tech meetup with some startup relevance
- 1–4: Marginal relevance, low startup density

To change the IMS context, edit `agent/events.py` and search for `IMS_CONTEXT`.

---

## Filters Applied Before Scoring

The bot filters events in Python before any LLM call:

| Filter | What it drops |
|---|---|
| Date gate | Events in the past or beyond `window_days` |
| Time gate | Events starting after 6:30pm Boston time |
| Location gate | Luma events marked as online-only |
| Duplicate gate | URLs already in memory |
| Lifestyle keywords | Yoga, sauna, cooking, wellness, hiking, etc. |
| Startup keyword gate | Events with no startup-related words in name or organizer |
| URL domain whitelist | Events linking to unknown domains |

TNT events bypass the lifestyle and startup keyword gates because TNT is pre-curated.

---

## Current Sources

| Source | Status | Notes |
|---|---|---|
| Luma API | ✅ Active | Geo search within `geo_radius_km` of Boston |
| TNT (tnt.so/events) | ✅ Active | HTML scraper, pre-curated startup events |
| Eventbrite | ⚠️ Built, inactive | Add `EVENTBRITE_TOKEN` secret to activate |

---

## How to Add a New Event Source

Adding a new source requires two functions in `agent/events.py` and two lines in `main()`.

### Step 1: Write the fetch function

```python
def fetch_SOURCENAME_events(config: dict) -> list:
    try:
        # Option A: API call
        resp = requests.get("https://api.example.com/events", timeout=15)
        resp.raise_for_status()
        raw = resp.json().get("events", [])
        print(f"[SOURCENAME] Fetched {len(raw)} raw entries")
        return raw

        # Option B: HTML scrape
        resp = requests.get("https://example.com/events", timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        # extract what you need from soup
        return entries

    except Exception as e:
        print(f"[SOURCENAME] Error: {e}")
        return []  # always return empty list on failure, never crash
```

### Step 2: Write the normalize function

Every source must map its data to this exact shape:

```python
def normalize_SOURCENAME_event(entry: dict):
    url = entry.get("url", "")

    # URL must be from the allowed domain list or it will be dropped
    if not url or not any(d in url for d in ALLOWED_URL_DOMAINS):
        return None

    name = sanitize(entry.get("title", ""))
    if not name:
        return None

    return {
        "name":             name,
        "url":              url,
        "start_at":         entry.get("start_time", ""),  # must be UTC ISO: "2026-05-14T22:00:00Z"
        "organizer_name":   sanitize(entry.get("host", "")),
        "organizer_desc":   sanitize(entry.get("host_bio", "")),
        "guest_count":      entry.get("attendee_count", 0),
        "require_approval": entry.get("private", False),
        "verified":         False,
        "luma_plus":        False,
        "host_names":       [],
        "guest_bios":       [],
        "location_type":    "offline",
        "source":           "sourcename",
    }
```

### Step 3: Add to main()

In `agent/events.py`, find the `# FETCH` block and add one line:

```python
sourcename_raw = fetch_SOURCENAME_events(config)
```

Then find the `# NORMALIZE` block and add:

```python
for entry in sourcename_raw:
    norm = normalize_SOURCENAME_event(entry)
    if norm:
        event_list.append(norm)
```

### Step 4: If the source uses a new domain

If events link to a domain not already in the whitelist (e.g. `meetup.com`), add it:

```python
# Near the top of events.py
ALLOWED_URL_DOMAINS = ["lu.ma", "luma.com", "partiful.com", "tnt.so", "eventbrite.com", "meetup.com"]
```

### Step 5: Add a config flag (optional but recommended)

If the source should be toggleable without code changes, add a flag to `config.json`:

```json
{ "meetup_enabled": true }
```

And wrap the fetch call:

```python
if config.get("meetup_enabled", False):
    meetup_raw = fetch_meetup_events(config)
```

### Important rules for normalize functions

- Always call `sanitize()` on every text field that comes from the source
- Always return `None` if name or url is missing — `main()` skips `None` automatically
- `start_at` must be UTC ISO format — the 6:30pm filter and date window both depend on this
- `source` should be a short lowercase string (used for debugging logs)

---

## Running Locally

```bash
cd /Users/droz/Documents/boston-ecosystem-events

# Install dependencies
python3 -m venv venv && source venv/bin/activate
pip install -r agent/requirements.txt

# Set up .env (copy from startup-news project)
cp ../startup-news/agent/.env agent/.env

# Dry run — full pipeline, no Discord post, no memory write
DRY_RUN=true python3 agent/events.py

# Run tests
python3 -m pytest tests/test_events.py -v

# Live run (posts to Discord, updates memory)
python3 agent/events.py
```

---

## Triggering a Manual Run on GitHub

1. Go to `https://github.com/drozrzd/weekly-events/actions`
2. Click **Weekly Boston Ecosystem Events** on the left
3. Click **Run workflow** → **Run workflow**

---

## LLM Fallback

The bot tries Groq first. If Groq is rate-limited or down, it falls back to a local Ollama instance automatically.

To use Ollama fallback, Ollama must be running locally (`ollama serve`) with a model loaded. The default model is `llama3.1`. Override with the `OLLAMA_MODEL` environment variable.

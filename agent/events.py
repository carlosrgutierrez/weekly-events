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
    r"act\s+as\s+(a\s+)?",
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

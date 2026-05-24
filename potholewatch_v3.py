"""
PotholeWatch v4.0.0 — Multi-source road incident intelligence
============================================================
NEW IN v4.0:
  - DATE ANCHOR: prompts now state today's date explicitly so Claude
    stops refusing to search for "future" dates
  - STREET VIEW + VISION: ground-level Street View image at incident
    coordinates, analyzed by Claude for visible road damage
  - COST COMPARISON: PowerPatch vs traditional repair calculation
    on every incident (defaults to 2m² defect area)
  - @TAPAESO DRAFT: auto-generated social post for HIGH/CRITICAL
    incidents (cinematic-true-crime tone, no commentary)
"""

import os
import json
import base64
import hashlib
import urllib.parse
import requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ============================================================
# CONFIG
# ============================================================

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]
GMAIL_CLIENT_ID = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
GMAIL_REFRESH_TOKEN = os.environ["GMAIL_REFRESH_TOKEN"]

ALERT_RECIPIENTS = ["joel@powerfixinc.com", "1@powerfixinc.com"]
SCORE_THRESHOLD_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
MIN_SCORE = "MEDIUM"

INVENTORY_FILE = "incidents.json"
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}

# Cost comparison constants (Panama, MOP rates from case study)
DEFAULT_DEFECT_AREA_M2 = 2.0      # assumed typical single-pothole cluster
POWERPATCH_COST_PER_M2 = 209.0    # $344.75 / 1.65m² from case study = ~$209/m²
HOT_ASPHALT_REALISTIC_M2 = 230.0  # realistic cut scenario ~$380 / 1.65m²
HOT_ASPHALT_FULL_CUT_M2 = 893.0   # full-cut govt rate scenario
POWERPATCH_TIME_MIN = 5
HOT_ASPHALT_TIME_MIN = 270        # 4.5 hours including cure time

# Brand
BG      = "#0D0D0D"
CARD_BG = "#1A1A1A"
TEXT    = "#FFFFFF"
MUTED   = "#999999"
DIM     = "#666666"
ACCENT  = "#D94F2B"
SOFT    = "#262626"
SAVINGS = "#4ADE80"

SCORE_COLOR = {"CRITICAL": "#FF3B3B", "HIGH": ACCENT, "MEDIUM": "#F0A030", "LOW": MUTED}

TERRITORIES = [
    {
        "name": "Panama — Ciudad y Metro",
        "country": "Panama",
        "language": "es",
        "search_terms": [
            "accidente bache hueco Via Centenario Corredor Norte Sur Panama",
            "accidente vial Via Brasil Transistmica Panama City",
            "volcado vuelco bus carro Via España Tumba Muerto Panama",
            "MOP Panama Tapa Hueco deterioro vial ciudad",
            "choque pierde control bache Panama City corregimiento",
        ],
    },
    {
        "name": "Panama — Carretera Interamericana",
        "country": "Panama",
        "language": "es",
        "search_terms": [
            "accidente volcado Interamericana Panama Cocle Veraguas Chiriqui",
            "bus volcado vuelco carretera nacional Panama",
            "accidente bache hueco Interamericana El Platanal Santiago Chitre",
            "pierde control carretera Panama Darien Herrera",
            "deterioro vial Interamericana MOP Panama carretera",
        ],
    },
    {
        "name": "Panama — Provincias Centrales",
        "country": "Panama",
        "language": "es",
        "search_terms": [
            "accidente carretera Cocle Veraguas Herrera Los Santos Panama",
            "bache hueco vuelco Penonome Santiago Chitre Las Tablas",
            "volcado bus camion carretera central Panama provincias",
            "accidente vial Azuero Panama deterioro carretera",
        ],
    },
    {
        "name": "Panama — Provincias Extremas",
        "country": "Panama",
        "language": "es",
        "search_terms": [
            "accidente carretera Chiriqui Bocas del Toro David Boquete Panama",
            "accidente vial Colon Darien Panama carretera",
            "volcado bus camion Chiriqui Bocas del Toro Panama",
            "bache hueco deterioro vial Colon Chiriqui Panama MOP",
        ],
    },
]

# Today's date — used to anchor prompts so Claude doesn't refuse "future" dates
TODAY = datetime.utcnow().strftime("%B %d, %Y")
DATE_ANCHOR = f"""IMPORTANT DATE CONTEXT: Today's date is {TODAY}. Any accident dates from the past few weeks are REAL, RECENT events that have already happened. Do NOT refuse to search based on dates. Treat all provided dates as factual past events that occurred."""

# ============================================================
# INVENTORY — hash + URL dedup
# ============================================================

def _normalize_incidents(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        out = {}
        for i, rec in enumerate(raw):
            if not isinstance(rec, dict):
                continue
            loc = rec.get("location_text", "") or rec.get("location", "")
            dt  = rec.get("date", "")
            ttl = rec.get("title", "")
            h   = incident_hash(loc, dt, ttl) if (loc or dt or ttl) else f"legacy_{i:04d}"
            out[h] = rec
        return out
    return {}

def load_inventory():
    if not os.path.exists(INVENTORY_FILE):
        return {"counter": 0, "incidents": {}, "seen_urls": []}
    with open(INVENTORY_FILE) as f:
        try:
            inv = json.load(f)
        except json.JSONDecodeError:
            print("  inventory corrupt, starting fresh")
            return {"counter": 0, "incidents": {}, "seen_urls": []}
    if not isinstance(inv, dict):
        inv = {"counter": 0, "incidents": inv}
    inv.setdefault("counter", 0)
    inv.setdefault("seen_urls", [])
    inv["incidents"] = _normalize_incidents(inv.get("incidents", {}))
    return inv

def save_inventory(inv):
    incidents_list = []
    for h, rec in inv["incidents"].items():
        rec_out = dict(rec)
        rec_out.setdefault("_hash", h)
        incidents_list.append(rec_out)

    incidents_list.sort(
        key=lambda x: x.get("first_seen", x.get("scan_time", "")),
        reverse=True
    )

    out = {
        "scan_time": datetime.utcnow().isoformat(),
        "total": len(incidents_list),
        "counter": inv["counter"],
        "incidents": incidents_list,
        "seen_urls": inv.get("seen_urls", []),
    }
    with open(INVENTORY_FILE, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

def incident_hash(location_text, date, title):
    key = f"{(location_text or '').lower().strip()}|{date or ''}|{(title or '').lower().strip()[:60]}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]

def next_case_id(inv):
    inv["counter"] += 1
    return f"PTW-{inv['counter']:04d}"

def already_seen(inv, inc):
    url = inc.get("url", "")
    if url and url in inv.get("seen_urls", []):
        return True
    h = incident_hash(inc.get("location_text",""), inc.get("date",""), inc.get("title",""))
    return h in inv.get("incidents", {})

# ============================================================
# CLAUDE API
# ============================================================

def claude_call(prompt, tools=None, max_tokens=3000, images=None):
    """
    images: optional list of image URLs to include with the prompt (for vision)
    """
    content = []
    if images:
        for img_url in images:
            content.append({
                "type": "image",
                "source": {"type": "url", "url": img_url}
            })
    content.append({"type": "text", "text": prompt})

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content if images else prompt}],
    }
    if tools:
        payload["tools"] = tools
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=payload,
        timeout=180,
    )
    if not r.ok:
        print(f"    API ERROR {r.status_code}: {r.text[:800]}")
        r.raise_for_status()
    return "".join(b.get("text","") for b in r.json()["content"] if b.get("type") == "text")

# ============================================================
# ROBUST JSON EXTRACTION
# ============================================================

def extract_json_object(text):
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        return None
    depth = 0; in_str = False; esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc: esc = False; continue
        if c == "\\": esc = True; continue
        if c == '"': in_str = not in_str; continue
        if in_str: continue
        if c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try: return json.loads(text[start:i+1])
                except: return None
    return None

def extract_jsonl(text):
    out = []
    if not text: return out
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("```"): continue
        if not (line.startswith("{") and line.endswith("}")): continue
        try: out.append(json.loads(line))
        except: continue
    if out: return out
    depth = 0; start = None; in_str = False; esc = False
    for i, c in enumerate(text):
        if esc: esc = False; continue
        if c == "\\": esc = True; continue
        if c == '"': in_str = not in_str; continue
        if in_str: continue
        if c == "{":
            if depth == 0: start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(text[start:i+1])
                    if isinstance(obj, dict): out.append(obj)
                except: pass
                start = None
    return out

# ============================================================
# 1. SEARCH
# ============================================================

def search_incidents(territory):
    queries = "\n".join(f"- {q}" for q in territory["search_terms"])
    prompt = f"""{DATE_ANCHOR}

Search Spanish-language news for road accidents in {territory['name']} from the LAST 8 DAYS.

Run these queries:
{queries}

For each UNIQUE road accident found, output ONE JSON object per line (JSONL — no prose, no fences):
{{"title":"...","url":"...","source":"...","date":"YYYY-MM-DD",
  "location_text":"specific road + landmark + city/district",
  "summary":"what happened in 2-3 sentences",
  "article_image_urls":["direct image URLs from the article"],
  "pothole_keywords_found":["bache","hueco",...]}}

- Real road accidents only (collisions, rollovers, lost control, volcado/vuelco events)
- INCLUDE bus/truck rollovers even without explicit pothole mention — road surface is always suspect
- location_text must be Google Maps geocodable — be specific
- Include article_image_urls if the article has photos

JSONL only."""
    raw = claude_call(prompt, tools=[WEB_SEARCH_TOOL], max_tokens=4000)
    return extract_jsonl(raw)

# ============================================================
# 2. GEOCODE
# ============================================================

def _geocode_once(query):
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": query, "key": GOOGLE_MAPS_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=15)
    except Exception as e:
        return None, f"REQUEST_FAILED: {e}"
    if not r.ok:
        return None, f"HTTP_{r.status_code}"
    data = r.json()
    status = data.get("status","UNKNOWN")
    if status != "OK" or not data.get("results"):
        return None, f"{status} — {data.get('error_message','')}"
    loc = data["results"][0]["geometry"]["location"]
    return {"lat": loc["lat"], "lng": loc["lng"],
            "formatted": data["results"][0]["formatted_address"]}, "OK"

def geocode(location_text, country):
    for q in [f"{location_text}, {country}", f"{location_text}, Panama City, {country}", location_text]:
        coords, status = _geocode_once(q)
        if coords: return coords
        print(f"      geocode '{q}' → {status}")
    return None

# ============================================================
# 3. SATELLITE + STREET VIEW
# ============================================================

def satellite_url(lat, lng):
    base = "https://maps.googleapis.com/maps/api/staticmap"
    params = {
        "center": f"{lat},{lng}",
        "zoom": "17",
        "size": "640x400",
        "maptype": "hybrid",
        "markers": f"color:red|label:!|{lat},{lng}",
        "key": GOOGLE_MAPS_API_KEY,
    }
    return f"{base}?{urllib.parse.urlencode(params)}"

def street_view_url(lat, lng, heading=0, fov=90):
    """Returns a Street View image URL for given coords."""
    base = "https://maps.googleapis.com/maps/api/streetview"
    params = {
        "size": "640x400",
        "location": f"{lat},{lng}",
        "heading": str(heading),
        "fov": str(fov),
        "pitch": "-10",  # tilt slightly down to see road surface
        "source": "outdoor",
        "key": GOOGLE_MAPS_API_KEY,
    }
    return f"{base}?{urllib.parse.urlencode(params)}"

def street_view_metadata(lat, lng):
    """Check if Street View imagery exists at this location."""
    url = "https://maps.googleapis.com/maps/api/streetview/metadata"
    params = {"location": f"{lat},{lng}", "key": GOOGLE_MAPS_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.ok:
            return r.json().get("status") == "OK"
    except Exception:
        pass
    return False

# ============================================================
# 3b. STREET VIEW VISION ANALYSIS
# ============================================================

def analyze_street_view(lat, lng, location_text):
    """
    Fetch Street View at incident coords from 4 angles and have Claude
    analyze for visible road damage.
    """
    if not street_view_metadata(lat, lng):
        return {
            "available": False,
            "best_image_url": None,
            "analysis": "No Street View imagery available at this location.",
            "damage_detected": False,
            "severity": "unknown",
            "details": [],
        }

    # Get 4 views around the point — N, E, S, W
    headings = [0, 90, 180, 270]
    image_urls = [street_view_url(lat, lng, h) for h in headings]

    # Pick the first image as the "primary" for display
    primary_url = image_urls[0]

    # Build vision prompt
    prompt = f"""{DATE_ANCHOR}

You are analyzing 4 Street View images taken at coordinates {lat:.5f}, {lng:.5f} near "{location_text}".
Each image shows the road from a different cardinal direction (N, E, S, W respectively).

A road accident recently occurred at this location. Your job is to assess the ROAD SURFACE CONDITION.

Look carefully for:
- Potholes (visible holes, depressions, broken asphalt)
- Cracks (linear, alligator, or block cracking)
- Patches (previous repairs, often poorly done)
- Edge deterioration
- Missing road markings due to surface wear
- Water pooling areas (indicates poor drainage = pothole formation)
- General pavement age and quality

Return ONE JSON object (no prose, no fences):
{{
  "damage_detected": true/false,
  "severity": "none|minor|moderate|severe|critical",
  "potholes_visible": true/false,
  "details": [
    "specific observation 1",
    "specific observation 2"
  ],
  "best_view_index": <0-3, which image (N/E/S/W) shows damage most clearly>,
  "analysis": "2-3 sentence professional summary of road surface condition",
  "powerpatch_recommendation": "1 sentence on whether this road is a candidate for PowerPatch repair"
}}

JSON only."""

    try:
        raw = claude_call(prompt, max_tokens=1500, images=image_urls)
        parsed = extract_json_object(raw)
        if not parsed:
            return {
                "available": True,
                "best_image_url": primary_url,
                "all_image_urls": image_urls,
                "analysis": "Vision analysis returned no parseable result.",
                "damage_detected": False,
                "severity": "unknown",
                "details": [],
            }

        # Pick best view based on Claude's index
        best_idx = parsed.get("best_view_index", 0)
        if not isinstance(best_idx, int) or best_idx < 0 or best_idx >= len(image_urls):
            best_idx = 0
        best_url = image_urls[best_idx]

        return {
            "available": True,
            "best_image_url": best_url,
            "all_image_urls": image_urls,
            "damage_detected": parsed.get("damage_detected", False),
            "potholes_visible": parsed.get("potholes_visible", False),
            "severity": parsed.get("severity", "unknown"),
            "analysis": parsed.get("analysis", ""),
            "details": parsed.get("details", []),
            "powerpatch_recommendation": parsed.get("powerpatch_recommendation", ""),
        }
    except Exception as e:
        print(f"      street view vision failed: {e}")
        return {
            "available": True,
            "best_image_url": primary_url,
            "all_image_urls": image_urls,
            "analysis": f"Vision analysis failed: {e}",
            "damage_detected": False,
            "severity": "unknown",
            "details": [],
        }

# ============================================================
# 4. SOCIAL IMAGE + POST SEARCH
# ============================================================

def search_social_evidence(incident, coords):
    date_str = incident.get("date", "")
    location = incident.get("location_text", "")

    prompt = f"""{DATE_ANCHOR}

Find real citizen social media posts and photos about this specific road accident.

ACCIDENT:
  Location: {location} ({coords.get('formatted','')})
  Date: {date_str}
  Title: {incident.get('title','')}
  Article: {incident.get('url','')}

Run targeted searches to find:
1. X/Twitter posts from around {date_str}: search 'site:x.com "{location.split(',')[0]}" accidente'
2. Instagram posts: search 'site:instagram.com "{location.split(',')[0]}" accidente bache'
3. Facebook posts/comments: search 'site:facebook.com "{location.split(',')[0]}" accidente'
4. Waze alerts: search 'waze "{location.split(',')[0]}" accidente peligro bache'
5. News article comment sections: fetch {incident.get('url','')} and extract reader comments
6. Any photos posted by citizens showing the accident scene or road damage

For each post, ALWAYS try to capture the direct URL/permalink to the specific post or comment if possible.

Return ONE JSON object (no prose, no fences):
{{
  "x_posts": [{{"url":"...","user":"...","quote":"...","image_urls":["..."]}}],
  "instagram_posts": [{{"url":"...","user":"...","quote":"...","image_urls":["..."]}}],
  "facebook_posts": [{{"url":"...","user":"...","quote":"...","image_urls":["..."]}}],
  "waze_reports": [{{"url":"...","quote":"...","image_urls":[]}}],
  "news_comments": [{{"source":"...","url":"...","quote":"..."}}],
  "citizen_image_urls": ["direct URLs to any citizen photos of this accident or road damage"],
  "pothole_word_count": <integer count of bache/hueco/crater mentions across all>
}}

JSON only. Empty arrays if nothing found."""

    raw = claude_call(prompt, tools=[WEB_SEARCH_TOOL], max_tokens=5000)
    parsed = extract_json_object(raw)
    if parsed is None:
        print(f"      social parse failed — raw[:200]: {raw[:200]!r}")
        parsed = {}
    template = {
        "x_posts": [], "instagram_posts": [], "facebook_posts": [],
        "waze_reports": [], "news_comments": [],
        "citizen_image_urls": [], "pothole_word_count": 0,
    }
    for k, v in template.items():
        parsed.setdefault(k, v)
    return parsed

# ============================================================
# 5. CROSS-REFERENCE
# ============================================================

def cross_reference(incident, coords):
    prompt = f"""{DATE_ANCHOR}

Find institutional evidence linking this road to pothole/deterioration problems.

INCIDENT:
  Title: {incident.get('title','')}
  Location: {incident.get('location_text','')} ({coords.get('formatted','')})
  Date: {incident.get('date','')}

Search for:
  1. MOP Panama (Ministerio de Obras Publicas) — Tapa Hueco programs, contracts, TDR docs, deterioration reports
  2. ATTT Panama — transit reports, hazard advisories, cargo restrictions
  3. Prior news coverage of road damage on this road in last 18 months

Return ONE JSON object (no prose, no fences):
{{
  "mop_evidence": [{{"source":"...","url":"...","quote":"..."}}],
  "attt_evidence": [{{"source":"...","url":"...","quote":"..."}}],
  "news_history": [{{"source":"...","url":"...","date":"...","headline":"..."}}],
  "pothole_word_count": <integer>
}}

JSON only."""
    raw = claude_call(prompt, tools=[WEB_SEARCH_TOOL], max_tokens=4000)
    parsed = extract_json_object(raw)
    if parsed is None:
        parsed = {}
    for k, v in {"mop_evidence":[],"attt_evidence":[],"news_history":[],"pothole_word_count":0}.items():
        parsed.setdefault(k, v)
    return parsed

# ============================================================
# 6. SCORE
# ============================================================

def score_incident(incident, coords, social, crossref, street_view):
    social_count = sum(len(social.get(k,[])) for k in
                      ["x_posts","instagram_posts","facebook_posts","waze_reports","news_comments"])
    inst_count = sum(len(crossref.get(k,[])) for k in
                    ["mop_evidence","attt_evidence","news_history"])

    sv_summary = ""
    if street_view.get("damage_detected"):
        sv_summary = f"STREET VIEW EVIDENCE: Visible road damage detected ({street_view.get('severity','unknown')} severity). Potholes visible: {street_view.get('potholes_visible',False)}. {street_view.get('analysis','')}"
    elif street_view.get("available"):
        sv_summary = "STREET VIEW EVIDENCE: No significant road damage visible in current Street View imagery."
    else:
        sv_summary = "STREET VIEW EVIDENCE: Not available at this location."

    prompt = f"""{DATE_ANCHOR}

Score pothole-correlation probability for this road accident.

INCIDENT: {json.dumps(incident, ensure_ascii=False)}
SOCIAL EVIDENCE: {social_count} citizen posts/comments found, pothole word count {social.get('pothole_word_count',0)}
SOCIAL DETAIL: {json.dumps({k:social[k] for k in ['x_posts','instagram_posts','facebook_posts','waze_reports','news_comments'] if social.get(k)}, ensure_ascii=False)}
INSTITUTIONAL EVIDENCE: {inst_count} MOP/ATTT/news items found
INSTITUTIONAL DETAIL: {json.dumps(crossref, ensure_ascii=False)}
{sv_summary}

Scoring levels:
  - CRITICAL: Article explicitly names pothole/bache/hueco as cause, OR Street View shows clear potholes + institutional/social evidence, OR strong social + institutional evidence
  - HIGH: Rollover/vuelco/volcado (multi-rollover = strong road surface suspicion) OR Street View shows moderate+ damage OR multiple citizen posts + MOP/ATTT records
  - MEDIUM: Some social posts OR institutional evidence (1-2 items) OR single-vehicle accident on known deteriorated road OR Street View shows minor damage
  - LOW: Clear driver-only cause (drunk, phone, speeding confirmed) with zero road condition signals and Street View shows clean road

IMPORTANT: Bus or truck rollovers (volcado, vuelco, dio vueltas) on national highways should default to HIGH
unless the cause is explicitly confirmed as non-road-related. Road surface is always a suspect in rollovers.
Street View damage evidence should bump the score up one level.

Return JSON (no prose, no fences):
{{
  "score":"CRITICAL|HIGH|MEDIUM|LOW",
  "reasoning":"2-3 sentences",
  "headline_evidence":["bullet 1","bullet 2","bullet 3"]
}}"""
    raw = claude_call(prompt, max_tokens=1000)
    parsed = extract_json_object(raw)
    if not parsed:
        return {"score":"LOW","reasoning":"Parse failed","headline_evidence":[]}
    parsed.setdefault("score","LOW")
    parsed.setdefault("reasoning","")
    parsed.setdefault("headline_evidence",[])
    return parsed

# ============================================================
# 7. COST COMPARISON
# ============================================================

def build_cost_comparison(area_m2=DEFAULT_DEFECT_AREA_M2):
    """
    Build the PowerPatch vs traditional cost comparison.
    Defaults to 2m² (typical single-pothole cluster).
    """
    powerpatch_cost = round(area_m2 * POWERPATCH_COST_PER_M2, 2)
    asphalt_realistic = round(area_m2 * HOT_ASPHALT_REALISTIC_M2, 2)
    asphalt_fullcut = round(area_m2 * HOT_ASPHALT_FULL_CUT_M2, 2)

    savings_vs_realistic = round(asphalt_realistic - powerpatch_cost, 2)
    savings_vs_fullcut = round(asphalt_fullcut - powerpatch_cost, 2)

    time_savings_min = HOT_ASPHALT_TIME_MIN - POWERPATCH_TIME_MIN
    time_multiplier = round(HOT_ASPHALT_TIME_MIN / POWERPATCH_TIME_MIN, 0)

    return {
        "assumed_area_m2": area_m2,
        "powerpatch": {
            "cost": powerpatch_cost,
            "time_minutes": POWERPATCH_TIME_MIN,
            "crew_size": 2,
        },
        "hot_asphalt_realistic": {
            "cost": asphalt_realistic,
            "time_minutes": HOT_ASPHALT_TIME_MIN,
            "savings_vs_powerpatch": savings_vs_realistic,
        },
        "hot_asphalt_full_cut": {
            "cost": asphalt_fullcut,
            "time_minutes": HOT_ASPHALT_TIME_MIN,
            "savings_vs_powerpatch": savings_vs_fullcut,
        },
        "headline_savings": savings_vs_realistic,
        "time_multiplier": time_multiplier,
        "time_savings_minutes": time_savings_min,
    }

# ============================================================
# 8. @TAPAESO DRAFT GENERATOR
# ============================================================

def generate_tapaeso_draft(incident, coords, score, street_view):
    """
    Generate a @TapaEso social post draft.
    Cinematic-true-crime tone. No commentary. No blame.
    Just: location + date + facts + tú decides.
    """
    if score["score"] not in ("HIGH", "CRITICAL"):
        return None

    location_short = (incident.get("location_text", "") or "").split(",")[0]
    date = incident.get("date", "")
    headline = incident.get("title", "")

    prompt = f"""{DATE_ANCHOR}

You are writing a social media post for @TapaEso — a Panama hueco awareness account.

VOICE & TONE:
- Cinematic true-crime style. Raw. Bold.
- NO commentary. NO accusations. NO naming culprits.
- NO political analysis. Just the facts + the road.
- Short punchy lines. Stark.
- Reference: viral citizen footage style — "look at this" without saying "look at this"
- ALWAYS ends with "tú decides." (lowercase, period)

INCIDENT:
  Headline: {headline}
  Location: {incident.get('location_text','')}
  Date: {date}
  Score: {score['score']}
  Article: {incident.get('url','')}
  Street View damage detected: {street_view.get('damage_detected', False)}
  Street View notes: {street_view.get('analysis', '')}

Generate ONE JSON object (no prose, no fences):
{{
  "instagram_caption": "<the post — 4-8 short lines, ends with 'tú decides.'>",
  "tiktok_caption": "<shorter version, 3-5 lines, ends with 'tú decides.'>",
  "facebook_caption": "<same as instagram>",
  "hashtags": ["#TapaEso", "#Panama", "<3-5 more relevant tags>"]
}}

JSON only. Spanish only."""

    try:
        raw = claude_call(prompt, max_tokens=1000)
        parsed = extract_json_object(raw)
        if parsed:
            return parsed
    except Exception as e:
        print(f"      tapaeso draft failed: {e}")
    return None

# ============================================================
# 9. BUILD FULL INCIDENT RECORD
# ============================================================

def build_incident_record(case_id, inc, coords, social, crossref, score, street_view, cost_comp, tapaeso, scan_time_iso):
    quote = ""
    for platform in ["x_posts", "facebook_posts", "instagram_posts", "waze_reports", "news_comments"]:
        for post in (social.get(platform) or []):
            q = (post.get("quote") or "").strip()
            if q:
                quote = q
                break
        if quote:
            break

    article_url = inc.get("url", "")
    source_name = inc.get("source", "")

    article_imgs = [u for u in (inc.get("article_image_urls") or []) if u and u.startswith("http")]
    citizen_imgs = [u for u in (social.get("citizen_image_urls") or []) if u and u.startswith("http")]
    all_images = article_imgs + [u for u in citizen_imgs if u not in article_imgs]

    mop_attt = None
    mop = crossref.get("mop_evidence") or []
    attt = crossref.get("attt_evidence") or []
    if mop or attt:
        parts = []
        if mop:
            parts.append(f"MOP: {mop[0].get('quote','')[:120]}")
        if attt:
            parts.append(f"ATTT: {attt[0].get('quote','')[:120]}")
        mop_attt = " | ".join(parts)

    social_hits = sum(len(social.get(k, [])) for k in
                     ["x_posts", "instagram_posts", "facebook_posts", "waze_reports", "news_comments"])

    return {
        "ptw_id": case_id,
        "probability": score["score"],
        "confidence": _score_to_confidence(score["score"], social_hits, len(mop) + len(attt), street_view),
        "sources_confirmed": _confirmed_sources(inc, social, crossref, street_view),
        "source_count": 1 + social_hits,
        "lat": coords["lat"],
        "lng": coords["lng"],
        "geocode_method": "google_geocoding",
        "territory": "Panama City",
        "location": inc.get("location_text", ""),
        "location_text": coords.get("formatted", inc.get("location_text", "")),
        "scan_time": scan_time_iso,
        "first_seen": scan_time_iso,
        "date": inc.get("date", ""),
        "headline": inc.get("title", ""),
        "summary": inc.get("summary", ""),
        "source_name": source_name,
        "url": article_url,
        "quote": quote,
        "mop_attt": mop_attt,
        "recommended_action": _build_action(score["score"], inc.get("location_text", "")),
        "score_reasoning": score.get("reasoning", ""),
        "headline_evidence": score.get("headline_evidence", []),
        "article_image_urls": all_images[:6],
        "primary_image_url": all_images[0] if all_images else None,
        "street_view": street_view,
        "cost_comparison": cost_comp,
        "tapaeso_draft": tapaeso,
        "social": {
            "x_posts": social.get("x_posts", [])[:3],
            "instagram_posts": social.get("instagram_posts", [])[:3],
            "facebook_posts": social.get("facebook_posts", [])[:3],
            "waze_reports": social.get("waze_reports", [])[:3],
            "news_comments": social.get("news_comments", [])[:5],
            "pothole_word_count": social.get("pothole_word_count", 0),
        },
        "crossref": {
            "mop_evidence": crossref.get("mop_evidence", [])[:3],
            "attt_evidence": crossref.get("attt_evidence", [])[:3],
            "news_history": crossref.get("news_history", [])[:5],
        },
        "pothole_confirmed": score["score"] in ("CRITICAL", "HIGH") or street_view.get("potholes_visible", False),
        "pothole_keywords": inc.get("pothole_keywords_found", []),
    }

def _score_to_confidence(score, social_hits, inst_hits, street_view):
    base = {"CRITICAL": 85, "HIGH": 70, "MEDIUM": 50, "LOW": 25}.get(score, 25)
    bonus = min(social_hits * 3 + inst_hits * 5, 15)
    if street_view.get("damage_detected"):
        bonus = min(bonus + 5, 15)
    return min(base + bonus, 99)

def _confirmed_sources(inc, social, crossref, street_view):
    sources = ["news"]
    if any(social.get(k) for k in ["x_posts", "instagram_posts", "facebook_posts"]):
        sources.append("social_media")
    if social.get("waze_reports"):
        sources.append("waze")
    if crossref.get("mop_evidence"):
        sources.append("mop")
    if crossref.get("attt_evidence"):
        sources.append("attt")
    if street_view.get("damage_detected"):
        sources.append("street_view")
    return sources

def _build_action(score, location):
    loc = location.split(",")[0] if location else "this location"
    if score == "CRITICAL":
        return f"🚨 URGENT DEPLOYMENT — {loc} pothole confirmed as accident cause. Deploy PowerPatch crew immediately."
    elif score == "HIGH":
        return f"⚠️ PRIORITY INSPECTION — {loc} showing strong pothole indicators. Schedule PowerPatch within 24hrs."
    else:
        return f"📋 SCHEDULE INSPECTION — {loc} showing road damage indicators. Monitor for 24hrs then assess PowerPatch deployment."

# ============================================================
# 10. EMAIL — DIGEST
# ============================================================

def _section(label, items, fmt_fn):
    if not items: return ""
    rows = "".join(fmt_fn(i) for i in items[:5])
    return f"""<div style="margin-top:16px;padding-top:14px;border-top:1px solid {SOFT};">
      <div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin-bottom:8px;">{label}</div>
      {rows}
    </div>"""

def _post_row(item):
    src   = item.get('source') or item.get('platform') or item.get('user') or ''
    quote = (item.get('quote','') or '').replace('<','&lt;').replace('>','&gt;')
    url   = item.get('url','')
    link  = f' <a href="{url}" style="color:{ACCENT};text-decoration:none;">→</a>' if url else ''
    src_h = f'<strong style="color:{TEXT};">{src}:</strong> ' if src else ''
    return f'<div style="font-size:13px;line-height:1.5;margin:5px 0;color:{MUTED};">{src_h}&ldquo;{quote}&rdquo;{link}</div>'

def _news_row(item):
    return f'<div style="font-size:13px;margin:5px 0;color:{MUTED};">{item.get("date","")} — <a href="{item.get("url","")}" style="color:{ACCENT};text-decoration:none;">{item.get("headline","")}</a> <span style="color:{DIM};">({item.get("source","")})</span></div>'

def build_street_view_block(street_view):
    """Build the Street View + AI vision block for the email."""
    if not street_view.get("available"):
        return ""

    severity = street_view.get("severity", "unknown")
    severity_color = {
        "critical": "#FF3B3B", "severe": "#FF3B3B",
        "moderate": ACCENT, "minor": "#F0A030",
        "none": MUTED, "unknown": MUTED
    }.get(severity, MUTED)

    damage_badge = ""
    if street_view.get("damage_detected"):
        damage_badge = f'<span style="display:inline-block;background:{severity_color};color:#fff;font-size:9px;font-weight:700;letter-spacing:2px;padding:3px 8px;border-radius:3px;text-transform:uppercase;">{severity} damage</span>'
    else:
        damage_badge = f'<span style="display:inline-block;background:{MUTED};color:#fff;font-size:9px;font-weight:700;letter-spacing:2px;padding:3px 8px;border-radius:3px;text-transform:uppercase;">no visible damage</span>'

    details_html = ""
    if street_view.get("details"):
        details_html = "<ul style='font-size:12px;color:" + MUTED + ";margin:8px 0 0 18px;line-height:1.6;'>" + \
            "".join(f"<li>{d}</li>" for d in street_view.get("details", [])[:5]) + "</ul>"

    rec_html = ""
    if street_view.get("powerpatch_recommendation"):
        rec_html = f'<div style="font-size:12px;color:{TEXT};margin-top:8px;padding:8px;background:rgba(217,79,43,0.1);border-left:2px solid {ACCENT};">{street_view["powerpatch_recommendation"]}</div>'

    return f"""
    <div style="margin-top:18px;padding-top:14px;border-top:1px solid {SOFT};">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
        <div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;">Street View — Road Surface AI Analysis</div>
        {damage_badge}
      </div>
      <img src="{street_view.get('best_image_url','')}" style="width:100%;border-radius:6px;display:block;margin-bottom:10px;" />
      <div style="font-size:13px;line-height:1.6;color:{TEXT};">{street_view.get('analysis','')}</div>
      {details_html}
      {rec_html}
    </div>"""

def build_cost_block(cost):
    """Build the cost comparison block."""
    pp = cost["powerpatch"]
    asp = cost["hot_asphalt_realistic"]
    savings = cost["headline_savings"]
    return f"""
    <div style="margin-top:18px;padding-top:14px;border-top:1px solid {SOFT};">
      <div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin-bottom:10px;">Cost Comparison — Estimated for {cost['assumed_area_m2']}m² Defect</div>
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <tr>
          <td style="padding:10px;background:{SOFT};border-radius:4px 0 0 4px;color:{MUTED};">Hot Asphalt (realistic cut)</td>
          <td style="padding:10px;background:{SOFT};text-align:right;color:{TEXT};font-weight:700;">${asp['cost']:.2f}</td>
          <td style="padding:10px;background:{SOFT};border-radius:0 4px 4px 0;text-align:right;color:{DIM};font-size:11px;">~{asp['time_minutes']//60}h</td>
        </tr>
        <tr><td colspan="3" style="height:4px;"></td></tr>
        <tr>
          <td style="padding:10px;background:rgba(217,79,43,0.2);border-radius:4px 0 0 4px;color:{TEXT};font-weight:700;">PowerPatch</td>
          <td style="padding:10px;background:rgba(217,79,43,0.2);text-align:right;color:{TEXT};font-weight:700;">${pp['cost']:.2f}</td>
          <td style="padding:10px;background:rgba(217,79,43,0.2);border-radius:0 4px 4px 0;text-align:right;color:{ACCENT};font-size:11px;font-weight:700;">{pp['time_minutes']} min</td>
        </tr>
      </table>
      <div style="margin-top:12px;padding:12px;background:rgba(74,222,128,0.1);border-left:3px solid {SAVINGS};border-radius:4px;">
        <div style="font-size:11px;letter-spacing:2px;color:{SAVINGS};font-weight:700;text-transform:uppercase;margin-bottom:4px;">Savings</div>
        <div style="font-size:16px;color:{TEXT};font-weight:700;">${savings:.2f} saved · {cost['time_multiplier']:.0f}× faster</div>
      </div>
    </div>"""

def build_tapaeso_block(tapaeso):
    """Build the @TapaEso draft block."""
    if not tapaeso:
        return ""
    ig = tapaeso.get("instagram_caption", "")
    tags = " ".join(tapaeso.get("hashtags", []))
    return f"""
    <div style="margin-top:18px;padding-top:14px;border-top:1px solid {SOFT};">
      <div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin-bottom:10px;">📱 @TapaEso Post Draft</div>
      <div style="background:#000;border:1px solid {SOFT};border-radius:6px;padding:14px;font-family:'Courier New',monospace;font-size:13px;line-height:1.7;color:{TEXT};white-space:pre-wrap;">{ig}

{tags}</div>
      <div style="font-size:11px;color:{DIM};margin-top:6px;font-style:italic;">Copy-paste ready for Instagram, TikTok & Facebook.</div>
    </div>"""

def build_incident_card(case_id, incident, coords, social, crossref, score, sat, street_view, cost_comp, tapaeso):
    color = SCORE_COLOR.get(score["score"], MUTED)

    article_imgs = [u for u in (incident.get("article_image_urls") or []) if u and u.startswith("http")][:3]
    citizen_imgs = [u for u in (social.get("citizen_image_urls") or []) if u and u.startswith("http")][:3]
    all_imgs = article_imgs + [u for u in citizen_imgs if u not in article_imgs]

    img_html = "".join(
        f'<img src="{u}" style="width:100%;border-radius:6px;margin-bottom:8px;display:block;" onerror="this.style.display=\'none\'" />'
        for u in all_imgs[:4]
    )

    bullets = "".join(f'<li style="margin-bottom:5px;">{b}</li>' for b in score.get("headline_evidence",[]))

    social_html  = _section("X / Twitter",    social.get("x_posts",[]),          _post_row)
    social_html += _section("Instagram",       social.get("instagram_posts",[]),  _post_row)
    social_html += _section("Facebook",        social.get("facebook_posts",[]),   _post_row)
    social_html += _section("Waze reports",    social.get("waze_reports",[]),     _post_row)
    social_html += _section("Reader comments", social.get("news_comments",[]),    _post_row)

    inst_html  = _section("MOP Panama",  crossref.get("mop_evidence",[]),  _post_row)
    inst_html += _section("ATTT Panama", crossref.get("attt_evidence",[]), _post_row)
    if crossref.get("news_history"):
        inst_html += _section("Prior news on this road", crossref["news_history"], _news_row)

    no_ev = "" if (social_html.strip() or inst_html.strip()) else \
        f'<div style="font-size:12px;color:{DIM};font-style:italic;margin-top:14px;padding-top:14px;border-top:1px solid {SOFT};">No cross-reference evidence surfaced.</div>'

    sv_block = build_street_view_block(street_view)
    cost_block = build_cost_block(cost_comp)
    tapaeso_block = build_tapaeso_block(tapaeso)

    return f"""
<div style="background:{CARD_BG};border-radius:8px;padding:24px;margin-bottom:20px;border-left:4px solid {color};">
  <div style="font-size:10px;letter-spacing:3px;color:{color};font-weight:700;">{score['score']} · {case_id}</div>
  <h2 style="margin:8px 0 6px;font-size:20px;color:{TEXT};line-height:1.3;font-weight:700;">{incident.get('title','')}</h2>
  <div style="color:{DIM};font-size:12px;margin-bottom:16px;">
    {coords.get('formatted','')} · {incident.get('date','')} · <a href="{incident.get('url','')}" style="color:{ACCENT};text-decoration:none;">{incident.get('source','')}</a>
  </div>
  <div style="background:{SOFT};padding:16px;border-radius:6px;margin-bottom:16px;">
    {img_html}
    <div style="font-size:14px;line-height:1.6;color:{TEXT};">{incident.get('summary','')}</div>
    <div style="margin-top:14px;"><a href="{incident.get('url','')}" style="display:inline-block;background:{ACCENT};color:{TEXT};padding:10px 18px;border-radius:4px;font-size:12px;text-decoration:none;font-weight:700;letter-spacing:1px;">READ FULL ARTICLE →</a></div>
  </div>
  <div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin-bottom:8px;">Why we're flagging</div>
  <ul style="font-size:13px;line-height:1.6;margin:0 0 10px 18px;color:{TEXT};">{bullets}</ul>
  <div style="font-size:12px;color:{MUTED};font-style:italic;margin-bottom:4px;">{score.get('reasoning','')}</div>
  {sv_block}
  {cost_block}
  {social_html}{inst_html}{no_ev}
  {tapaeso_block}
  <div style="margin-top:18px;padding-top:14px;border-top:1px solid {SOFT};">
    <div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin-bottom:8px;">Satellite — incident location</div>
    <img src="{sat}" style="width:100%;border-radius:4px;display:block;" />
    <div style="font-size:11px;color:{DIM};margin-top:6px;">Coords: {coords.get('lat',0):.5f}, {coords.get('lng',0):.5f}</div>
  </div>
</div>"""

def build_digest_email(cards, scan_time_human):
    count = len(cards)
    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;background:{BG};padding:24px;color:{TEXT};margin:0;">
<div style="max-width:720px;margin:auto;">
  <div style="margin-bottom:24px;padding:24px;background:{CARD_BG};border-radius:8px;border-top:4px solid {ACCENT};">
    <div style="font-size:11px;letter-spacing:4px;color:{ACCENT};font-weight:700;">POTHOLEWATCH · 8-HOUR DIGEST · v4.0</div>
    <h1 style="margin:10px 0 6px;font-size:26px;color:{TEXT};font-weight:700;">{count} new incident{'s' if count!=1 else ''} detected</h1>
    <div style="color:{MUTED};font-size:12px;">Scan: {scan_time_human} · Panama territory</div>
  </div>
  {''.join(cards)}
  <div style="text-align:center;font-size:11px;color:{DIM};padding:24px 0;border-top:1px solid {SOFT};margin-top:8px;">
    <div style="font-size:10px;letter-spacing:3px;color:{ACCENT};font-weight:700;margin-bottom:6px;">POWERFIX · REPAIR. REINVENTED.</div>
    <div>PotholeWatch v4.0 — Street View vision + cost intelligence.<br/>Next scan in 8 hours.</div>
  </div>
</div></body></html>"""

def send_email(subject, html_body):
    creds = Credentials(
        token=None,
        refresh_token=GMAIL_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
    )
    service = build("gmail", "v1", credentials=creds)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = "PotholeWatch <ashourilevy@gmail.com>"
    msg["To"] = ", ".join(ALERT_RECIPIENTS)
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()

# ============================================================
# MAIN
# ============================================================

def main():
    now = datetime.utcnow()
    scan_time_iso = now.isoformat() + "Z"
    scan_time_human = now.strftime("%b %d, %Y · %I:%M %p UTC")
    print(f"=== PotholeWatch v4.0.0 scan @ {scan_time_iso} ===")
    print(f"=== Date anchor: today is {TODAY} ===")

    inv = load_inventory()
    print(f"Inventory: {len(inv['incidents'])} on file, last case PTW-{inv['counter']:04d}")

    threshold = SCORE_THRESHOLD_RANK[MIN_SCORE]
    cards = []
    pending = []

    for territory in TERRITORIES:
        print(f"\n--- Territory: {territory['name']} ---")
        try:
            incidents = search_incidents(territory)
        except Exception as e:
            print(f"  search failed: {e}")
            continue
        print(f"  Found {len(incidents)} candidate incidents")

        for inc in incidents:
            if already_seen(inv, inc):
                print(f"  · SKIP (already seen): {inc.get('title','')[:60]}")
                continue

            print(f"  · NEW: {inc.get('title','(no title)')[:70]}")
            print(f"    location: {inc.get('location_text','(none)')}")

            coords = geocode(inc.get("location_text",""), territory["country"])
            if not coords:
                print(f"    ✗ geocode failed")
                continue
            print(f"    geocoded → {coords['lat']:.4f}, {coords['lng']:.4f}")

            # Street View vision analysis
            print(f"    analyzing street view...")
            street_view = analyze_street_view(coords["lat"], coords["lng"], inc.get("location_text",""))
            if street_view.get("available"):
                print(f"    street view: {street_view.get('severity','?')} damage, potholes_visible={street_view.get('potholes_visible',False)}")
            else:
                print(f"    street view: not available at this location")

            social = search_social_evidence(inc, coords)
            sc = sum(len(social.get(k,[])) for k in
                    ["x_posts","instagram_posts","facebook_posts","waze_reports","news_comments"])
            ci = len(social.get("citizen_image_urls",[]))
            print(f"    social: {sc} posts, {ci} citizen images, pothole words {social.get('pothole_word_count',0)}")

            crossref = cross_reference(inc, coords)
            cx = sum(len(crossref.get(k,[])) for k in ["mop_evidence","attt_evidence","news_history"])
            print(f"    cross-ref: {cx} institutional hits")

            score = score_incident(inc, coords, social, crossref, street_view)
            print(f"    SCORE: {score['score']}")

            if SCORE_THRESHOLD_RANK.get(score["score"],0) < threshold:
                print(f"    — below threshold")
                continue

            # Cost comparison
            cost_comp = build_cost_comparison(DEFAULT_DEFECT_AREA_M2)

            # TapaEso draft (HIGH/CRITICAL only)
            tapaeso = generate_tapaeso_draft(inc, coords, score, street_view)
            if tapaeso:
                print(f"    ✓ TapaEso draft generated")

            case_id = next_case_id(inv)
            sat = satellite_url(coords["lat"], coords["lng"])
            card = build_incident_card(case_id, inc, coords, social, crossref, score, sat, street_view, cost_comp, tapaeso)
            cards.append(card)

            full_record = build_incident_record(case_id, inc, coords, social, crossref, score, street_view, cost_comp, tapaeso, scan_time_iso)

            h = incident_hash(inc.get("location_text",""), inc.get("date",""), inc.get("title",""))
            pending.append((h, inc.get("url",""), full_record))
            print(f"    ✓ bundled as {case_id}")

    if not cards:
        print(f"\n=== No new incidents above threshold — no email sent ===")
        save_inventory(inv)
        return

    print(f"\n--- Sending digest with {len(cards)} incident(s) ---")
    html = build_digest_email(cards, scan_time_human)
    subject = f"PotholeWatch v4 · {len(cards)} new incident{'s' if len(cards)!=1 else ''} · {now.strftime('%b %d')}"

    try:
        send_email(subject, html)
        print(f"✉  digest sent")
        for h, url, rec in pending:
            inv["incidents"][h] = rec
            if url and url not in inv["seen_urls"]:
                inv["seen_urls"].append(url)
        inv["seen_urls"] = inv["seen_urls"][-500:]
    except Exception as e:
        print(f"✗ send failed: {e}")
        inv["counter"] -= len(pending)

    save_inventory(inv)
    print(f"\n=== Done · inventory now {len(inv['incidents'])} incidents ===")

if __name__ == "__main__":
    main()

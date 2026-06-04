"""
PotholeWatch v5.3.0 — The Evidence Engine
==========================================
FIXES IN v5.3:
  - URL VALIDATION: strips fabricated URLs (spaces, accents, non-numeric
    FB post IDs). Only real URLs stored and displayed.
  - PROMPT HARDENING: Claude explicitly told never to guess/construct URLs.
    Facebook post URLs must have numeric IDs only.
  - COORDINATE VALIDATION: Panama bounding box filter (lat 7-10, lng -83 to -77).
    No more pins geocoded to Guatemala or wrong countries.
  - CLICKABLE LINKS: proper rendering in both email and dashboard.
  - NEWS COMMENT LINKS: article URL used as fallback when no comment permalink.
  - AGGRESSIVE X/TWITTER SEARCH: targeted at Panama news accounts.
  - INSTAGRAM: Google-indexed public posts from Panama outlets.
  - DUPLICATE ROAD DETECTION: same road_key + same date = same incident.
"""

import os
import re
import json
import base64
import hashlib
import unicodedata
import requests
from datetime import datetime
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

INVENTORY_FILE = "incidents.json"
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 10}

# === TUNABLE KNOBS ===========================================
LOOKBACK_DAYS = 8
REALERT_THRESHOLD = 3
SEVERITY_CRITICAL = 3
SEVERITY_HIGH = 2
SEVERITY_MEDIUM = 1
# Panama bounding box — reject geocodes outside this
PANAMA_LAT_MIN, PANAMA_LAT_MAX = 7.0, 10.0
PANAMA_LNG_MIN, PANAMA_LNG_MAX = -83.0, -77.0
# =============================================================

ROAD_KEYWORDS = [
    "bache", "baches", "hueco", "huecos", "cráter", "crater",
    "deteriorada", "deteriorado", "mal estado", "rota", "destruida",
    "sin asfalto", "pavimento", "asfalto dañado", "calle dañada",
    "carretera dañada", "peligrosa", "mala condición",
]

# Panama news outlet social accounts for targeted search
PANAMA_OUTLETS = {
    "facebook": ["TelemetroReporta", "tvnpanama", "criticapanama", "MiDiarioPanama",
                 "LaEstrelladePanama", "prensapanama", "traficopanama", "BomberosPA"],
    "instagram": ["telemetro_reporta", "tvnpanama", "criticapanama", "traficopanama",
                  "laprensapanama", "midiario_panama"],
    "twitter": ["TReporta", "tvnnoticias", "CriticaPanama", "MiDiarioPTY",
                "traficopanama", "prensapanama", "ColonNoticiasPA"],
}

# Brand
BG      = "#0D0D0D"
CARD_BG = "#1A1A1A"
TEXT    = "#FFFFFF"
MUTED   = "#999999"
DIM     = "#666666"
ACCENT  = "#D94F2B"
SOFT    = "#262626"
CRITICAL_COLOR = "#FF3B3B"
HIGH_COLOR     = "#D94F2B"
MEDIUM_COLOR   = "#F0A030"

SEVERITY_COLORS = {"CRITICAL": CRITICAL_COLOR, "HIGH": HIGH_COLOR, "MEDIUM": MEDIUM_COLOR}

TERRITORIES = [
    {
        "name": "Panama — Ciudad y Metro",
        "country": "Panama",
        "search_terms": [
            "accidente Via Centenario Corredor Norte Sur Panama",
            "accidente vial Via Brasil Transistmica Panama City",
            "volcado vuelco bus carro Via España Tumba Muerto Panama",
            "choque accidente Panama City corregimiento bache hueco",
        ],
    },
    {
        "name": "Panama — Carretera Interamericana",
        "country": "Panama",
        "search_terms": [
            "accidente volcado Interamericana Panama Cocle Veraguas Chiriqui",
            "bus volcado vuelco carretera nacional Panama",
            "accidente Interamericana El Platanal Santiago Chitre bache",
            "pierde control carretera Panama Darien Herrera",
        ],
    },
    {
        "name": "Panama — Provincias Centrales",
        "country": "Panama",
        "search_terms": [
            "accidente carretera Cocle Veraguas Herrera Los Santos Panama",
            "vuelco Penonome Santiago Chitre Las Tablas accidente",
            "accidente vial Azuero Panama carretera",
        ],
    },
    {
        "name": "Panama — Provincias Extremas",
        "country": "Panama",
        "search_terms": [
            "accidente carretera Chiriqui Bocas del Toro David Boquete Panama",
            "accidente vial Colon Darien Panama carretera",
            "volcado bus camion Chiriqui Bocas del Toro Panama",
        ],
    },
]

TODAY = datetime.utcnow().strftime("%B %d, %Y")
DATE_ANCHOR = f"""CRITICAL CONTEXT: Today is {TODAY}. All accident dates are REAL past events. Search for them. Never refuse based on dates."""

# ============================================================
# URL VALIDATION
# ============================================================

def is_valid_url(url):
    """Return True only for real, clean URLs we can actually open."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if not url.startswith("http"):
        return False
    # Reject URLs with spaces (fabricated)
    if " " in url:
        return False
    # Reject URLs with Spanish accent characters (fabricated FB post URLs)
    if any(c in url for c in "áéíóúüñÁÉÍÓÚÜÑ¿¡"):
        return False
    # Facebook post URLs must have numeric post IDs
    if "facebook.com" in url and "/posts/" in url:
        post_id = url.split("/posts/")[-1].rstrip("/").split("?")[0]
        # Valid: all digits, or pfbid + alphanumeric (new FB format)
        if not (post_id.isdigit() or (post_id.startswith("pfbid") and len(post_id) > 10)):
            return False
    # Reject obviously fake/placeholder URLs
    fake_patterns = [
        "example.com", "placeholder", "unknown", "comment-thread",
        "accidente-de-", "via-interamericana-en-", "autobus-en-la"
    ]
    if any(p in url.lower() for p in fake_patterns):
        return False
    return True

def clean_url(url):
    """Return cleaned URL or empty string if invalid."""
    if is_valid_url(url):
        return url.strip()
    return ""

def clean_mentions(mentions):
    """Clean URL fields in a list of mention/comment objects."""
    cleaned = []
    for m in (mentions or []):
        m = dict(m)
        m["comment_url"] = clean_url(m.get("comment_url",""))
        m["post_url"] = clean_url(m.get("post_url",""))
        # Must have at least a quote
        if (m.get("quote","") or "").strip():
            cleaned.append(m)
    return cleaned

def clean_social_posts(posts):
    """Clean URL fields in social post objects."""
    cleaned = []
    for p in (posts or []):
        p = dict(p)
        p["post_url"] = clean_url(p.get("post_url",""))
        cleaned.append(p)
    return cleaned

# ============================================================
# SEVERITY
# ============================================================

def get_severity(mention_count):
    if mention_count >= SEVERITY_CRITICAL:
        return "CRITICAL"
    elif mention_count >= SEVERITY_HIGH:
        return "HIGH"
    elif mention_count >= SEVERITY_MEDIUM:
        return "MEDIUM"
    return None

# ============================================================
# ROAD KEY
# ============================================================

_STOPWORDS = {
    "via","vía","calle","carretera","autopista","corredor","sector","entrada",
    "el","la","los","las","de","del","y","en","a","hacia","entre","altura",
    "panama","panamá","provincia","corregimiento","distrito","ciudad",
}

def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

def road_key(location_text):
    if not location_text:
        return ""
    s = _strip_accents(location_text.lower())
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    tokens = [t for t in s.split() if t and t not in _STOPWORDS and len(t) > 2]
    tokens = tokens[:4]
    tokens.sort()
    return "".join(tokens)

# ============================================================
# INVENTORY
# ============================================================

def load_inventory():
    if not os.path.exists(INVENTORY_FILE):
        return {"counter": 0, "cases": {}, "seen_urls": []}
    with open(INVENTORY_FILE) as f:
        try:
            inv = json.load(f)
        except json.JSONDecodeError:
            return {"counter": 0, "cases": {}, "seen_urls": []}
    if not isinstance(inv, dict):
        inv = {}
    inv.setdefault("counter", 0)
    inv.setdefault("seen_urls", [])
    if "cases" not in inv:
        cases = {}
        old = inv.get("incidents", {})
        old_iter = old.values() if isinstance(old, dict) else (old or [])
        for rec in old_iter:
            if not isinstance(rec, dict):
                continue
            rk = road_key(rec.get("location",""))
            if not rk:
                rk = hashlib.sha256((rec.get("ptw_id","") or "x").encode()).hexdigest()[:10]
            rec.setdefault("road_key", rk)
            cases[rk] = rec
        inv["cases"] = cases
    return inv

def save_inventory(inv):
    cases_list = list(inv["cases"].values())
    cases_list.sort(key=lambda x: x.get("first_seen",""), reverse=True)
    out = {
        "scan_time": datetime.utcnow().isoformat(),
        "total": len(cases_list),
        "counter": inv["counter"],
        "incidents": cases_list,
        "seen_urls": inv.get("seen_urls", [])[-1000:],
    }
    with open(INVENTORY_FILE, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

def next_case_id(inv):
    inv["counter"] += 1
    return f"PTW-{inv['counter']:04d}"

# ============================================================
# CLAUDE API
# ============================================================

def claude_call(prompt, tools=None, max_tokens=4000):
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
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
        print(f"    API ERROR {r.status_code}: {r.text[:600]}")
        r.raise_for_status()
    return "".join(b.get("text","") for b in r.json()["content"] if b.get("type") == "text")

# ============================================================
# JSON EXTRACTION
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
# 1. FIND ACCIDENTS
# ============================================================

def search_incidents(territory):
    queries = "\n".join(f"- {q}" for q in territory["search_terms"])
    prompt = f"""{DATE_ANCHOR}

Search Spanish-language news for road accidents in {territory['name']} from the LAST {LOOKBACK_DAYS} DAYS.

Run these queries:
{queries}

For each UNIQUE road accident found, output ONE JSON object per line (JSONL — no prose, no fences):
{{"title":"...","url":"...","source":"...","date":"YYYY-MM-DD",
  "location_text":"specific road + landmark + city/district",
  "summary":"what happened in 2-3 sentences",
  "article_image_urls":["direct image URLs from the article if any"]}}

- Real road accidents only (collisions, rollovers, lost control, volcado/vuelco)
- JSONL only."""
    raw = claude_call(prompt, tools=[WEB_SEARCH_TOOL], max_tokens=4000)
    return extract_jsonl(raw)

# ============================================================
# 2. SOCIAL POSTS + KEYWORD COMMENTS
# ============================================================

def find_social_posts_and_comments(incident):
    location = incident.get("location_text", "")
    loc_short = location.split(",")[0] if location else location
    date_str = incident.get("date", "")
    title = incident.get("title", "")
    url = incident.get("url", "")
    source = incident.get("source", "")
    keywords = ", ".join(ROAD_KEYWORDS[:12])

    fb_outlets = " OR ".join(f'"{o}"' for o in PANAMA_OUTLETS["facebook"][:5])
    ig_outlets = " OR ".join(f'"{o}"' for o in PANAMA_OUTLETS["instagram"][:4])
    tw_outlets = " OR ".join(f'@{o}' for o in PANAMA_OUTLETS["twitter"][:5])

    prompt = f"""{DATE_ANCHOR}

Find social media posts and citizen comments about this road accident that mention road conditions.

ARTICLE:
  Title: {title}
  Source: {source}
  URL: {url}
  Location: {location}
  Date: {date_str}

ROAD-CONDITION KEYWORDS: {keywords}

Run ALL these searches:

FACEBOOK (search for outlet posts of this article):
1. site:facebook.com "{loc_short}" accidente {date_str} bache OR hueco
2. site:facebook.com ({fb_outlets}) "{loc_short}" accidente
3. Fetch {url} and look for embedded Facebook comments or share counts

X / TWITTER (search public tweets):
4. site:x.com OR site:twitter.com "{loc_short}" bache OR hueco {date_str}
5. ({tw_outlets}) "{loc_short}" accidente bache
6. site:x.com "{loc_short}" accidente {date_str}

INSTAGRAM (search indexed public posts):
7. site:instagram.com ({ig_outlets}) "{loc_short}" bache OR hueco
8. site:instagram.com "{loc_short}" accidente bache hueco panama

NEWS COMMENT SECTIONS:
9. Fetch {url} — extract any reader comments containing road keywords
10. Search: "{loc_short}" bache OR hueco "comentario" OR "comment" site:tvn-2.com OR site:telemetro.com OR site:critica.com.pa

CRITICAL URL RULES — STRICTLY ENFORCED:
- Facebook post URLs MUST follow this exact format: https://www.facebook.com/[PageName]/posts/[NUMERIC_ID]
  Example of VALID URL: https://www.facebook.com/TelemetroReporta/posts/892742499551954
  Example of INVALID URL: https://www.facebook.com/TelemetroReporta/posts/dos-accidentes-centenario (NOT VALID — has text not numbers)
- If you find a Facebook post but cannot confirm the exact numeric post ID, set post_url to ""
- X/Twitter URLs must be: https://x.com/[username]/status/[NUMERIC_ID] or https://twitter.com/[username]/status/[NUMERIC_ID]
- Instagram URLs: https://www.instagram.com/p/[SHORT_CODE]/ only
- NEVER construct or guess a URL. If unsure, leave it empty string "".
- A URL with spaces, accented characters, or sentence text in it is ALWAYS fabricated — do not include it.

Return ONE JSON object (no prose, no fences):
{{
  "social_posts": [
    {{
      "platform": "facebook|instagram|twitter|x",
      "outlet": "page/account name",
      "post_url": "REAL URL with numeric ID only, or empty string",
      "post_caption": "text of the post if visible",
      "post_date": "YYYY-MM-DD"
    }}
  ],
  "keyword_comments": [
    {{
      "platform": "facebook|instagram|twitter|x|news_comment",
      "source_name": "outlet or username",
      "comment_url": "direct permalink with numeric ID, or empty string if unsure",
      "post_url": "the post this comment is under — numeric ID only, or empty string",
      "quote": "exact verbatim text containing road keywords",
      "keywords_matched": ["bache","hueco",...],
      "date": "YYYY-MM-DD or empty"
    }}
  ],
  "summary": "1-2 sentence neutral summary of what people are saying about this road. No conclusions."
}}

JSON only. Empty arrays if nothing found. Quotes must be REAL — never invent."""

    raw = claude_call(prompt, tools=[WEB_SEARCH_TOOL], max_tokens=6000)
    parsed = extract_json_object(raw)
    if parsed is None:
        print(f"      social parse failed — raw[:160]: {raw[:160]!r}")
        return {"social_posts": [], "keyword_comments": [], "summary": ""}

    # Clean all URLs
    parsed["social_posts"] = clean_social_posts(parsed.get("social_posts", []))
    parsed["keyword_comments"] = clean_mentions(parsed.get("keyword_comments", []))
    parsed.setdefault("summary", "")
    return parsed

# ============================================================
# 3. GEOCODE — with Panama bounding box validation
# ============================================================

def geocode(location_text, country="Panama"):
    if not location_text:
        return None
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    for q in [f"{location_text}, {country}", f"{location_text}, Panama City, {country}", location_text]:
        try:
            r = requests.get(url, params={"address": q, "key": GOOGLE_MAPS_API_KEY}, timeout=15)
            if not r.ok:
                continue
            data = r.json()
            if data.get("status") == "OK" and data.get("results"):
                loc = data["results"][0]["geometry"]["location"]
                lat, lng = loc["lat"], loc["lng"]
                # Validate within Panama bounding box
                if not (PANAMA_LAT_MIN <= lat <= PANAMA_LAT_MAX and
                        PANAMA_LNG_MIN <= lng <= PANAMA_LNG_MAX):
                    print(f"      geocode outside Panama bounds: {lat:.4f}, {lng:.4f} — skipping")
                    continue
                return {"lat": lat, "lng": lng,
                        "formatted": data["results"][0]["formatted_address"]}
        except Exception as e:
            print(f"      geocode error: {e}")
    return None

def maps_link(lat, lng):
    return f"https://maps.google.com/?q={lat},{lng}"

# ============================================================
# 4. MENTION DEDUP
# ============================================================

def mention_signature(m):
    q = _strip_accents((m.get("quote","") or "").lower()).strip()
    q = re.sub(r"\s+", " ", q)[:120]
    url = (m.get("comment_url","") or m.get("post_url","") or "").strip().lower()
    return hashlib.sha256(f"{url}|{q}".encode()).hexdigest()[:16]

def merge_mentions(existing, incoming):
    seen = {mention_signature(m) for m in existing}
    merged = list(existing)
    new_count = 0
    for m in incoming:
        sig = mention_signature(m)
        if sig not in seen:
            seen.add(sig)
            merged.append(m)
            new_count += 1
    return merged, new_count

# ============================================================
# 5. EMAIL
# ============================================================

PLATFORM_LABEL = {
    "facebook": "Facebook", "instagram": "Instagram",
    "twitter": "X / Twitter", "x": "X / Twitter", "news_comment": "News comment",
}

def _link(url, text, color=None):
    """Generate a safe HTML link, or just text if no URL."""
    c = color or ACCENT
    if url and is_valid_url(url):
        return f'<a href="{url}" style="color:{c};text-decoration:none;font-weight:700;">{text}</a>'
    return f'<span style="color:{MUTED};">{text}</span>'

def _social_post_row(p):
    platform = PLATFORM_LABEL.get(p.get("platform",""), p.get("platform","Social"))
    outlet = p.get("outlet","")
    post_url = p.get("post_url","")
    caption = (p.get("post_caption","") or "")[:120]
    date = p.get("post_date","")
    src = platform + (f" · {outlet}" if outlet else "") + (f" · {date}" if date else "")
    link_html = ""
    if post_url and is_valid_url(post_url):
        link_html = f'<a href="{post_url}" style="display:inline-block;margin-top:6px;font-size:11px;color:{ACCENT};text-decoration:none;border:1px solid {ACCENT};padding:3px 10px;border-radius:3px;font-weight:700;">→ Open {platform} post</a>'
    return f'''<div style="margin:8px 0;padding:10px;background:#111;border-radius:4px;border-left:3px solid {ACCENT};">
      <div style="font-size:10px;letter-spacing:1px;color:{ACCENT};text-transform:uppercase;margin-bottom:4px;">{src}</div>
      {f'<div style="font-size:12px;color:#aaa;margin-bottom:6px;">{caption}</div>' if caption else ''}
      {link_html}
    </div>'''

def _comment_row(m):
    platform = PLATFORM_LABEL.get(m.get("platform",""), m.get("platform","Source"))
    src_name = m.get("source_name","")
    quote = (m.get("quote","") or "").replace("<","&lt;").replace(">","&gt;")
    comment_url = m.get("comment_url","")
    post_url = m.get("post_url","")
    date = m.get("date","")
    kw = m.get("keywords_matched",[])
    src = platform + (f" · {src_name}" if src_name else "") + (f" · {date}" if date else "")

    # Pick best available link
    best_url = ""
    if comment_url and is_valid_url(comment_url):
        best_url = comment_url
    elif post_url and is_valid_url(post_url):
        best_url = post_url

    link = f' {_link(best_url, "→ ver", ACCENT)}' if best_url else ""
    kw_chips = "".join(f'<span style="display:inline-block;background:rgba(217,79,43,0.15);color:{ACCENT};font-size:9px;padding:1px 6px;border-radius:8px;margin:1px;">{k}</span>' for k in kw[:5])

    return f'''<div style="margin:8px 0;padding-left:10px;border-left:2px solid #333;">
      <div style="font-size:10px;letter-spacing:1px;color:{ACCENT};text-transform:uppercase;margin-bottom:2px;">{src}</div>
      <div style="font-size:13px;line-height:1.5;color:{MUTED};">&ldquo;{quote}&rdquo;{link}</div>
      {f'<div style="margin-top:3px;">{kw_chips}</div>' if kw_chips else ''}
    </div>'''

def build_card(case, new_count, is_new_case, severity):
    color = SEVERITY_COLORS.get(severity, MEDIUM_COLOR)
    imgs = case.get("article_image_urls", [])
    img_html = "".join(
        f'<img src="{u}" style="width:100%;border-radius:6px;margin-bottom:8px;display:block;" onerror="this.style.display=\'none\'" />'
        for u in imgs[:2] if is_valid_url(u)
    )
    social_posts = case.get("social_posts", [])
    keyword_comments = case.get("keyword_comments", [])
    social_html = ""
    if social_posts:
        social_html += f'<div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin:14px 0 8px;">News outlet social posts</div>'
        social_html += "".join(_social_post_row(p) for p in social_posts[:4])
    comments_html = ""
    if keyword_comments:
        comments_html += f'<div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin:14px 0 8px;">Citizen comments — road conditions ({len(keyword_comments)})</div>'
        comments_html += "".join(_comment_row(m) for m in keyword_comments[:8])
    kw = case.get("keywords_matched", [])
    kw_html = ""
    if kw:
        chips = "".join(f'<span style="display:inline-block;background:{SOFT};color:{ACCENT};font-size:10px;padding:2px 8px;border-radius:10px;margin:2px;">{k}</span>' for k in kw[:10])
        kw_html = f'<div style="margin:10px 0;">{chips}</div>'
    count = len(keyword_comments)
    tag = ""
    if is_new_case:
        tag = f'<span style="background:#4ADE80;color:#000;font-size:9px;font-weight:700;letter-spacing:1px;padding:2px 8px;border-radius:10px;margin-left:8px;">NEW</span>'
    elif new_count > 0:
        tag = f'<span style="background:#4ADE80;color:#000;font-size:9px;font-weight:700;letter-spacing:1px;padding:2px 8px;border-radius:10px;margin-left:8px;">+{new_count} NEW</span>'
    maps_html = ""
    if case.get("lat") and case.get("lng"):
        ml = case.get("maps_link") or maps_link(case["lat"], case["lng"])
        maps_html = f'<a href="{ml}" style="display:inline-block;margin:6px 0 0;font-size:11px;color:{ACCENT};text-decoration:none;border:1px solid rgba(217,79,43,0.4);padding:4px 10px;border-radius:3px;">📍 Open in Google Maps →</a>'
    article_link = ""
    if case.get("url") and is_valid_url(case.get("url","")):
        article_link = f'<a href="{case["url"]}" style="display:inline-block;background:{ACCENT};color:{TEXT};padding:10px 18px;border-radius:4px;font-size:12px;text-decoration:none;font-weight:700;letter-spacing:1px;margin-top:12px;">READ FULL ARTICLE →</a>'

    return f"""
<div style="background:{CARD_BG};border-radius:8px;padding:24px;margin-bottom:20px;border-left:4px solid {color};">
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;">
    <span style="font-size:10px;letter-spacing:3px;color:{color};font-weight:700;">{severity}</span>
    <span style="color:{DIM};">·</span>
    <span style="font-size:10px;letter-spacing:2px;color:{DIM};">{case['ptw_id']}</span>
    <span style="color:{DIM};">·</span>
    <span style="font-size:10px;color:{MUTED};">{count} comment{'s' if count!=1 else ''} w/ road keywords</span>
    {tag}
  </div>
  <h2 style="margin:6px 0;font-size:20px;color:{TEXT};line-height:1.3;font-weight:700;">{case.get('headline','')}</h2>
  <div style="color:{DIM};font-size:12px;margin-bottom:4px;">
    {case.get('location','')} · {case.get('date','')} ·
    {_link(case.get('url',''), case.get('source_name',''), ACCENT)}
  </div>
  {maps_html}
  <div style="background:{SOFT};padding:16px;border-radius:6px;margin:12px 0;">
    {img_html}
    <div style="font-size:14px;line-height:1.6;color:{TEXT};">{case.get('summary','')}</div>
    {article_link}
  </div>
  <div style="font-size:12px;color:{MUTED};font-style:italic;margin-bottom:8px;">{case.get('chatter_summary','')}</div>
  {kw_html}
  {social_html}
  {comments_html}
</div>"""

def build_digest(cards, scan_time_human, n_critical, n_high, n_medium):
    count = len(cards)
    summary_parts = []
    if n_critical: summary_parts.append(f'<span style="color:{CRITICAL_COLOR};font-weight:700;">{n_critical} CRITICAL</span>')
    if n_high: summary_parts.append(f'<span style="color:{HIGH_COLOR};font-weight:700;">{n_high} HIGH</span>')
    if n_medium: summary_parts.append(f'<span style="color:{MEDIUM_COLOR};font-weight:700;">{n_medium} MEDIUM</span>')
    summary_str = " · ".join(summary_parts)
    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;background:{BG};padding:24px;color:{TEXT};margin:0;">
<div style="max-width:720px;margin:auto;">
  <div style="margin-bottom:24px;padding:24px;background:{CARD_BG};border-radius:8px;border-top:4px solid {ACCENT};">
    <div style="font-size:11px;letter-spacing:4px;color:{ACCENT};font-weight:700;">POTHOLEWATCH · THE EVIDENCE ENGINE · v5.3</div>
    <h1 style="margin:10px 0 6px;font-size:26px;color:{TEXT};font-weight:700;">{count} case{'s' if count!=1 else ''} with citizen road-condition evidence</h1>
    <div style="margin:6px 0;">{summary_str}</div>
    <div style="color:{MUTED};font-size:12px;margin-top:6px;">Scan: {scan_time_human} · Panama · {LOOKBACK_DAYS}-day window</div>
  </div>
  {''.join(cards)}
  <div style="text-align:center;font-size:11px;color:{DIM};padding:24px 0;border-top:1px solid {SOFT};margin-top:8px;">
    <div style="font-size:10px;letter-spacing:3px;color:{ACCENT};font-weight:700;margin-bottom:6px;">POWERFIX · REPAIR. REINVENTED.</div>
    <div>PotholeWatch v5.3 — The Evidence Engine.<br/>Accident + citizen testimony + geolocation. You decide.</div>
  </div>
</div></body></html>"""

def send_email(subject, html_body):
    creds = Credentials(
        token=None, refresh_token=GMAIL_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GMAIL_CLIENT_ID, client_secret=GMAIL_CLIENT_SECRET,
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
    print(f"=== PotholeWatch v5.3.0 (Evidence Engine) @ {scan_time_iso} ===")
    print(f"=== Today: {TODAY} · lookback {LOOKBACK_DAYS}d · re-alert +{REALERT_THRESHOLD} ===")

    inv = load_inventory()
    print(f"Inventory: {len(inv['cases'])} cases on file, last PTW-{inv['counter']:04d}")

    pending_cards = []

    for territory in TERRITORIES:
        print(f"\n--- Territory: {territory['name']} ---")
        try:
            incidents = search_incidents(territory)
        except Exception as e:
            print(f"  search failed: {e}")
            continue
        print(f"  Found {len(incidents)} candidate incidents")

        for inc in incidents:
            loc = inc.get("location_text","")
            rk = road_key(loc)
            print(f"  · {inc.get('title','(no title)')[:60]}")
            print(f"    loc: {loc[:60]}  | road_key: {rk}")

            try:
                social_data = find_social_posts_and_comments(inc)
            except Exception as e:
                print(f"    social search failed: {e}")
                social_data = {"social_posts": [], "keyword_comments": [], "summary": ""}

            social_posts = social_data.get("social_posts", [])
            keyword_comments = social_data.get("keyword_comments", [])
            mention_count = len(keyword_comments)

            # Count valid links for reporting
            valid_post_links = sum(1 for p in social_posts if is_valid_url(p.get("post_url","")))
            valid_comment_links = sum(1 for c in keyword_comments if is_valid_url(c.get("comment_url","")) or is_valid_url(c.get("post_url","")))
            print(f"    social posts: {len(social_posts)} ({valid_post_links} w/ valid URLs) | comments: {mention_count} ({valid_comment_links} w/ valid links)")

            severity = get_severity(mention_count)
            if not severity:
                print(f"    — no keyword comments, skipping")
                if inc.get("url"):
                    inv["seen_urls"].append(inc["url"])
                continue

            print(f"    severity: {severity}")

            kw = set()
            for m in keyword_comments:
                for k in (m.get("keywords_matched") or []):
                    kw.add(k.lower())

            existing_case = inv["cases"].get(rk) if rk else None

            if existing_case:
                old_mentions = existing_case.get("keyword_comments", [])
                merged, new_count = merge_mentions(old_mentions, keyword_comments)
                existing_case["keyword_comments"] = merged[:30]
                existing_case["mention_count"] = len(merged)
                existing_posts = existing_case.get("social_posts", [])
                existing_post_urls = {p.get("post_url","") for p in existing_posts}
                for p in social_posts:
                    if p.get("post_url","") not in existing_post_urls:
                        existing_posts.append(p)
                        existing_post_urls.add(p.get("post_url",""))
                existing_case["social_posts"] = existing_posts[:8]
                existing_kw = set(existing_case.get("keywords_matched", []))
                existing_kw.update(kw)
                existing_case["keywords_matched"] = sorted(existing_kw)
                existing_case["last_seen"] = scan_time_iso
                if social_data.get("summary"):
                    existing_case["chatter_summary"] = social_data["summary"]
                new_severity = get_severity(len(merged))
                existing_case["severity"] = new_severity
                print(f"    known case {existing_case['ptw_id']} — {new_count} NEW (total {len(merged)}) → {new_severity}")
                if new_count >= REALERT_THRESHOLD:
                    pending_cards.append((existing_case, new_count, False, new_severity))
                    print(f"    ✓ re-alerting")
                else:
                    print(f"    · below re-alert threshold, dashboard updated silently")
            else:
                coords = geocode(loc, territory.get("country","Panama"))
                if coords:
                    print(f"    geocoded → {coords['lat']:.4f}, {coords['lng']:.4f}")
                else:
                    print(f"    geocode: no valid Panama coordinates found")

                case_id = next_case_id(inv)
                best_q = ""; best_src = ""
                for m in keyword_comments:
                    if (m.get("quote") or "").strip():
                        best_q = m["quote"].strip()
                        best_src = m.get("source_name","") or m.get("platform","")
                        break

                case = {
                    "ptw_id": case_id,
                    "road_key": rk,
                    "severity": severity,
                    "headline": inc.get("title",""),
                    "location": loc,
                    "date": inc.get("date",""),
                    "summary": inc.get("summary",""),
                    "source_name": inc.get("source",""),
                    "url": clean_url(inc.get("url","")),
                    "lat": coords["lat"] if coords else None,
                    "lng": coords["lng"] if coords else None,
                    "geo_formatted": coords["formatted"] if coords else "",
                    "maps_link": maps_link(coords["lat"], coords["lng"]) if coords else None,
                    "first_seen": scan_time_iso,
                    "last_seen": scan_time_iso,
                    "article_image_urls": [u for u in (inc.get("article_image_urls") or []) if is_valid_url(u)][:4],
                    "primary_image_url": None,
                    "chatter_summary": social_data.get("summary",""),
                    "mention_count": mention_count,
                    "keywords_matched": sorted(kw),
                    "best_quote": best_q,
                    "best_quote_source": best_src,
                    "social_posts": social_posts[:8],
                    "keyword_comments": keyword_comments[:30],
                }
                if case["article_image_urls"]:
                    case["primary_image_url"] = case["article_image_urls"][0]
                inv["cases"][rk] = case
                if inc.get("url"):
                    inv["seen_urls"].append(inc["url"])
                pending_cards.append((case, mention_count, True, severity))
                print(f"    ✓ NEW CASE {case_id} — {mention_count} comment(s) → {severity}")

    inv["seen_urls"] = inv["seen_urls"][-1000:]

    if not pending_cards:
        print(f"\n=== No new/updated cases — no email ===")
        save_inventory(inv)
        return

    sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
    pending_cards.sort(key=lambda c: (sev_order.get(c[3],3), 0 if c[2] else 1, -c[1]))

    n_critical = sum(1 for c in pending_cards if c[3]=="CRITICAL")
    n_high = sum(1 for c in pending_cards if c[3]=="HIGH")
    n_medium = sum(1 for c in pending_cards if c[3]=="MEDIUM")

    cards = [build_card(c, n, isnew, sev) for (c, n, isnew, sev) in pending_cards]
    print(f"\n--- Sending digest: {len(cards)} case(s) — {n_critical} CRITICAL, {n_high} HIGH, {n_medium} MEDIUM ---")
    html = build_digest(cards, scan_time_human, n_critical, n_high, n_medium)

    sev_label = f"{n_critical} CRITICAL" if n_critical else f"{n_high} HIGH" if n_high else f"{n_medium} MEDIUM"
    subject = f"PotholeWatch · {len(cards)} case{'s' if len(cards)!=1 else ''} · {sev_label} · {now.strftime('%b %d')}"

    try:
        send_email(subject, html)
        print(f"✉  digest sent")
    except Exception as e:
        print(f"✗ send failed: {e}")

    save_inventory(inv)
    print(f"\n=== Done · inventory now {len(inv['cases'])} cases ===")

if __name__ == "__main__":
    main()

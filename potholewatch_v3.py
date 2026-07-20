"""
PotholeWatch v5.5.0 — The Evidence Engine + Instagram via Apify
===============================================================
NEW IN v5.5:
  - @traficopanama now scraped directly as a profile (like the news
    accounts) instead of via a dead Google-search prong — Google
    barely indexes Instagram, so that prong was costing a Claude
    call per incident and returning nothing
  - Instagram vs web-search comment/post counts are now logged
    separately so the numbers add up
NEW IN v5.4:
  - APIFY INSTAGRAM INTEGRATION: for each incident, scrapes
    @tvnnoticias, @criticapanama, @midiario_panama, @laprensapanama
    for recent posts mentioning the accident location, then extracts
    keyword-matching comments
  - All v5.3 fixes retained (URL validation, Panama bbox, etc.)
"""

import os
import re
import json
import time
import base64
import hashlib
import unicodedata
import requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ============================================================
# CONFIG
# ============================================================

ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_MAPS_API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]
GMAIL_CLIENT_ID    = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
GMAIL_REFRESH_TOKEN = os.environ["GMAIL_REFRESH_TOKEN"]
APIFY_API_TOKEN    = os.environ["APIFY_API_TOKEN"]

ALERT_RECIPIENTS = ["joel@powerfixinc.com", "1@powerfixinc.com"]

INVENTORY_FILE = "incidents.json"
CLAUDE_MODEL   = "claude-sonnet-4-5-20250929"
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}

# Apify
APIFY_INSTAGRAM_ACTOR = "apify~instagram-scraper"
APIFY_BASE = "https://api.apify.com/v2"

# Instagram news accounts that post recent Panama accident content
INSTAGRAM_NEWS_ACCOUNTS = [
    "https://www.instagram.com/tvnnoticias/",
    "https://www.instagram.com/criticapanama/",
    "https://www.instagram.com/midiario_panama/",
    "https://www.instagram.com/laprensapanama/",
    "https://www.instagram.com/traficopanama/",
]

# Tunable knobs
LOOKBACK_DAYS      = 8
REALERT_THRESHOLD  = 3
SEVERITY_CRITICAL  = 3
SEVERITY_HIGH      = 2
SEVERITY_MEDIUM    = 1

# Panama bounding box
PANAMA_LAT_MIN, PANAMA_LAT_MAX = 7.0, 10.0
PANAMA_LNG_MIN, PANAMA_LNG_MAX = -83.0, -77.0

ROAD_KEYWORDS = [
    "bache", "baches", "hueco", "huecos", "cráter", "crater",
    "deteriorada", "deteriorado", "mal estado", "rota", "destruida",
    "sin asfalto", "pavimento", "asfalto dañado", "calle dañada",
    "carretera dañada", "peligrosa", "mala condición",
]

# Relevance gate: a candidate article must read like a road/traffic incident,
# and must not read like general news (politics, war, sports, crime, showbiz).
INCIDENT_KEYWORDS = ROAD_KEYWORDS + [
    "accidente", "choque", "colisión", "colision", "vuelco", "volcado",
    "volcadura", "atropello", "atropellado", "atropellada", "derrape",
    "pierde el control", "perdió el control", "perdio el control",
    "salida de vía", "salida de via", "aparatoso", "tránsito", "transito",
    "vial", "carretera", "autopista", "corredor",
]
OFFTOPIC_KEYWORDS = [
    "elección", "eleccion", "electoral", "candidato", "campaña política",
    "campana politica", "diputado", "asamblea nacional",
    "guerra", "misil", "tropas", "bombardeo", "ataque militar",
    "fútbol", "futbol", "béisbol", "beisbol", "selección nacional",
    "seleccion nacional", "concierto", "farándula", "farandula",
    "homicidio", "pandilla", "narcotráfico", "narcotrafico", "balacera",
]

def is_relevant_incident(inc):
    """True only for articles that look like real road/traffic incidents."""
    text = " ".join([
        inc.get("title", ""), inc.get("summary", ""), inc.get("location_text", ""),
    ]).lower()
    if not any(k in text for k in INCIDENT_KEYWORDS):
        return False
    if any(k in text for k in OFFTOPIC_KEYWORDS):
        return False
    return True

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
        "name": "Panama — Ciudad y Metro", "country": "Panama",
        "search_terms": [
            "accidente Via Centenario Corredor Norte Sur Panama",
            "accidente vial Via Brasil Transistmica Panama City",
            "volcado vuelco bus carro Via España Tumba Muerto Panama",
            "choque accidente Panama City corregimiento bache hueco",
        ],
    },
    {
        "name": "Panama — Carretera Interamericana", "country": "Panama",
        "search_terms": [
            "accidente volcado Interamericana Panama Cocle Veraguas Chiriqui",
            "bus volcado vuelco carretera nacional Panama",
            "accidente Interamericana El Platanal Santiago Chitre bache",
            "pierde control carretera Panama Darien Herrera",
        ],
    },
    {
        "name": "Panama — Provincias Centrales", "country": "Panama",
        "search_terms": [
            "accidente carretera Cocle Veraguas Herrera Los Santos Panama",
            "vuelco Penonome Santiago Chitre Las Tablas accidente",
            "accidente vial Azuero Panama carretera",
        ],
    },
    {
        "name": "Panama — Provincias Extremas", "country": "Panama",
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
    if not url or not isinstance(url, str): return False
    url = url.strip()
    if not url.startswith("http"): return False
    if " " in url: return False
    if any(c in url for c in "áéíóúüñÁÉÍÓÚÜÑ¿¡"): return False
    if "facebook.com" in url and "/posts/" in url:
        post_id = url.split("/posts/")[-1].rstrip("/").split("?")[0]
        if not (post_id.isdigit() or (post_id.startswith("pfbid") and len(post_id) > 10)):
            return False
    fake = ["example.com","placeholder","unknown","comment-thread",
            "accidente-de-","via-interamericana-en-","autobus-en-la"]
    if any(p in url.lower() for p in fake): return False
    return True

def clean_url(url):
    return url.strip() if is_valid_url(url) else ""

def clean_mentions(mentions):
    cleaned = []
    for m in (mentions or []):
        m = dict(m)
        m["comment_url"] = clean_url(m.get("comment_url",""))
        m["post_url"]    = clean_url(m.get("post_url",""))
        if (m.get("quote","") or "").strip():
            cleaned.append(m)
    return cleaned

def clean_social_posts(posts):
    return [dict(p, post_url=clean_url(p.get("post_url",""))) for p in (posts or [])]

# ============================================================
# SEVERITY
# ============================================================

def get_severity(n):
    if n >= SEVERITY_CRITICAL: return "CRITICAL"
    if n >= SEVERITY_HIGH:     return "HIGH"
    if n >= SEVERITY_MEDIUM:   return "MEDIUM"
    return None

# ============================================================
# ROAD KEY
# ============================================================

_STOP = {"via","vía","calle","carretera","autopista","corredor","sector","entrada",
         "el","la","los","las","de","del","y","en","a","hacia","entre","altura",
         "panama","panamá","provincia","corregimiento","distrito","ciudad"}

def _strip(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

def road_key(loc):
    if not loc: return ""
    s = _strip(loc.lower())
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    tokens = sorted([t for t in s.split() if t and t not in _STOP and len(t) > 2][:4])
    return "".join(tokens)

# ============================================================
# INVENTORY
# ============================================================

def load_inventory():
    if not os.path.exists(INVENTORY_FILE):
        return {"counter": 0, "cases": {}, "seen_urls": []}
    with open(INVENTORY_FILE) as f:
        try: inv = json.load(f)
        except: return {"counter": 0, "cases": {}, "seen_urls": []}
    if not isinstance(inv, dict): inv = {}
    inv.setdefault("counter", 0)
    inv.setdefault("seen_urls", [])
    if "cases" not in inv:
        cases = {}
        for rec in (inv.get("incidents") or []):
            if not isinstance(rec, dict): continue
            rk = road_key(rec.get("location",""))
            if not rk: rk = hashlib.sha256((rec.get("ptw_id","") or "x").encode()).hexdigest()[:10]
            cases[rk] = rec
        inv["cases"] = cases
    return inv

def save_inventory(inv):
    cases_list = sorted(inv["cases"].values(), key=lambda x: x.get("first_seen",""), reverse=True)
    out = {
        "scan_time": datetime.utcnow().isoformat(),
        "total": len(cases_list),
        "counter": inv["counter"],
        "incidents": cases_list,
        "seen_urls": inv.get("seen_urls",[])[-1000:],
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
        "model": CLAUDE_MODEL, "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if tools: payload["tools"] = tools
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json=payload, timeout=180,
    )
    if not r.ok:
        print(f"    API ERROR {r.status_code}: {r.text[:600]}")
        r.raise_for_status()
    return "".join(b.get("text","") for b in r.json()["content"] if b.get("type") == "text")

# ============================================================
# JSON EXTRACTION
# ============================================================

def extract_json_object(text):
    if not text: return None
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"): text = text[4:]
            text = text.strip()
    try: return json.loads(text)
    except: pass
    start = text.find("{")
    if start == -1: return None
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
# APIFY INSTAGRAM SCRAPER
# ============================================================

def apify_run_and_wait(actor_id, run_input, timeout=120):
    """Run an Apify actor and wait for results."""
    headers = {"Authorization": f"Bearer {APIFY_API_TOKEN}", "Content-Type": "application/json"}

    # Start the run
    r = requests.post(
        f"{APIFY_BASE}/acts/{actor_id}/runs",
        headers=headers, json=run_input, timeout=30,
        params={"token": APIFY_API_TOKEN}
    )
    if not r.ok:
        print(f"    Apify start error: {r.status_code} {r.text[:200]}")
        return []

    run_id = r.json().get("data", {}).get("id")
    if not run_id:
        print(f"    Apify: no run ID returned")
        return []

    print(f"    Apify run started: {run_id}")

    # Poll for completion
    start_time = time.time()
    while time.time() - start_time < timeout:
        time.sleep(5)
        status_r = requests.get(
            f"{APIFY_BASE}/actor-runs/{run_id}",
            headers=headers, timeout=15
        )
        if not status_r.ok: continue
        status = status_r.json().get("data", {}).get("status","")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            print(f"    Apify run {status}")
            break

    if status != "SUCCEEDED":
        return []

    # Get results
    dataset_id = status_r.json().get("data", {}).get("defaultDatasetId")
    if not dataset_id: return []

    items_r = requests.get(
        f"{APIFY_BASE}/datasets/{dataset_id}/items",
        headers=headers, timeout=30,
        params={"token": APIFY_API_TOKEN, "format": "json", "limit": 200}
    )
    if not items_r.ok: return []
    return items_r.json() if isinstance(items_r.json(), list) else []

def scrape_instagram_for_incident(incident):
    """
    Scrapes news account profiles (including @traficopanama) via Apify
    for posts matching this incident location.
    """
    location = incident.get("location_text", "")
    loc_short = location.split(",")[0] if location else location

    all_posts = []
    keyword_comments = []
    social_posts = []

    # ── Scrape news account profiles ──────────────
    print(f"    [Apify] scraping news accounts for '{loc_short}'...")
    run_input = {
        "directUrls": INSTAGRAM_NEWS_ACCOUNTS,
        "resultsType": "posts",
        "resultsLimit": 50,
        "addParentData": False,
    }
    posts = apify_run_and_wait(APIFY_INSTAGRAM_ACTOR, run_input, timeout=90)
    print(f"    [Apify] got {len(posts)} posts from news accounts")
    all_posts.extend(posts)

    # ── FILTER: posts mentioning this location + road keywords ──
    loc_tokens = set(_strip(loc_short.lower()).split())
    kw_set = set(ROAD_KEYWORDS)
    cutoff = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).isoformat()

    for post in all_posts:
        caption = (post.get("caption") or "").lower()
        post_date = (post.get("timestamp") or "")[:10]
        post_url = post.get("url") or ""

        # Must be recent
        if post_date and post_date < cutoff[:10]:
            continue

        # Must mention the location
        caption_clean = _strip(caption)
        loc_match = any(t in caption_clean for t in loc_tokens if len(t) > 3)
        if not loc_match:
            continue

        # Add as social post if valid URL
        if is_valid_url(post_url):
            social_posts.append({
                "platform": "instagram",
                "outlet": post.get("ownerUsername",""),
                "post_url": post_url,
                "post_caption": (post.get("caption") or "")[:200],
                "post_date": post_date,
            })

        # Check caption for road keywords
        matched_kw = [k for k in kw_set if k in caption]
        if matched_kw:
            keyword_comments.append({
                "platform": "instagram",
                "source_name": f"@{post.get('ownerUsername','')}",
                "comment_url": post_url,
                "post_url": post_url,
                "quote": (post.get("caption") or "")[:300],
                "keywords_matched": matched_kw,
                "date": post_date,
            })

        # Check post comments for road keywords
        for comment in (post.get("comments") or []):
            comment_text = (comment.get("text") or "").lower()
            matched_kw = [k for k in kw_set if k in comment_text]
            if matched_kw:
                keyword_comments.append({
                    "platform": "instagram",
                    "source_name": f"@{comment.get('ownerUsername','')}",
                    "comment_url": post_url,
                    "post_url": post_url,
                    "quote": (comment.get("text") or "")[:300],
                    "keywords_matched": matched_kw,
                    "date": post_date or comment.get("timestamp","")[:10],
                })

    print(f"    [Apify] Instagram: {len(social_posts)} matching posts, {len(keyword_comments)} keyword comments")
    return social_posts, keyword_comments

# ============================================================
# 1. FIND ACCIDENTS
# ============================================================

def search_incidents(territory):
    queries = "\n".join(f"- {q}" for q in territory["search_terms"])
    prompt = f"""{DATE_ANCHOR}

Search Spanish-language news for road accidents in {territory['name']} from the LAST {LOOKBACK_DAYS} DAYS.

Run these queries:
{queries}

For each UNIQUE road accident found, output ONE JSON object per line (JSONL):
{{"title":"...","url":"...","source":"...","date":"YYYY-MM-DD",
  "location_text":"specific road + landmark + city/district",
  "summary":"what happened in 2-3 sentences",
  "article_image_urls":["direct image URLs from the article if any"]}}

STRICT RELEVANCE RULES:
- ONLY real road/traffic incidents: crashes, overturns, run-offs, vehicles
  damaged by potholes or bad pavement, road-condition emergencies.
- EXCLUDE everything else, even if it mentions a road in passing: politics,
  elections, wars, international news, sports, entertainment, crime stories
  (shootings, robberies), weather stories with no specific road incident.
- If a result is not clearly a road/traffic incident in {territory['name']},
  DO NOT output it. Fewer, relevant results beat more, irrelevant ones.
- JSONL only."""
    raw = claude_call(prompt, tools=[WEB_SEARCH_TOOL], max_tokens=4000)
    return extract_jsonl(raw)

# ============================================================
# 2. WEB SOCIAL SEARCH (Claude-based, as in v5.3)
# ============================================================

def find_web_social(incident):
    location = incident.get("location_text", "")
    loc_short = location.split(",")[0] if location else location
    date_str = incident.get("date", "")
    url = incident.get("url", "")
    source = incident.get("source", "")
    keywords = ", ".join(ROAD_KEYWORDS[:12])

    prompt = f"""{DATE_ANCHOR}

Find social media posts and citizen comments about this road accident that mention road conditions.

ARTICLE: {incident.get('title','')} | {source} | {url}
Location: {location} | Date: {date_str}
ROAD KEYWORDS: {keywords}

Run these searches:
1. site:facebook.com "{loc_short}" accidente {date_str} bache OR hueco
2. site:facebook.com (TelemetroReporta OR tvnpanama OR criticapanama OR MiDiarioPanama) "{loc_short}"
3. Fetch {url} — extract witness quotes with road keywords
4. site:x.com OR site:twitter.com "{loc_short}" bache OR hueco {date_str}
5. "{loc_short}" bache OR hueco site:facebook.com
6. "{loc_short}" bache OR hueco MOP accidente news comment
7. site:x.com (TReporta OR tvnnoticias OR CriticaPanama) "{loc_short}" accidente
8. "{loc_short}" deterioro vial MOP hueco accidente

CRITICAL URL RULES:
- Facebook: https://www.facebook.com/[Page]/posts/[NUMERIC_ID] only
- X/Twitter: https://x.com/[user]/status/[NUMERIC_ID] only
- NEVER construct or guess URLs — leave empty string if unsure

Return ONE JSON object:
{{
  "social_posts": [{{"platform":"facebook|twitter|x","outlet":"...","post_url":"...","post_caption":"...","post_date":"YYYY-MM-DD"}}],
  "keyword_comments": [{{"platform":"facebook|twitter|x|news_comment","source_name":"...","comment_url":"...","post_url":"...","quote":"...","keywords_matched":[...],"date":"YYYY-MM-DD"}}],
  "summary": "1-2 neutral sentences about what people say about this road. No conclusions."
}}
JSON only."""

    raw = claude_call(prompt, tools=[WEB_SEARCH_TOOL], max_tokens=5000)
    parsed = extract_json_object(raw)
    if parsed is None:
        return {"social_posts": [], "keyword_comments": [], "summary": ""}
    parsed["social_posts"]     = clean_social_posts(parsed.get("social_posts", []))
    parsed["keyword_comments"] = clean_mentions(parsed.get("keyword_comments", []))
    parsed.setdefault("summary", "")
    return parsed

# ============================================================
# 3. GEOCODE
# ============================================================

def geocode(location_text, country="Panama"):
    if not location_text: return None
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    for q in [f"{location_text}, {country}", f"{location_text}, Panama City, {country}", location_text]:
        try:
            r = requests.get(url, params={"address": q, "key": GOOGLE_MAPS_API_KEY}, timeout=15)
            if not r.ok: continue
            data = r.json()
            if data.get("status") == "OK" and data.get("results"):
                loc = data["results"][0]["geometry"]["location"]
                lat, lng = loc["lat"], loc["lng"]
                if not (PANAMA_LAT_MIN <= lat <= PANAMA_LAT_MAX and PANAMA_LNG_MIN <= lng <= PANAMA_LNG_MAX):
                    print(f"      geocode outside Panama: {lat:.4f},{lng:.4f}")
                    continue
                return {"lat": lat, "lng": lng, "formatted": data["results"][0]["formatted_address"]}
        except Exception as e:
            print(f"      geocode error: {e}")
    return None

def maps_link(lat, lng):
    return f"https://maps.google.com/?q={lat},{lng}"

# ============================================================
# 4. MENTION DEDUP
# ============================================================

def mention_sig(m):
    q = _strip((m.get("quote","") or "").lower()).strip()
    q = re.sub(r"\s+", " ", q)[:120]
    u = (m.get("comment_url","") or m.get("post_url","") or "").lower()
    return hashlib.sha256(f"{u}|{q}".encode()).hexdigest()[:16]

def merge_mentions(existing, incoming):
    seen = {mention_sig(m) for m in existing}
    merged, new_count = list(existing), 0
    for m in incoming:
        sig = mention_sig(m)
        if sig not in seen:
            seen.add(sig); merged.append(m); new_count += 1
    return merged, new_count

# ============================================================
# 5. EMAIL
# ============================================================

PLAT_LABEL = {"facebook":"Facebook","instagram":"Instagram","twitter":"X / Twitter",
              "x":"X / Twitter","news_comment":"News comment"}

def _link(url, text, color=None):
    c = color or ACCENT
    if url and is_valid_url(url):
        return f'<a href="{url}" style="color:{c};text-decoration:none;font-weight:700;">{text}</a>'
    return f'<span style="color:{MUTED};">{text}</span>'

def _social_post_row(p):
    plat = PLAT_LABEL.get(p.get("platform",""), p.get("platform","Social"))
    outlet = p.get("outlet","")
    src = plat + (f" · {outlet}" if outlet else "") + (f" · {p.get('post_date','')}" if p.get('post_date') else "")
    caption = (p.get("post_caption","") or "")[:120]
    post_url = p.get("post_url","")
    link_html = f'<a href="{post_url}" style="display:inline-block;margin-top:6px;font-size:11px;color:{ACCENT};text-decoration:none;border:1px solid {ACCENT};padding:3px 10px;border-radius:3px;font-weight:700;">→ Open {plat} post</a>' if (post_url and is_valid_url(post_url)) else ""
    return f'''<div style="margin:8px 0;padding:10px;background:#111;border-radius:4px;border-left:3px solid {ACCENT};">
      <div style="font-size:10px;letter-spacing:1px;color:{ACCENT};text-transform:uppercase;margin-bottom:4px;">{src}</div>
      {f'<div style="font-size:12px;color:#aaa;margin-bottom:6px;">{caption}</div>' if caption else ""}
      {link_html}
    </div>'''

def _comment_row(m):
    plat = PLAT_LABEL.get(m.get("platform",""), m.get("platform","Source"))
    src  = plat + (f" · {m.get('source_name','')}" if m.get('source_name') else "") + (f" · {m.get('date','')}" if m.get('date') else "")
    quote = (m.get("quote","") or "").replace("<","&lt;").replace(">","&gt;")
    best_url = m.get("comment_url","") if is_valid_url(m.get("comment_url","")) else (m.get("post_url","") if is_valid_url(m.get("post_url","")) else "")
    link = f' {_link(best_url, "→ ver", ACCENT)}' if best_url else ""
    kw_chips = "".join(f'<span style="display:inline-block;background:rgba(217,79,43,0.15);color:{ACCENT};font-size:9px;padding:1px 6px;border-radius:8px;margin:1px;">{k}</span>' for k in (m.get("keywords_matched") or [])[:5])
    return f'''<div style="margin:8px 0;padding-left:10px;border-left:2px solid #333;">
      <div style="font-size:10px;letter-spacing:1px;color:{ACCENT};text-transform:uppercase;margin-bottom:2px;">{src}</div>
      <div style="font-size:13px;line-height:1.5;color:{MUTED};">&ldquo;{quote}&rdquo;{link}</div>
      {f'<div style="margin-top:3px;">{kw_chips}</div>' if kw_chips else ""}
    </div>'''

def build_card(case, new_count, is_new, severity):
    color = SEVERITY_COLORS.get(severity, MEDIUM_COLOR)
    imgs = case.get("article_image_urls",[])
    img_html = "".join(f'<img src="{u}" style="width:100%;border-radius:6px;margin-bottom:8px;display:block;" onerror="this.style.display=\'none\'" />' for u in imgs[:2] if is_valid_url(u))
    social_posts     = case.get("social_posts",[])
    keyword_comments = case.get("keyword_comments",[])
    social_html  = ""
    if social_posts:
        social_html += f'<div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin:14px 0 8px;">Social posts — click to read comments</div>'
        social_html += "".join(_social_post_row(p) for p in social_posts[:4])
    comments_html = ""
    if keyword_comments:
        comments_html += f'<div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin:14px 0 8px;">Citizen comments — road conditions ({len(keyword_comments)})</div>'
        comments_html += "".join(_comment_row(m) for m in keyword_comments[:8])
    kw = case.get("keywords_matched",[])
    kw_chips_inner = "".join(f'<span style="display:inline-block;background:{SOFT};color:{ACCENT};font-size:10px;padding:2px 8px;border-radius:10px;margin:2px;">{k}</span>' for k in kw[:10])
    kw_html = f'<div style="margin:10px 0;">{kw_chips_inner}</div>' if kw else ""
    count = len(keyword_comments)
    tag = f'<span style="background:#4ADE80;color:#000;font-size:9px;font-weight:700;letter-spacing:1px;padding:2px 8px;border-radius:10px;margin-left:8px;">{"NEW" if is_new else f"+{new_count} NEW"}</span>' if (is_new or new_count > 0) else ""
    maps_html = f'<a href="{case.get("maps_link","")}" style="display:inline-block;margin:6px 0 0;font-size:11px;color:{ACCENT};text-decoration:none;border:1px solid rgba(217,79,43,0.4);padding:4px 10px;border-radius:3px;">📍 Open in Google Maps →</a>' if case.get("maps_link") else ""
    article_link = f'<a href="{case["url"]}" style="display:inline-block;background:{ACCENT};color:{TEXT};padding:10px 18px;border-radius:4px;font-size:12px;text-decoration:none;font-weight:700;letter-spacing:1px;margin-top:12px;">READ FULL ARTICLE →</a>' if (case.get("url") and is_valid_url(case.get("url",""))) else ""

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
  <div style="color:{DIM};font-size:12px;margin-bottom:4px;">{case.get('location','')} · {case.get('date','')} · {_link(case.get('url',''), case.get('source_name',''), ACCENT)}</div>
  {maps_html}
  <div style="background:{SOFT};padding:16px;border-radius:6px;margin:12px 0;">
    {img_html}
    <div style="font-size:14px;line-height:1.6;color:{TEXT};">{case.get('summary','')}</div>
    {article_link}
  </div>
  <div style="font-size:12px;color:{MUTED};font-style:italic;margin-bottom:8px;">{case.get('chatter_summary','')}</div>
  {kw_html}{social_html}{comments_html}
</div>"""

def build_digest(cards, scan_time_human, nc, nh, nm):
    count = len(cards)
    parts = []
    if nc: parts.append(f'<span style="color:{CRITICAL_COLOR};font-weight:700;">{nc} CRITICAL</span>')
    if nh: parts.append(f'<span style="color:{HIGH_COLOR};font-weight:700;">{nh} HIGH</span>')
    if nm: parts.append(f'<span style="color:{MEDIUM_COLOR};font-weight:700;">{nm} MEDIUM</span>')
    return f"""<!DOCTYPE html><html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;background:{BG};padding:24px;color:{TEXT};margin:0;">
<div style="max-width:720px;margin:auto;">
  <div style="margin-bottom:24px;padding:24px;background:{CARD_BG};border-radius:8px;border-top:4px solid {ACCENT};">
    <div style="font-size:11px;letter-spacing:4px;color:{ACCENT};font-weight:700;">POTHOLEWATCH · THE EVIDENCE ENGINE · v5.4</div>
    <h1 style="margin:10px 0 6px;font-size:26px;color:{TEXT};font-weight:700;">{count} case{'s' if count!=1 else ''} with citizen road-condition evidence</h1>
    <div style="margin:6px 0;">{" · ".join(parts)}</div>
    <div style="color:{MUTED};font-size:12px;margin-top:6px;">Scan: {scan_time_human} · Panama · {LOOKBACK_DAYS}-day window · Instagram via Apify</div>
  </div>
  {''.join(cards)}
  <div style="text-align:center;font-size:11px;color:{DIM};padding:24px 0;border-top:1px solid {SOFT};margin-top:8px;">
    <div style="font-size:10px;letter-spacing:3px;color:{ACCENT};font-weight:700;margin-bottom:6px;">POWERFIX · REPAIR. REINVENTED.</div>
    <div>PotholeWatch v5.4 — The Evidence Engine + Instagram.<br/>Accident + citizen testimony + geolocation. You decide.</div>
  </div>
</div></body></html>"""

def send_email(subject, html_body):
    creds = Credentials(token=None, refresh_token=GMAIL_REFRESH_TOKEN,
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=GMAIL_CLIENT_ID, client_secret=GMAIL_CLIENT_SECRET)
    service = build("gmail", "v1", credentials=creds)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = "PotholeWatch <ashourilevy@gmail.com>"
    msg["To"]      = ", ".join(ALERT_RECIPIENTS)
    msg.attach(MIMEText(html_body, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()

# ============================================================
# MAIN
# ============================================================

def main():
    now = datetime.utcnow()
    scan_time_iso    = now.isoformat() + "Z"
    scan_time_human  = now.strftime("%b %d, %Y · %I:%M %p UTC")
    print(f"=== PotholeWatch v5.4.0 (Evidence Engine + Instagram) @ {scan_time_iso} ===")
    print(f"=== Today: {TODAY} · lookback {LOOKBACK_DAYS}d · re-alert +{REALERT_THRESHOLD} ===")

    inv = load_inventory()
    print(f"Inventory: {len(inv['cases'])} cases, last PTW-{inv['counter']:04d}")

    pending_cards = []

    for territory in TERRITORIES:
        print(f"\n--- Territory: {territory['name']} ---")
        try:
            incidents = search_incidents(territory)
        except Exception as e:
            print(f"  search failed: {e}"); continue
        print(f"  Found {len(incidents)} candidate incidents")

        relevant = [i for i in incidents if is_relevant_incident(i)]
        dropped = len(incidents) - len(relevant)
        if dropped:
            for i in incidents:
                if not is_relevant_incident(i):
                    print(f"  ✗ off-topic, dropped: {i.get('title','')[:70]}")
            print(f"  Relevance gate: kept {len(relevant)}, dropped {dropped}")
        incidents = relevant

        for inc in incidents:
            loc = inc.get("location_text","")
            rk  = road_key(loc)
            print(f"  · {inc.get('title','')[:60]}")
            print(f"    loc: {loc[:60]} | rk: {rk}")

            # Web-based social search (Claude)
            try:
                web_data = find_web_social(inc)
            except Exception as e:
                print(f"    web social failed: {e}")
                web_data = {"social_posts":[], "keyword_comments":[], "summary":""}
            web_posts    = web_data.get("social_posts",[])
            web_comments = web_data.get("keyword_comments",[])
            print(f"    [Web] {len(web_posts)} posts, {len(web_comments)} keyword comments")

            # Instagram via Apify
            try:
                ig_posts, ig_comments = scrape_instagram_for_incident(inc)
            except Exception as e:
                print(f"    Apify Instagram failed: {e}")
                ig_posts, ig_comments = [], []

            # Merge web + Instagram
            all_posts    = web_posts    + ig_posts
            all_comments = web_comments + ig_comments
            summary      = web_data.get("summary","")

            # Dedup comments by signature
            seen_sigs = set()
            deduped_comments = []
            for m in all_comments:
                sig = mention_sig(m)
                if sig not in seen_sigs:
                    seen_sigs.add(sig)
                    deduped_comments.append(m)

            mention_count = len(deduped_comments)
            valid_post_links    = sum(1 for p in all_posts    if is_valid_url(p.get("post_url","")))
            valid_comment_links = sum(1 for c in deduped_comments if is_valid_url(c.get("comment_url","")) or is_valid_url(c.get("post_url","")))
            print(f"    posts: web={len(web_posts)} ig={len(ig_posts)} total={len(all_posts)} ({valid_post_links} w/ valid URLs)")
            print(f"    comments: web={len(web_comments)} ig={len(ig_comments)} combined={len(all_comments)} deduped={mention_count} ({valid_comment_links} w/ links)")

            severity = get_severity(mention_count)
            if not severity:
                print(f"    — no keyword comments, skipping")
                if inc.get("url"): inv["seen_urls"].append(inc["url"])
                continue

            print(f"    severity: {severity}")

            kw = set()
            for m in deduped_comments:
                for k in (m.get("keywords_matched") or []):
                    kw.add(k.lower())

            existing = inv["cases"].get(rk) if rk else None

            if existing:
                merged, new_count = merge_mentions(existing.get("keyword_comments",[]), deduped_comments)
                existing["keyword_comments"] = merged[:30]
                existing["mention_count"]    = len(merged)
                # merge social posts
                ex_urls = {p.get("post_url","") for p in existing.get("social_posts",[])}
                for p in all_posts:
                    if p.get("post_url","") not in ex_urls:
                        existing.setdefault("social_posts",[]).append(p)
                        ex_urls.add(p.get("post_url",""))
                existing["social_posts"] = existing.get("social_posts",[])[:8]
                existing["keywords_matched"] = sorted(set(existing.get("keywords_matched",[])) | kw)
                existing["last_seen"]    = scan_time_iso
                if summary: existing["chatter_summary"] = summary
                new_sev = get_severity(len(merged))
                existing["severity"] = new_sev
                print(f"    known {existing['ptw_id']} — +{new_count} (total {len(merged)}) → {new_sev}")
                if new_count >= REALERT_THRESHOLD:
                    pending_cards.append((existing, new_count, False, new_sev))
                    print(f"    ✓ re-alerting")
                else:
                    print(f"    · silent update")
            else:
                coords = geocode(loc, territory.get("country","Panama"))
                if coords: print(f"    geocoded → {coords['lat']:.4f},{coords['lng']:.4f}")
                case_id = next_case_id(inv)
                best_q, best_src = "", ""
                for m in deduped_comments:
                    if (m.get("quote") or "").strip():
                        best_q   = m["quote"].strip()
                        best_src = m.get("source_name","") or m.get("platform","")
                        break
                case = {
                    "ptw_id": case_id, "road_key": rk, "severity": severity,
                    "headline": inc.get("title",""), "location": loc,
                    "date": inc.get("date",""), "summary": inc.get("summary",""),
                    "source_name": inc.get("source",""), "url": clean_url(inc.get("url","")),
                    "lat":  coords["lat"]       if coords else None,
                    "lng":  coords["lng"]       if coords else None,
                    "geo_formatted": coords["formatted"] if coords else "",
                    "maps_link": maps_link(coords["lat"], coords["lng"]) if coords else None,
                    "first_seen": scan_time_iso, "last_seen": scan_time_iso,
                    "article_image_urls": [u for u in (inc.get("article_image_urls") or []) if is_valid_url(u)][:4],
                    "primary_image_url": None,
                    "chatter_summary": summary,
                    "mention_count": mention_count,
                    "keywords_matched": sorted(kw),
                    "best_quote": best_q, "best_quote_source": best_src,
                    "social_posts":     all_posts[:8],
                    "keyword_comments": deduped_comments[:30],
                }
                if case["article_image_urls"]: case["primary_image_url"] = case["article_image_urls"][0]
                inv["cases"][rk] = case
                if inc.get("url"): inv["seen_urls"].append(inc["url"])
                pending_cards.append((case, mention_count, True, severity))
                print(f"    ✓ NEW {case_id} — {mention_count} comment(s) → {severity}")

    inv["seen_urls"] = inv["seen_urls"][-1000:]

    if not pending_cards:
        print(f"\n=== No new/updated cases — no email ===")
        save_inventory(inv)
        return

    sev_ord = {"CRITICAL":0,"HIGH":1,"MEDIUM":2}
    pending_cards.sort(key=lambda c: (sev_ord.get(c[3],3), 0 if c[2] else 1, -c[1]))
    nc = sum(1 for c in pending_cards if c[3]=="CRITICAL")
    nh = sum(1 for c in pending_cards if c[3]=="HIGH")
    nm = sum(1 for c in pending_cards if c[3]=="MEDIUM")

    cards = [build_card(c,n,isnew,sev) for (c,n,isnew,sev) in pending_cards]
    print(f"\n--- Digest: {len(cards)} case(s) — {nc} CRITICAL, {nh} HIGH, {nm} MEDIUM ---")
    html = build_digest(cards, scan_time_human, nc, nh, nm)
    sev_label = f"{nc} CRITICAL" if nc else f"{nh} HIGH" if nh else f"{nm} MEDIUM"
    subject = f"PotholeWatch · {len(cards)} case{'s' if len(cards)!=1 else ''} · {sev_label} · {now.strftime('%b %d')}"

    try:
        send_email(subject, html)
        print(f"✉  sent")
    except Exception as e:
        print(f"✗ send failed: {e}")

    save_inventory(inv)
    print(f"\n=== Done · {len(inv['cases'])} cases in inventory ===")

if __name__ == "__main__":
    main()

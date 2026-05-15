"""
PotholeWatch v3.0.6 — Multi-source road incident intelligence
============================================================
CADENCE:   Every 8 hours (cron: '0 */8 * * *')
DELIVERY:  ONE digest email per scan — all new incidents bundled
LOOK:      PowerFix brand — black bg, white text, red-orange accent

Pipeline per scan:
  1. Web search → fresh road accidents per territory (last 8 days)
  2. Geocode location → lat/lng (with fallback retries)
  3. DEDUP against incidents.json inventory
  4. For each NEW incident:
       a. Pull article photos (primary visual)
       b. Street View sweep at coords (4 directions, vision-filtered)
       c. Satellite static map
       d. Cross-reference: Waze, X/Twitter, Instagram, Facebook,
          MOP Panama, ATTT Panama, prior news history, comment mining
       e. AI scorer combines news + cross-ref + vision
  5. Build ONE digest email with every Medium+ incident as a stacked card
  6. Send + commit inventory
"""

import os
import re
import json
import base64
import hashlib
import urllib.parse
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
SCORE_THRESHOLD_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
MIN_SCORE = "MEDIUM"

INVENTORY_FILE = "incidents.json"
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}

# Brand
BG = "#0D0D0D"
CARD_BG = "#1A1A1A"
TEXT = "#FFFFFF"
MUTED = "#999999"
DIM = "#666666"
ACCENT = "#D94F2B"
SOFT = "#262626"

TERRITORIES = [
    {
        "name": "Panama",
        "country": "Panama",
        "language": "es",
        "search_terms": [
            "accidente carretera bache hueco Panama",
            "accidente vial Via Centenario Corredor Norte Sur",
            "MOP Panama Tapa Hueco deterioro vial",
            "choque carro Via Brasil Transistmica Panama",
        ],
    },
]

# ============================================================
# INVENTORY (migrates v2 list-shape → dict-shape)
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
            dt = rec.get("date", "")
            ttl = rec.get("title", "")
            h = incident_hash(loc, dt, ttl) if (loc or dt or ttl) else f"legacy_{i:04d}"
            out[h] = rec
        return out
    return {}

def load_inventory():
    if not os.path.exists(INVENTORY_FILE):
        return {"counter": 0, "incidents": {}}
    with open(INVENTORY_FILE) as f:
        try:
            inv = json.load(f)
        except json.JSONDecodeError:
            print(f"  inventory file corrupt, starting fresh")
            return {"counter": 0, "incidents": {}}
    if not isinstance(inv, dict):
        inv = {"counter": 0, "incidents": inv}
    inv.setdefault("counter", 0)
    inv["incidents"] = _normalize_incidents(inv.get("incidents", {}))
    return inv

def save_inventory(inv):
    with open(INVENTORY_FILE, "w") as f:
        json.dump(inv, f, indent=2, ensure_ascii=False)

def incident_hash(location_text, date, title):
    key = f"{(location_text or '').lower().strip()}|{date or ''}|{(title or '').lower().strip()[:60]}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]

def next_case_id(inv):
    inv["counter"] += 1
    return f"PTW-{inv['counter']:04d}"

# ============================================================
# CLAUDE API
# ============================================================

def claude_call(prompt, tools=None, max_tokens=3000):
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
        print(f"    API ERROR {r.status_code}: {r.text[:800]}")
        r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")

def claude_vision(image_url, prompt):
    try:
        img_bytes = requests.get(image_url, timeout=20).content
        b64 = base64.standard_b64encode(img_bytes).decode()
    except Exception as e:
        return f"VISION_ERROR: {e}"
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 300,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                                              "media_type": "image/jpeg",
                                              "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if not r.ok:
        print(f"    VISION ERROR {r.status_code}: {r.text[:400]}")
        return f"VISION_ERROR: {r.status_code}"
    return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")

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
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                snippet = text[start:i+1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError:
                    return None
    return None

def extract_jsonl(text):
    out = []
    if not text:
        return out
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("```"):
            continue
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if out:
        return out
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, c in enumerate(text):
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                snippet = text[start:i+1]
                try:
                    obj = json.loads(snippet)
                    if isinstance(obj, dict):
                        out.append(obj)
                except json.JSONDecodeError:
                    pass
                start = None
    return out

# ============================================================
# 1. SEARCH
# ============================================================

def search_incidents(territory):
    queries = "\n".join(f"- {q}" for q in territory["search_terms"])
    prompt = f"""Search Spanish-language news for road accidents in {territory['name']} from the LAST 8 DAYS.

Run these queries:
{queries}

For each UNIQUE road accident, output ONE JSON object per line (JSONL only, no prose, no fences):
{{"title": "...", "url": "...", "source": "...", "date": "YYYY-MM-DD",
  "location_text": "specific road + landmark + city/district (be precise)",
  "summary": "what happened in 2-3 sentences",
  "article_image_urls": ["direct image URLs from the article if any"],
  "pothole_keywords_found": ["bache", "hueco", "cráter", ...]}}

Rules:
- Real road accidents only (collisions, rollovers, lost control). No shootings, no pedestrian-only events.
- Pothole keywords are bonus — include the incident even if none appear.
- For location_text be as specific as possible — Google Maps must geocode it.

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
        return None, f"HTTP_{r.status_code}: {r.text[:200]}"
    data = r.json()
    status = data.get("status", "UNKNOWN")
    if status != "OK" or not data.get("results"):
        err_msg = data.get("error_message", "")
        return None, f"{status}{' — ' + err_msg if err_msg else ''}"
    loc = data["results"][0]["geometry"]["location"]
    return {
        "lat": loc["lat"],
        "lng": loc["lng"],
        "formatted": data["results"][0]["formatted_address"],
    }, "OK"

def geocode(location_text, country):
    candidates = [
        f"{location_text}, {country}",
        f"{location_text}, Panama City, {country}",
        location_text,
    ]
    seen = set()
    for q in candidates:
        if q in seen:
            continue
        seen.add(q)
        coords, status = _geocode_once(q)
        if coords:
            return coords
        print(f"      geocode try '{q}' → {status}")
    return None

# ============================================================
# 3. STREET VIEW
# ============================================================

def street_view_urls(lat, lng):
    base = "https://maps.googleapis.com/maps/api/streetview"
    urls = []
    for heading in [0, 90, 180, 270]:
        params = {
            "size": "640x400",
            "location": f"{lat},{lng}",
            "heading": heading,
            "pitch": -20,
            "fov": 80,
            "key": GOOGLE_MAPS_API_KEY,
        }
        urls.append(f"{base}?{urllib.parse.urlencode(params)}")
    return urls

def satellite_url(lat, lng):
    base = "https://maps.googleapis.com/maps/api/staticmap"
    params = {
        "center": f"{lat},{lng}",
        "zoom": "18",
        "size": "640x400",
        "maptype": "hybrid",
        "markers": f"color:red|{lat},{lng}",
        "key": GOOGLE_MAPS_API_KEY,
    }
    return f"{base}?{urllib.parse.urlencode(params)}"

def vision_filter_streetview(urls):
    hits = []
    for url in urls:
        verdict = claude_vision(
            url,
            "Is there any visible road damage (potholes, cracks, patches, deterioration) in this image? "
            "Answer with just YES or NO followed by one sentence."
        )
        if verdict.upper().startswith("YES"):
            hits.append({"url": url, "note": verdict})
    return hits

# ============================================================
# 4. CROSS-REFERENCE
# ============================================================

def cross_reference(incident, coords):
    prompt = f"""Find supporting evidence that links this road accident to pothole or road-deterioration causes.

INCIDENT:
  Title: {incident.get('title','')}
  Location: {incident.get('location_text','')} ({coords['formatted']})
  Date: {incident.get('date','')}
  Article URL: {incident.get('url','')}
  Summary: {incident.get('summary','')}

Run web searches across these sources and collect quotes/links. Search BROADLY — even partial matches are worth surfacing:

  1. Waze — user reports of road hazards, potholes (bache/hueco), road damage on this road
  2. X / Twitter — recent citizen posts complaining about this road
  3. Instagram — citizen photos/posts of road damage on this road
  4. Facebook — citizen posts and comments about this road's condition
  5. News article comments — readers mentioning bache, hueco, cráter, forado on this road
  6. MOP Panama (Ministerio de Obras Publicas) — Tapa Hueco programs, contracts, deterioration reports for this road
  7. ATTT Panama (Autoridad de Tránsito) — transit reports, hazard advisories
  8. Prior news coverage of road damage on this road in last 12 months

Return ONE JSON object (no prose, no fences):
{{
  "waze_reports": [{{"url": "...", "quote": "..."}}],
  "x_posts": [{{"url": "...", "quote": "...", "user": "..."}}],
  "instagram_posts": [{{"url": "...", "quote": "...", "user": "..."}}],
  "facebook_posts": [{{"url": "...", "quote": "...", "user": "..."}}],
  "citizen_comments": [{{"source": "...", "quote": "..."}}],
  "mop_evidence": [{{"source": "...", "url": "...", "quote": "..."}}],
  "attt_evidence": [{{"source": "...", "url": "...", "quote": "..."}}],
  "news_history": [{{"source": "...", "url": "...", "date": "...", "headline": "..."}}],
  "pothole_word_count": <integer>
}}

If a source has nothing, use an empty array. ALWAYS return the full object structure.

JSON only."""

    raw = claude_call(prompt, tools=[WEB_SEARCH_TOOL], max_tokens=5000)
    parsed = extract_json_object(raw)
    if parsed is None:
        print(f"      cross-ref parse failed — raw[:200]: {raw[:200]!r}")
        parsed = {}
    template = {
        "waze_reports": [], "x_posts": [], "instagram_posts": [],
        "facebook_posts": [], "citizen_comments": [], "mop_evidence": [],
        "attt_evidence": [], "news_history": [], "pothole_word_count": 0,
    }
    for k, default in template.items():
        parsed.setdefault(k, default)
    return parsed

# ============================================================
# 5. SCORE
# ============================================================

def score_incident(incident, coords, crossref, vision_hits):
    prompt = f"""Score how likely it is that this road accident was caused by a pothole or road deterioration.

INCIDENT: {json.dumps(incident, ensure_ascii=False)}
COORDINATES: {coords}
CROSS-REFERENCE EVIDENCE: {json.dumps(crossref, ensure_ascii=False)}
STREET VIEW DAMAGE DETECTED: {len(vision_hits)} of 4 directional images show road damage

Levels:
  - CRITICAL: Article explicitly names pothole/bache as cause, OR strong cross-refs + vision confirmation
  - HIGH: Strong cross-ref (MOP plan + social posts) OR vision + 1+ citizen complaints
  - MEDIUM: Some indirect evidence (1-2 cross-refs OR vision damage)
  - LOW: No supporting evidence

Return JSON (no prose, no fences):
{{
  "score": "CRITICAL|HIGH|MEDIUM|LOW",
  "reasoning": "2-3 sentence explanation",
  "headline_evidence": ["bullet 1", "bullet 2", "bullet 3"]
}}"""
    raw = claude_call(prompt, max_tokens=1000)
    parsed = extract_json_object(raw)
    if not parsed:
        return {"score": "LOW", "reasoning": "Score parse failed", "headline_evidence": []}
    parsed.setdefault("score", "LOW")
    parsed.setdefault("reasoning", "")
    parsed.setdefault("headline_evidence", [])
    return parsed

# ============================================================
# 6. EMAIL — DIGEST (PowerFix brand)
# ============================================================

SCORE_COLOR = {"CRITICAL": "#FF3B3B", "HIGH": ACCENT,
               "MEDIUM": "#F0A030", "LOW": MUTED}

def _quote_block(label, items):
    if not items:
        return ""
    rows = ""
    for item in items[:5]:
        src = item.get('source') or item.get('platform') or item.get('user') or ''
        quote = (item.get('quote', '') or '').replace('<', '&lt;').replace('>', '&gt;')
        url = item.get('url', '')
        link = f' <a href="{url}" style="color:{ACCENT};text-decoration:none;">→</a>' if url else ''
        src_html = f'<strong style="color:{TEXT};">{src}:</strong> ' if src else ''
        rows += f'<div style="font-size:13px;line-height:1.5;margin:5px 0;color:{MUTED};">{src_html}&ldquo;{quote}&rdquo;{link}</div>'
    return f"""<div style="margin-top:16px;padding-top:14px;border-top:1px solid {SOFT};">
      <div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin-bottom:8px;">{label}</div>
      {rows}
    </div>"""

def build_incident_card(case_id, incident, coords, crossref, vision_hits, score, sv_urls, sat):
    color = SCORE_COLOR.get(score["score"], MUTED)
    
    article_imgs = [u for u in (incident.get("article_image_urls") or []) if u][:2]
    article_img_html = "".join(
        f'<img src="{u}" style="width:100%;border-radius:6px;margin-bottom:10px;display:block;" />'
        for u in article_imgs
    )
    
    sv_hit_html = "".join(
        f'<img src="{h["url"]}" style="width:48%;margin:1%;border-radius:4px;display:inline-block;" />'
        for h in vision_hits[:4]
    )
    
    bullets = "".join(
        f'<li style="margin-bottom:4px;">{b}</li>' for b in score.get("headline_evidence", [])
    )
    
    cross = ""
    cross += _quote_block("Waze reports", crossref.get("waze_reports", []))
    cross += _quote_block("X / Twitter", crossref.get("x_posts", []))
    cross += _quote_block("Instagram", crossref.get("instagram_posts", []))
    cross += _quote_block("Facebook", crossref.get("facebook_posts", []))
    cross += _quote_block("Reader comments", crossref.get("citizen_comments", []))
    cross += _quote_block("MOP Panama", crossref.get("mop_evidence", []))
    cross += _quote_block("ATTT Panama", crossref.get("attt_evidence", []))
    
    if crossref.get("news_history"):
        rows = "".join(
            f'<div style="font-size:13px;margin:5px 0;color:{MUTED};">{i.get("date","")} — <a href="{i.get("url","")}" style="color:{ACCENT};text-decoration:none;">{i.get("headline","")}</a> <span style="color:{DIM};">({i.get("source","")})</span></div>'
            for i in crossref["news_history"][:5]
        )
        cross += f'<div style="margin-top:16px;padding-top:14px;border-top:1px solid {SOFT};"><div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin-bottom:8px;">Prior news on this road</div>{rows}</div>'
    
    if not cross.strip():
        cross = f'<div style="font-size:12px;color:{DIM};font-style:italic;margin-top:14px;padding-top:14px;border-top:1px solid {SOFT};">No cross-reference evidence surfaced for this incident.</div>'

    return f"""
<div style="background:{CARD_BG};border-radius:8px;padding:24px;margin-bottom:20px;border-left:4px solid {color};">

  <!-- HEADER -->
  <div style="font-size:10px;letter-spacing:3px;color:{color};font-weight:700;">{score['score']} · {case_id}</div>
  <h2 style="margin:8px 0 6px 0;font-size:20px;color:{TEXT};line-height:1.3;font-weight:700;">{incident.get('title','')}</h2>
  <div style="color:{DIM};font-size:12px;margin-bottom:16px;">
    {coords['formatted']} · {incident.get('date','')} · <a href="{incident.get('url','')}" style="color:{ACCENT};text-decoration:none;">{incident.get('source','')}</a>
  </div>

  <!-- NEWS ARTICLE (the headline source) -->
  <div style="background:{SOFT};padding:16px;border-radius:6px;margin-bottom:16px;">
    {article_img_html}
    <div style="font-size:14px;line-height:1.6;color:{TEXT};">{incident.get('summary','')}</div>
    <div style="margin-top:14px;"><a href="{incident.get('url','')}" style="display:inline-block;background:{ACCENT};color:{TEXT};padding:10px 16px;border-radius:4px;font-size:12px;text-decoration:none;font-weight:700;letter-spacing:1px;">READ FULL ARTICLE →</a></div>
  </div>

  <!-- WHY WE'RE FLAGGING -->
  <div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin-bottom:8px;">Why we're flagging</div>
  <ul style="font-size:13px;line-height:1.6;margin:0 0 10px 18px;color:{TEXT};padding-left:0;">{bullets}</ul>
  <div style="font-size:12px;color:{MUTED};font-style:italic;">{score.get('reasoning','')}</div>

  <!-- CROSS-REFERENCES -->
  {cross}

  <!-- VISION-CONFIRMED STREET VIEW -->
  {f'<div style="margin-top:18px;padding-top:14px;border-top:1px solid {SOFT};"><div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin-bottom:8px;">Street View — vision-confirmed damage ({len(vision_hits)}/4)</div><div>{sv_hit_html}</div></div>' if vision_hits else ''}

  <!-- SATELLITE -->
  <div style="margin-top:18px;padding-top:14px;border-top:1px solid {SOFT};">
    <div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin-bottom:8px;">Satellite</div>
    <img src="{sat}" style="width:100%;border-radius:4px;display:block;" />
    <div style="font-size:11px;color:{DIM};margin-top:6px;">Coords: {coords['lat']:.5f}, {coords['lng']:.5f}</div>
  </div>

</div>"""

def build_digest_email(cards, scan_time):
    if not cards:
        return None
    
    count = len(cards)
    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;background:{BG};padding:24px;color:{TEXT};margin:0;">
<div style="max-width:720px;margin:auto;">

  <!-- MASTHEAD -->
  <div style="margin-bottom:24px;padding:24px;background:{CARD_BG};border-radius:8px;border-top:4px solid {ACCENT};">
    <div style="font-size:11px;letter-spacing:4px;color:{ACCENT};font-weight:700;">POTHOLEWATCH · 8-HOUR DIGEST</div>
    <h1 style="margin:10px 0 6px 0;font-size:26px;color:{TEXT};font-weight:700;">{count} new incident{'s' if count != 1 else ''} detected</h1>
    <div style="color:{MUTED};font-size:12px;">Scan: {scan_time} · Panama territory</div>
  </div>

  {''.join(cards)}

  <!-- FOOTER -->
  <div style="text-align:center;font-size:11px;color:{DIM};padding:24px 0;border-top:1px solid {SOFT};margin-top:24px;">
    <div style="font-size:10px;letter-spacing:3px;color:{ACCENT};font-weight:700;margin-bottom:6px;">POWERFIX · REPAIR. REINVENTED.</div>
    <div>PotholeWatch v3.0.6 — automated road incident intelligence.<br/>Next scan in 8 hours.</div>
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
    scan_time = datetime.utcnow().isoformat() + "Z"
    print(f"=== PotholeWatch v3.0.6 scan @ {scan_time} ===")
    inv = load_inventory()
    print(f"Inventory: {len(inv['incidents'])} on file, last case PTW-{inv['counter']:04d}")
    
    threshold = SCORE_THRESHOLD_RANK[MIN_SCORE]
    cards = []
    pending_inventory = []
    
    for territory in TERRITORIES:
        print(f"\n--- Territory: {territory['name']} ---")
        try:
            incidents = search_incidents(territory)
        except Exception as e:
            print(f"  search failed: {e}")
            continue
        print(f"  Found {len(incidents)} candidate incidents")
        
        for inc in incidents:
            h = incident_hash(inc.get("location_text", ""), inc.get("date", ""), inc.get("title", ""))
            if h in inv["incidents"]:
                print(f"  · {h} already in inventory, skipping")
                continue
            
            print(f"  · NEW: {inc.get('title','(no title)')[:70]}")
            print(f"    location_text: {inc.get('location_text','(none)')}")
            
            coords = geocode(inc.get("location_text", ""), territory["country"])
            if not coords:
                print(f"    ✗ geocode failed")
                continue
            print(f"    geocoded → {coords['lat']:.4f}, {coords['lng']:.4f}")
            
            sv_urls = street_view_urls(coords["lat"], coords["lng"])
            vision_hits = vision_filter_streetview(sv_urls)
            print(f"    vision: {len(vision_hits)}/4 show damage")
            
            crossref = cross_reference(inc, coords)
            cx_count = sum(len(crossref.get(k, [])) for k in
                          ["waze_reports", "x_posts", "instagram_posts", "facebook_posts",
                           "citizen_comments", "mop_evidence", "attt_evidence", "news_history"])
            print(f"    cross-ref: {cx_count} hits, pothole word count {crossref.get('pothole_word_count', 0)}")
            
            score = score_incident(inc, coords, crossref, vision_hits)
            print(f"    SCORE: {score['score']}")
            
            if SCORE_THRESHOLD_RANK.get(score["score"], 0) < threshold:
                print(f"    — below threshold, not bundling")
                continue
            
            case_id = next_case_id(inv)
            sat = satellite_url(coords["lat"], coords["lng"])
            card = build_incident_card(case_id, inc, coords, crossref, vision_hits, score, sv_urls, sat)
            cards.append(card)
            pending_inventory.append((h, {
                "case_id": case_id,
                "title": inc.get("title", ""),
                "url": inc.get("url", ""),
                "date": inc.get("date", ""),
                "location_text": inc.get("location_text", ""),
                "coords": coords,
                "score": score["score"],
                "vision_hits": len(vision_hits),
                "crossref_size": cx_count,
                "first_seen": scan_time,
            }))
            print(f"    ✓ bundled as {case_id}")
    
    if not cards:
        print(f"\n=== No new incidents above threshold — no email sent ===")
        save_inventory(inv)
        return
    
    print(f"\n--- Sending digest with {len(cards)} incident(s) ---")
    html = build_digest_email(cards, scan_time)
    subject = f"PotholeWatch · {len(cards)} new incident{'s' if len(cards) != 1 else ''} · {datetime.utcnow().strftime('%b %d')}"
    
    try:
        send_email(subject, html)
        print(f"✉  digest sent")
        for h, rec in pending_inventory:
            inv["incidents"][h] = rec
    except Exception as e:
        print(f"✗ digest send failed: {e}")
        inv["counter"] -= len(pending_inventory)
    
    save_inventory(inv)
    print(f"\n=== Done · inventory now {len(inv['incidents'])} incidents ===")

if __name__ == "__main__":
    main()

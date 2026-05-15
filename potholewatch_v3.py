"""
PotholeWatch v3.0.3 — Multi-source road incident intelligence
============================================================
Pipeline per scan:
  1. Web search → fresh accident news per territory
  2. Geocode location → lat/lng (with retries + error logging)
  3. DEDUP against incidents.json inventory
  4. Cross-reference (MOP, ATTT, Instagram/X, news comments)
  5. Street View sweep at exact coords (4 directions)
  6. AI scorer combines ALL evidence
  7. If Medium+ → rich HTML email + commit to inventory

Run every 30 min on GitHub Actions.

REQUIREMENTS:
  - Anthropic org: web search must be enabled in Privacy controls
  - Google Cloud project: Geocoding API + Maps Static API + Street View Static API
    all enabled, billing active
"""

import os
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
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}

TERRITORIES = [
    {
        "name": "Panama",
        "country": "Panama",
        "language": "es",
        "search_terms": [
            "accidente carretera bache hueco Panama",
            "accidente vial Via Centenario Corredor Norte Sur",
            "MOP Panama Tapa Hueco deterioro vial",
        ],
    },
]

# ============================================================
# INVENTORY (with v2 backfill)
# ============================================================

def load_inventory():
    if not os.path.exists(INVENTORY_FILE):
        return {"counter": 0, "incidents": {}}
    with open(INVENTORY_FILE) as f:
        inv = json.load(f)
    inv.setdefault("counter", 0)
    inv.setdefault("incidents", {})
    return inv

def save_inventory(inv):
    with open(INVENTORY_FILE, "w") as f:
        json.dump(inv, f, indent=2, ensure_ascii=False)

def incident_hash(location_text, date, title):
    key = f"{location_text.lower().strip()}|{date}|{title.lower().strip()[:60]}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]

def next_case_id(inv):
    inv["counter"] += 1
    return f"PTW-{inv['counter']:04d}"

# ============================================================
# CLAUDE API (with verbose error logging)
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

def strip_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()

# ============================================================
# 1. SEARCH
# ============================================================

def search_incidents(territory):
    queries = "\n".join(f"- {q}" for q in territory["search_terms"])
    prompt = f"""Search for road accident news from the last 7 days in {territory['name']}.

Run these queries:
{queries}

For EACH unique road accident found, output ONE JSON object per line (JSONL):
{{"title": "...", "url": "...", "source": "...", "date": "YYYY-MM-DD",
  "location_text": "exact road/intersection — be as specific as possible, include city/district",
  "summary": "2-3 sentences",
  "article_image_urls": ["..."],
  "pothole_keywords_found": ["bache", "hueco", ...]}}

For location_text, prefer specific landmarks (e.g., "Via Centenario near Pedro Miguel bridge, Panama City") over vague names ("Via Centenario").

Only road accidents. JSONL only — no preamble, no fences."""

    raw = claude_call(prompt, tools=[WEB_SEARCH_TOOL], max_tokens=4000)
    incidents = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        try:
            incidents.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return incidents

# ============================================================
# 2. GEOCODE (with fallbacks + error logging)
# ============================================================

def _geocode_once(query):
    """Single geocoding attempt. Returns (coords_dict, status_string)."""
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
    """Try multiple query variants — log each failure."""
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
# 3. STREET VIEW SWEEP
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
    prompt = f"""You are researching a road accident to find evidence of pothole correlation.

INCIDENT:
  Title: {incident['title']}
  Location: {incident['location_text']} ({coords['formatted']})
  Date: {incident['date']}
  Summary: {incident['summary']}

Search for cross-reference evidence from these sources:
  1. MOP Panama (Ministerio de Obras Publicas) — Tapa Hueco programs, contracts, deterioration reports for this road
  2. ATTT Panama (Autoridad de Tránsito) — transit reports or hazard advisories
  3. Instagram / X / Twitter / Facebook — citizen posts complaining about potholes on this road
  4. News article comments — readers mentioning bache, hueco, cráter, forado
  5. Prior news coverage of road damage on this road in last 12 months

Return a single JSON object:
{{
  "mop_evidence": [{{"source": "...", "url": "...", "quote": "..."}}],
  "attt_evidence": [{{"source": "...", "url": "...", "quote": "..."}}],
  "social_posts": [{{"platform": "...", "url": "...", "quote": "..."}}],
  "citizen_comments": [{{"source": "...", "quote": "..."}}],
  "news_history": [{{"source": "...", "url": "...", "date": "...", "headline": "..."}}],
  "pothole_word_count": <total times pothole-related Spanish words appear across all sources>
}}

JSON only, no markdown fences."""
    raw = claude_call(prompt, tools=[WEB_SEARCH_TOOL], max_tokens=4000)
    try:
        return json.loads(strip_json(raw))
    except json.JSONDecodeError:
        return {"mop_evidence": [], "attt_evidence": [], "social_posts": [],
                "citizen_comments": [], "news_history": [], "pothole_word_count": 0}

# ============================================================
# 5. SCORE
# ============================================================

def score_incident(incident, coords, crossref, vision_hits):
    prompt = f"""You are scoring how likely it is that a road accident was caused by a pothole or road deterioration.

INCIDENT: {json.dumps(incident, ensure_ascii=False)}
COORDINATES: {coords}
CROSS-REFERENCE EVIDENCE: {json.dumps(crossref, ensure_ascii=False)}
STREET VIEW DAMAGE DETECTED: {len(vision_hits)} of 4 directional images show road damage

Score the pothole-correlation probability as one of:
  - CRITICAL: Direct evidence (article names pothole) OR multiple strong cross-references + vision confirmation
  - HIGH: Strong cross-reference (MOP plan + social posts) OR vision + 1+ citizen complaints
  - MEDIUM: Some indirect evidence (1-2 cross-refs, vision damage but no direct mention)
  - LOW: No supporting evidence beyond the accident itself

Return JSON:
{{
  "score": "CRITICAL|HIGH|MEDIUM|LOW",
  "reasoning": "2-3 sentence explanation",
  "headline_evidence": ["bullet 1", "bullet 2", "bullet 3"]
}}

JSON only."""
    raw = claude_call(prompt, max_tokens=1000)
    try:
        return json.loads(strip_json(raw))
    except json.JSONDecodeError:
        return {"score": "LOW", "reasoning": "Score parse failed", "headline_evidence": []}

# ============================================================
# 6. EMAIL
# ============================================================

def build_email_html(case_id, incident, coords, crossref, vision_hits, score, sv_urls, sat):
    color = {"CRITICAL": "#B00020", "HIGH": "#D94F2B",
             "MEDIUM": "#E08E00", "LOW": "#888"}.get(score["score"], "#888")
    
    sv_imgs = "".join(
        f'<img src="{u}" style="width:48%;margin:1%;border-radius:4px;" />'
        for u in sv_urls
    )
    
    def section(title, items, fmt):
        if not items:
            return ""
        rows = "".join(fmt(i) for i in items)
        return f"<h3 style='color:#0D0D0D;margin-top:24px;'>{title}</h3>{rows}"
    
    mop = section("MOP Evidence", crossref.get("mop_evidence", []),
                  lambda i: f"<p><strong>{i.get('source','')}:</strong> &ldquo;{i.get('quote','')}&rdquo; <a href='{i.get('url','')}'>link</a></p>")
    attt = section("ATTT Evidence", crossref.get("attt_evidence", []),
                   lambda i: f"<p><strong>{i.get('source','')}:</strong> &ldquo;{i.get('quote','')}&rdquo; <a href='{i.get('url','')}'>link</a></p>")
    social = section("Social Posts", crossref.get("social_posts", []),
                     lambda i: f"<p><strong>{i.get('platform','')}:</strong> &ldquo;{i.get('quote','')}&rdquo; <a href='{i.get('url','')}'>link</a></p>")
    comments = section("Citizen Comments", crossref.get("citizen_comments", []),
                       lambda i: f"<p><strong>{i.get('source','')}:</strong> &ldquo;{i.get('quote','')}&rdquo;</p>")
    history = section("Prior News on This Road", crossref.get("news_history", []),
                      lambda i: f"<p>{i.get('date','')} — <a href='{i.get('url','')}'>{i.get('headline','')}</a> ({i.get('source','')})</p>")
    
    bullets = "".join(f"<li>{b}</li>" for b in score.get("headline_evidence", []))
    
    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;background:#F5F2ED;padding:24px;color:#0D0D0D;">
<div style="max-width:720px;margin:auto;background:white;padding:32px;border-radius:8px;">

<div style="border-left:6px solid {color};padding-left:16px;margin-bottom:24px;">
  <div style="font-size:11px;letter-spacing:2px;color:{color};font-weight:700;">{score['score']} · {case_id}</div>
  <h1 style="margin:8px 0 4px 0;font-size:22px;">{incident['title']}</h1>
  <div style="color:#666;font-size:13px;">{coords['formatted']} · {incident['date']}</div>
</div>

<p style="font-size:14px;line-height:1.6;">{incident['summary']}</p>

<h3 style="margin-top:24px;">Why we're flagging this</h3>
<ul style="font-size:14px;line-height:1.6;">{bullets}</ul>
<p style="font-size:13px;color:#666;font-style:italic;">{score['reasoning']}</p>

<h3 style="margin-top:24px;">Street View at incident coordinates</h3>
<div>{sv_imgs}</div>
<p style="font-size:12px;color:#888;">Vision-confirmed damage in {len(vision_hits)} of 4 directional views.</p>

<h3 style="margin-top:24px;">Satellite</h3>
<img src="{sat}" style="width:100%;border-radius:4px;" />

{mop}{attt}{social}{comments}{history}

<hr style="margin:32px 0;border:none;border-top:1px solid #eee;" />
<p style="font-size:11px;color:#888;">
  <strong>PotholeWatch v3.0.3</strong> — automated road incident intelligence for PowerFix.<br/>
  Source: <a href="{incident['url']}">{incident['source']}</a> · Coords: {coords['lat']}, {coords['lng']}
</p>

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
    print(f"=== PotholeWatch v3.0.3 scan @ {datetime.utcnow().isoformat()}Z ===")
    inv = load_inventory()
    print(f"Inventory: {len(inv['incidents'])} on file, last case PTW-{inv['counter']:04d}")
    
    threshold = SCORE_THRESHOLD_RANK[MIN_SCORE]
    
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
                print(f"    ✗ geocode failed for all variants")
                continue
            print(f"    geocoded → {coords['lat']:.4f}, {coords['lng']:.4f} ({coords['formatted']})")
            
            sv_urls = street_view_urls(coords["lat"], coords["lng"])
            vision_hits = vision_filter_streetview(sv_urls)
            print(f"    vision: {len(vision_hits)}/4 show damage")
            
            crossref = cross_reference(inc, coords)
            cx_count = sum(len(crossref.get(k, [])) for k in
                          ["mop_evidence", "attt_evidence", "social_posts",
                           "citizen_comments", "news_history"])
            print(f"    cross-ref: {cx_count} hits, pothole word count {crossref.get('pothole_word_count', 0)}")
            
            score = score_incident(inc, coords, crossref, vision_hits)
            print(f"    SCORE: {score['score']}")
            
            if SCORE_THRESHOLD_RANK.get(score["score"], 0) >= threshold:
                case_id = next_case_id(inv)
                sat = satellite_url(coords["lat"], coords["lng"])
                html = build_email_html(case_id, inc, coords, crossref,
                                        vision_hits, score, sv_urls, sat)
                subject = f"[{score['score']}] {case_id} · {inc['title'][:60]}"
                try:
                    send_email(subject, html)
                    print(f"    ✉  sent · {case_id}")
                    inv["incidents"][h] = {
                        "case_id": case_id,
                        "title": inc.get("title", ""),
                        "url": inc.get("url", ""),
                        "date": inc.get("date", ""),
                        "location_text": inc.get("location_text", ""),
                        "coords": coords,
                        "score": score["score"],
                        "vision_hits": len(vision_hits),
                        "crossref_size": cx_count,
                        "first_seen": datetime.utcnow().isoformat() + "Z",
                    }
                except Exception as e:
                    print(f"    ✗ send failed: {e}")
            else:
                print(f"    — below threshold, not sending")
    
    save_inventory(inv)
    print(f"\n=== Done · inventory now {len(inv['incidents'])} incidents ===")

if __name__ == "__main__":
    main()

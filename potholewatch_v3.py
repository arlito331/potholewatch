"""
PotholeWatch v3.0.7 — Multi-source road incident intelligence
============================================================
CADENCE:   Every 8 hours (cron: '0 */8 * * *')
DELIVERY:  ONE digest email per scan — all new incidents bundled
LOOK:      PowerFix brand — black bg, white text, red-orange accent

FIXES vs v3.0.6:
  - URL-based dedup (no more re-sending same article with different hash)
  - Street View DROPPED — replaced with article images + social image search
  - Social search uses targeted date-bounded queries to find real citizen posts
  - Scan timestamp human-readable format
  - Street View label cleaned up
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

# Brand
BG      = "#0D0D0D"
CARD_BG = "#1A1A1A"
TEXT    = "#FFFFFF"
MUTED   = "#999999"
DIM     = "#666666"
ACCENT  = "#D94F2B"
SOFT    = "#262626"

SCORE_COLOR = {"CRITICAL": "#FF3B3B", "HIGH": ACCENT, "MEDIUM": "#F0A030", "LOW": MUTED}

TERRITORIES = [
    {
        "name": "Panama",
        "country": "Panama",
        "language": "es",
        "search_terms": [
            "accidente carretera bache hueco Panama",
            "accidente vial Via Centenario Corredor Norte Sur Panama",
            "MOP Panama Tapa Hueco deterioro vial",
            "choque carro Via Brasil Transistmica Panama",
        ],
    },
]

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
    with open(INVENTORY_FILE, "w") as f:
        json.dump(inv, f, indent=2, ensure_ascii=False)

def incident_hash(location_text, date, title):
    key = f"{(location_text or '').lower().strip()}|{date or ''}|{(title or '').lower().strip()[:60]}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]

def next_case_id(inv):
    inv["counter"] += 1
    return f"PTW-{inv['counter']:04d}"

def already_seen(inv, inc):
    """Return True if this incident URL or hash is already in inventory."""
    url = inc.get("url", "")
    if url and url in inv.get("seen_urls", []):
        return True
    h = incident_hash(inc.get("location_text",""), inc.get("date",""), inc.get("title",""))
    return h in inv.get("incidents", {})

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
    # fallback: find all balanced { }
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
    prompt = f"""Search Spanish-language news for road accidents in {territory['name']} from the LAST 8 DAYS.

Run these queries:
{queries}

For each UNIQUE road accident found, output ONE JSON object per line (JSONL — no prose, no fences):
{{"title":"...","url":"...","source":"...","date":"YYYY-MM-DD",
  "location_text":"specific road + landmark + city/district",
  "summary":"what happened in 2-3 sentences",
  "article_image_urls":["direct image URLs from the article"],
  "pothole_keywords_found":["bache","hueco",...]}}

- Real road accidents only (collisions, rollovers, lost control)
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
# 3. SATELLITE (kept — useful for location context)
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

# ============================================================
# 4. SOCIAL IMAGE + POST SEARCH (replaces Street View)
# ============================================================

def search_social_evidence(incident, coords):
    """
    Search for citizen posts, images and comments specifically about this
    accident and this road — from X, Instagram, Facebook, Waze, and news comments.
    Returns structured evidence with image URLs and post quotes.
    """
    date_str = incident.get("date", "")
    location = incident.get("location_text", "")
    title_keywords = " ".join(incident.get("title", "").split()[:5])

    prompt = f"""Find real citizen social media posts and photos about this specific road accident.

ACCIDENT:
  Location: {location} ({coords.get('formatted','')})
  Date: {date_str}
  Title: {incident.get('title','')}
  Article: {incident.get('url','')}

Run targeted searches to find:

1. X/Twitter posts from around {date_str}: search 'site:x.com "{location.split(',')[0]}" accidente' and variations
2. Instagram posts: search 'site:instagram.com "{location.split(',')[0]}" accidente bache'
3. Facebook posts/comments: search 'site:facebook.com "{location.split(',')[0]}" accidente'
4. Waze alerts: search 'waze "{location.split(',')[0]}" accidente peligro bache'
5. News article comment sections: fetch {incident.get('url','')} and extract reader comments
6. Any photos posted by citizens showing the accident scene or road damage

For EACH piece of evidence found, note:
- The platform (X, Instagram, Facebook, Waze, News comments)
- The post URL if available
- The exact quote or description
- Any image URLs found in the post

Return ONE JSON object (no prose, no fences):
{{
  "x_posts": [{{"url":"...","user":"...","quote":"...","image_urls":["..."]}}],
  "instagram_posts": [{{"url":"...","user":"...","quote":"...","image_urls":["..."]}}],
  "facebook_posts": [{{"url":"...","user":"...","quote":"...","image_urls":["..."]}}],
  "waze_reports": [{{"url":"...","quote":"...","image_urls":[]}}],
  "news_comments": [{{"source":"...","quote":"..."}}],
  "citizen_image_urls": ["direct URLs to any citizen photos of this accident or road damage"],
  "pothole_word_count": <integer count of bache/hueco/cráter/forado mentions across all>
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
# 5. CROSS-REFERENCE — MOP, ATTT, news history
# ============================================================

def cross_reference(incident, coords):
    prompt = f"""Find institutional evidence linking this road to pothole/deterioration problems.

INCIDENT:
  Title: {incident.get('title','')}
  Location: {incident.get('location_text','')} ({coords.get('formatted','')})
  Date: {incident.get('date','')}

Search for:
  1. MOP Panama (Ministerio de Obras Publicas) — Tapa Hueco programs, contracts, TDR docs, deterioration reports for this road
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

def score_incident(incident, coords, social, crossref):
    social_count = sum(len(social.get(k,[])) for k in
                      ["x_posts","instagram_posts","facebook_posts","waze_reports","news_comments"])
    inst_count = sum(len(crossref.get(k,[])) for k in
                    ["mop_evidence","attt_evidence","news_history"])

    prompt = f"""Score pothole-correlation probability for this road accident.

INCIDENT: {json.dumps(incident, ensure_ascii=False)}
SOCIAL EVIDENCE: {social_count} citizen posts/comments found, pothole word count {social.get('pothole_word_count',0)}
SOCIAL DETAIL: {json.dumps({k:social[k] for k in ['x_posts','instagram_posts','facebook_posts','waze_reports','news_comments'] if social.get(k)}, ensure_ascii=False)}
INSTITUTIONAL EVIDENCE: {inst_count} MOP/ATTT/news items found
INSTITUTIONAL DETAIL: {json.dumps(crossref, ensure_ascii=False)}

Levels:
  - CRITICAL: Article names pothole/bache as cause, OR strong social + institutional evidence
  - HIGH: Multiple citizen posts + MOP/ATTT records OR strong news history
  - MEDIUM: Some social posts OR institutional evidence (1-2 items)
  - LOW: No supporting evidence

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
# 7. EMAIL — DIGEST
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

def build_incident_card(case_id, incident, coords, social, crossref, score, sat):
    color = SCORE_COLOR.get(score["score"], MUTED)

    # IMAGES: article first, then citizen photos
    article_imgs = [u for u in (incident.get("article_image_urls") or []) if u and u.startswith("http")][:3]
    citizen_imgs = [u for u in (social.get("citizen_image_urls") or []) if u and u.startswith("http")][:3]
    all_imgs = article_imgs + [u for u in citizen_imgs if u not in article_imgs]

    img_html = "".join(
        f'<img src="{u}" style="width:100%;border-radius:6px;margin-bottom:8px;display:block;" onerror="this.style.display=\'none\'" />'
        for u in all_imgs[:4]
    )

    bullets = "".join(f'<li style="margin-bottom:5px;">{b}</li>' for b in score.get("headline_evidence",[]))

    # Social sections
    social_html  = _section("X / Twitter",   social.get("x_posts",[]),         _post_row)
    social_html += _section("Instagram",      social.get("instagram_posts",[]), _post_row)
    social_html += _section("Facebook",       social.get("facebook_posts",[]),  _post_row)
    social_html += _section("Waze reports",   social.get("waze_reports",[]),    _post_row)
    social_html += _section("Reader comments",social.get("news_comments",[]),   _post_row)

    # Institutional sections
    inst_html  = _section("MOP Panama",  crossref.get("mop_evidence",[]),  _post_row)
    inst_html += _section("ATTT Panama", crossref.get("attt_evidence",[]), _post_row)

    if crossref.get("news_history"):
        inst_html += _section("Prior news on this road", crossref["news_history"], _news_row)

    if not social_html.strip() and not inst_html.strip():
        no_ev = f'<div style="font-size:12px;color:{DIM};font-style:italic;margin-top:14px;padding-top:14px;border-top:1px solid {SOFT};">No cross-reference evidence surfaced.</div>'
    else:
        no_ev = ""

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

  {social_html}
  {inst_html}
  {no_ev}

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
    <div style="font-size:11px;letter-spacing:4px;color:{ACCENT};font-weight:700;">POTHOLEWATCH · 8-HOUR DIGEST</div>
    <h1 style="margin:10px 0 6px;font-size:26px;color:{TEXT};font-weight:700;">{count} new incident{'s' if count!=1 else ''} detected</h1>
    <div style="color:{MUTED};font-size:12px;">Scan: {scan_time_human} · Panama territory</div>
  </div>

  {''.join(cards)}

  <div style="text-align:center;font-size:11px;color:{DIM};padding:24px 0;border-top:1px solid {SOFT};margin-top:8px;">
    <div style="font-size:10px;letter-spacing:3px;color:{ACCENT};font-weight:700;margin-bottom:6px;">POWERFIX · REPAIR. REINVENTED.</div>
    <div>PotholeWatch v3.0.7 — automated road incident intelligence.<br/>Next scan in 8 hours.</div>
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
    print(f"=== PotholeWatch v3.0.7 scan @ {scan_time_iso} ===")

    inv = load_inventory()
    print(f"Inventory: {len(inv['incidents'])} on file, last case PTW-{inv['counter']:04d}")

    threshold = SCORE_THRESHOLD_RANK[MIN_SCORE]
    cards = []
    pending = []  # [(hash, url, record)]

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

            # Social evidence (citizen posts, images, comments)
            social = search_social_evidence(inc, coords)
            sc = sum(len(social.get(k,[])) for k in
                    ["x_posts","instagram_posts","facebook_posts","waze_reports","news_comments"])
            ci = len(social.get("citizen_image_urls",[]))
            print(f"    social: {sc} posts, {ci} citizen images, pothole words {social.get('pothole_word_count',0)}")

            # Institutional cross-reference
            crossref = cross_reference(inc, coords)
            cx = sum(len(crossref.get(k,[])) for k in ["mop_evidence","attt_evidence","news_history"])
            print(f"    cross-ref: {cx} institutional hits")

            score = score_incident(inc, coords, social, crossref)
            print(f"    SCORE: {score['score']}")

            if SCORE_THRESHOLD_RANK.get(score["score"],0) < threshold:
                print(f"    — below threshold")
                continue

            case_id = next_case_id(inv)
            sat = satellite_url(coords["lat"], coords["lng"])
            card = build_incident_card(case_id, inc, coords, social, crossref, score, sat)
            cards.append(card)
            h = incident_hash(inc.get("location_text",""), inc.get("date",""), inc.get("title",""))
            pending.append((h, inc.get("url",""), {
                "case_id": case_id,
                "title": inc.get("title",""),
                "url": inc.get("url",""),
                "date": inc.get("date",""),
                "location_text": inc.get("location_text",""),
                "coords": coords,
                "score": score["score"],
                "social_hits": sc,
                "crossref_hits": cx,
                "first_seen": scan_time_iso,
            }))
            print(f"    ✓ bundled as {case_id}")

    if not cards:
        print(f"\n=== No new incidents above threshold — no email sent ===")
        save_inventory(inv)
        return

    print(f"\n--- Sending digest with {len(cards)} incident(s) ---")
    html = build_digest_email(cards, scan_time_human)
    subject = f"PotholeWatch · {len(cards)} new incident{'s' if len(cards)!=1 else ''} · {now.strftime('%b %d')}"

    try:
        send_email(subject, html)
        print(f"✉  digest sent")
        for h, url, rec in pending:
            inv["incidents"][h] = rec
            if url and url not in inv["seen_urls"]:
                inv["seen_urls"].append(url)
        # keep seen_urls from growing unboundedly — keep last 500
        inv["seen_urls"] = inv["seen_urls"][-500:]
    except Exception as e:
        print(f"✗ send failed: {e}")
        inv["counter"] -= len(pending)

    save_inventory(inv)
    print(f"\n=== Done · inventory now {len(inv['incidents'])} incidents ===")

if __name__ == "__main__":
    main()

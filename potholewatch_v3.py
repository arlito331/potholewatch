"""
PotholeWatch v5.1.0 — Citizen Correlation Engine + Persistent Case Tracking
============================================================================
Builds on v5.0 (citizen-chatter correlation) with:

  - LOOKBACK_DAYS knob (set to 30 for this catch-up run to reach the
    Vía Centenario case; flip back to 8 afterward)
  - LIGHTWEIGHT GEOCODING — only for placing a map pin, NOT for
    finding potholes
  - SMART DEDUP by road + location (same road = same case, merged)
  - PERSISTENT CASE NUMBERS — PTW-0001 onward, fixed forever per case
  - RE-ALERT ONLY WHEN MENTIONS GROW by >= REALERT_THRESHOLD (default 3)
    since the last time the case was alerted. Otherwise the case stays
    on the dashboard quietly accumulating chatter.

PHILOSOPHY unchanged: correlation report card. Present what people are
saying about the road. No verdicts, no cost, no Street View. You decide.
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
LOOKBACK_DAYS = 30      # <<< ONE-TIME CATCH-UP: set back to 8 after this run
REALERT_THRESHOLD = 3   # re-surface a known case only if mentions grow by >= this
# =============================================================

ROAD_KEYWORDS = [
    "bache", "baches", "hueco", "huecos", "cráter", "crater",
    "deteriorada", "deteriorado", "mal estado", "rota", "destruida",
    "sin asfalto", "pavimento", "asfalto dañado", "calle dañada",
    "carretera dañada", "peligrosa", "mala condición",
]

# Brand
BG      = "#0D0D0D"
CARD_BG = "#1A1A1A"
TEXT    = "#FFFFFF"
MUTED   = "#999999"
DIM     = "#666666"
ACCENT  = "#D94F2B"
SOFT    = "#262626"
NEW_TAG = "#4ADE80"

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
DATE_ANCHOR = f"""CRITICAL CONTEXT: Today is {TODAY}. All accident dates referenced are REAL events that have ALREADY HAPPENED in the recent past. These are factual news events. You MUST search for them. Never refuse a search because a date seems recent or future — the dates are real and in the past relative to today. Proceed with all searches."""

# ============================================================
# ROAD-KEY NORMALIZATION (for dedup by road + location)
# ============================================================

_STOPWORDS = {
    "via","vía","calle","carretera","autopista","corredor","sector","entrada",
    "el","la","los","las","de","del","y","en","a","hacia","entre","altura",
    "panama","panamá","provincia","corregimiento","distrito","ciudad",
}

def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")

def road_key(location_text):
    """
    Normalize a location string into a stable 'road key' so that the same
    road/location across different articles collapses to one case.
    e.g. 'Vía Centenario, Río Pedro Miguel, Panamá' -> 'centenario riopedromiguel'
    """
    if not location_text:
        return ""
    s = _strip_accents(location_text.lower())
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    tokens = [t for t in s.split() if t and t not in _STOPWORDS and len(t) > 2]
    # keep the most distinctive tokens (first few after stopword removal)
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
            print("  inventory corrupt, starting fresh")
            return {"counter": 0, "cases": {}, "seen_urls": []}
    if not isinstance(inv, dict):
        inv = {}
    inv.setdefault("counter", 0)
    inv.setdefault("seen_urls", [])
    # migrate older formats: prefer 'cases', fall back to 'incidents'
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
        # dashboard reads "incidents"; keep that key for compatibility
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
- Include the specific road/location as precisely as the article states
- JSONL only, one object per line."""
    raw = claude_call(prompt, tools=[WEB_SEARCH_TOOL], max_tokens=4000)
    return extract_jsonl(raw)

# ============================================================
# 2. CITIZEN CORRELATION — CORE
# ============================================================

def find_citizen_chatter(incident):
    location = incident.get("location_text", "")
    loc_short = location.split(",")[0] if location else location
    date_str = incident.get("date", "")
    keywords = ", ".join(ROAD_KEYWORDS[:12])

    prompt = f"""{DATE_ANCHOR}

Your single job: find any PUBLIC mention by citizens, witnesses, journalists, or commenters
that ties the road/location of this accident to BAD ROAD CONDITIONS (potholes, deterioration).

ACCIDENT:
  Location: {location}
  Date: {date_str}
  Headline: {incident.get('title','')}
  Article: {incident.get('url','')}

ROAD-CONDITION KEYWORDS (Spanish): {keywords}

Run AGGRESSIVE searches — use all available searches. Angles:
1. Fetch the news article and look for witness quotes about road conditions
2. News comment sections: '"{loc_short}" bache OR hueco OR "mal estado" accidente'
3. Other outlets covering the same road for condition complaints
4. Public X/Twitter posts: '"{loc_short}" bache OR hueco peligro'
5. Public Facebook posts: 'site:facebook.com "{loc_short}" bache OR hueco'
6. Instagram public posts mentioning the road + conditions
7. ANY prior complaints about this specific road's condition (last 12 months)
8. MOP/government acknowledgement of problems on this road

For EVERY mention where a real person/source connects this road to bad conditions,
capture it. Prioritize DIRECT QUOTES.

Return ONE JSON object (no prose, no fences):
{{
  "chatter_found": true/false,
  "mentions": [
    {{
      "source_type": "news_article|news_comment|twitter|facebook|instagram|forum|mop_gov",
      "source_name": "outlet or username or platform",
      "url": "direct link if available",
      "quote": "the actual words used (Spanish, verbatim)",
      "date": "YYYY-MM-DD if known, else empty",
      "keywords_matched": ["bache","hueco",...]
    }}
  ],
  "summary": "1-2 sentence NEUTRAL summary of what citizens/sources are saying about this road's condition. Present only — do NOT conclude anything about the accident cause."
}}

RULES:
- Only mentions genuinely referencing THIS road/location's condition.
- Quotes must be real — never invent.
- If nothing found, chatter_found:false with empty mentions.
- Summary stays neutral. No verdicts.

JSON only."""

    raw = claude_call(prompt, tools=[WEB_SEARCH_TOOL], max_tokens=6000)
    parsed = extract_json_object(raw)
    if parsed is None:
        print(f"      chatter parse failed — raw[:160]: {raw[:160]!r}")
        return {"chatter_found": False, "mentions": [], "summary": ""}
    parsed.setdefault("mentions", [])
    parsed.setdefault("summary", "")
    parsed["chatter_found"] = bool(parsed["mentions"])
    return parsed

# ============================================================
# 3. LIGHTWEIGHT GEOCODE (map pin only)
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
                return {"lat": loc["lat"], "lng": loc["lng"],
                        "formatted": data["results"][0]["formatted_address"]}
        except Exception as e:
            print(f"      geocode error: {e}")
    return None

# ============================================================
# 4. MENTION DEDUP (within a case over time)
# ============================================================

def mention_signature(m):
    """Stable signature for a single mention, to detect genuinely-new ones."""
    q = _strip_accents((m.get("quote","") or "").lower()).strip()
    q = re.sub(r"\s+", " ", q)[:120]
    url = (m.get("url","") or "").strip().lower()
    return hashlib.sha256(f"{url}|{q}".encode()).hexdigest()[:16]

def merge_mentions(existing, incoming):
    """Return (merged_list, num_new) — dedup by signature."""
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

SRC_LABEL = {
    "news_article": "News article", "news_comment": "Reader comment",
    "twitter": "X / Twitter", "facebook": "Facebook", "instagram": "Instagram",
    "forum": "Forum", "mop_gov": "MOP / Gov",
}

def _mention_row(m):
    label = SRC_LABEL.get(m.get("source_type",""), m.get("source_type","Source"))
    name = m.get("source_name","")
    quote = (m.get("quote","") or "").replace("<","&lt;").replace(">","&gt;")
    url = m.get("url","")
    link = f' <a href="{url}" style="color:{ACCENT};text-decoration:none;">&rarr;</a>' if url else ''
    src = label + (f" · {name}" if name else "")
    return f'''<div style="margin:8px 0;padding-left:10px;border-left:2px solid {SOFT};">
      <div style="font-size:10px;letter-spacing:1px;color:{ACCENT};text-transform:uppercase;margin-bottom:2px;">{src}</div>
      <div style="font-size:13px;line-height:1.5;color:{MUTED};">&ldquo;{quote}&rdquo;{link}</div>
    </div>'''

def build_card(case, new_count, is_new_case):
    imgs = case.get("article_image_urls", [])
    img_html = "".join(
        f'<img src="{u}" style="width:100%;border-radius:6px;margin-bottom:8px;display:block;" onerror="this.style.display=\'none\'" />'
        for u in imgs[:2]
    )
    mentions_html = "".join(_mention_row(m) for m in case.get("mentions", [])[:8])
    kw = case.get("keywords_matched", [])
    kw_html = ""
    if kw:
        chips = "".join(
            f'<span style="display:inline-block;background:{SOFT};color:{ACCENT};font-size:10px;padding:2px 8px;border-radius:10px;margin:2px;">{k}</span>'
            for k in kw[:10]
        )
        kw_html = f'<div style="margin:10px 0;">{chips}</div>'

    count = len(case.get("mentions", []))
    tag = ""
    if is_new_case:
        tag = f'<span style="background:{NEW_TAG};color:#000;font-size:9px;font-weight:700;letter-spacing:1px;padding:2px 8px;border-radius:10px;margin-left:8px;">NEW CASE</span>'
    elif new_count > 0:
        tag = f'<span style="background:{NEW_TAG};color:#000;font-size:9px;font-weight:700;letter-spacing:1px;padding:2px 8px;border-radius:10px;margin-left:8px;">+{new_count} NEW</span>'

    return f"""
<div style="background:{CARD_BG};border-radius:8px;padding:24px;margin-bottom:20px;border-left:4px solid {ACCENT};">
  <div style="font-size:10px;letter-spacing:3px;color:{ACCENT};font-weight:700;">{case['ptw_id']} · {count} CITIZEN MENTION{'S' if count!=1 else ''}{tag}</div>
  <h2 style="margin:8px 0 6px;font-size:20px;color:{TEXT};line-height:1.3;font-weight:700;">{case.get('headline','')}</h2>
  <div style="color:{DIM};font-size:12px;margin-bottom:16px;">
    {case.get('location','')} · {case.get('date','')} · <a href="{case.get('url','')}" style="color:{ACCENT};text-decoration:none;">{case.get('source_name','')}</a>
  </div>
  <div style="background:{SOFT};padding:16px;border-radius:6px;margin-bottom:16px;">
    {img_html}
    <div style="font-size:14px;line-height:1.6;color:{TEXT};">{case.get('summary','')}</div>
    <div style="margin-top:14px;"><a href="{case.get('url','')}" style="display:inline-block;background:{ACCENT};color:{TEXT};padding:10px 18px;border-radius:4px;font-size:12px;text-decoration:none;font-weight:700;letter-spacing:1px;">READ FULL ARTICLE &rarr;</a></div>
  </div>
  <div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin-bottom:8px;">What people are saying about this road</div>
  <div style="font-size:12px;color:{MUTED};font-style:italic;margin-bottom:8px;">{case.get('chatter_summary','')}</div>
  {kw_html}
  {mentions_html}
</div>"""

def build_digest(cards, scan_time_human):
    count = len(cards)
    return f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;background:{BG};padding:24px;color:{TEXT};margin:0;">
<div style="max-width:720px;margin:auto;">
  <div style="margin-bottom:24px;padding:24px;background:{CARD_BG};border-radius:8px;border-top:4px solid {ACCENT};">
    <div style="font-size:11px;letter-spacing:4px;color:{ACCENT};font-weight:700;">POTHOLEWATCH · CITIZEN CORRELATION DIGEST</div>
    <h1 style="margin:10px 0 6px;font-size:26px;color:{TEXT};font-weight:700;">{count} case{'s' if count!=1 else ''} with new road-condition chatter</h1>
    <div style="color:{MUTED};font-size:12px;">Scan: {scan_time_human} · Panama · {LOOKBACK_DAYS}-day window</div>
  </div>
  {''.join(cards)}
  <div style="text-align:center;font-size:11px;color:{DIM};padding:24px 0;border-top:1px solid {SOFT};margin-top:8px;">
    <div style="font-size:10px;letter-spacing:3px;color:{ACCENT};font-weight:700;margin-bottom:6px;">POWERFIX · REPAIR. REINVENTED.</div>
    <div>PotholeWatch v5.1 — citizen correlation engine.<br/>A report of what people are saying. You decide.</div>
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
    print(f"=== PotholeWatch v5.1.0 @ {scan_time_iso} ===")
    print(f"=== Today: {TODAY} · lookback {LOOKBACK_DAYS}d · re-alert +{REALERT_THRESHOLD} ===")

    inv = load_inventory()
    print(f"Inventory: {len(inv['cases'])} cases on file, last PTW-{inv['counter']:04d}")

    cards = []  # (case_dict, new_count, is_new_case)

    for territory in TERRITORIES:
        print(f"\n--- Territory: {territory['name']} ---")
        try:
            incidents = search_incidents(territory)
        except Exception as e:
            print(f"  search failed: {e}")
            continue
        print(f"  Found {len(incidents)} candidate incidents")

        for inc in incidents:
            title = inc.get("title","(no title)")
            loc = inc.get("location_text","")
            rk = road_key(loc)
            print(f"  · {title[:60]}")
            print(f"    loc: {loc}  | road_key: {rk}")

            # hunt chatter
            try:
                chatter = find_citizen_chatter(inc)
            except Exception as e:
                print(f"    chatter failed: {e}")
                chatter = {"chatter_found": False, "mentions": [], "summary": ""}
            incoming = chatter.get("mentions", [])
            print(f"    citizen mentions: {len(incoming)}")

            if not incoming:
                print(f"    — no road chatter, skipping")
                if inc.get("url"):
                    inv["seen_urls"].append(inc["url"])
                continue

            existing_case = inv["cases"].get(rk) if rk else None

            if existing_case:
                # MERGE into existing case, detect genuinely-new mentions
                merged, new_count = merge_mentions(existing_case.get("mentions", []), incoming)
                existing_case["mentions"] = merged[:30]
                # refresh keyword union
                kw = set(existing_case.get("keywords_matched", []))
                for m in incoming:
                    for k in (m.get("keywords_matched") or []):
                        kw.add(k.lower())
                existing_case["keywords_matched"] = sorted(kw)
                existing_case["mention_count"] = len(merged)
                existing_case["last_seen"] = scan_time_iso
                if chatter.get("summary"):
                    existing_case["chatter_summary"] = chatter["summary"]

                print(f"    known case {existing_case['ptw_id']} — {new_count} NEW mention(s) (total {len(merged)})")

                if new_count >= REALERT_THRESHOLD:
                    cards.append((existing_case, new_count, False))
                    print(f"    ✓ re-alerting (>= +{REALERT_THRESHOLD})")
                else:
                    print(f"    · below re-alert threshold, dashboard updated silently")
            else:
                # NEW CASE
                coords = geocode(loc, territory.get("country","Panama"))
                if coords:
                    print(f"    geocoded → {coords['lat']:.4f}, {coords['lng']:.4f}")
                else:
                    print(f"    geocode: no pin")

                case_id = next_case_id(inv)
                kw = set()
                for m in incoming:
                    for k in (m.get("keywords_matched") or []):
                        kw.add(k.lower())
                best_q = ""; best_src = ""
                for m in incoming:
                    if (m.get("quote") or "").strip():
                        best_q = m["quote"].strip()
                        best_src = m.get("source_name","") or m.get("source_type","")
                        break
                case = {
                    "ptw_id": case_id,
                    "road_key": rk,
                    "headline": inc.get("title",""),
                    "location": loc,
                    "date": inc.get("date",""),
                    "summary": inc.get("summary",""),
                    "source_name": inc.get("source",""),
                    "url": inc.get("url",""),
                    "lat": coords["lat"] if coords else None,
                    "lng": coords["lng"] if coords else None,
                    "geo_formatted": coords["formatted"] if coords else "",
                    "first_seen": scan_time_iso,
                    "last_seen": scan_time_iso,
                    "article_image_urls": [u for u in (inc.get("article_image_urls") or []) if u and u.startswith("http")][:4],
                    "primary_image_url": None,
                    "chatter_summary": chatter.get("summary",""),
                    "mention_count": len(incoming),
                    "keywords_matched": sorted(kw),
                    "best_quote": best_q,
                    "best_quote_source": best_src,
                    "mentions": incoming[:30],
                }
                if case["article_image_urls"]:
                    case["primary_image_url"] = case["article_image_urls"][0]
                inv["cases"][rk] = case
                if inc.get("url"):
                    inv["seen_urls"].append(inc["url"])
                cards.append((case, len(incoming), True))
                print(f"    ✓ NEW CASE {case_id} — {len(incoming)} mention(s)")

    inv["seen_urls"] = inv["seen_urls"][-1000:]

    if not cards:
        print(f"\n=== No new/updated cases above threshold — no email ===")
        save_inventory(inv)
        return

    print(f"\n--- Sending digest: {len(cards)} case(s) ---")
    # sort: new cases + bigger jumps first
    cards.sort(key=lambda c: (0 if c[2] else 1, -c[1]))
    html = build_digest([build_card(c, n, isnew) for (c, n, isnew) in cards], scan_time_human)
    n_new = sum(1 for c in cards if c[2])
    subject = f"PotholeWatch · {len(cards)} case{'s' if len(cards)!=1 else ''} ({n_new} new) · {now.strftime('%b %d')}"

    try:
        send_email(subject, html)
        print(f"✉  digest sent")
    except Exception as e:
        print(f"✗ send failed: {e}")

    save_inventory(inv)
    print(f"\n=== Done · inventory now {len(inv['cases'])} cases ===")

if __name__ == "__main__":
    main()

"""
PotholeWatch v3.0 — Multi-source road incident intelligence
============================================================
Cadence:  every 8 hours (3 scans/day)
Weekly:   Sundays 08:00 Panama time (13:00 UTC)

Pipeline per scan:
  1. Web search → fresh accident news per territory
  2. Geocode location → lat/lng
  3. DEDUP against incidents.json inventory (hash of location+date+headline)
        - New incident → full pipeline
        - Known incident → check for new social/news activity → maybe update alert
  4. Cross-reference (MOP, ATTT, Instagram/X, news comments scanned for
     bache/hueco/cráter/forado)
  5. Pull Street View sweep (multiple headings + offsets along road)
     → AI vision filters to images that actually show road damage
  6. AI scorer combines ALL evidence + comment-mining bonus
  7. If Medium+ → rich HTML email + commit to inventory

Weekly report: Sunday → summary of all incidents tracked that week.
"""

import os, json, base64, hashlib, math, requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ============================================================
# CONFIG
# ============================================================

ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_MAPS_API_KEY  = os.environ["GOOGLE_MAPS_API_KEY"]
GMAIL_CLIENT_ID      = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET  = os.environ["GMAIL_CLIENT_SECRET"]
GMAIL_REFRESH_TOKEN  = os.environ["GMAIL_REFRESH_TOKEN"]

ALERT_RECIPIENTS  = ["joel@powerfixinc.com", "1@powerfixinc.com"]
SCORE_THRESHOLD   = "MEDIUM"
INVENTORY_FILE    = "incidents.json"

POTHOLE_WORDS = ["bache", "baches", "hueco", "huecos", "cráter", "crater",
                 "forado", "roto", "dañado", "dañada", "pothole", "potholes"]

TERRITORIES = [
    {
        "name": "Panama",
        "country": "Panama",
        "language": "es",
        "search_terms": [
            "accidente carretera Panama",
            "accidente Via Centenario Corredor Norte Sur Panama",
            "choque bache hueco carretera Panama",
            "MOP Panama deterioro vial Tapa Hueco",
        ],
    },
]

CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

# ============================================================
# INVENTORY
# ============================================================

def load_inventory():
    if not os.path.exists(INVENTORY_FILE):
        return {"counter": 0, "incidents": {}}
    with open(INVENTORY_FILE) as f:
        return json.load(f)

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
# CLAUDE CALLS
# ============================================================

def claude_call(prompt, tools=None, max_tokens=3000):
    payload = {"model": CLAUDE_MODEL, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    if tools:
        payload["tools"] = tools
    r = requests.post("https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json=payload, timeout=180)
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")

def claude_vision(image_url, prompt):
    img_bytes = requests.get(image_url, timeout=20).content
    b64 = base64.standard_b64encode(img_bytes).decode()
    r = requests.post("https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": CLAUDE_MODEL, "max_tokens": 300,
              "messages": [{"role": "user", "content": [
                  {"type": "image", "source": {"type": "base64",
                                                "media_type": "image/jpeg",
                                                "data": b64}},
                  {"type": "text", "text": prompt}]}]}, timeout=60)
    r.raise_for_status()
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
    prompt = f"""Search for road accident news from the last 8 days in {territory['name']}.

Run these queries:
{queries}

For EACH unique accident, output ONE JSON object per line (JSONL):
{{"title":"...","url":"...","source":"...","date":"YYYY-MM-DD",
  "location_text":"exact road/intersection","summary":"2-3 sentences",
  "article_image_urls":["..."]}}

Only road accidents. JSONL only — no preamble, no fences."""
    text = claude_call(prompt,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        max_tokens=4000)
    incidents = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try: incidents.append(json.loads(line))
            except: pass
    print(f"  Found {len(incidents)} candidate incidents")
    return incidents

# ============================================================
# 2. GEOCODING
# ============================================================

def geocode(location_text, country="Panama"):
    r = requests.get("https://maps.googleapis.com/maps/api/geocode/json",
        params={"address": f"{location_text}, {country}", "key": GOOGLE_MAPS_API_KEY},
        timeout=15)
    data = r.json()
    if data.get("status") != "OK" or not data.get("results"):
        return None
    loc = data["results"][0]["geometry"]["location"]
    return {"lat": loc["lat"], "lng": loc["lng"],
            "formatted": data["results"][0]["formatted_address"]}

# ============================================================
# 3. STREET VIEW + VISION
# ============================================================

def offset_coord(lat, lng, dx_m, dy_m):
    dlat = dy_m / 111111
    dlng = dx_m / (111111 * max(abs(math.cos(math.radians(lat))), 0.01))
    return lat + dlat, lng + dlng

def sv_url(lat, lng, heading):
    return ("https://maps.googleapis.com/maps/api/streetview"
            f"?size=640x400&location={lat},{lng}&heading={heading}&pitch=-15&fov=80"
            f"&key={GOOGLE_MAPS_API_KEY}")

def sv_has_imagery(lat, lng):
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/streetview/metadata",
            params={"location": f"{lat},{lng}", "key": GOOGLE_MAPS_API_KEY}, timeout=10)
        return r.json().get("status") == "OK"
    except:
        return False

def sweep_street_view(lat, lng, max_images=12):
    """3 points × 4 headings = 12 frames max (was 24)."""
    points = [(lat, lng)]
    for dx, dy in [(15, 0), (-15, 0)]:   # dropped from 4 offsets to 2
        points.append(offset_coord(lat, lng, dx, dy))
    candidates = []
    for plat, plng in points:
        if not sv_has_imagery(plat, plng):
            continue
        for heading in [0, 90, 180, 270]:    # dropped from 8 headings to 4
            candidates.append({"url": sv_url(plat, plng, heading),
                               "lat": plat, "lng": plng, "heading": heading})
            if len(candidates) >= max_images:
                return candidates
    return candidates

def vision_filter_potholes(candidates, top_n=4):
    prompt = ("Look at this Google Street View image. Answer with JSON ONLY:\n"
              '{"has_road_damage": true/false, "confidence": 0-100, "description": "what you see"}\n'
              "Look for: potholes, cracked asphalt, broken slabs, sunken sections, "
              "patches of repair, exposed base material. Ignore: shadows, manhole "
              "covers, paint markings, normal wear.")
    scored = []
    for c in candidates:
        try:
            resp = claude_vision(c["url"], prompt)
            data = json.loads(strip_json(resp))
            if data.get("has_road_damage"):
                c["confidence"] = data.get("confidence", 0)
                c["description"] = data.get("description", "")
                scored.append(c)
                print(f"      ✓ damage {c['confidence']}% heading {c['heading']}")
        except Exception:
            continue
    scored.sort(key=lambda x: x["confidence"], reverse=True)
    return scored[:top_n]

# ============================================================
# 4. CROSS-REFERENCE
# ============================================================

def cross_reference(incident, coords):
    word_list = ", ".join(POTHOLE_WORDS)
    prompt = f"""Cross-reference this accident location for pothole evidence.

LOCATION: {incident['location_text']}
COORDINATES: {coords['lat']}, {coords['lng']}
ARTICLE: {incident.get('title')} — {incident.get('url')}

Search for:
1. MOP Panama (Ministerio de Obras Públicas) — Tapa Hueco plans / contracts mentioning this road
2. ATTT (Autoridad de Tránsito) — incident reports at this location
3. Instagram posts mentioning this road and road damage
4. X (Twitter) posts about this location
5. Article comments mentioning any of: {word_list}
6. Prior news coverage (La Prensa, Crítica, TVN, Mi Diario) of road damage here

Return ONE JSON object:
{{
  "mop_evidence":[{{"source":"...","url":"...","summary":"..."}}],
  "attt_evidence":[...],
  "social_posts":[{{"platform":"instagram|x","url":"...","snippet":"..."}}],
  "news_history":[{{"source":"...","url":"...","date":"...","summary":"..."}}],
  "citizen_comments":[{{"text":"verbatim quote","contains_pothole_word":true,"source":"..."}}]
}}

JSON only, no preamble, no fences. Empty arrays if nothing."""
    text = claude_call(prompt,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
        max_tokens=3500)
    try:
        return json.loads(strip_json(text))
    except:
        return {"mop_evidence": [], "attt_evidence": [], "social_posts": [],
                "news_history": [], "citizen_comments": []}

def count_pothole_confirmations(crossref):
    return sum(1 for c in crossref.get("citizen_comments", [])
               if c.get("contains_pothole_word"))

# ============================================================
# 5. SCORER
# ============================================================

def score_incident(incident, coords, crossref, vision_hits, pw_count):
    prompt = f"""Score the probability that road condition contributed to this accident.

INCIDENT: {json.dumps(incident, ensure_ascii=False)}
LOCATION: {coords['formatted']}
CROSS-REFERENCE: {json.dumps(crossref, ensure_ascii=False)[:3000]}
VISUAL EVIDENCE: AI vision found {len(vision_hits)} Street View frames with road damage.
CITIZEN POTHOLE MENTIONS: {pw_count} verbatim mentions of bache/hueco/cráter.

Scoring:
- CRITICAL: vision + citizen mentions + MOP/ATTT + article cites road
- HIGH: vision hits OR 2+ citizen mentions OR strong MOP/news history
- MEDIUM: some evidence (1-2 sources, plausible)
- LOW: no road-condition evidence

JSON: {{"score":"LOW|MEDIUM|HIGH|CRITICAL","reasoning":"2-3 sentences","key_evidence":["...","..."]}}"""
    text = claude_call(prompt, max_tokens=800)
    return json.loads(strip_json(text))

# ============================================================
# 6. EMAIL
# ============================================================

SCORE_COLORS = {"CRITICAL":"#8B0000","HIGH":"#D94F2B","MEDIUM":"#E89B2B","LOW":"#999"}
SCORE_RANK   = {"LOW":0,"MEDIUM":1,"HIGH":2,"CRITICAL":3}

def maps_static_url(lat, lng):
    return ("https://maps.googleapis.com/maps/api/staticmap"
            f"?center={lat},{lng}&zoom=18&size=640x400&maptype=hybrid"
            f"&markers=color:red|{lat},{lng}&key={GOOGLE_MAPS_API_KEY}")

def build_email_html(case_id, incident, coords, crossref, score, vision_hits, article_photos, is_update=False):
    color = SCORE_COLORS.get(score["score"], "#999")
    update_tag = "<span style='background:#444;color:#fff;padding:2px 8px;font-size:11px;border-radius:3px;margin-left:8px'>UPDATE</span>" if is_update else ""
    
    sv_html = ""
    if vision_hits:
        sv_html = "<h3>Road Damage Detected at Incident Coordinates</h3>"
        sv_html += "<p style='color:#666;font-size:13px'>AI vision scanned Street View imagery; these frames show road damage:</p>"
        sv_html += "<table cellpadding='6'>"
        for i in range(0, len(vision_hits), 2):
            sv_html += "<tr>"
            for h in vision_hits[i:i+2]:
                sv_html += (f"<td valign='top'><img src='{h['url']}' width='300'/>"
                            f"<br/><small>Confidence: {h['confidence']}% — {h['description'][:120]}</small></td>")
            sv_html += "</tr>"
        sv_html += "</table>"
    
    photos_html = ""
    if article_photos:
        imgs = "".join(f'<img src="{u}" width="240" style="margin:4px"/>' for u in article_photos[:4])
        photos_html = f"<h3>News Article Photos</h3>{imgs}"
    
    crossref_html = ""
    for label, key in [("MOP Records", "mop_evidence"), ("ATTT Reports", "attt_evidence"),
                       ("Social Posts", "social_posts"), ("Prior Coverage", "news_history")]:
        items = crossref.get(key, [])
        if items:
            crossref_html += f"<h4>{label}</h4><ul>"
            for it in items:
                url = it.get("url", "#")
                snip = it.get("summary") or it.get("snippet") or ""
                src = it.get("source") or it.get("platform") or "link"
                crossref_html += f'<li><a href="{url}">{src}</a> — {snip}</li>'
            crossref_html += "</ul>"
    
    comments_html = ""
    if crossref.get("citizen_comments"):
        comments_html = "<h4>Citizen Confirmations <span style='color:#D94F2B'>(bingo signals)</span></h4><ul>"
        for c in crossref["citizen_comments"]:
            badge = "🎯" if c.get("contains_pothole_word") else ""
            comments_html += f'<li>{badge} <em>"{c["text"]}"</em> — {c.get("source", "")}</li>'
        comments_html += "</ul>"
    
    return f"""<!doctype html><html><body style="font-family:Arial,sans-serif;max-width:820px;margin:auto">
    <div style="background:#0D0D0D;color:#fff;padding:20px">
      <h1 style="margin:0">POTHOLEWATCH ALERT{update_tag}</h1>
      <p style="margin:4px 0 0">Case <strong>{case_id}</strong> · {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
    </div>
    <div style="background:{color};color:#fff;padding:12px 20px;font-size:18px">
      <strong>PROBABILITY: {score['score']}</strong>
    </div>
    <div style="padding:20px">
      <h2 style="margin-top:0">{incident['title']}</h2>
      <p><strong>Location:</strong> {coords['formatted']}<br/>
      <strong>Coordinates:</strong> {coords['lat']:.5f}, {coords['lng']:.5f}<br/>
      <strong>Date:</strong> {incident.get('date','—')}<br/>
      <strong>Source:</strong> <a href="{incident.get('url','#')}">{incident.get('source','')}</a></p>
      <p>{incident.get('summary','')}</p>

      <h3>Scoring</h3>
      <p>{score['reasoning']}</p>
      <ul>{"".join(f"<li>{e}</li>" for e in score.get('key_evidence', []))}</ul>

      {sv_html}

      <h3>Satellite View</h3>
      <img src="{maps_static_url(coords['lat'], coords['lng'])}" width="600"/>

      {photos_html}

      <h3>Cross-Reference</h3>
      {crossref_html}
      {comments_html}

      <hr/><p style="color:#888;font-size:12px">PotholeWatch v3.0 · PowerFix Inc. · {case_id}</p>
    </div></body></html>"""

def gmail_service():
    creds = Credentials(token=None, refresh_token=GMAIL_REFRESH_TOKEN,
        client_id=GMAIL_CLIENT_ID, client_secret=GMAIL_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token")
    return build("gmail", "v1", credentials=creds)

def send_email(html, subject):
    msg = MIMEMultipart("alternative")
    msg["to"] = ", ".join(ALERT_RECIPIENTS)
    msg["subject"] = subject
    msg.attach(MIMEText(html, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service().users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"    ✉ sent to {ALERT_RECIPIENTS}")

# ============================================================
# 7. WEEKLY REPORT
# ============================================================

def maybe_send_weekly_report(inv):
    """Sundays 13:00 UTC = 08:00 Panama time."""
    now = datetime.utcnow()
    if now.weekday() != 6 or now.hour != 13:
        return
    week_ago = (now - timedelta(days=7)).isoformat()
    week_incidents = [i for i in inv["incidents"].values()
                      if i.get("first_seen", "") >= week_ago]
    if not week_incidents:
        return
    rows = ""
    for i in sorted(week_incidents, key=lambda x: x["first_seen"]):
        rows += (f"<tr><td>{i['case_id']}</td><td>{i.get('date','—')}</td>"
                 f"<td>{i.get('location','')}</td><td>{i.get('score','')}</td>"
                 f"<td><a href='{i.get('url','#')}'>{i.get('source','')}</a></td></tr>")
    html = f"""<!doctype html><html><body style="font-family:Arial,sans-serif;max-width:820px;margin:auto">
    <div style="background:#0D0D0D;color:#fff;padding:20px">
      <h1 style="margin:0">POTHOLEWATCH — WEEKLY REPORT</h1>
      <p>Week ending {now.strftime('%Y-%m-%d')} · {len(week_incidents)} incidents tracked</p>
    </div>
    <div style="padding:20px">
      <table border="1" cellpadding="8" style="border-collapse:collapse;width:100%">
        <tr style="background:#F5F2ED"><th>Case</th><th>Date</th><th>Location</th><th>Score</th><th>Source</th></tr>
        {rows}
      </table>
      <p style="color:#888;font-size:12px;margin-top:20px">Total incidents in inventory: {len(inv['incidents'])} ·
      Last case: PTW-{inv['counter']:04d}</p>
    </div></body></html>"""
    send_email(html, f"📊 PotholeWatch Weekly · Week ending {now.strftime('%Y-%m-%d')}")
    print("  ✉ weekly report sent")

# ============================================================
# 8. MAIN
# ============================================================

def main():
    print(f"\n=== PotholeWatch v3 scan @ {datetime.utcnow().isoformat()}Z ===")
    inv = load_inventory()
    print(f"Inventory: {len(inv['incidents'])} on file, last case PTW-{inv['counter']:04d}")

    for territory in TERRITORIES:
        print(f"\n>>> Territory: {territory['name']}")
        for incident in search_incidents(territory):
            loc_text = incident.get("location_text", "")
            if not loc_text or not incident.get("title"):
                continue
            h = incident_hash(loc_text, incident.get("date", ""), incident["title"])
            print(f"\n  • {incident['title'][:70]}  [{h}]")

            # DEDUP path
            if h in inv["incidents"]:
                known = inv["incidents"][h]
                print(f"    ↺ Known {known['case_id']} — checking for new activity")
                coords = known.get("coords")
                if not coords:
                    continue
                new_crossref = cross_reference(incident, coords)
                old_size = known.get("crossref_size", 0)
                new_size = sum(len(new_crossref.get(k, [])) for k in
                               ["mop_evidence","attt_evidence","social_posts",
                                "news_history","citizen_comments"])
                if new_size > old_size:
                    print(f"    ⚡ +{new_size - old_size} new evidence — UPDATE")
                    score_obj = {"score": known.get("score", "MEDIUM"),
                                 "reasoning": f"Update — {new_size - old_size} new evidence items since {known['first_seen'][:10]}",
                                 "key_evidence": []}
                    html = build_email_html(known["case_id"], incident, coords,
                                            new_crossref, score_obj, [],
                                            incident.get("article_image_urls", []),
                                            is_update=True)
                    send_email(html, f"🔁 UPDATE {known['case_id']} · {coords['formatted'][:50]}")
                    known["crossref_size"] = new_size
                    known["last_update"] = datetime.utcnow().isoformat()
                continue

            # NEW incident
            coords = geocode(loc_text, country=territory["country"])
            if not coords:
                print(f"    ⚠ geocode failed"); continue
            print(f"    📍 {coords['lat']:.5f}, {coords['lng']:.5f}")

            print(f"    🔍 cross-referencing...")
            crossref = cross_reference(incident, coords)
            pw_count = count_pothole_confirmations(crossref)
            print(f"       MOP:{len(crossref['mop_evidence'])} "
                  f"ATTT:{len(crossref['attt_evidence'])} "
                  f"Social:{len(crossref['social_posts'])} "
                  f"News:{len(crossref['news_history'])} "
                  f"🎯Bingo:{pw_count}")

            print(f"    👁 Street View sweep + vision...")
            sv_candidates = sweep_street_view(coords["lat"], coords["lng"])
            print(f"       {len(sv_candidates)} candidate frames")
            vision_hits = vision_filter_potholes(sv_candidates, top_n=4) if sv_candidates else []
            print(f"       {len(vision_hits)} frames with detected damage")

            score = score_incident(incident, coords, crossref, vision_hits, pw_count)
            print(f"    📊 {score['score']}")

            if SCORE_RANK[score["score"]] >= SCORE_RANK[SCORE_THRESHOLD]:
                case_id = next_case_id(inv)
                html = build_email_html(case_id, incident, coords, crossref,
                                        score, vision_hits,
                                        incident.get("article_image_urls", []))
                try:
                    send_email(html, f"🚨 {case_id} | {score['score']} | {coords['formatted'][:55]}")
                    inv["incidents"][h] = {
                        "case_id": case_id,
                        "first_seen": datetime.utcnow().isoformat(),
                        "title": incident["title"],
                        "url": incident.get("url"),
                        "source": incident.get("source"),
                        "date": incident.get("date"),
                        "location": coords["formatted"],
                        "location_text": loc_text,
                        "coords": coords,
                        "score": score["score"],
                        "pothole_word_count": pw_count,
                        "vision_hits": len(vision_hits),
                        "crossref_size": sum(len(crossref.get(k, [])) for k in
                                             ["mop_evidence","attt_evidence","social_posts",
                                              "news_history","citizen_comments"]),
                    }
                except Exception as e:
                    print(f"    ✗ send failed: {e}")
            else:
                print(f"    — below threshold")

    maybe_send_weekly_report(inv)
    save_inventory(inv)
    print(f"\n=== Done · inventory now {len(inv['incidents'])} incidents ===")

if __name__ == "__main__":
    main()

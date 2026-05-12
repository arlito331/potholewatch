"""
PotholeWatch Phase 2 — 4-source road incident scanner
Sources: Tráfico Panama (IG/FB) · X via Nitter · Waze live map · Google Street View + AI Vision
Runs every 2 hours via GitHub Actions
Sends email alerts for MEDIUM+ incidents to joel@powerfixinc.com, 1@powerfixinc.com
"""

import os
import json
import time
import base64
import smtplib
import requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from anthropic import Anthropic

# ─── CONFIG ──────────────────────────────────────────────────────────────────

ALERT_RECIPIENTS = ["joel@powerfixinc.com", "1@powerfixinc.com"]
ALERT_THRESHOLD  = "MEDIUM"  # MEDIUM | HIGH | CRITICAL
PROBABILITY_ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]

KEYWORDS_ES = ["hueco", "huecos", "cráter", "cráteres", "bache", "baches",
               "accidente", "carretera", "vía deteriorada", "pavimento", "losa"]

# Panama bounding box for Waze
WAZE_BBOX = {
    "bottom": 7.2,
    "top":    9.7,
    "left":  -83.0,
    "right": -77.2
}

# Nitter instances (public X scrapers, no API key needed)
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]

# Tráfico Panama public pages
TRAFICO_PANAMA_PAGES = [
    "https://nitter.net/traficopanama",           # X account
    "https://www.instagram.com/traficopanama/",   # IG (public scrape)
]

TERRITORIES = [
    {
        "name": "Panama City",
        "queries": [
            "accidente Vía Centenario Panamá",
            "accidente Corredor Norte Panamá",
            "accidente Corredor Sur Panamá",
            "accidente Vía Interamericana Panamá",
            "accidente Transístmica Panamá",
            "accidente puente Panamá",
            "hueco cráter bache accidente Panamá",
        ],
        "waze_bbox": WAZE_BBOX
    },
    {
        "name": "Colón",
        "queries": [
            "accidente autopista Colón Panamá",
            "accidente vía Colón bache hueco",
        ],
        "waze_bbox": WAZE_BBOX
    },
    {
        "name": "La Chorrera",
        "queries": [
            "accidente Interamericana La Chorrera",
            "hueco bache La Chorrera Panamá",
        ],
        "waze_bbox": WAZE_BBOX
    }
]

client = Anthropic()

# ─── SOURCE 1: TRÁFICO PANAMA (X via Nitter) ─────────────────────────────────

def fetch_trafico_panama():
    """Scrape Tráfico Panama posts from Nitter (public X feed, no API key)."""
    posts = []
    for instance in NITTER_INSTANCES:
        try:
            url = f"{instance}/traficopanama"
            headers = {"User-Agent": "Mozilla/5.0 (compatible; PotholeWatch/2.0)"}
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            # Extract tweets containing pothole keywords
            lines = r.text.split("\n")
            current_tweet = ""
            for line in lines:
                if 'tweet-content' in line or 'timeline-item' in line:
                    current_tweet = line
                if current_tweet:
                    text_lower = current_tweet.lower()
                    if any(k in text_lower for k in KEYWORDS_ES):
                        # Strip HTML tags
                        import re
                        clean = re.sub(r'<[^>]+>', '', current_tweet).strip()
                        if len(clean) > 20:
                            posts.append({
                                "source": "trafico_panama",
                                "text": clean,
                                "url": f"https://x.com/traficopanama",
                                "timestamp": datetime.utcnow().isoformat()
                            })
                    current_tweet = ""
            if posts:
                break  # Got results from this instance
        except Exception as e:
            print(f"Nitter instance {instance} failed: {e}")
            continue
    return posts


# ─── SOURCE 2: X (TWITTER) via Nitter search ─────────────────────────────────

def fetch_x_posts(query):
    """Search X posts via Nitter public search — no API key required."""
    posts = []
    search_query = f"{query} hueco OR cráter OR bache OR accidente"
    encoded = requests.utils.quote(search_query)

    for instance in NITTER_INSTANCES:
        try:
            url = f"{instance}/search?q={encoded}&f=tweets"
            headers = {"User-Agent": "Mozilla/5.0 (compatible; PotholeWatch/2.0)"}
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            import re
            # Pull tweet texts
            tweet_blocks = re.findall(r'class="tweet-content[^"]*"[^>]*>(.*?)</div>', r.text, re.DOTALL)
            for block in tweet_blocks[:10]:
                clean = re.sub(r'<[^>]+>', '', block).strip()
                if len(clean) > 20 and any(k in clean.lower() for k in KEYWORDS_ES):
                    posts.append({
                        "source": "x_twitter",
                        "text": clean,
                        "url": f"{instance}/search?q={encoded}",
                        "timestamp": datetime.utcnow().isoformat()
                    })
            if posts:
                break
        except Exception as e:
            print(f"Nitter search failed on {instance}: {e}")
            continue
    return posts


# ─── SOURCE 3: WAZE LIVE MAP ──────────────────────────────────────────────────

def fetch_waze_potholes(bbox):
    """
    Pull pothole/hazard pins from Waze's public live map tile endpoint.
    No API key required — same data the browser loads at waze.com.
    Filters for HAZARD_ON_ROAD_POT_HOLE and related types.
    """
    url = "https://www.waze.com/live-map/api/georss"
    params = {
        "top":    bbox["top"],
        "bottom": bbox["bottom"],
        "left":   bbox["left"],
        "right":  bbox["right"],
        "env":    "row",
        "types":  "alerts"
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PotholeWatch/2.0)",
        "Referer": "https://www.waze.com/live-map",
        "Accept": "application/json"
    }
    pothole_types = {
        "HAZARD_ON_ROAD_POT_HOLE",
        "HAZARD_ON_ROAD",
        "HAZARD_ON_SHOULDER",
        "ROAD_CLOSED"
    }
    pins = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            alerts = data.get("alerts", [])
            for alert in alerts:
                alert_type = alert.get("type", "")
                subtype = alert.get("subtype", "")
                if alert_type in pothole_types or "POT_HOLE" in subtype or "POT_HOLE" in alert_type:
                    pins.append({
                        "source": "waze",
                        "type": alert_type,
                        "subtype": subtype,
                        "lat": alert.get("location", {}).get("y"),
                        "lng": alert.get("location", {}).get("x"),
                        "street": alert.get("street", "Unknown street"),
                        "reports": alert.get("nThumbsUp", 0) + 1,
                        "timestamp": alert.get("pubMillis", "")
                    })
    except Exception as e:
        print(f"Waze fetch failed: {e}")
    return pins


# ─── SOURCE 4: GOOGLE STREET VIEW + AI VISION ────────────────────────────────

def geocode_location(location_text):
    """Convert location text to lat/lng using Nominatim (free, no key needed)."""
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": location_text + ", Panama", "format": "json", "limit": 1}
        headers = {"User-Agent": "PotholeWatch/2.0"}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 200 and r.json():
            result = r.json()[0]
            return float(result["lat"]), float(result["lon"])
    except Exception as e:
        print(f"Geocoding failed for '{location_text}': {e}")
    return None, None


def fetch_street_view_image(lat, lng, api_key):
    """Fetch Google Street View static image at given coordinates."""
    if not api_key or not lat or not lng:
        return None
    url = "https://maps.googleapis.com/maps/api/streetview"
    params = {
        "size": "640x320",
        "location": f"{lat},{lng}",
        "heading": 0,
        "pitch": -15,
        "fov": 90,
        "key": api_key
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200 and len(r.content) > 5000:  # Real image, not placeholder
            return base64.b64encode(r.content).decode("utf-8")
    except Exception as e:
        print(f"Street View fetch failed: {e}")
    return None


def analyze_street_view(image_b64, location_name):
    """Use Claude Vision to analyze Street View image for pothole/road damage."""
    if not image_b64:
        return None, False
    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64
                        }
                    },
                    {
                        "type": "text",
                        "text": f"""Analyze this Google Street View image of {location_name}, Panama.

Look specifically for:
1. Potholes, craters, or large road depressions
2. Cracked or deteriorated pavement
3. Road surface damage that could cause vehicle accidents
4. Visible road subsidence or missing material

Respond in JSON format:
{{
  "craters_detected": true/false,
  "severity": "none/minor/moderate/severe",
  "description": "brief description of what you see",
  "estimated_size": "estimated pothole/crater size if visible",
  "accident_risk": "low/medium/high"
}}"""
                    }
                ]
            }]
        )
        text = response.content[0].text
        # Extract JSON
        import re
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            confirmed = result.get("craters_detected", False) and result.get("severity") in ["moderate", "severe"]
            return result, confirmed
    except Exception as e:
        print(f"Street View AI analysis failed: {e}")
    return None, False


# ─── AI INCIDENT SCORER ───────────────────────────────────────────────────────

def score_incident(incident_data, territory):
    """Use Claude to score incident probability and extract coordinates."""
    prompt = f"""You are PotholeWatch, an AI analyzing road incidents in Panama for pothole-related accident risk.

Territory: {territory}
Incident data:
{json.dumps(incident_data, ensure_ascii=False, indent=2)}

Keywords found: hueco, cráter, bache, accidente, carretera

Analyze this and respond ONLY in this exact JSON format:
{{
  "probability": "LOW|MEDIUM|HIGH|CRITICAL",
  "confidence": 0-100,
  "location_name": "specific road/intersection name",
  "coordinates_guess": {{"lat": 0.0, "lng": 0.0}},
  "summary": "2-sentence summary of the incident and road conditions",
  "pothole_likely_cause": true/false,
  "recommended_action": "brief action for PowerFix team"
}}

Base probability on:
- CRITICAL: 3+ sources confirm, Street View shows craters, multiple accidents
- HIGH: 2+ sources confirm, Waze pins nearby, credible news
- MEDIUM: 1-2 sources, keyword match, plausible location
- LOW: vague or unrelated

Coordinates: estimate based on known Panama road locations."""

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text
        import re
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"Scoring failed: {e}")
    return None


# ─── WAZE PIN MATCHER ─────────────────────────────────────────────────────────

def find_nearby_waze_pins(lat, lng, waze_pins, radius_km=2.0):
    """Find Waze pins within radius_km of given coordinates."""
    import math
    nearby = []
    if not lat or not lng:
        return nearby
    for pin in waze_pins:
        if not pin.get("lat") or not pin.get("lng"):
            continue
        dlat = math.radians(pin["lat"] - lat)
        dlng = math.radians(pin["lng"] - lng)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat)) * math.cos(math.radians(pin["lat"])) * math.sin(dlng/2)**2
        dist_km = 6371 * 2 * math.asin(math.sqrt(a))
        if dist_km <= radius_km:
            pin["distance_km"] = round(dist_km, 2)
            nearby.append(pin)
    return sorted(nearby, key=lambda p: p["distance_km"])


# ─── EMAIL BUILDER ────────────────────────────────────────────────────────────

def build_email_html(incident):
    score = incident.get("score", {})
    sv = incident.get("streetview", {})
    waze_pins = incident.get("nearby_waze_pins", [])
    social_posts = incident.get("social_posts", [])
    x_posts = incident.get("x_posts", [])

    prob = score.get("probability", "UNKNOWN")
    color_map = {"CRITICAL": "#A32D2D", "HIGH": "#993C1D", "MEDIUM": "#854F0B", "LOW": "#5F5E5A"}
    bg_map = {"CRITICAL": "#FCEBEB", "HIGH": "#FAECE7", "MEDIUM": "#FAEEDA", "LOW": "#F1EFE8"}
    color = color_map.get(prob, "#5F5E5A")
    bg = bg_map.get(prob, "#F1EFE8")

    lat = score.get("coordinates_guess", {}).get("lat", "")
    lng = score.get("coordinates_guess", {}).get("lng", "")
    maps_link = f"https://www.google.com/maps?q={lat},{lng}" if lat and lng else "#"

    sv_analysis = sv.get("analysis", {})
    sv_confirmed = sv.get("confirmed", False)

    waze_html = ""
    for p in waze_pins[:3]:
        waze_html += f"<li>📍 {p.get('street','Unknown')} — {p.get('distance_km','?')}km away · {p.get('reports',1)} reports</li>"

    social_html = ""
    for p in (social_posts + x_posts)[:4]:
        src = "🚦 Tráfico Panama" if p.get("source") == "trafico_panama" else "𝕏 Twitter/X"
        text = p.get("text", "")[:200]
        # Highlight keywords
        for kw in KEYWORDS_ES:
            text = text.replace(kw, f"<strong style='color:{color}'>{kw}</strong>")
            text = text.replace(kw.capitalize(), f"<strong style='color:{color}'>{kw.capitalize()}</strong>")
        social_html += f"<div style='margin-bottom:10px;padding:10px;background:#f9f9f9;border-radius:6px;font-size:13px'><strong>{src}:</strong> {text}</div>"

    sv_section = ""
    if sv_confirmed:
        sv_section = f"""
        <tr><td style='padding:12px 0;border-bottom:1px solid #eee'>
          <strong>🛰️ Street View AI Analysis</strong><br>
          <span style='color:#3B6D11'>✓ Road damage visually confirmed</span><br>
          <span style='font-size:13px;color:#555'>{sv_analysis.get('description','')}</span><br>
          <span style='font-size:12px;color:#888'>Severity: {sv_analysis.get('severity','').upper()} · Size: {sv_analysis.get('estimated_size','N/A')} · Accident risk: {sv_analysis.get('accident_risk','').upper()}</span>
        </td></tr>"""

    ptw_id = incident.get("ptw_id", "PTW-???")
    location = score.get("location_name", "Unknown location")
    summary = score.get("summary", "")
    action = score.get("recommended_action", "")
    confidence = score.get("confidence", 0)
    src_count = incident.get("source_count", 0)
    scan_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    return f"""<!DOCTYPE html>
<html><body style='font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#333'>
<div style='background:{bg};border-left:4px solid {color};padding:16px 20px;margin-bottom:20px;border-radius:4px'>
  <div style='font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:{color};margin-bottom:4px'>PotholeWatch Alert · {ptw_id}</div>
  <div style='font-size:20px;font-weight:bold;color:{color}'>{prob} PROBABILITY</div>
  <div style='font-size:14px;margin-top:4px'>{location}</div>
</div>

<table width='100%' cellpadding='0' cellspacing='0'>
  <tr><td style='padding:12px 0;border-bottom:1px solid #eee'>
    <strong>Confidence:</strong> {confidence}% · <strong>Sources:</strong> {src_count}/4 confirmed · <strong>Scan:</strong> {scan_time}
  </td></tr>
  <tr><td style='padding:12px 0;border-bottom:1px solid #eee'>
    <strong>Summary:</strong><br>{summary}
  </td></tr>
  <tr><td style='padding:12px 0;border-bottom:1px solid #eee'>
    <strong>📍 Coordinates:</strong> {lat}, {lng}<br>
    <a href='{maps_link}' style='color:#185FA5'>Open in Google Maps →</a>
  </td></tr>
  {'<tr><td style="padding:12px 0;border-bottom:1px solid #eee"><strong>🚦 Social Reports (Tráfico Panama + X)</strong><br>' + social_html + '</td></tr>' if social_html else ''}
  {'<tr><td style="padding:12px 0;border-bottom:1px solid #eee"><strong>📡 Waze Pins Nearby</strong><ul style="margin:8px 0;padding-left:20px;font-size:13px">' + waze_html + '</ul></td></tr>' if waze_html else ''}
  {sv_section}
  <tr><td style='padding:12px 0;border-bottom:1px solid #eee'>
    <strong>⚡ Recommended Action:</strong><br>{action}
  </td></tr>
</table>

<div style='margin-top:20px;padding:12px;background:#f5f5f5;border-radius:4px;font-size:12px;color:#888'>
  PotholeWatch by PowerFix Inc. · Automated road incident monitoring · 
  Sources: Tráfico Panama · X/Twitter · Waze Live Map · Google Street View AI
</div>
</body></html>"""


# ─── EMAIL SENDER ─────────────────────────────────────────────────────────────

def send_alert_email(incident):
    """Send alert via Gmail using OAuth refresh token stored in GitHub Secrets."""
    import google.auth.transport.requests
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    refresh_token  = os.environ.get("GMAIL_REFRESH_TOKEN")
    client_id      = os.environ.get("GMAIL_CLIENT_ID")
    client_secret  = os.environ.get("GMAIL_CLIENT_SECRET")

    if not all([refresh_token, client_id, client_secret]):
        print("⚠️  Gmail credentials not set — skipping email send")
        return False

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/gmail.send"]
    )
    creds.refresh(google.auth.transport.requests.Request())

    score = incident.get("score", {})
    prob  = score.get("probability", "UNKNOWN")
    loc   = score.get("location_name", "Unknown location")
    ptw_id = incident.get("ptw_id", "PTW-???")

    emoji_map = {"CRITICAL": "🚨", "HIGH": "⚠️", "MEDIUM": "📍"}
    emoji = emoji_map.get(prob, "📍")

    subject = f"{emoji} PotholeWatch {prob} — {ptw_id} | {loc}"
    html_body = build_email_html(incident)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = "potholewatch@powerfixinc.com"
    msg["To"]      = ", ".join(ALERT_RECIPIENTS)
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    service = build("gmail", "v1", credentials=creds)
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"✅ Alert sent: {subject}")
    return True


# ─── MAIN SCAN LOOP ───────────────────────────────────────────────────────────

def run_scan():
    print(f"\n{'='*60}")
    print(f"PotholeWatch Phase 2 — Scan started {datetime.utcnow().isoformat()}")
    print(f"{'='*60}\n")

    google_sv_key = os.environ.get("GOOGLE_STREETVIEW_API_KEY", "")
    incident_counter = int(os.environ.get("INCIDENT_COUNTER", "1"))
    alerts_sent = 0

    # Fetch Tráfico Panama posts once
    print("📡 Fetching Tráfico Panama posts...")
    trafico_posts = fetch_trafico_panama()
    print(f"   Found {len(trafico_posts)} posts with pothole keywords")

    for territory in TERRITORIES:
        print(f"\n🌎 Scanning territory: {territory['name']}")

        # Fetch Waze pins for this territory
        print("   🔵 Fetching Waze live map pins...")
        waze_pins = fetch_waze_potholes(territory["waze_bbox"])
        print(f"   Found {len(waze_pins)} Waze pothole/hazard pins")

        for query in territory["queries"]:
            print(f"\n   🔍 Query: {query}")

            # Source 1+2: AI web search for news + X posts
            print("   📰 Searching news and X posts via Claude...")
            x_posts = fetch_x_posts(query)
            print(f"   Found {len(x_posts)} X posts")

            # Build incident data bundle
            incident_data = {
                "query": query,
                "territory": territory["name"],
                "trafico_posts": trafico_posts[:5],
                "x_posts": x_posts[:5],
                "waze_pin_count": len(waze_pins),
                "waze_sample": waze_pins[:3],
                "scan_time": datetime.utcnow().isoformat()
            }

            # Score with Claude AI
            print("   🤖 Scoring incident with Claude AI...")
            score = score_incident(incident_data, territory["name"])
            if not score:
                print("   ⚠️  Scoring failed, skipping")
                continue

            prob = score.get("probability", "LOW")
            confidence = score.get("confidence", 0)
            print(f"   Result: {prob} ({confidence}% confidence)")

            if PROBABILITY_ORDER.index(prob) < PROBABILITY_ORDER.index(ALERT_THRESHOLD):
                print(f"   Below threshold ({ALERT_THRESHOLD}), skipping")
                continue

            # Get coordinates
            lat = score.get("coordinates_guess", {}).get("lat")
            lng = score.get("coordinates_guess", {}).get("lng")
            location_name = score.get("location_name", query)

            # If no coordinates from AI, geocode the location name
            if not lat or not lng:
                lat, lng = geocode_location(location_name)
                if lat and lng:
                    score["coordinates_guess"] = {"lat": lat, "lng": lng}

            # Source 3: Waze pin matching
            nearby_pins = find_nearby_waze_pins(lat, lng, waze_pins)
            print(f"   📡 {len(nearby_pins)} Waze pins within 2km")

            # Source 4: Street View + AI Vision
            sv_result = {"confirmed": False, "analysis": {}}
            if google_sv_key and lat and lng:
                print("   🛰️  Fetching Street View image...")
                img_b64 = fetch_street_view_image(lat, lng, google_sv_key)
                if img_b64:
                    print("   🤖 Analyzing Street View with AI Vision...")
                    analysis, confirmed = analyze_street_view(img_b64, location_name)
                    sv_result = {"confirmed": confirmed, "analysis": analysis or {}}
                    if confirmed:
                        print(f"   ✅ Craters CONFIRMED by Street View AI")
                    else:
                        print(f"   ℹ️  No craters confirmed in Street View")

            # Recalculate confidence with all sources
            source_count = 0
            if trafico_posts: source_count += 1
            if x_posts: source_count += 1
            if nearby_pins: source_count += 1
            if sv_result["confirmed"]: source_count += 1

            # Boost confidence if multiple sources agree
            if source_count == 4:
                prob = "CRITICAL"
                confidence = min(confidence + 15, 99)
            elif source_count == 3:
                if prob == "MEDIUM": prob = "HIGH"
                confidence = min(confidence + 10, 95)
            elif source_count == 2:
                confidence = min(confidence + 5, 85)

            score["probability"] = prob
            score["confidence"] = confidence

            ptw_id = f"PTW-{incident_counter:03d}"
            incident_counter += 1

            full_incident = {
                "ptw_id": ptw_id,
                "score": score,
                "social_posts": trafico_posts[:3],
                "x_posts": x_posts[:3],
                "nearby_waze_pins": nearby_pins[:5],
                "streetview": sv_result,
                "source_count": source_count,
                "territory": territory["name"],
                "scan_time": datetime.utcnow().isoformat()
            }

            print(f"\n   🚨 ALERT: {ptw_id} — {prob} at {location_name}")
            print(f"   Coordinates: {lat}, {lng}")
            print(f"   Sources: {source_count}/4 · Confidence: {confidence}%")
            print(f"   Sending email alert...")

            sent = send_alert_email(full_incident)
            if sent:
                alerts_sent += 1

            time.sleep(2)  # Rate limit between queries

    print(f"\n{'='*60}")
    print(f"Scan complete. Alerts sent: {alerts_sent}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    run_scan()

"""
PotholeWatch Phase 2 — 4-source road incident scanner
Sources: Tráfico Panama · X/Twitter · Waze (web search) · Google Street View + AI Vision
Fixes: Single digest email · Weekly Excel · Waze via web search · Street View photos embedded
Runs every 2 hours via GitHub Actions
"""

import os
import io
import json
import time
import base64
import requests
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from anthropic import Anthropic

# ─── CONFIG ──────────────────────────────────────────────────────────────────

ALERT_RECIPIENTS  = ["joel@powerfixinc.com", "1@powerfixinc.com"]
ALERT_THRESHOLD   = "MEDIUM"
PROBABILITY_ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]

KEYWORDS_ES = ["hueco", "huecos", "cráter", "cráteres", "bache", "baches",
               "accidente", "carretera", "vía deteriorada", "pavimento", "losa"]

WAZE_BBOX = {"bottom": 7.2, "top": 9.7, "left": -83.0, "right": -77.2}

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
        ]
    },
    {
        "name": "Colón",
        "queries": [
            "accidente autopista Colón Panamá",
            "accidente vía Colón bache hueco",
        ]
    },
    {
        "name": "La Chorrera",
        "queries": [
            "accidente Interamericana La Chorrera",
            "hueco bache La Chorrera Panamá",
        ]
    }
]

PROB_COLOR = {"CRITICAL": "#A32D2D", "HIGH": "#993C1D", "MEDIUM": "#854F0B", "LOW": "#5F5E5A"}
PROB_BG    = {"CRITICAL": "#FCEBEB", "HIGH": "#FAECE7", "MEDIUM": "#FAEEDA", "LOW": "#F1EFE8"}

client = Anthropic()

# ─── SOURCE 1: TRÁFICO PANAMA ─────────────────────────────────────────────────

def fetch_trafico_panama():
    print("📡 Fetching Tráfico Panama posts...")
    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1500,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content":
                """Search for very recent posts from Tráfico Panama (x.com/traficopanama) 
                mentioning: hueco, huecos, cráter, bache, baches, accidente, vía deteriorada.
                Search: site:x.com traficopanama hueco OR bache OR cráter OR accidente 2026
                Return actual post texts with dates. Last 48 hours only."""
            }]
        )
        posts = []
        for block in response.content:
            if hasattr(block, 'type') and block.type == "text" and len(block.text) > 30:
                if any(k in block.text.lower() for k in KEYWORDS_ES):
                    posts.append({
                        "source": "trafico_panama",
                        "text": block.text[:500],
                        "url": "https://x.com/traficopanama",
                        "timestamp": datetime.utcnow().isoformat()
                    })
        print(f"   Found {len(posts)} posts")
        return posts
    except Exception as e:
        print(f"   Failed: {e}")
        return []


# ─── SOURCE 2: X/TWITTER ─────────────────────────────────────────────────────

def fetch_x_posts(query):
    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1500,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content":
                f"""Search X/Twitter and news for: {query}
                Keywords: hueco, cráter, bache, accidente, vía deteriorada Panama 2026
                Return actual post/article texts with sources. Last 48 hours only."""
            }]
        )
        posts = []
        for block in response.content:
            if hasattr(block, 'type') and block.type == "text" and len(block.text) > 30:
                if any(k in block.text.lower() for k in KEYWORDS_ES):
                    posts.append({
                        "source": "x_twitter",
                        "text": block.text[:500],
                        "url": f"https://x.com/search?q={requests.utils.quote(query)}",
                        "timestamp": datetime.utcnow().isoformat()
                    })
        return posts
    except Exception as e:
        print(f"   X search failed: {e}")
        return []


# ─── SOURCE 3: WAZE ──────────────────────────────────────────────────────────

def fetch_waze_direct():
    """Try Waze direct API first."""
    url = "https://www.waze.com/live-map/api/georss"
    params = {"top": WAZE_BBOX["top"], "bottom": WAZE_BBOX["bottom"],
              "left": WAZE_BBOX["left"], "right": WAZE_BBOX["right"], "env": "row", "types": "alerts"}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.waze.com/live-map"}
    pins = []
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            for alert in r.json().get("alerts", []):
                t = alert.get("type", "")
                if "POT_HOLE" in t or "HAZARD" in t:
                    pins.append({
                        "street": alert.get("street", "Unknown"),
                        "lat": alert.get("location", {}).get("y"),
                        "lng": alert.get("location", {}).get("x"),
                        "reports": alert.get("nThumbsUp", 0) + 1
                    })
    except Exception as e:
        print(f"   Waze direct failed: {e}")
    return pins


def fetch_waze_web(territory_name):
    """Fallback: search Waze reports via web search."""
    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=800,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content":
                f"""Search for recent Waze pothole/hazard reports in {territory_name}, Panama.
                Search: waze hueco OR bache OR pothole Panama {territory_name} 2026
                Return street names and locations of reported hazards."""
            }]
        )
        reports = []
        for block in response.content:
            if hasattr(block, 'type') and block.type == "text" and len(block.text) > 20:
                reports.append({"source": "waze_web", "text": block.text[:300]})
        return reports
    except Exception as e:
        print(f"   Waze web search failed: {e}")
        return []


def find_nearby_pins(lat, lng, pins, radius_km=2.0):
    import math
    if not lat or not lng:
        return []
    nearby = []
    for pin in pins:
        if not pin.get("lat") or not pin.get("lng"):
            continue
        dlat = math.radians(pin["lat"] - lat)
        dlng = math.radians(pin["lng"] - lng)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(lat)) * math.cos(math.radians(pin["lat"])) * math.sin(dlng/2)**2
        dist = 6371 * 2 * math.asin(math.sqrt(a))
        if dist <= radius_km:
            pin["distance_km"] = round(dist, 2)
            nearby.append(pin)
    return sorted(nearby, key=lambda p: p["distance_km"])


# ─── SOURCE 4: STREET VIEW + AI VISION ───────────────────────────────────────

def geocode_location(text):
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": text + ", Panama", "format": "json", "limit": 1},
                         headers={"User-Agent": "PotholeWatch/2.0"}, timeout=10)
        if r.status_code == 200 and r.json():
            d = r.json()[0]
            return float(d["lat"]), float(d["lon"])
    except Exception as e:
        print(f"   Geocode failed: {e}")
    return None, None


def fetch_street_view(lat, lng, api_key):
    if not api_key or not lat or not lng:
        return None
    for heading in [0, 90, 180, 270]:
        try:
            r = requests.get("https://maps.googleapis.com/maps/api/streetview",
                             params={"size": "640x360", "location": f"{lat},{lng}",
                                     "heading": heading, "pitch": -10, "fov": 90, "key": api_key},
                             timeout=15)
            if r.status_code == 200 and len(r.content) > 8000:
                print(f"   📸 Street View fetched (heading {heading}°)")
                return base64.b64encode(r.content).decode("utf-8")
        except Exception as e:
            print(f"   Street View failed: {e}")
    return None


def analyze_street_view(img_b64, location_name):
    if not img_b64:
        return None, False
    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=500,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text", "text": f"""Analyze this Google Street View of {location_name}, Panama for road damage.
Look for: potholes, craters, cracked pavement, deteriorated road surface.
Respond ONLY in JSON:
{{"craters_detected": true/false, "severity": "none/minor/moderate/severe",
  "description": "1-2 sentences", "estimated_size": "size or N/A", "accident_risk": "low/medium/high"}}"""}
            ]}]
        )
        import re
        match = re.search(r'\{.*\}', response.content[0].text, re.DOTALL)
        if match:
            result = json.loads(match.group())
            confirmed = result.get("craters_detected", False) and result.get("severity") in ["moderate", "severe"]
            return result, confirmed
    except Exception as e:
        print(f"   Vision analysis failed: {e}")
    return None, False


# ─── AI SCORER ───────────────────────────────────────────────────────────────

def score_incident(data, territory):
    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=600,
            messages=[{"role": "user", "content":
                f"""PotholeWatch AI — analyze road incident risk in {territory}, Panama.
Data: {json.dumps(data, ensure_ascii=False, indent=2)}

Respond ONLY in JSON:
{{"probability": "LOW|MEDIUM|HIGH|CRITICAL", "confidence": 0-100,
  "location_name": "specific road name",
  "coordinates_guess": {{"lat": 0.0, "lng": 0.0}},
  "summary": "2 sentences on incident and road conditions",
  "pothole_likely_cause": true/false,
  "recommended_action": "brief action for PowerFix team"}}

CRITICAL=3+sources+confirmed accidents. HIGH=2+sources+credible. MEDIUM=1-2+plausible. LOW=vague."""
            }]
        )
        import re
        match = re.search(r'\{.*\}', response.content[0].text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"   Scoring failed: {e}")
    return None


# ─── DIGEST EMAIL ─────────────────────────────────────────────────────────────

def build_digest_html(incidents, scan_time):
    critical = [i for i in incidents if i["score"].get("probability") == "CRITICAL"]
    high     = [i for i in incidents if i["score"].get("probability") == "HIGH"]
    medium   = [i for i in incidents if i["score"].get("probability") == "MEDIUM"]
    scan_dt  = datetime.fromisoformat(scan_time).strftime("%B %d, %Y at %H:%M UTC")

    def incident_card(inc):
        score    = inc.get("score", {})
        prob     = score.get("probability", "MEDIUM")
        loc      = score.get("location_name", "Unknown")
        ptw_id   = inc.get("ptw_id", "")
        conf     = score.get("confidence", 0)
        summary  = score.get("summary", "")
        action   = score.get("recommended_action", "")
        lat      = score.get("coordinates_guess", {}).get("lat", "")
        lng      = score.get("coordinates_guess", {}).get("lng", "")
        src_cnt  = inc.get("source_count", 0)
        maps_url = f"https://www.google.com/maps?q={lat},{lng}" if lat and lng else "#"
        sv       = inc.get("streetview", {})
        sv_b64   = inc.get("streetview_b64", "")
        sv_anal  = sv.get("analysis", {})
        color    = PROB_COLOR.get(prob, "#5F5E5A")
        bg       = PROB_BG.get(prob, "#F1EFE8")

        # Street View image — embedded directly in email
        if sv_b64:
            sv_html = f"""<div style="margin:10px 0">
              <img src="data:image/jpeg;base64,{sv_b64}" 
                   style="width:100%;max-width:580px;border-radius:6px;border:1px solid #ddd" 
                   alt="Street View {loc}"/>
              <div style="font-size:11px;color:#888;margin-top:3px;font-style:italic">
                📸 Google Street View · {lat}, {lng}
                {(' · ' + sv_anal.get('description','')) if sv_anal.get('description') else ''}
              </div></div>"""
        elif lat and lng:
            sv_html = f"""<div style="margin:8px 0;font-size:12px">
              📸 <a href="https://www.google.com/maps/@{lat},{lng},3a,75y,0h,85t/data=!3m1!1e3" style="color:#185FA5">View Street View in Google Maps →</a></div>"""
        else:
            sv_html = ""

        # Social posts
        social_html = ""
        for p in (inc.get("social_posts", []) + inc.get("x_posts", []))[:2]:
            src = "🚦 Tráfico Panama" if p.get("source") == "trafico_panama" else "𝕏 X/Twitter"
            social_html += f'<div style="font-size:12px;color:#555;padding:5px 8px;background:#f9f9f9;border-radius:4px;margin-top:4px"><strong>{src}:</strong> {p.get("text","")[:180]}</div>'

        # Waze
        waze_html = ""
        for w in inc.get("nearby_waze_pins", [])[:2]:
            waze_html += f'<div style="font-size:12px;color:#555;margin-top:2px">📍 {w.get("street","?")} · {w.get("distance_km","?")}km · {w.get("reports",1)} reports</div>'
        for w in inc.get("waze_web_reports", [])[:1]:
            waze_html += f'<div style="font-size:12px;color:#555;margin-top:2px">📡 {w.get("text","")[:120]}</div>'

        return f"""
<div style="border:1px solid {color};border-radius:8px;margin-bottom:18px;overflow:hidden">
  <div style="background:{bg};padding:10px 14px;border-bottom:1px solid {color}">
    <span style="background:{color};color:#fff;font-size:10px;font-weight:bold;padding:2px 8px;border-radius:10px;margin-right:8px">{prob}</span>
    <strong style="font-size:13px;color:{color}">{ptw_id} — {loc}</strong>
    <span style="float:right;font-size:11px;color:#888">{conf}% · {src_cnt}/4 sources</span>
  </div>
  <div style="padding:12px 14px">
    {sv_html}
    <p style="font-size:13px;color:#333;margin:6px 0">{summary}</p>
    <div style="font-size:12px;color:#555;margin:6px 0">
      📍 <a href="{maps_url}" style="color:#185FA5">Open in Google Maps</a>
      {f' · {lat}, {lng}' if lat and lng else ''}
    </div>
    {social_html}
    {waze_html}
    <div style="margin-top:8px;padding:7px 10px;background:#EAF3DE;border-radius:4px;font-size:12px;color:#3B6D11">
      ⚡ <strong>Action:</strong> {action}
    </div>
  </div>
</div>"""

    blocks = ""
    for label, group, color in [
        ("🚨 CRITICAL", critical, "#A32D2D"),
        ("⚠️ HIGH", high, "#993C1D"),
        ("📍 MEDIUM", medium, "#854F0B")
    ]:
        if group:
            blocks += f'<h2 style="color:{color};font-size:15px;margin:20px 0 10px;border-bottom:2px solid {color};padding-bottom:5px">{label} ({len(group)})</h2>'
            for inc in group:
                blocks += incident_card(inc)

    return f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;color:#333;padding:16px">
<div style="background:#0D0D0D;padding:14px 18px;border-radius:8px;margin-bottom:20px">
  <span style="color:#E24B4A;font-size:10px;margin-right:8px">●</span>
  <span style="color:#fff;font-size:15px;font-weight:bold;letter-spacing:.04em">PotholeWatch</span>
  <span style="color:#666;font-size:11px;margin-left:8px">by PowerFix Inc.</span>
  <span style="float:right;color:#666;font-size:11px">{scan_dt}</span>
</div>
<div style="display:flex;gap:10px;margin-bottom:20px">
  <div style="flex:1;text-align:center;padding:10px;background:#FCEBEB;border-radius:6px">
    <div style="font-size:22px;font-weight:bold;color:#A32D2D">{len(critical)}</div>
    <div style="font-size:10px;color:#A32D2D;text-transform:uppercase;letter-spacing:.05em">Critical</div>
  </div>
  <div style="flex:1;text-align:center;padding:10px;background:#FAECE7;border-radius:6px">
    <div style="font-size:22px;font-weight:bold;color:#993C1D">{len(high)}</div>
    <div style="font-size:10px;color:#993C1D;text-transform:uppercase;letter-spacing:.05em">High</div>
  </div>
  <div style="flex:1;text-align:center;padding:10px;background:#FAEEDA;border-radius:6px">
    <div style="font-size:22px;font-weight:bold;color:#854F0B">{len(medium)}</div>
    <div style="font-size:10px;color:#854F0B;text-transform:uppercase;letter-spacing:.05em">Medium</div>
  </div>
  <div style="flex:1;text-align:center;padding:10px;background:#F1EFE8;border-radius:6px">
    <div style="font-size:22px;font-weight:bold;color:#5F5E5A">{len(incidents)}</div>
    <div style="font-size:10px;color:#5F5E5A;text-transform:uppercase;letter-spacing:.05em">Total</div>
  </div>
</div>
{blocks}
<div style="margin-top:20px;padding:10px;background:#f5f5f5;border-radius:6px;font-size:10px;color:#aaa;text-align:center">
  PotholeWatch Phase 2 · Tráfico Panama · X/Twitter · Waze · Google Street View AI · PowerFix Inc.
</div></body></html>"""


# ─── WEEKLY EXCEL ─────────────────────────────────────────────────────────────

def build_excel(incidents):
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "PotholeWatch Weekly"

        headers = ["PTW ID", "Probability", "Confidence %", "Location", "Territory",
                   "Lat", "Lng", "Google Maps Link", "Sources", "Tráfico Panama",
                   "X/Twitter", "Waze", "Street View", "Summary", "Action", "Scan Time"]

        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.fill = PatternFill("solid", fgColor="0D0D0D")
            cell.font = Font(color="FFFFFF", bold=True, size=10)
            cell.alignment = Alignment(horizontal="center", wrap_text=True)

        pfills = {"CRITICAL": "FCEBEB", "HIGH": "FAECE7", "MEDIUM": "FAEEDA", "LOW": "F1EFE8"}
        pfonts = {"CRITICAL": "A32D2D", "HIGH": "993C1D", "MEDIUM": "854F0B", "LOW": "5F5E5A"}

        for row, inc in enumerate(incidents, 2):
            score = inc.get("score", {})
            prob  = score.get("probability", "MEDIUM")
            lat   = score.get("coordinates_guess", {}).get("lat", "")
            lng   = score.get("coordinates_guess", {}).get("lng", "")
            maps  = f"https://www.google.com/maps?q={lat},{lng}" if lat and lng else ""
            sv    = inc.get("streetview", {})

            row_data = [
                inc.get("ptw_id", ""), prob, score.get("confidence", 0),
                score.get("location_name", ""), inc.get("territory", ""),
                lat, lng, maps, f"{inc.get('source_count',0)}/4",
                "✓" if inc.get("social_posts") else "—",
                "✓" if inc.get("x_posts") else "—",
                "✓" if (inc.get("nearby_waze_pins") or inc.get("waze_web_reports")) else "—",
                "✓ Confirmed" if sv.get("confirmed") else ("✓ Fetched" if inc.get("streetview_b64") else "—"),
                score.get("summary", ""), score.get("recommended_action", ""), inc.get("scan_time", "")
            ]

            for col, val in enumerate(row_data, 1):
                cell = ws.cell(row=row, column=col, value=val)
                cell.fill = PatternFill("solid", fgColor=pfills.get(prob, "FAEEDA"))
                if col == 2:
                    cell.font = Font(color=pfonts.get(prob, "854F0B"), bold=True)
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                if col == 8 and maps:
                    cell.hyperlink = maps
                    cell.font = Font(color="185FA5", underline="single")

        widths = [10, 10, 12, 35, 15, 10, 10, 20, 10, 12, 12, 10, 14, 50, 40, 22]
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.row_dimensions[1].height = 28
        ws.freeze_panes = "A2"

        # Summary tab
        ws2 = wb.create_sheet("Summary")
        ws2["A1"] = "PotholeWatch Weekly Summary"
        ws2["A1"].font = Font(bold=True, size=13)
        for r, (label, val) in enumerate([
            ("Total incidents", len(incidents)),
            ("Critical", sum(1 for i in incidents if i["score"].get("probability") == "CRITICAL")),
            ("High", sum(1 for i in incidents if i["score"].get("probability") == "HIGH")),
            ("Medium", sum(1 for i in incidents if i["score"].get("probability") == "MEDIUM")),
            ("Report generated", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
        ], start=3):
            ws2.cell(row=r, column=1, value=label).font = Font(bold=True)
            ws2.cell(row=r, column=2, value=val)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        print(f"   Excel build failed: {e}")
        return None


# ─── GMAIL ────────────────────────────────────────────────────────────────────

def send_digest(incidents, scan_time, excel_bytes=None):
    if not all([os.environ.get("GMAIL_REFRESH_TOKEN"),
                os.environ.get("GMAIL_CLIENT_ID"),
                os.environ.get("GMAIL_CLIENT_SECRET")]):
        print("⚠️  Gmail credentials missing")
        return False
    try:
        import google.auth.transport.requests
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=os.environ.get("GMAIL_REFRESH_TOKEN"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ.get("GMAIL_CLIENT_ID"),
            client_secret=os.environ.get("GMAIL_CLIENT_SECRET"),
            scopes=["https://www.googleapis.com/auth/gmail.send"]
        )
        creds.refresh(google.auth.transport.requests.Request())
        service = build("gmail", "v1", credentials=creds)

        n_crit = sum(1 for i in incidents if i["score"].get("probability") == "CRITICAL")
        n_high = sum(1 for i in incidents if i["score"].get("probability") == "HIGH")
        n_tot  = len(incidents)

        if n_crit > 0:
            subject = f"🚨 PotholeWatch — {n_crit} CRITICAL, {n_high} HIGH · {n_tot} incidents"
        elif n_high > 0:
            subject = f"⚠️ PotholeWatch — {n_high} HIGH · {n_tot} incidents"
        else:
            subject = f"📍 PotholeWatch — {n_tot} incidents detected"

        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = "potholewatch@powerfixinc.com"
        msg["To"]      = ", ".join(ALERT_RECIPIENTS)

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(build_digest_html(incidents, scan_time), "html"))
        msg.attach(alt)

        if excel_bytes:
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
            part = MIMEBase("application", "octet-stream")
            part.set_payload(excel_bytes)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="PotholeWatch_{date_str}.xlsx"')
            msg.attach(part)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        attach_note = " + Excel attachment" if excel_bytes else ""
        print(f"✅ Digest sent: {subject}{attach_note}")
        return True
    except Exception as e:
        print(f"❌ Email failed: {e}")
        return False


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run_scan():
    print(f"\n{'='*60}")
    print(f"PotholeWatch Phase 2 — {datetime.utcnow().isoformat()}")
    print(f"{'='*60}\n")

    sv_key           = os.environ.get("GOOGLE_STREETVIEW_API_KEY", "")
    incident_counter = int(os.environ.get("INCIDENT_COUNTER", "1"))
    is_weekly        = os.environ.get("WEEKLY_REPORT", "false").lower() == "true"
    scan_time        = datetime.utcnow().isoformat()
    all_incidents    = []

    trafico_posts = fetch_trafico_panama()
    waze_pins     = fetch_waze_direct()
    print(f"   Waze direct: {len(waze_pins)} pins")

    for territory in TERRITORIES:
        print(f"\n🌎 {territory['name']}")

        waze_web = fetch_waze_web(territory["name"]) if not waze_pins else []

        for query in territory["queries"]:
            print(f"\n   🔍 {query}")

            x_posts = fetch_x_posts(query)
            print(f"   X posts: {len(x_posts)}")

            data = {
                "query": query, "territory": territory["name"],
                "trafico_posts": trafico_posts[:5], "x_posts": x_posts[:5],
                "waze_pins": waze_pins[:3], "waze_web": waze_web[:2],
                "scan_time": scan_time
            }

            score = score_incident(data, territory["name"])
            if not score:
                continue

            prob = score.get("probability", "LOW")
            conf = score.get("confidence", 0)
            print(f"   Score: {prob} ({conf}%)")

            if PROBABILITY_ORDER.index(prob) < PROBABILITY_ORDER.index(ALERT_THRESHOLD):
                print(f"   Below threshold — skip")
                continue

            lat = score.get("coordinates_guess", {}).get("lat")
            lng = score.get("coordinates_guess", {}).get("lng")
            loc = score.get("location_name", query)

            if not lat or not lng:
                lat, lng = geocode_location(loc)
                if lat and lng:
                    score["coordinates_guess"] = {"lat": lat, "lng": lng}

            nearby_pins = find_nearby_pins(lat, lng, waze_pins)

            # Street View
            sv_b64   = None
            sv_result = {"confirmed": False, "analysis": {}}
            if sv_key and lat and lng:
                sv_b64 = fetch_street_view(lat, lng, sv_key)
                if sv_b64:
                    analysis, confirmed = analyze_street_view(sv_b64, loc)
                    sv_result = {"confirmed": confirmed, "analysis": analysis or {}}
                    print(f"   Street View: {'✅ craters confirmed' if confirmed else 'ℹ️  no craters'}")
                else:
                    print(f"   Street View: no imagery at this location")
            else:
                if not sv_key:
                    print(f"   Street View: no API key")

            src_count = sum([bool(trafico_posts), bool(x_posts),
                             bool(nearby_pins or waze_web), sv_result["confirmed"]])
            if src_count == 4:
                prob = "CRITICAL"; conf = min(conf + 15, 99)
            elif src_count == 3:
                if prob == "MEDIUM": prob = "HIGH"
                conf = min(conf + 10, 95)
            elif src_count == 2:
                conf = min(conf + 5, 85)

            score["probability"] = prob
            score["confidence"]  = conf

            ptw_id = f"PTW-{incident_counter:03d}"
            incident_counter += 1

            all_incidents.append({
                "ptw_id": ptw_id, "score": score,
                "social_posts": trafico_posts[:3], "x_posts": x_posts[:3],
                "nearby_waze_pins": nearby_pins[:3], "waze_web_reports": waze_web[:2],
                "streetview": sv_result, "streetview_b64": sv_b64,
                "source_count": src_count, "territory": territory["name"],
                "scan_time": scan_time
            })
            print(f"   ✅ {ptw_id} — {prob} ({conf}%) · {lat}, {lng} · {src_count}/4 sources")
            time.sleep(1)

    print(f"\n{'='*60}")
    print(f"Done. {len(all_incidents)} incidents above threshold.")

    if all_incidents:
        excel_bytes = build_excel(all_incidents) if is_weekly else None
        send_digest(all_incidents, scan_time, excel_bytes)
    else:
        print("No incidents — no email sent.")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    run_scan()

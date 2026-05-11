import os
import json
import base64
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── Config ─────────────────────────────────────────────────────────────────
ALERT_RECIPIENTS = ["joel@powerfixinc.com", "1@powerfixinc.com"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_CLIENT_ID = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
GMAIL_REFRESH_TOKEN = os.environ["GMAIL_REFRESH_TOKEN"]

SCANS = [
    {
        "name": "Panama — News",
        "type": "news",
        "query": "accidente carretera Panama 2026 bache hueco camion choque via"
    },
    {
        "name": "Panama — X (Twitter)",
        "type": "social",
        "query": "site:x.com bache Panama hueco carretera accidente 2026"
    },
    {
        "name": "Panama — Instagram",
        "type": "social",
        "query": "site:instagram.com bache Panama hueco carretera accidente 2026"
    }
]

SYSTEM_PROMPT = (
    "You are a road incident and pothole intelligence extractor for Panama. "
    "You ONLY output valid JSON. Never output explanations, apologies, or any text outside of JSON. "
    "If you have nothing to report, output exactly: {\"incidents\": []}"
)

# ── Gmail ───────────────────────────────────────────────────────────────────
def get_gmail_service():
    creds = Credentials(
        token=None,
        refresh_token=GMAIL_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.send"]
    )
    return build("gmail", "v1", credentials=creds)

# ── API Call ────────────────────────────────────────────────────────────────
def claude_search(prompt):
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 3000,
                "system": SYSTEM_PROMPT,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=60
        )
        response.raise_for_status()
    except Exception as e:
        print("  API call failed: " + str(e))
        return None

    data = response.json()
    text = ""
    for block in data.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text += block.get("text", "")
    return text

# ── Incident Scan ───────────────────────────────────────────────────────────
def search_incidents(scan):
    query = scan["query"]
    scan_type = scan["type"]

    if scan_type == "social":
        prompt = (
            "Search for recent public social media posts (last 7 days) about potholes, road damage, or accidents in Panama: " + query + "\n\n"
            "Include EVERYTHING — even LOW probability (10%+ counts).\n"
            "Return ONLY this JSON:\n"
            "{\"incidents\": [{\"title\": \"\", \"url\": \"\", \"date\": \"\", \"location\": \"\", \"description\": \"\", \"probability\": \"HIGH or MEDIUM or LOW\", \"probability_reason\": \"\", \"source\": \"X or Instagram\"}]}\n"
            "If nothing found: {\"incidents\": []}"
        )
    else:
        prompt = (
            "Search for road accident or pothole news in Panama from the last 7 days: " + query + "\n\n"
            "Include EVERYTHING — even LOW probability (10%+ counts).\n"
            "Return ONLY this JSON:\n"
            "{\"incidents\": [{\"title\": \"\", \"url\": \"\", \"date\": \"\", \"location\": \"\", \"description\": \"\", \"probability\": \"HIGH or MEDIUM or LOW\", \"probability_reason\": \"\", \"source\": \"News\"}]}\n"
            "If nothing found: {\"incidents\": []}"
        )

    text = claude_search(prompt)
    if not text:
        return []

    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end <= start:
        print("  No JSON found")
        return []

    try:
        parsed = json.loads(text[start:end])
        incidents = parsed.get("incidents", [])
        print("  Found " + str(len(incidents)) + " result(s)")
        return incidents
    except json.JSONDecodeError as e:
        print("  Parse error: " + str(e))
        return []

# ── Cross Reference ─────────────────────────────────────────────────────────
def cross_reference(incident):
    location = incident.get("location", "")
    title = incident.get("title", "")
    if not location and not title:
        return {}

    search_term = location if location else title

    print("    Cross-referencing: " + search_term[:50])

    # MOP
    mop_prompt = (
        "Search for MOP Panama (Ministerio de Obras Publicas) records, Tapa Hueco plans, or road maintenance reports "
        "related to this location in Panama: " + search_term + "\n\n"
        "Return ONLY this JSON:\n"
        "{\"found\": true or false, \"summary\": \"what MOP records say about this road\", \"url\": \"link if found\"}\n"
        "If nothing found: {\"found\": false, \"summary\": \"No MOP records found\", \"url\": \"\"}"
    )

    # ATTT
    attt_prompt = (
        "Search for ATTT Panama (Autoridad de Transito y Transporte Terrestre) reports, complaints, or alerts "
        "related to this location: " + search_term + "\n\n"
        "Return ONLY this JSON:\n"
        "{\"found\": true or false, \"summary\": \"what ATTT records say\", \"url\": \"link if found\"}\n"
        "If nothing found: {\"found\": false, \"summary\": \"No ATTT records found\", \"url\": \"\"}"
    )

    # Social comments
    social_prompt = (
        "Search X (Twitter) and Instagram for public complaints, comments, or posts about road conditions, potholes, or accidents "
        "at this location in Panama: " + search_term + "\n\n"
        "Return ONLY this JSON:\n"
        "{\"found\": true or false, \"summary\": \"summary of what people are saying\", \"posts\": [\"post 1\", \"post 2\"], \"url\": \"link if found\"}\n"
        "If nothing found: {\"found\": false, \"summary\": \"No social comments found\", \"posts\": [], \"url\": \"\"}"
    )

    results = {}

    mop_text = claude_search(mop_prompt)
    if mop_text:
        s = mop_text.find("{"); e = mop_text.rfind("}") + 1
        if s != -1 and e > s:
            try:
                results["mop"] = json.loads(mop_text[s:e])
            except:
                results["mop"] = {"found": False, "summary": "Parse error", "url": ""}

    attt_text = claude_search(attt_prompt)
    if attt_text:
        s = attt_text.find("{"); e = attt_text.rfind("}") + 1
        if s != -1 and e > s:
            try:
                results["attt"] = json.loads(attt_text[s:e])
            except:
                results["attt"] = {"found": False, "summary": "Parse error", "url": ""}

    social_text = claude_search(social_prompt)
    if social_text:
        s = social_text.find("{"); e = social_text.rfind("}") + 1
        if s != -1 and e > s:
            try:
                results["social"] = json.loads(social_text[s:e])
            except:
                results["social"] = {"found": False, "summary": "Parse error", "posts": [], "url": ""}

    return results

# ── Email ───────────────────────────────────────────────────────────────────
def source_badge(source):
    colors = {"X": "#000000", "Instagram": "#E1306C", "News": "#2563eb"}
    color = colors.get(source, "#6B7280")
    return '<span style="background:' + color + ';color:white;padding:1px 7px;border-radius:10px;font-size:10px;margin-left:6px;">' + source + '</span>'

def ref_block(label, emoji, data):
    if not data:
        return ""
    found = data.get("found", False)
    summary = data.get("summary", "No data")
    url = data.get("url", "")
    posts = data.get("posts", [])
    bg = "#f0fdf4" if found else "#f9fafb"
    border = "#86efac" if found else "#e5e7eb"
    icon = "✅" if found else "⬜"
    posts_html = ""
    if posts:
        posts_html = '<ul style="margin:4px 0 0;padding-left:16px;">'
        for p in posts[:3]:
            posts_html += '<li style="font-size:11px;color:#374151;margin-bottom:2px;">' + p + '</li>'
        posts_html += '</ul>'
    link = ('<a href="' + url + '" style="font-size:11px;color:#2563eb;text-decoration:none;"> View →</a>' if url else "")
    return (
        '<div style="background:' + bg + ';border:1px solid ' + border + ';border-radius:6px;padding:10px;margin-top:6px;">'
        '<p style="margin:0;font-size:12px;font-weight:bold;">' + icon + ' ' + emoji + ' ' + label + link + '</p>'
        '<p style="margin:4px 0 0;font-size:12px;color:#374151;">' + summary + '</p>'
        + posts_html +
        '</div>'
    )

def build_incident_card(inc):
    prob = inc.get("probability", "LOW").upper()
    prob_color = "#B91C1C" if prob == "HIGH" else "#D97706" if prob == "MEDIUM" else "#6B7280"
    source = inc.get("source", "News")
    xref = inc.get("xref", {})

    mop_html = ref_block("MOP — Road Maintenance Records", "🏗️", xref.get("mop"))
    attt_html = ref_block("ATTT — Transit Authority", "🚦", xref.get("attt"))
    social_html = ref_block("Social Media Comments", "💬", xref.get("social"))

    return (
        '<div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin-bottom:16px;background:white;">'
        '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">'
        '<div style="flex:1;"><strong style="font-size:14px;">' + inc.get("title", "Untitled") + '</strong>' + source_badge(source) + '</div>'
        '<span style="background:' + prob_color + ';color:white;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:bold;white-space:nowrap;margin-left:8px;">' + prob + '</span>'
        '</div>'
        '<p style="margin:0 0 4px;font-size:12px;color:#6b7280;">📍 ' + inc.get("location", "Unknown") + ' &nbsp;|&nbsp; 📅 ' + inc.get("date", "") + '</p>'
        '<p style="margin:8px 0;font-size:13px;color:#111827;">' + inc.get("description", "") + '</p>'
        '<p style="margin:4px 0 8px;font-size:11px;color:#6b7280;font-style:italic;">' + inc.get("probability_reason", "") + '</p>'
        '<a href="' + inc.get("url", "#") + '" style="font-size:12px;color:#2563eb;text-decoration:none;">View source →</a>'
        '<div style="margin-top:10px;">'
        + mop_html + attt_html + social_html +
        '</div>'
        '</div>'
    )

def build_email_html(all_incidents):
    cards = "".join([build_incident_card(inc) for inc in all_incidents])
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    news = len([i for i in all_incidents if i.get("source") == "News"])
    social = len([i for i in all_incidents if i.get("source") in ["X", "Instagram"]])
    high = len([i for i in all_incidents if i.get("probability", "").upper() == "HIGH"])
    medium = len([i for i in all_incidents if i.get("probability", "").upper() == "MEDIUM"])

    return (
        '<div style="font-family:Arial,sans-serif;max-width:660px;margin:0 auto;background:#f3f4f6;padding:16px;">'
        '<div style="background:#B91C1C;padding:20px 24px;border-radius:10px 10px 0 0;">'
        '<h1 style="color:white;margin:0;font-size:20px;letter-spacing:0.5px;">🚨 POTHOLEWATCH INTELLIGENCE REPORT</h1>'
        '<p style="color:#fca5a5;margin:6px 0 0;font-size:13px;">Panama &nbsp;·&nbsp; ' + str(len(all_incidents)) + ' incidents &nbsp;·&nbsp; ' + str(high) + ' HIGH &nbsp;·&nbsp; ' + str(medium) + ' MEDIUM &nbsp;·&nbsp; ' + timestamp + '</p>'
        '<p style="color:#fca5a5;margin:4px 0 0;font-size:12px;">' + str(news) + ' from news &nbsp;·&nbsp; ' + str(social) + ' from social media</p>'
        '</div>'
        '<div style="padding:20px;background:white;border-radius:0 0 10px 10px;border:1px solid #e5e7eb;border-top:none;">'
        + cards +
        '<p style="font-size:10px;color:#9ca3af;margin-top:20px;border-top:1px solid #e5e7eb;padding-top:12px;">'
        'PotholeWatch — PowerFix Inc. | News + X + Instagram + MOP + ATTT Cross-Reference | Auto-scan every 2 hours'
        '</p>'
        '</div>'
        '</div>'
    )

def send_alert(all_incidents):
    service = get_gmail_service()
    high = len([i for i in all_incidents if i.get("probability", "").upper() == "HIGH"])
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "🚨 PotholeWatch Panama | " + str(len(all_incidents)) + " incidents | " + str(high) + " HIGH | " + datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    msg["From"] = "ashourilevy@gmail.com"
    msg["To"] = ", ".join(ALERT_RECIPIENTS)
    msg.attach(MIMEText(build_email_html(all_incidents), "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print("Email sent to " + ", ".join(ALERT_RECIPIENTS))

# ── Main ────────────────────────────────────────────────────────────────────
def main():
    print("PotholeWatch started — " + datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))

    all_incidents = []
    seen_urls = set()

    for scan in SCANS:
        print("\n── " + scan["name"] + " ──")
        print("  Query: " + scan["query"])
        incidents = search_incidents(scan)
        for inc in incidents:
            url = inc.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_incidents.append(inc)
                print("  [" + inc.get("probability", "?") + "] [" + inc.get("source", "?") + "] " + inc.get("title", "")[:70])

    print("\n── Cross-Referencing " + str(len(all_incidents)) + " incidents ──")
    for inc in all_incidents:
        print("\n  Incident: " + inc.get("title", "")[:60])
        inc["xref"] = cross_reference(inc)

    print("\n── Summary ──")
    print("Total incidents: " + str(len(all_incidents)))

    if all_incidents:
        send_alert(all_incidents)
    else:
        print("Nothing found — no email sent.")

    print("\nScan complete.")

if __name__ == "__main__":
    main()

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

# One query per scan to stay within free tier rate limits
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
    "You are a road incident and pothole complaint data extractor. "
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

# ── Scan ────────────────────────────────────────────────────────────────────
def search_incidents(scan):
    query = scan["query"]
    scan_type = scan["type"]

    if scan_type == "social":
        user_prompt = (
            "Search for recent public social media posts (last 7 days) about potholes or road damage in Panama: " + query + "\n\n"
            "Find ALL posts, complaints, or mentions — even if probability of road damage is very low (10% or more counts).\n"
            "For each post found return:\n"
            "{\n"
            "  \"incidents\": [\n"
            "    {\n"
            "      \"title\": \"short description of the post\",\n"
            "      \"url\": \"link to the post\",\n"
            "      \"date\": \"YYYY-MM-DD\",\n"
            "      \"location\": \"street or area mentioned\",\n"
            "      \"description\": \"what the post says\",\n"
            "      \"probability\": \"HIGH or MEDIUM or LOW\",\n"
            "      \"probability_reason\": \"why road conditions are likely a factor\",\n"
            "      \"source\": \"X or Instagram\"\n"
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Include EVERYTHING — even LOW probability. If nothing found return: {\"incidents\": []}"
        )
    else:
        user_prompt = (
            "Search for road accident news in Panama from the last 7 days: " + query + "\n\n"
            "Find ALL incidents — even if probability of road damage is very low (10% or more counts).\n"
            "For each incident found return:\n"
            "{\n"
            "  \"incidents\": [\n"
            "    {\n"
            "      \"title\": \"headline\",\n"
            "      \"url\": \"https://...\",\n"
            "      \"date\": \"YYYY-MM-DD\",\n"
            "      \"location\": \"street or area\",\n"
            "      \"description\": \"1-2 sentence summary\",\n"
            "      \"probability\": \"HIGH or MEDIUM or LOW\",\n"
            "      \"probability_reason\": \"why road conditions may have contributed\",\n"
            "      \"source\": \"News\"\n"
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Include EVERYTHING — even LOW probability. If nothing found return: {\"incidents\": []}"
        )

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
                "messages": [{"role": "user", "content": user_prompt}]
            },
            timeout=60
        )
        response.raise_for_status()
    except Exception as e:
        print("  API call failed: " + str(e))
        return []

    data = response.json()
    text = ""
    for block in data.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text += block.get("text", "")

    if not text.strip():
        print("  Empty response")
        return []

    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end <= start:
        print("  No JSON found. Response: " + text[:200])
        return []

    try:
        parsed = json.loads(text[start:end])
        incidents = parsed.get("incidents", [])
        print("  Found " + str(len(incidents)) + " result(s)")
        return incidents
    except json.JSONDecodeError as e:
        print("  JSON parse error: " + str(e))
        print("  Raw: " + text[:300])
        return []

# ── Email ───────────────────────────────────────────────────────────────────
def source_badge(source):
    colors = {
        "X": "#000000",
        "Instagram": "#E1306C",
        "News": "#2563eb"
    }
    color = colors.get(source, "#6B7280")
    return '<span style="background:' + color + ';color:white;padding:1px 7px;border-radius:10px;font-size:10px;margin-left:6px;">' + source + '</span>'

def build_email_html(all_incidents):
    rows = ""
    for inc in all_incidents:
        prob = inc.get("probability", "LOW").upper()
        prob_color = "#B91C1C" if prob == "HIGH" else "#D97706" if prob == "MEDIUM" else "#6B7280"
        source = inc.get("source", "News")
        rows += (
            '<div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin-bottom:12px;">'
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">'
            '<div><strong style="font-size:13px;">' + inc.get("title", "Untitled") + '</strong>' + source_badge(source) + '</div>'
            '<span style="background:' + prob_color + ';color:white;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:bold;">' + prob + '</span>'
            '</div>'
            '<p style="margin:3px 0;font-size:12px;color:#6b7280;">📍 ' + inc.get("location", "Unknown") + ' &nbsp;|&nbsp; 📅 ' + inc.get("date", "") + '</p>'
            '<p style="margin:8px 0;font-size:13px;">' + inc.get("description", "") + '</p>'
            '<p style="margin:4px 0;font-size:11px;color:#6b7280;font-style:italic;">' + inc.get("probability_reason", "") + '</p>'
            '<a href="' + inc.get("url", "#") + '" style="font-size:12px;color:#2563eb;text-decoration:none;">View source →</a>'
            '</div>'
        )

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    news = [i for i in all_incidents if i.get("source") == "News"]
    social = [i for i in all_incidents if i.get("source") in ["X", "Instagram"]]

    return (
        '<div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;">'
        '<div style="background:#B91C1C;padding:16px 20px;border-radius:8px 8px 0 0;">'
        '<h1 style="color:white;margin:0;font-size:18px;">🚨 POTHOLEWATCH ALERT — Panama</h1>'
        '<p style="color:#fca5a5;margin:6px 0 0;font-size:13px;">'
        + str(len(all_incidents)) + ' total &nbsp;·&nbsp; '
        + str(len(news)) + ' news &nbsp;·&nbsp; '
        + str(len(social)) + ' social &nbsp;·&nbsp; '
        + timestamp +
        '</p>'
        '</div>'
        '<div style="padding:20px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;background:#f9fafb;">'
        + rows +
        '<p style="font-size:10px;color:#9ca3af;margin-top:20px;border-top:1px solid #e5e7eb;padding-top:12px;">'
        'PotholeWatch — PowerFix Inc. | Monitors news + X + Instagram | Auto-scan every 30 min'
        '</p>'
        '</div>'
        '</div>'
    )

def send_alert(all_incidents):
    service = get_gmail_service()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "🚨 PotholeWatch Panama | " + str(len(all_incidents)) + " incident(s) | " + datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
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

    print("\n── Summary ──")
    print("Total unique incidents: " + str(len(all_incidents)))

    if all_incidents:
        send_alert(all_incidents)
    else:
        print("Nothing found — no email sent.")

    print("\nScan complete.")

if __name__ == "__main__":
    main()

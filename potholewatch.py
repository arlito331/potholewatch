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

TERRITORIES = [
    {
        "name": "Panama",
        "queries": [
            "accidente carretera Panama 2026",
            "accidente via Panama mayo 2026",
            "camion accidente Panama 2026",
            "choque Panama carretera mayo 2026",
            "accidente via Centenario Panama",
        ]
    }
]

SYSTEM_PROMPT = (
    "You are a road incident data extractor. "
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
def search_incidents(query):
    user_prompt = (
        "Search for road accident news in Panama from the last 7 days using this query: " + query + "\n\n"
        "Return a JSON object with this exact structure (no other text):\n"
        "{\n"
        "  \"incidents\": [\n"
        "    {\n"
        "      \"title\": \"headline of the article\",\n"
        "      \"url\": \"https://...\",\n"
        "      \"date\": \"YYYY-MM-DD\",\n"
        "      \"location\": \"street or area name\",\n"
        "      \"description\": \"1-2 sentence summary\",\n"
        "      \"probability\": \"HIGH or MEDIUM or LOW\",\n"
        "      \"probability_reason\": \"why road conditions may have contributed\"\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "If no incidents found, return: {\"incidents\": []}"
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

    # Collect all text blocks from the response
    text = ""
    for block in data.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text += block.get("text", "")

    if not text.strip():
        print("  Empty response from API")
        return []

    # Extract JSON robustly — find outermost { }
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end <= start:
        print("  No JSON object found. Response was: " + text[:200])
        return []

    try:
        parsed = json.loads(text[start:end])
        incidents = parsed.get("incidents", [])
        print("  Found " + str(len(incidents)) + " incident(s)")
        return incidents
    except json.JSONDecodeError as e:
        print("  JSON parse error: " + str(e))
        print("  Raw text: " + text[:300])
        return []

# ── Email ───────────────────────────────────────────────────────────────────
def build_email_html(incidents, territory):
    rows = ""
    for inc in incidents:
        prob = inc.get("probability", "MEDIUM").upper()
        color = "#B91C1C" if prob == "HIGH" else "#D97706" if prob == "MEDIUM" else "#6B7280"
        title = inc.get("title", "Untitled")
        location = inc.get("location", "Unknown")
        date = inc.get("date", "")
        description = inc.get("description", "")
        reason = inc.get("probability_reason", "")
        url = inc.get("url", "#")
        rows += (
            '<div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin-bottom:12px;">'
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">'
            '<strong style="font-size:13px;">' + title + '</strong>'
            '<span style="background:' + color + ';color:white;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:bold;">' + prob + '</span>'
            '</div>'
            '<p style="margin:3px 0;font-size:12px;color:#6b7280;">📍 ' + location + ' &nbsp;|&nbsp; 📅 ' + date + '</p>'
            '<p style="margin:8px 0;font-size:13px;">' + description + '</p>'
            '<p style="margin:4px 0;font-size:11px;color:#6b7280;font-style:italic;">' + reason + '</p>'
            '<a href="' + url + '" style="font-size:12px;color:#2563eb;text-decoration:none;">Read article →</a>'
            '</div>'
        )

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return (
        '<div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;background:#ffffff;">'
        '<div style="background:#B91C1C;padding:16px 20px;border-radius:8px 8px 0 0;">'
        '<h1 style="color:white;margin:0;font-size:18px;letter-spacing:0.5px;">🚨 POTHOLEWATCH ALERT</h1>'
        '<p style="color:#fca5a5;margin:4px 0 0;font-size:13px;">' + territory + ' &nbsp;·&nbsp; ' + str(len(incidents)) + ' incident(s) &nbsp;·&nbsp; ' + timestamp + '</p>'
        '</div>'
        '<div style="padding:20px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;background:#f9fafb;">'
        + rows +
        '<p style="font-size:10px;color:#9ca3af;margin-top:20px;border-top:1px solid #e5e7eb;padding-top:12px;">'
        'PotholeWatch — PowerFix Inc. Road Incident Monitoring | Auto-scan every 30 min'
        '</p>'
        '</div>'
        '</div>'
    )

def send_alert(incidents, territory):
    service = get_gmail_service()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "🚨 PotholeWatch | " + territory + " | " + str(len(incidents)) + " incident(s) | " + datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    msg["From"] = "ashourilevy@gmail.com"
    msg["To"] = ", ".join(ALERT_RECIPIENTS)
    msg.attach(MIMEText(build_email_html(incidents, territory), "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print("Email sent to " + ", ".join(ALERT_RECIPIENTS))

# ── Main ────────────────────────────────────────────────────────────────────
def main():
    print("PotholeWatch started — " + datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))

    for territory in TERRITORIES:
        print("\n── Scanning: " + territory["name"] + " ──")
        all_incidents = []

        for query in territory["queries"]:
            print("\n  Query: " + query)
            incidents = search_incidents(query)
            for inc in incidents:
                print("    [" + inc.get("probability", "?") + "] " + inc.get("title", "")[:80])
            all_incidents.extend(incidents)

        # Deduplicate by URL
        seen = set()
        unique = []
        for inc in all_incidents:
            url = inc.get("url", "")
            if url and url not in seen:
                seen.add(url)
                unique.append(inc)

        print("\n  Total unique incidents: " + str(len(unique)))

        if unique:
            send_alert(unique, territory["name"])
        else:
            print("  No incidents found — no email sent.")

    print("\nScan complete.")

if __name__ == "__main__":
    main()

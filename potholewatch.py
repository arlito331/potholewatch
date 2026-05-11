import os
import json
import base64
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

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
    },
]

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

def search_incidents(query):
    prompt = (
        "Search for recent news (last 7 days) about road accidents in Panama: " + query + "\n\n"
        "You MUST return ONLY valid JSON. No explanations, no preamble, no markdown backticks.\n"
        "If you find nothing, return exactly: {\"incidents\": []}\n"
        "Format:\n"
        "{\"incidents\": [{\"title\": \"\", \"url\": \"\", \"date\": \"\", \"location\": \"\", "
        "\"description\": \"\", \"probability\": \"HIGH\", \"probability_reason\": \"\"}]}"
    )

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 3000,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": prompt}]
        }
    )

    data = response.json()
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")

    try:
        # Robustly find JSON boundaries — ignore any preamble or trailing text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            print("  No JSON found in response. Raw: " + text[:300])
            return []
        clean = text[start:end]
        return json.loads(clean).get("incidents", [])
    except Exception as e:
        print("  Parse error: " + str(e))
        print("  Raw: " + text[:500])
        return []

def build_email_html(incidents, territory):
    rows = ""
    for inc in incidents:
        prob = inc.get("probability", "MEDIUM")
        color = "#B91C1C" if prob == "HIGH" else "#D97706" if prob == "MEDIUM" else "#6B7280"
        rows += (
            '<div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin-bottom:12px;">'
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">'
            '<strong style="font-size:13px;">' + inc.get("title", "") + '</strong>'
            '<span style="background:' + color + ';color:white;padding:2px 8px;border-radius:12px;font-size:11px;">' + prob + '</span>'
            '</div>'
            '<p style="margin:3px 0;font-size:12px;color:#6b7280;">📍 ' + inc.get("location", "") + ' | 📅 ' + inc.get("date", "") + '</p>'
            '<p style="margin:6px 0;font-size:12px;">' + inc.get("description", "") + '</p>'
            '<p style="margin:4px 0;font-size:11px;color:#6b7280;"><em>' + inc.get("probability_reason", "") + '</em></p>'
            '<a href="' + inc.get("url", "") + '" style="font-size:11px;color:#2563eb;">Read article →</a>'
            '</div>'
        )
    return (
        '<div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;">'
        '<div style="background:#B91C1C;padding:14px 18px;border-radius:8px 8px 0 0;">'
        '<h1 style="color:white;margin:0;font-size:16px;">🚨 POTHOLEWATCH — ' + territory + ' | ' + str(len(incidents)) + ' incident(s)</h1>'
        '</div>'
        '<div style="background:#f9fafb;padding:18px;border-radius:0 0 8px 8px;border:1px solid #e5e7eb;">'
        '<p style="font-size:12px;color:#6b7280;margin-top:0;">Scan: ' + datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC") + ' | ALL incidents (no filter)</p>'
        + rows +
        '<p style="font-size:10px;color:#9ca3af;margin-top:16px;">PotholeWatch — PowerFix Inc. | Auto-scan every 30 min</p>'
        '</div></div>'
    )

def send_alert(incidents, territory):
    service = get_gmail_service()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "PotholeWatch | " + territory + " | " + str(len(incidents)) + " incident(s) | " + datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    msg["From"] = "ashourilevy@gmail.com"
    msg["To"] = ", ".join(ALERT_RECIPIENTS)
    msg.attach(MIMEText(build_email_html(incidents, territory), "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print("Email sent! " + str(len(incidents)) + " incidents.")

def main():
    print("PotholeWatch started — " + datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    for territory in TERRITORIES:
        print("\nScanning: " + territory["name"])
        all_incidents = []
        for query in territory["queries"]:
            print("  Query: " + query)
            incidents = search_incidents(query)
            print("  Found: " + str(len(incidents)))
            for inc in incidents:
                print("    [" + inc.get("probability", "?") + "] " + inc.get("title", "")[:70])
            all_incidents.extend(incidents)

        # Deduplicate by URL
        seen = set()
        unique = []
        for inc in all_incidents:
            url = inc.get("url", "")
            if url not in seen:
                seen.add(url)
                unique.append(inc)

        print("\nTotal unique incidents: " + str(len(unique)))
        if unique:
            send_alert(unique, territory["name"])
        else:
            print("No incidents found at all.")

    print("\nScan complete.")

if __name__ == "__main__":
    main()

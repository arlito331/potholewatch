import os
import json
import base64
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── Config ──────────────────────────────────────────────────────────────
ALERT_RECIPIENTS = ["joel@powerfixinc.com", "1@powerfixinc.com"]
THRESHOLD = "MEDIUM"  # MEDIUM or HIGH triggers alert
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_CLIENT_ID = os.environ["GMAIL_CLIENT_ID"]
GMAIL_CLIENT_SECRET = os.environ["GMAIL_CLIENT_SECRET"]
GMAIL_REFRESH_TOKEN = os.environ["GMAIL_REFRESH_TOKEN"]

# ── Territories to monitor ───────────────────────────────────────────────
TERRITORIES = [
    {
        "name": "Panama",
        "queries": [
            "accidente vía Centenario Panamá",
            "accidente carretera Panamá bache hueco",
            "accidente vía Interamericana Panamá",
            "accidente Corredor Norte Panamá",
            "accidente Corredor Sur Panamá",
        ]
    },
    # Add more territories here:
    # {
    #     "name": "Costa Rica",
    #     "queries": ["accidente carretera Costa Rica bache"]
    # },
]

# ── Gmail setup ──────────────────────────────────────────────────────────
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

# ── Search for incidents ─────────────────────────────────────────────────
def search_incidents(query):
    """Search for recent news using Anthropic API with web search tool."""
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "tools-2024-04-04",
            "Content-Type": "application/json"
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 2000,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{
                "role": "user",
                "content": f"""Search for very recent news (last 48 hours) about: {query}
                
Find any road accidents or incidents that may be related to poor road conditions (potholes, cracks, deteriorated pavement).
Return a JSON object with this structure:
{{
  "incidents": [
    {{
      "title": "article title",
      "url": "article url",
      "date": "date of incident",
      "location": "specific location",
      "description": "brief description",
      "road_condition_evidence": "any mention of road conditions, potholes, or pavement issues",
      "probability": "HIGH/MEDIUM/LOW",
      "probability_reason": "why this probability was assigned"
    }}
  ]
}}

Only include incidents where road conditions COULD be a contributing factor.
Return ONLY the JSON, no other text."""
            }]
        }
    )
    
    data = response.json()
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    
    try:
        clean = text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(clean).get("incidents", [])
    except:
        return []

# ── Score and filter ─────────────────────────────────────────────────────
def should_alert(incident):
    prob = incident.get("probability", "LOW")
    if THRESHOLD == "MEDIUM":
        return prob in ["MEDIUM", "HIGH"]
    return prob == "HIGH"

# ── Build email HTML ─────────────────────────────────────────────────────
def build_email_html(incidents, territory):
    rows = ""
    for inc in incidents:
        prob = inc.get("probability", "LOW")
        color = "#B91C1C" if prob == "HIGH" else "#D97706"
        rows += f"""
        <div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin-bottom:16px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <strong style="font-size:14px;">{inc.get('title','')}</strong>
            <span style="background:{color};color:white;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:bold;">{prob}</span>
          </div>
          <p style="margin:4px 0;font-size:13px;color:#6b7280;">📍 {inc.get('location','')} &nbsp;|&nbsp; 📅 {inc.get('date','')}</p>
          <p style="margin:8px 0;font-size:13px;">{inc.get('description','')}</p>
          {"<p style='margin:8px 0;font-size:13px;color:#7c3aed;'><strong>Road condition evidence:</strong> " + inc.get('road_condition_evidence','') + "</p>" if inc.get('road_condition_evidence') else ""}
          <p style="margin:8px 0;font-size:12px;color:#6b7280;"><em>{inc.get('probability_reason','')}</em></p>
          <a href="{inc.get('url','')}" style="font-size:12px;color:#2563eb;">Read article →</a>
        </div>"""

    return f"""
    <div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;">
      <div style="background:#B91C1C;padding:16px 20px;border-radius:8px 8px 0 0;">
        <h1 style="color:white;margin:0;font-size:18px;">🚨 POTHOLEWATCH ALERT — {territory}</h1>
      </div>
      <div style="background:#f9fafb;padding:20px;border-radius:0 0 8px 8px;border:1px solid #e5e7eb;">
        <p style="font-size:13px;color:#6b7280;margin-top:0;">
          Scan time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} &nbsp;|&nbsp; 
          Threshold: {THRESHOLD}+ &nbsp;|&nbsp; 
          Territory: {territory}
        </p>
        {rows}
        <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;">
        <p style="font-size:11px;color:#9ca3af;margin:0;">
          Sent by PotholeWatch — PowerFix Inc. Road Incident Monitoring System<br>
          Automated scan every 30 minutes.
        </p>
      </div>
    </div>"""

# ── Send email ───────────────────────────────────────────────────────────
def send_alert(incidents, territory):
    service = get_gmail_service()
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🚨 PotholeWatch Alert | {territory} | {len(incidents)} incident(s) detected"
    msg["From"] = "ashourilevy@gmail.com"
    msg["To"] = ", ".join(ALERT_RECIPIENTS)
    
    html = build_email_html(incidents, territory)
    msg.attach(MIMEText(html, "html"))
    
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"✅ Alert sent for {territory} — {len(incidents)} incident(s)")

# ── Main ─────────────────────────────────────────────────────────────────
def main():
    print(f"PotholeWatch scan started — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    
    for territory in TERRITORIES:
        print(f"\nScanning: {territory['name']}")
        all_incidents = []
        
        for query in territory["queries"]:
            print(f"  Query: {query}")
            incidents = search_incidents(query)
            for inc in incidents:
                if should_alert(inc):
                    all_incidents.append(inc)
        
        # Deduplicate by URL
        seen = set()
        unique = []
        for inc in all_incidents:
            url = inc.get("url", "")
            if url not in seen:
                seen.add(url)
                unique.append(inc)
        
        if unique:
            print(f"  → {len(unique)} alert-worthy incident(s) found. Sending email...")
            send_alert(unique, territory["name"])
        else:
            print(f"  → No incidents above threshold.")
    
    print("\nScan complete.")

if __name__ == "__main__":
    main()

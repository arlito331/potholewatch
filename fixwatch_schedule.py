#!/usr/bin/env python3
"""FixWatch monitoring schedule for Yeison (Panama).

Turns the milestone data into a photography plan so updated pictures are on the
app BEFORE each pothole's milestone email fires.

- Every fixed Panama pothole has upcoming milestones (7/15/30/45/90/180/365 days
  since its repair). Each milestone gets a "visit day" a few days before it
  (LEAD_DAYS, shifted off weekends) so there's buffer.
- DAILY (run Mon-Fri): emails the potholes whose visit day is TODAY, ordered as
  an optimized driving route with a Google Maps link.
- WEEKLY (run Monday): emails the whole week's plan (Mon-Fri), grouped by day,
  each day route-ordered.

Reuses the same Gmail login and data key as fixwatch_milestones.py. The
recipient is the secret YEISON_EMAIL; the job no-ops if it is not set.

Manual test:  MODE=weekly|daily  (workflow_dispatch input)
"""
import os, json, base64, datetime, hashlib, urllib.request, math, unicodedata
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

FIXWATCH_RAW = "https://raw.githubusercontent.com/arlito331/fixwatch/main/"
FIXES_PATH   = "data/fixes.enc"
KEYS_PATH    = "data/keys.json"
MILESTONES   = [7, 15, 30, 45, 90, 180, 365]
LEAD_DAYS    = 3            # photograph ~2-3 days before the milestone (weekends shift earlier)
COUNTRY      = "panama"     # Yeison's territory (matched accent/case-insensitively)
YEISON_EMAIL = os.environ.get("YEISON_EMAIL", "").strip()
BG, CARD_BG, TEXT, MUTED, ACCENT, GOOD = "#0D0D0D", "#161616", "#F5F5F5", "#8A8A8A", "#E8442A", "#46C97E"
DATA_KEY = None

def panama_today():
    return (datetime.datetime.utcnow() - datetime.timedelta(hours=5)).date()

# ------------------------------------------------------------------ data
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fixwatch-schedule"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def get_data_key():
    if os.environ.get("FIXWATCH_DATA_KEY"):
        return base64.b64decode(os.environ["FIXWATCH_DATA_KEY"].strip())
    pw = os.environ.get("FIXWATCH_MASTER_PASSWORD", "").strip()
    cfg = json.loads(fetch(FIXWATCH_RAW + KEYS_PATH))
    salt, it = base64.b64decode(cfg["kdf"]["salt"]), cfg["kdf"]["iter"]
    kek = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it)
    for e in cfg["entries"]:
        try:
            payload = AESGCM(kek).decrypt(base64.b64decode(e["iv"]), base64.b64decode(e["ct"]), None)
            return base64.b64decode(json.loads(payload)["k"])
        except Exception:
            continue
    raise SystemExit("FIXWATCH_MASTER_PASSWORD did not match any FixWatch account.")

def load_fixes():
    local = os.environ.get("FIXWATCH_LOCAL")
    raw = open(local, "rb").read() if local else fetch(FIXWATCH_RAW + FIXES_PATH)
    data = json.loads(AESGCM(DATA_KEY).decrypt(raw[:12], raw[12:], None))
    return data if isinstance(data, list) else data.get("fixes", [])

def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn").lower().strip()

def title(f):
    return (f.get("label") or "").strip() or f"Pothole #{f.get('num','')}".strip() or "Pothole"

def place(f):
    return ", ".join(x for x in (f.get("city"), f.get("country")) if x)

def fmt(d):
    return d.strftime("%a %b %d")

# ------------------------------------------------------------------ scheduling
def visit_date_for(milestone_date):
    d = milestone_date - datetime.timedelta(days=LEAD_DAYS)
    while d.weekday() >= 5:          # Sat/Sun -> back up to the preceding Friday
        d -= datetime.timedelta(days=1)
    return d

def visits_for(fix):
    """[(visit_date, milestone, milestone_date)] for every future milestone."""
    out = []
    try:
        fd = datetime.date.fromisoformat(fix["fix_date"])
    except Exception:
        return out
    for m in MILESTONES:
        md = fd + datetime.timedelta(days=m)
        out.append((visit_date_for(md), m, md))
    return out

def haversine(a, b):
    R = 6371.0
    dlat, dlon = math.radians(b[0]-a[0]), math.radians(b[1]-a[1])
    x = math.sin(dlat/2)**2 + math.cos(math.radians(a[0]))*math.cos(math.radians(b[0]))*math.sin(dlon/2)**2
    return 2*R*math.asin(math.sqrt(x))

def route_order(items):
    """Nearest-neighbor chain starting from the northern-most point."""
    pts = items[:]
    if len(pts) <= 2:
        return pts
    start = max(range(len(pts)), key=lambda i: pts[i]["fix"]["lat"])
    order = [pts.pop(start)]
    while pts:
        last = (order[-1]["fix"]["lat"], order[-1]["fix"]["lng"])
        nxt = min(range(len(pts)), key=lambda i: haversine(last, (pts[i]["fix"]["lat"], pts[i]["fix"]["lng"])))
        order.append(pts.pop(nxt))
    return order

def maps_link(ordered):
    pts = "/".join(f"{it['fix']['lat']:.6f},{it['fix']['lng']:.6f}" for it in ordered)
    return "https://www.google.com/maps/dir/" + pts

# ------------------------------------------------------------------ email
def stop_rows(ordered):
    rows = []
    for i, it in enumerate(ordered, 1):
        f, m, md = it["fix"], it["milestone"], it["milestone_date"]
        rows.append(f"""
      <div style="display:flex;gap:12px;padding:12px 0;border-bottom:1px solid #222;">
        <div style="min-width:26px;height:26px;border-radius:13px;background:{ACCENT};color:#fff;font-weight:800;font-size:13px;text-align:center;line-height:26px;">{i}</div>
        <div style="flex:1;">
          <div style="color:{TEXT};font-weight:700;font-size:15px;">{title(f)}</div>
          <div style="color:{MUTED};font-size:12px;">{place(f)}</div>
          <div style="color:{GOOD};font-size:12px;margin-top:2px;">Photograph for the <b>Day {m}</b> check · milestone {fmt(md)}</div>
          <div style="font-size:12px;margin-top:3px;"><a href="https://www.google.com/maps/search/?api=1&query={f['lat']},{f['lng']}" style="color:{ACCENT};">open pin</a></div>
        </div>
      </div>""")
    return "".join(rows)

def day_block(day, ordered):
    if not ordered:
        return ""
    return f"""
  <div style="margin-bottom:20px;padding:18px;background:{CARD_BG};border-radius:10px;border-left:4px solid {ACCENT};">
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <div style="font-size:16px;color:{TEXT};font-weight:800;">{day.strftime('%A')} · {fmt(day)}</div>
      <div style="font-size:12px;color:{MUTED};">{len(ordered)} stop{'s' if len(ordered)!=1 else ''}</div>
    </div>
    <div style="margin:6px 0 10px;"><a href="{maps_link(ordered)}" style="display:inline-block;background:{ACCENT};color:#fff;text-decoration:none;font-weight:700;font-size:13px;padding:8px 14px;border-radius:8px;">▶ Open driving route</a></div>
    {stop_rows(ordered)}
  </div>"""

def shell(heading, sub, body):
    return f"""<!DOCTYPE html><html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;background:{BG};padding:24px;color:{TEXT};margin:0;">
<div style="max-width:640px;margin:auto;">
  <div style="margin-bottom:22px;padding:22px;background:{CARD_BG};border-radius:10px;border-top:4px solid {ACCENT};">
    <div style="font-size:11px;letter-spacing:4px;color:{ACCENT};font-weight:700;">FIXWATCH · MONITORING ROUTE</div>
    <h1 style="margin:10px 0 4px;font-size:23px;color:{TEXT};font-weight:700;">{heading}</h1>
    <div style="color:{MUTED};font-size:12px;">{sub}</div>
  </div>
  {body}
  <div style="text-align:center;font-size:11px;color:{MUTED};padding:20px 0;border-top:1px solid #262626;margin-top:6px;">
    <div style="font-size:10px;letter-spacing:3px;color:{ACCENT};font-weight:700;margin-bottom:6px;">POWERFIX · REPAIR. REINVENTED.</div>
    Take updated photos at each stop and upload them in the FixWatch app.
  </div>
</div></body></html>"""

def send_html(subject, html):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(
        token=None, refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"], client_secret=os.environ["GMAIL_CLIENT_SECRET"])
    service = build("gmail", "v1", credentials=creds)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = "FixWatch <ashourilevy@gmail.com>"
    msg["To"] = YEISON_EMAIL
    msg.attach(MIMEText(html, "html"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()

DRY = bool(os.environ.get("DRY_RUN"))

def collect(fixes, day):
    items = []
    for f in fixes:
        for vd, m, md in visits_for(f):
            if vd == day:
                items.append({"fix": f, "milestone": m, "milestone_date": md})
    return route_order(items)

def do_daily(fixes, today):
    ordered = collect(fixes, today)
    if not ordered:
        print(f"daily {today}: nothing to photograph."); return
    body = day_block(today, ordered)
    subject = f"FixWatch · Today's route — {len(ordered)} pothole{'s' if len(ordered)!=1 else ''} to photograph"
    print(f"daily {today}: {len(ordered)} stops")
    if DRY: print(subject); return
    send_html(subject, shell("Today's monitoring route", f"{fmt(today)} · Panama", body))

def do_weekly(fixes, monday):
    days = [monday + datetime.timedelta(days=i) for i in range(5)]   # Mon..Fri
    blocks, total = [], 0
    for d in days:
        ordered = collect(fixes, d)
        total += len(ordered)
        blocks.append(day_block(d, ordered) or
                      f'<div style="padding:10px 4px;color:{MUTED};font-size:13px;">{d.strftime("%A")} · {fmt(d)} — no stops</div>')
    if not total:
        print(f"weekly {monday}: nothing this week."); return
    subject = f"FixWatch · This week's route — {total} pothole visit{'s' if total!=1 else ''} (Mon–Fri)"
    print(f"weekly {monday}: {total} stops across the week")
    if DRY: print(subject); return
    send_html(subject, shell("This week's monitoring plan", f"Week of {fmt(monday)} · Panama", "".join(blocks)))

def main():
    global DATA_KEY
    if not YEISON_EMAIL and not DRY:
        print("YEISON_EMAIL not set — nothing to send."); return
    DATA_KEY = get_data_key()
    today = panama_today()
    fixes = [f for f in load_fixes()
             if f.get("fix_date") and fold(f.get("country")) == COUNTRY
             and isinstance(f.get("lat"), (int, float)) and isinstance(f.get("lng"), (int, float))]
    print(f"=== schedule @ {today} ({today.strftime('%A')}) · {len(fixes)} fixed Panama potholes ===")
    mode = os.environ.get("MODE", "").strip().lower()
    if mode == "weekly":
        do_weekly(fixes, today - datetime.timedelta(days=today.weekday()))
    elif mode == "daily":
        do_daily(fixes, today)
    else:
        # scheduled run: daily Mon-Fri, plus the weekly plan on Mondays
        if today.weekday() == 0:
            do_weekly(fixes, today)
        if today.weekday() < 5:
            do_daily(fixes, today)

if __name__ == "__main__":
    main()

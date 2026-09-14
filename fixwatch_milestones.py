#!/usr/bin/env python3
"""FixWatch repair-milestone emailer.

Runs daily (GitHub Actions). For every FIXED pothole in the FixWatch data it
checks how many days have passed since fix_date. When a pothole crosses a
milestone (7, 15, 30, 45, 90, 180, 365 days) that has not been emailed yet, it
sends a digest email to Joel + 1@ that names the pothole and embeds its most
recent photo.

- Never repeats: what has been sent is recorded in fixwatch_milestones_sent.json.
- Never blasts the backlog: on the very first run (no state file yet) it records
  every already-passed milestone WITHOUT emailing, then only emails milestones
  that are freshly crossed from then on.
- Reuses the PotholeWatch Gmail login (ashourilevy@gmail.com). The only new
  secret is FIXWATCH_DATA_KEY, the base64 AES-256 key that decrypts FixWatch
  data (the contents of KEYS/fixwatch-master-key.txt).

Local dry run (no email, no Gmail creds needed), against a local data file:
    FIXWATCH_DATA_KEY=<b64key> DRY_RUN=1 \
    FIXWATCH_LOCAL=/path/to/fixes.enc python fixwatch_milestones.py
"""
import os, json, base64, datetime, urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ------------------------------------------------------------------ config
FIXWATCH_RAW = "https://raw.githubusercontent.com/arlito331/fixwatch/main/"
FIXES_PATH   = "data/fixes.enc"
MILESTONES   = [7, 15, 30, 45, 90, 180, 365]
RECIPIENTS   = ["joel@powerfixinc.com", "1@powerfixinc.com"]
STATE_FILE   = "fixwatch_milestones_sent.json"
GRACE_DAYS   = 2   # still catch a milestone if a daily run was missed by a day or two

DRY_RUN = bool(os.environ.get("DRY_RUN"))
DATA_KEY = base64.b64decode(os.environ["FIXWATCH_DATA_KEY"])

# brand palette (matches the PotholeWatch email look)
BG, CARD_BG, TEXT, MUTED, ACCENT, GOOD = "#0D0D0D", "#161616", "#F5F5F5", "#8A8A8A", "#E8442A", "#46C97E"

# ------------------------------------------------------------------ helpers
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fixwatch-milestones"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def aes_decrypt(blob):
    return AESGCM(DATA_KEY).decrypt(blob[:12], blob[12:], None)

def load_fixes():
    local = os.environ.get("FIXWATCH_LOCAL")
    raw = open(local, "rb").read() if local else fetch(FIXWATCH_RAW + FIXES_PATH)
    data = json.loads(aes_decrypt(raw))
    return data if isinstance(data, list) else data.get("fixes", [])

VIDEO_EXT = ("mp4", "mov", "m4v", "webm", "3gp")
def is_video(p):
    return p.get("type") == "video" or p.get("path", "").lower().rsplit(".", 1)[-1] in VIDEO_EXT

def last_photo(fix):
    """The most recent photo (by date, then path). For a video, its poster still."""
    photos = fix.get("photos") or []
    if not photos:
        return None
    p = max(photos, key=lambda x: (x.get("date", ""), x.get("path", "")))
    path = p.get("poster") if (is_video(p) and p.get("poster")) else p.get("path")
    return {"path": path, "date": p.get("date", "")}

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f), True
    return {}, False

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)

def place(fix):
    return ", ".join(x for x in (fix.get("city"), fix.get("country")) if x)

def title(fix):
    return (fix.get("label") or "").strip() or f"Pothole #{fix.get('num', '')}".strip() or "Pothole"

def fmt_date(ds):
    try:
        return datetime.date.fromisoformat(ds).strftime("%b %d, %Y")
    except Exception:
        return ds

# ------------------------------------------------------------------ email
def build_email(hits):
    """hits: list of dicts {fix, milestone, cid, photo_date}. Returns (subject, html)."""
    n = len(hits)
    if n == 1:
        h = hits[0]
        subject = f"FixWatch · Day {h['milestone']} · {title(h['fix'])} ({h['fix'].get('country','')})"
    else:
        subject = f"FixWatch · {n} repair milestones today"

    cards = []
    for h in hits:
        f, m = h["fix"], h["milestone"]
        img = (f'<img src="cid:{h["cid"]}" width="100%" '
               f'style="display:block;border-radius:8px;margin-top:14px;max-width:100%;">'
               if h.get("cid") else "")
        cards.append(f"""
      <div style="margin-bottom:18px;padding:22px;background:{CARD_BG};border-radius:10px;border-left:4px solid {GOOD};">
        <div style="font-size:11px;letter-spacing:3px;color:{GOOD};font-weight:700;">DAY {m} · REPAIR HOLDING</div>
        <div style="font-size:22px;color:{TEXT};font-weight:700;margin:6px 0 2px;">{title(f)}</div>
        <div style="color:{MUTED};font-size:13px;">{place(f)}</div>
        <div style="color:{MUTED};font-size:13px;margin-top:6px;">Fixed {fmt_date(f.get('fix_date',''))} · {m} days ago{(' · latest photo ' + fmt_date(h['photo_date'])) if h.get('photo_date') else ''}</div>
        {img}
      </div>""")

    html = f"""<!DOCTYPE html><html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;background:{BG};padding:24px;color:{TEXT};margin:0;">
<div style="max-width:640px;margin:auto;">
  <div style="margin-bottom:22px;padding:22px;background:{CARD_BG};border-radius:10px;border-top:4px solid {ACCENT};">
    <div style="font-size:11px;letter-spacing:4px;color:{ACCENT};font-weight:700;">FIXWATCH · REPAIR MILESTONES</div>
    <h1 style="margin:10px 0 4px;font-size:24px;color:{TEXT};font-weight:700;">{n} repair{'s' if n != 1 else ''} hit a milestone today</h1>
    <div style="color:{MUTED};font-size:12px;">Automatic follow-up · {datetime.date.today().strftime('%b %d, %Y')}</div>
  </div>
  {''.join(cards)}
  <div style="text-align:center;font-size:11px;color:{MUTED};padding:20px 0;border-top:1px solid #262626;margin-top:6px;">
    <div style="font-size:10px;letter-spacing:3px;color:{ACCENT};font-weight:700;margin-bottom:6px;">POWERFIX · REPAIR. REINVENTED.</div>
    Automatic milestone report from FixWatch.
  </div>
</div></body></html>"""
    return subject, html

def send_email(subject, html, images):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(
        token=None, refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"], client_secret=os.environ["GMAIL_CLIENT_SECRET"])
    service = build("gmail", "v1", credentials=creds)
    root = MIMEMultipart("related")
    root["Subject"] = subject
    root["From"] = "FixWatch <ashourilevy@gmail.com>"
    root["To"] = ", ".join(RECIPIENTS)
    alt = MIMEMultipart("alternative"); root.attach(alt)
    alt.attach(MIMEText(html, "html"))
    for cid, jpg in images.items():
        img = MIMEImage(jpg, _subtype="jpeg")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header("Content-Disposition", "inline")
        root.attach(img)
    raw = base64.urlsafe_b64encode(root.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()

# ------------------------------------------------------------------ main
def main():
    today = datetime.date.today()
    fixes = load_fixes()
    state, existed = load_state()
    print(f"=== FixWatch milestones @ {today} · {len(fixes)} potholes · "
          f"state {'loaded' if existed else 'NEW (baseline run — no emails)'} ===")

    hits, images, changed = [], {}, False
    for f in fixes:
        fd = f.get("fix_date")
        if not fd:
            continue
        try:
            days = (today - datetime.date.fromisoformat(fd)).days
        except Exception:
            continue
        sent = set(state.get(f["id"], []))
        for m in MILESTONES:
            if m in sent or days < m:
                continue
            fresh = existed and days <= m + GRACE_DAYS
            if fresh:
                hits.append({"fix": f, "milestone": m})
            sent.add(m); changed = True
        if sent:
            state[f["id"]] = sorted(sent)

    if not existed:
        save_state(state)
        print(f"Baseline recorded for {len(state)} potholes. No email sent on the first run.")
        return

    if not hits:
        if changed:
            save_state(state)
        print("No new milestones today.")
        return

    # attach the latest photo for each hit
    for i, h in enumerate(hits):
        lp = last_photo(h["fix"])
        if lp and lp["path"]:
            h["photo_date"] = lp["date"]
            if not DRY_RUN:
                try:
                    h["cid"] = f"ph{i}"
                    images[h["cid"]] = aes_decrypt(fetch(FIXWATCH_RAW + lp["path"] + ".enc"))
                except Exception as e:
                    print(f"  photo fetch failed for {h['fix']['id']}: {e}")
                    h.pop("cid", None)

    subject, html = build_email(hits)
    print("Milestones to email:")
    for h in hits:
        print(f"  · Day {h['milestone']:>3}  {title(h['fix'])}  ({h['fix'].get('country','')})  fixed {h['fix'].get('fix_date')}")
    print(f"Subject: {subject}")

    if DRY_RUN:
        print("DRY_RUN — not sending, not saving state.")
        return

    send_email(subject, html, images)
    save_state(state)
    print(f"Sent to {', '.join(RECIPIENTS)} with {len(images)} photo(s).")

if __name__ == "__main__":
    main()

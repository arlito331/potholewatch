# Integration: Adding citizen_comments to potholewatch_v3.py

## Step 1 — Add this file to your repo

Drop `citizen_comments.py` next to `potholewatch_v3.py` in the root of the repo.

## Step 2 — Add this import near the top of potholewatch_v3.py (around line 25)

```python
from citizen_comments import harvest_citizen_comments, score_boost_from_citizens
```

## Step 3 — In the main() function, after the existing social/crossref calls
## and before score_incident(), add this block:

Find this section (around line 770):

```python
            crossref = cross_reference(inc, coords)
            cx = sum(len(crossref.get(k,[])) for k in ["mop_evidence","attt_evidence","news_history"])
            print(f"    cross-ref: {cx} institutional hits")

            score = score_incident(inc, coords, social, crossref)
            print(f"    SCORE: {score['score']}")
```

Replace with:

```python
            crossref = cross_reference(inc, coords)
            cx = sum(len(crossref.get(k,[])) for k in ["mop_evidence","attt_evidence","news_history"])
            print(f"    cross-ref: {cx} institutional hits")

            # NEW — harvest citizen comments from IG, FB, X, news comments
            citizens = harvest_citizen_comments(claude_call, WEB_SEARCH_TOOL, inc)

            score = score_incident(inc, coords, social, crossref)

            # NEW — boost score based on citizen sentiment
            boosted_score, boost_reasons = score_boost_from_citizens(citizens, score["score"])
            if boosted_score != score["score"]:
                print(f"    SCORE: {score['score']} → {boosted_score} (citizen boost: {', '.join(boost_reasons)})")
                score["score"] = boosted_score
                score["reasoning"] += f" Citizen boost: {', '.join(boost_reasons)}."
            else:
                print(f"    SCORE: {score['score']}")
```

## Step 4 — In build_incident_record(), add citizens to the saved JSON

Find this section (around line 565):

```python
        # Cross-reference
        "crossref": {
            "mop_evidence": crossref.get("mop_evidence", [])[:3],
            "attt_evidence": crossref.get("attt_evidence", [])[:3],
            "news_history": crossref.get("news_history", [])[:5],
        },
```

Right after it (still inside the return dict), add:

```python
        # Citizen comments (NEW)
        "citizens": {
            "instagram": citizens.get("instagram", []),
            "facebook": citizens.get("facebook", []),
            "x_twitter": citizens.get("x_twitter", []),
            "article_comments": citizens.get("article_comments", []),
            "total_relevant": citizens.get("total_relevant", 0),
            "signal_score": citizens.get("signal_score", 0),
        },
```

And update the function signature to accept citizens:

```python
def build_incident_record(case_id, inc, coords, social, crossref, citizens, score, scan_time_iso):
```

And the call site in main():

```python
full_record = build_incident_record(case_id, inc, coords, social, crossref, citizens, score, scan_time_iso)
```

## Step 5 — Update email card to show citizen quotes (optional but nice)

In build_incident_card(), after the existing social_html block, add:

```python
    # Citizen comments section
    citizen_html = ""
    for label, key in [("Instagram", "instagram"), ("Facebook / Tráfico Panamá", "facebook"),
                       ("X / Twitter", "x_twitter"), ("Reader comments", "article_comments")]:
        items = citizens.get(key, [])
        if items:
            rows = "".join(_post_row({"user": c.get("user",""), "quote": c.get("quote",""), "url": c.get("url","")}) for c in items)
            citizen_html += f"""<div style="margin-top:16px;padding-top:14px;border-top:1px solid {SOFT};">
              <div style="font-size:10px;letter-spacing:2px;color:{ACCENT};font-weight:700;text-transform:uppercase;margin-bottom:8px;">Citizens — {label}</div>
              {rows}
            </div>"""
```

Then add `{citizen_html}` to the card template after `{social_html}{inst_html}`.

And update build_incident_card signature to accept citizens.

## What this gets you

- Max 3 quality-filtered comments per platform per incident
- Auto-boost incidents from MEDIUM → HIGH when 3+ citizens across 2+ platforms confirm
- All free, no Apify, no paid APIs
- Quality over quantity: only comments with pothole/road keywords pass the filter

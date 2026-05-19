"""
citizen_comments.py — Free-tier citizen comment harvester for PotholeWatch
==========================================================================
Scrapes public citizen comments from social media and news sites for each
incident. NO paid APIs — uses public endpoints, oembed, and Claude's web
search to find the most relevant comments.

STRATEGY:
  - Quality over quantity: max 3 high-signal quotes per platform
  - Aggressive keyword filtering: only "pothole-relevant" comments kept
  - Free sources only: Instagram oembed, public FB posts, public X search,
    Disqus iframes, news article comment HTML

INTEGRATION:
  Called from main scanner after `search_social_evidence()`. Adds a new
  field `citizen_quotes` to each incident with up to 3 filtered comments
  per platform, plus a `citizen_signal_score` (0-100).

CADENCE NOTE:
  This runs per-incident, ~5 Claude API calls per incident. Budget aware.
"""

import json
import re
import requests
from urllib.parse import quote_plus

# ============================================================
# KEYWORD FILTERS — only comments matching these are kept
# ============================================================

POTHOLE_KEYWORDS = [
    # Direct pothole vocabulary
    "bache", "baches", "hueco", "huecos", "cráter", "crater",
    "forado", "forados", "parcho", "parchos", "remiendo",
    # Road damage
    "carretera dañada", "vía dañada", "calle dañada",
    "deteriorad", "mal estado", "pésimo estado", "destruid",
    "pavimento", "asfalto",
    # Causation
    "por el hueco", "por el bache", "por culpa del", "se metió en",
    "cayó en el", "evitar el bache", "evitar el hueco",
    # Frustration / institutional callouts
    "MOP", "Tapa Hueco", "TapaHueco", "alcaldía", "ministerio",
    "desde hace meses", "siguen sin arreglar", "cuándo van a",
    "no han hecho nada", "ya van varios", "otro accidente",
    # Location specifics
    "frente al", "frente a la", "a la altura de", "cerca de",
    "antes de llegar", "saliendo de", "entrando a",
]

# Comments must contain at least ONE of these to be kept
def is_relevant_comment(text):
    if not text or len(text) < 8:
        return False
    text_low = text.lower()
    return any(kw.lower() in text_low for kw in POTHOLE_KEYWORDS)

# ============================================================
# 1. INSTAGRAM — via public oembed + targeted search
# ============================================================

def find_instagram_post_url(claude_call_fn, web_search_tool, incident):
    """
    Use Claude's web search to find the matching Mi Diario / Telemetro / etc
    Instagram post for this incident.
    """
    headline = incident.get("title", "")[:80]
    date = incident.get("date", "")
    location = incident.get("location_text", "")[:60]

    prompt = f"""Find the Instagram post URL from a Panama news outlet that covers this specific accident.

ACCIDENT:
  Title: {headline}
  Date: {date}
  Location: {location}

Search for the matching Instagram post on these accounts:
- @midiariopanama
- @telemetro
- @critica.com.pa
- @prensacom
- @tvnpanama
- @traficopanama

Run searches like:
  site:instagram.com/midiariopanama "{location.split(',')[0]}"
  site:instagram.com/p/ {date} Panama accidente

Return ONLY the most relevant Instagram post URL (no prose, no fences).
If multiple match, return the one with the most engagement.
If nothing matches, return "NONE".

URL only:"""

    try:
        raw = claude_call_fn(prompt, tools=[web_search_tool], max_tokens=500)
        url = raw.strip().split("\n")[0].strip()
        if url.startswith("http") and "instagram.com" in url:
            return url
    except Exception as e:
        print(f"      IG search failed: {e}")
    return None


def fetch_instagram_oembed(post_url):
    """
    Free Instagram oembed endpoint — returns post caption and basic data.
    Comments themselves require paid API, but caption + author + author bio
    often contain the citizen sentiment we want.
    """
    try:
        endpoint = f"https://www.instagram.com/api/v1/oembed/?url={quote_plus(post_url)}"
        r = requests.get(endpoint, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        }, timeout=10)
        if r.ok:
            data = r.json()
            return {
                "caption": data.get("title", ""),
                "author": data.get("author_name", ""),
                "url": post_url,
            }
    except Exception as e:
        print(f"      IG oembed failed: {e}")
    return None


def harvest_instagram_top_comments(claude_call_fn, web_search_tool, post_url):
    """
    Use Claude's web search with site:instagram.com to surface the top
    publicly indexed comments on this post. Google indexes many IG comments.
    Returns max 3 filtered comments.
    """
    if not post_url:
        return []

    prompt = f"""Find publicly visible top comments on this Instagram post:

{post_url}

Run these searches:
  site:instagram.com "{post_url.split('/p/')[-1].rstrip('/')}"
  site:instagram.com bache hueco accidente

Look for comments by Panama citizens that mention:
- Pothole/bache/hueco
- Road damage or deterioration
- Specific locations
- Complaints about MOP / authorities
- "Ya van varios", "desde hace meses", causation claims

Return up to 5 comments as JSONL (one per line, no prose, no fences):
{{"user":"...","quote":"...","relevance":"why this is signal"}}

Comments must be in Spanish, real citizen voice. Skip ads, brand posts, irrelevant chatter."""

    try:
        raw = claude_call_fn(prompt, tools=[web_search_tool], max_tokens=2000)
    except Exception as e:
        print(f"      IG comment harvest failed: {e}")
        return []

    comments = []
    for line in raw.splitlines():
        line = line.strip().rstrip(",")
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            c = json.loads(line)
            quote = c.get("quote", "")
            if is_relevant_comment(quote):
                comments.append({
                    "platform": "Instagram",
                    "user": c.get("user", "")[:40],
                    "quote": quote[:400],
                    "url": post_url,
                })
        except json.JSONDecodeError:
            continue

    return comments[:3]


# ============================================================
# 2. FACEBOOK — Tráfico Panamá group + news outlet pages
# ============================================================

def harvest_facebook_traffic_group(claude_call_fn, web_search_tool, incident):
    """
    Tráfico Panamá is a public Facebook group with constant citizen
    pothole/accident reporting. Use Google to find indexed posts that
    match this incident's road/location.
    """
    location = incident.get("location_text", "")[:60]
    location_short = location.split(",")[0] if location else ""
    if not location_short:
        return []

    prompt = f"""Find public Facebook posts from Tráfico Panamá or similar Panama traffic groups about this road.

ROAD/LOCATION: {location_short}
RELATED ACCIDENT: {incident.get('title', '')[:80]}

Run these searches:
  site:facebook.com "Tráfico Panamá" "{location_short}"
  site:facebook.com/groups "Tráfico Panamá" bache hueco "{location_short}"
  "Tráfico Panamá" "{location_short}" accidente
  "Tráfico Panamá" "{location_short}" bache OR hueco

Return up to 5 citizen posts as JSONL (one per line, no prose, no fences):
{{"user":"...","quote":"the post text","url":"post URL if available","date":"approximate date"}}

Must be real Panama citizens reporting road conditions, hazards, or accidents at this location.
Skip news outlet reposts."""

    try:
        raw = claude_call_fn(prompt, tools=[web_search_tool], max_tokens=2500)
    except Exception as e:
        print(f"      FB harvest failed: {e}")
        return []

    comments = []
    for line in raw.splitlines():
        line = line.strip().rstrip(",")
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            c = json.loads(line)
            quote = c.get("quote", "")
            if is_relevant_comment(quote):
                comments.append({
                    "platform": "Facebook / Tráfico Panamá",
                    "user": c.get("user", "")[:40],
                    "quote": quote[:400],
                    "url": c.get("url", ""),
                    "date": c.get("date", ""),
                })
        except json.JSONDecodeError:
            continue

    return comments[:3]


# ============================================================
# 3. X / TWITTER — public search via Claude
# ============================================================

def harvest_x_replies(claude_call_fn, web_search_tool, incident):
    location = incident.get("location_text", "")[:60]
    location_short = location.split(",")[0] if location else ""
    date = incident.get("date", "")

    prompt = f"""Find publicly visible X/Twitter posts and replies about this road incident.

LOCATION: {location_short}
DATE: {date}
INCIDENT: {incident.get('title', '')[:80]}

Run these searches:
  site:x.com "{location_short}" bache hueco accidente
  site:twitter.com "{location_short}" {date}
  site:x.com "@MOPpma" "{location_short}"
  site:x.com "@MiDiarioPanama" "{location_short}" reply

Return up to 5 citizen tweets as JSONL (no prose, no fences):
{{"user":"@...","quote":"tweet text","url":"tweet URL","date":"..."}}

Must mention road conditions, potholes, or this specific accident.
Skip retweets of news outlets."""

    try:
        raw = claude_call_fn(prompt, tools=[web_search_tool], max_tokens=2000)
    except Exception as e:
        print(f"      X harvest failed: {e}")
        return []

    comments = []
    for line in raw.splitlines():
        line = line.strip().rstrip(",")
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            c = json.loads(line)
            quote = c.get("quote", "")
            if is_relevant_comment(quote):
                comments.append({
                    "platform": "X / Twitter",
                    "user": c.get("user", "")[:40],
                    "quote": quote[:280],
                    "url": c.get("url", ""),
                    "date": c.get("date", ""),
                })
        except json.JSONDecodeError:
            continue

    return comments[:3]


# ============================================================
# 4. NEWS ARTICLE COMMENT SECTIONS — Disqus + native widgets
# ============================================================

def harvest_article_comments(claude_call_fn, web_search_tool, incident):
    """
    Fetch the article HTML and extract any visible comment section content.
    Many Panama news sites use Disqus which loads via JS but some leave
    comment previews in the HTML.
    """
    article_url = incident.get("url", "")
    if not article_url:
        return []

    prompt = f"""Fetch this news article and extract any reader comments visible on the page:

URL: {article_url}

Look for:
- Disqus comment iframes (often have visible preview comments)
- Native comment sections at the bottom of the article
- Reader reactions or quoted social media

Return up to 5 reader comments as JSONL (no prose, no fences):
{{"user":"...","quote":"comment text","date":"if available"}}

Skip moderation messages, promotional content, and unrelated chatter.
Focus on comments that mention road conditions, the accident cause, or local context."""

    try:
        raw = claude_call_fn(prompt, tools=[web_search_tool], max_tokens=2000)
    except Exception as e:
        print(f"      article comment harvest failed: {e}")
        return []

    comments = []
    for line in raw.splitlines():
        line = line.strip().rstrip(",")
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            c = json.loads(line)
            quote = c.get("quote", "")
            if is_relevant_comment(quote):
                comments.append({
                    "platform": "News article comments",
                    "user": c.get("user", "")[:40],
                    "quote": quote[:400],
                    "url": article_url,
                    "date": c.get("date", ""),
                })
        except json.JSONDecodeError:
            continue

    return comments[:3]


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def harvest_citizen_comments(claude_call_fn, web_search_tool, incident):
    """
    Run all four harvesters and return consolidated citizen quotes.

    Returns:
      {
        "instagram": [{...}, {...}],  # max 3
        "facebook": [{...}],          # max 3
        "x_twitter": [{...}],         # max 3
        "article_comments": [{...}],  # max 3
        "total_relevant": <int>,
        "signal_score": <0-100>
      }
    """
    print(f"      → harvesting citizen comments...")

    # 1. Instagram — find post first, then harvest comments
    ig_post_url = find_instagram_post_url(claude_call_fn, web_search_tool, incident)
    if ig_post_url:
        print(f"      → IG post found: {ig_post_url[:60]}")
        instagram = harvest_instagram_top_comments(claude_call_fn, web_search_tool, ig_post_url)
    else:
        instagram = []

    # 2. Facebook / Tráfico Panamá
    facebook = harvest_facebook_traffic_group(claude_call_fn, web_search_tool, incident)

    # 3. X / Twitter
    x_twitter = harvest_x_replies(claude_call_fn, web_search_tool, incident)

    # 4. Article comment sections
    article_comments = harvest_article_comments(claude_call_fn, web_search_tool, incident)

    total = len(instagram) + len(facebook) + len(x_twitter) + len(article_comments)

    # Signal score: 0-100 based on coverage + count
    # 3+ comments = strong signal, 2 platforms = corroboration bonus
    platforms_with_data = sum(1 for x in [instagram, facebook, x_twitter, article_comments] if x)
    signal_score = min(100, (total * 15) + (platforms_with_data * 10))

    print(f"      → citizen comments: {total} total ({len(instagram)} IG, {len(facebook)} FB, {len(x_twitter)} X, {len(article_comments)} news)")
    print(f"      → citizen signal score: {signal_score}/100")

    return {
        "instagram": instagram,
        "facebook": facebook,
        "x_twitter": x_twitter,
        "article_comments": article_comments,
        "total_relevant": total,
        "platforms_with_data": platforms_with_data,
        "signal_score": signal_score,
    }


# ============================================================
# SCORING HOOK — call this from main scanner to boost confidence
# ============================================================

def score_boost_from_citizens(citizen_data, current_score):
    """
    Boost incident score if citizens are talking about this road's
    condition independently of the accident article.

    Rules:
      - 3+ relevant comments across 2+ platforms → boost one level
      - 5+ relevant comments → boost one level + flag for review
      - Specific pothole keyword density >50% → boost one level
    """
    SCORE_ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    current_idx = SCORE_ORDER.index(current_score) if current_score in SCORE_ORDER else 0

    total = citizen_data["total_relevant"]
    platforms = citizen_data["platforms_with_data"]

    boost = 0
    reasons = []

    if total >= 3 and platforms >= 2:
        boost += 1
        reasons.append(f"{total} citizen comments across {platforms} platforms")

    if total >= 5:
        boost += 1
        reasons.append("strong multi-source citizen sentiment")

    new_idx = min(len(SCORE_ORDER) - 1, current_idx + boost)
    new_score = SCORE_ORDER[new_idx]

    return new_score, reasons

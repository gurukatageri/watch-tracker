#!/usr/bin/env python3
"""
Watch Launch Tracker v2
-----------------------
Collects watch-industry news, uses Claude to find genuine new releases, scores
them, measures hype, estimates resale potential, and reports via Telegram.

Modes:
  --mode collect   Hourly: check sources, process Telegram commands, send urgent alerts
  --mode digest    Daily: collect, refresh market data, send the morning digest
  --mode test      Check every configured key and send a test message
  --mode probe     Print raw WatchCharts responses (to verify the API connection)
"""

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
from bs4 import BeautifulSoup

from common import (
    CONFIG_PATH, HTTP, RELEASES_ENC_PATH, RELEASES_PATH, SEEN_PATH, STATE_PATH, ask_claude,
    load_json, load_secure, norm, save_secure,
    now_utc, parse_day, parse_iso, save_json, telegram_photos, telegram_send, to_aed,
)
import private
import signals

STATUS_RANK = {"rumor": 0, "announced": 1, "preorder_open": 2, "available_now": 3, "sold_out": 4}
STATUS_LABEL = {"rumor": "Rumored", "announced": "Announced", "preorder_open": "Pre-order open",
                "available_now": "Available now", "sold_out": "Sold out"}


def clean_text(value):
    text = BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def entry_date(entry):
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


TRIAGE_SYSTEM = """You screen watch-industry news for a collector who buys newly launched watches.
For each numbered article, decide whether it announces, previews or credibly leaks a SPECIFIC NEW watch
(new model, limited or numbered edition, boutique or regional exclusive, collaboration, pre-order, online drop,
or a new release date/availability for such a watch).
Exclude: reviews of long-existing models, auctions, pre-owned/market price reports, interviews, business or
corporate news, vintage pieces, straps and accessories, buying guides, lists, discounts and sales.
Reply with ONLY JSON: {"relevant": [article numbers]}"""


EXTRACT_SYSTEM = """You extract new watch releases from an article for a collector-investor.

Collector profile: {interests}
Today's date: {today}

Return ONLY JSON in this shape (max 5 releases; an empty list if the article has no specific new release):
{{"releases": [{{
  "brand": "brand name",
  "model": "model / edition name without the brand",
  "reference": "reference number or null",
  "is_limited": true/false,
  "pieces": integer or null,
  "limited_type": "numbered" | "limited" | "boutique_exclusive" | "regional_exclusive" | "annual_production" | "not_limited" | "unknown",
  "price": number or null,
  "currency": "ISO code such as CHF, USD, EUR, GBP, JPY, AED, or null",
  "status": "rumor" | "announced" | "preorder_open" | "available_now" | "sold_out",
  "launch_date": "YYYY-MM-DD or null (the date it can be bought or reserved)",
  "launch_date_text": "launch timing as stated, incl. time and time zone if given, or null",
  "launch_time": "HH:MM 24-hour clock time of the drop in its own time zone, or null",
  "launch_timezone": "IANA time zone of launch_time, e.g. Asia/Tokyo, Europe/Zurich, America/New_York, or null",
  "sale_method": "online_drop" | "boutique" | "preorder" | "raffle" | "allocation" | "retailers" | "unknown",
  "how_to_buy": "one short practical sentence on where/how to buy or reserve",
  "buy_url": "the URL from ARTICLE LINKS where this watch can be bought or pre-ordered online (brand or retailer shop page), else null",
  "book_url": "the URL from ARTICLE LINKS to reserve, register interest, join a waitlist/raffle or book an appointment, else null",
  "reservation_possible": true/false/null,
  "availability_notes": "regions/markets, especially mentions of UAE, Dubai or Middle East, else null",
  "specs": "short: case size, material, movement, dial",
  "dial": "the dial / colour / material of THIS specific watch, e.g. 'Ice Agate stone dial', or null",
  "variant_count": "how many dial/colour/model versions the launch includes in total (integer), or null",
  "score": integer 1-10,
  "score_reason": "one sentence explaining the score",
  "allocation_likely": true/false
}}]}}

Scoring (collectability and value-retention outlook, be realistic - most limited editions do NOT appreciate):
9-10 demand clearly exceeds supply, brand and spec with a strong record of trading above retail.
7-8 good chance to hold or gain value; desirable brand, low numbers or hyped collaboration.
5-6 interesting collectible, value retention uncertain.
1-4 large runs, fashion brands, or brands that are usually discounted on the secondary market.
Set allocation_likely true when buyers typically need purchase history with the dealer (e.g. Rolex, Patek Philippe,
Audemars Piguet, most sought-after limited pieces from top maisons).
Use null when a fact is not stated; never invent prices, dates or piece counts."""


# ------------------------------------------------------------- collection

def collect_articles(cfg, seen):
    articles, errors = [], []
    cutoff = now_utc() - timedelta(days=cfg.get("max_article_age_days", 4))
    for feed in cfg["feeds"]:
        try:
            resp = HTTP.get(feed["url"], timeout=30)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if not parsed.entries:
                raise ValueError("feed returned no articles")
        except Exception as exc:
            errors.append(f"{feed['name']} ({str(exc)[:80]})")
            continue
        for entry in parsed.entries[:40]:
            link = entry.get("link")
            if not link or link in seen:
                continue
            published = entry_date(entry)
            if published and published < cutoff:
                seen[link] = now_utc().isoformat()
                continue
            articles.append({
                "title": clean_text(entry.get("title", "")),
                "summary": clean_text(entry.get("summary", ""))[:500],
                "link": link,
                "source": feed["name"],
                "published": published.isoformat() if published else None,
            })
    articles.sort(key=lambda a: a["published"] or "", reverse=True)
    return articles, errors


def triage(cfg, articles):
    relevant = []
    for i in range(0, len(articles), 40):
        batch = articles[i:i + 40]
        listing = "\n".join(
            f"{n}. [{a['source']}] {a['title']} — {a['summary'][:220]}"
            for n, a in enumerate(batch, 1)
        )
        reply = ask_claude(cfg, TRIAGE_SYSTEM, listing, max_tokens=400)
        for n in reply.get("relevant", []):
            if isinstance(n, int) and 1 <= n <= len(batch):
                relevant.append(batch[n - 1])
    return relevant


def fetch_article(url):
    """Return (text, image, outbound_links, images) for an article."""
    if "news.google.com" in url:
        return "", None, [], []
    try:
        resp = HTTP.get(url, timeout=25)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        meta = soup.find("meta", property="og:image")
        image = meta.get("content") if meta else None
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
            tag.decompose()
        node = soup.find("article") or soup.find("main") or soup.body
        text = node.get_text(" ", strip=True) if node else ""
        host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", url).split("/")[0])
        links, seen = [], set()
        for a in (node.find_all("a", href=True) if node else []):
            href = a["href"].split("#")[0]
            if not href.startswith("http") or host in href or href in seen:
                continue
            if re.search(r"facebook|twitter|x\.com|instagram|pinterest|linkedin|youtube|whatsapp|mailto|/tag/|/author/", href):
                continue
            seen.add(href)
            links.append({"text": a.get_text(" ", strip=True)[:60], "url": href})
        images, seen_img = [], set()
        for img in (node.find_all("img") if node else []):
            src = img.get("data-src") or img.get("data-lazy-src") or img.get("src") or ""
            if img.get("srcset") and not src.startswith("http"):
                src = img["srcset"].split(",")[-1].strip().split(" ")[0]
            if not src.startswith("http") or src in seen_img or re.search(r"\.svg|logo|icon|avatar|gravatar|sprite|pixel", src, re.I):
                continue
            try:
                if img.get("width") and int(str(img["width"]).rstrip("px")) < 300:
                    continue
            except ValueError:
                pass
            seen_img.add(src)
            caption = img.find_parent("figure").get_text(" ", strip=True)[:120] if img.find_parent("figure") else ""
            images.append({"url": src, "alt": (img.get("alt") or caption or "")[:120]})
        return re.sub(r"\s+", " ", text)[:7000], image, links[:25], images[:15]
    except Exception:
        return "", None, [], []


def extract(cfg, article):
    body, image, links, images = fetch_article(article["link"])
    system = EXTRACT_SYSTEM.format(interests=cfg["interests"], today=date.today().isoformat())
    user = (
        f"Source: {article['source']}\nTitle: {article['title']}\n"
        f"Published: {article['published']}\nSummary: {article['summary']}\n\n"
        f"Article text:\n{body or '(not available, use title and summary)'}\n\n"
        f"ARTICLE LINKS (outbound):\n" + ("\n".join(f"- {l['text']}: {l['url']}" for l in links) or "(none)")
    )
    reply = ask_claude(cfg, system, user, max_tokens=2500)
    releases = []
    for rel in reply.get("releases", [])[:5]:
        if not rel.get("brand") or not rel.get("model"):
            continue
        rel["image"] = image
        rel["article_images"] = images
        allowed = {l["url"] for l in links}
        for key in ("buy_url", "book_url"):
            if rel.get(key) not in allowed:  # only keep links that really appear in the article
                rel[key] = None
        releases.append(rel)
    return releases


# ---------------------------------------------------------- release store

def same_release(a, b):
    """True when two records describe the same release."""
    if norm(a["brand"]) != norm(b["brand"]):
        return False
    ref_a, ref_b = norm(a.get("reference")), norm(b.get("reference"))
    if ref_a and ref_a == ref_b:
        return True
    model_a, model_b = norm(a["model"]), norm(b["model"])
    if difflib.SequenceMatcher(None, model_a, model_b).ratio() >= 0.85:
        return True
    # One name contains the other (e.g. "Type 9 Jules Wittock" vs "Type 9 Jules Wittock Emerald Green")
    # and the price matches: treat as the same release announced at collection level.
    ta, tb = set(model_a.split()), set(model_b.split())
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    pa, pb = a.get("price_aed"), b.get("price_aed")
    same_price = pa and pb and abs(pa - pb) <= 0.03 * max(pa, pb)
    return len(small) >= 3 and small <= big and bool(same_price)


def find_match(rel, releases):
    probe = dict(rel, price_aed=rel.get("price_aed") or to_aed(rel.get("price"), rel.get("currency")))
    return next((r for r in releases if same_release(probe, r)), None)


def consolidate(releases):
    """Merge duplicate records that slipped through (keeps the earliest record)."""
    releases.sort(key=lambda r: r["first_seen"])
    kept = []
    for r in releases:
        twin = next((k for k in kept if same_release(r, k)), None)
        if not twin:
            kept.append(r)
            continue
        for s in r["sources"]:
            if all(s["url"] != x["url"] for x in twin["sources"]):
                twin["sources"].append(s)
        for field, value in r.items():
            if twin.get(field) in (None, "", []) and value not in (None, "", []):
                twin[field] = value
        if STATUS_RANK.get(r.get("status"), -1) > STATUS_RANK.get(twin.get("status"), -1):
            twin["status"], twin["status_changed"] = r["status"], r.get("status_changed")
        twin["score"] = max(twin.get("score", 0), r.get("score", 0))
        if len(r["model"]) < len(twin["model"]):
            twin["model"] = r["model"]  # prefer the collection-level name
    return kept


TZ_ABBR = {
    "JST": "Asia/Tokyo", "KST": "Asia/Seoul", "HKT": "Asia/Hong_Kong", "SGT": "Asia/Singapore",
    "CST": "America/Chicago", "CDT": "America/Chicago", "IST": "Asia/Kolkata", "GST": "Asia/Dubai",
    "CET": "Europe/Zurich", "CEST": "Europe/Zurich", "BST": "Europe/London", "GMT": "Europe/London",
    "UTC": "UTC", "EST": "America/New_York", "EDT": "America/New_York", "ET": "America/New_York",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles", "PT": "America/Los_Angeles",
    "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
}


CITY_TZ = {
    "geneva": "Europe/Zurich", "swiss": "Europe/Zurich", "zurich": "Europe/Zurich", "paris": "Europe/Paris",
    "london": "Europe/London", "uk": "Europe/London", "new york": "America/New_York", "eastern": "America/New_York",
    "pacific": "America/Los_Angeles", "los angeles": "America/Los_Angeles", "tokyo": "Asia/Tokyo",
    "japan": "Asia/Tokyo", "hong kong": "Asia/Hong_Kong", "singapore": "Asia/Singapore",
    "dubai": "Asia/Dubai", "uae": "Asia/Dubai", "gulf": "Asia/Dubai", "glashütte": "Europe/Berlin",
}


def normalise_time(cfg, rel):
    """Work out the drop time in UAE time when the local time and zone are known."""
    rel.pop("launch_uae", None)
    day = parse_day(rel.get("launch_date"))
    if not day:
        return
    hhmm, zone = rel.get("launch_time"), rel.get("launch_timezone")
    text = rel.get("launch_date_text") or ""
    if not hhmm:
        pattern = r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?\s?m\b\.?|p\.?\s?m\b\.?)?"
        m = next((x for x in re.finditer(pattern, text, re.I) if x.group(2) or x.group(3)), None)
        if m:
            h, mins = int(m.group(1)), int(m.group(2) or 0)
            ampm = (m.group(3) or "").lower().replace(".", "").replace(" ", "")
            if re.search(r"\bnoon\b", text, re.I) and h == 12:
                ampm = ""
            if ampm == "pm" and h < 12:
                h += 12
            elif ampm == "am" and h == 12:
                h = 0
            if h < 24 and mins < 60:
                hhmm = f"{h:02d}:{mins:02d}"
    if not zone:
        m = re.search(r"\b(" + "|".join(TZ_ABBR) + r")\b", text)
        zone = TZ_ABBR.get(m.group(1)) if m else None
    if not zone:
        m = re.search(r"\b(" + "|".join(CITY_TZ) + r")\b", text, re.I)
        zone = CITY_TZ.get(m.group(1).lower()) if m else None
    if not hhmm or not zone:
        return
    try:
        h, mins = (int(x) for x in hhmm.split(":")[:2])
        local = datetime(day.year, day.month, day.day, h, mins, tzinfo=ZoneInfo(zone))
    except Exception:
        return
    uae = local.astimezone(ZoneInfo(cfg["timezone"]))
    abbr = local.tzname() or zone
    rel["launch_uae"] = {"date": uae.date().isoformat(), "time": uae.strftime("%H:%M"),
                         "utc": local.astimezone(timezone.utc).isoformat(),
                         "local": f"{local.strftime('%H:%M')} {abbr}"}


def maps_link(*parts):
    q = " ".join(p for p in parts if p)
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(q) if q else None


def build_where_to_buy(rel):
    """Merge article links and web research into one where-to-buy block."""
    data = (rel.get("research") or {}).get("data") or {}
    online = data.get("online") or {}
    stores = []
    for s in (data.get("uae_stores") or data.get("uae_buy") or [])[:8]:
        if not s.get("name") or s.get("type") == "online":
            continue
        mall, city = s.get("mall") or s.get("where"), s.get("city")
        stores.append({"name": s["name"], "type": s.get("type") or "boutique", "mall": mall, "city": city,
                       "phone": s.get("phone"), "url": s.get("url"),
                       "maps": maps_link(s["name"], mall, city or "UAE")})
    rel["where_to_buy"] = {
        "buy_url": rel.get("buy_url") or online.get("buy_url"),
        "book_url": rel.get("book_url") or online.get("book_url"),
        "ships_to_uae": online.get("ships_to_uae", data.get("online_ships_to_uae")),
        "online_note": online.get("note"),
        "stores": stores,
    }


def build_variants(rel):
    data = (rel.get("research") or {}).get("data") or {}
    variants = data.get("variants") or []
    rel["variants"] = variants if len(variants) > 1 else []
    rel["collection_url"] = data.get("collection_url")
    top = variants[0] if variants else None
    this = next((v for v in variants if v.get("is_this_entry")), None)
    # The photo that best shows THIS entry: its own variant photo, else the top pick, else the article photo.
    rel["photo"] = (this or {}).get("image_url") or (top or {}).get("image_url") or rel.get("image")
    rel["photo_caption"] = ((this or top or {}).get("name") if rel["photo"] != rel.get("image") else None)


def launch_day(rel):
    """The launch date as seen in the UAE (falls back to the announced date)."""
    return parse_day((rel.get("launch_uae") or {}).get("date")) or parse_day(rel.get("launch_date"))


def fmt_launch(rel, today):
    day = launch_day(rel)
    if not day:
        return rel.get("launch_date_text")
    text = fmt_day(day, today)
    uae = rel.get("launch_uae")
    if uae:
        text += f", {uae['time']} UAE time"
        if not uae["local"].startswith(uae["time"]):
            text += f" ({uae['local']})"
    return text


def upsert(cfg, releases, rel, article):
    stamp = now_utc().isoformat()
    source = {"name": article["source"], "url": article["link"]}
    price_aed = to_aed(rel.get("price"), rel.get("currency"))
    existing = find_match(rel, releases)

    if existing:
        if all(s["url"] != source["url"] for s in existing["sources"]):
            existing["sources"].append(source)
        known = {i["url"] for i in existing.get("article_images", [])}
        existing["article_images"] = (existing.get("article_images", []) +
                                      [i for i in rel.get("article_images", []) if i["url"] not in known])[:30]
        for field in ("reference", "pieces", "price", "currency", "launch_date",
                      "launch_date_text", "launch_time", "launch_timezone", "how_to_buy", "availability_notes",
                      "buy_url", "book_url", "dial", "variant_count",
                      "specs", "image", "reservation_possible"):
            if rel.get(field) not in (None, "", "null"):
                if field in ("launch_date", "launch_date_text", "launch_time", "launch_timezone", "how_to_buy") \
                        or not existing.get(field):
                    existing[field] = rel[field]
        if price_aed:
            existing["price_aed"] = price_aed
        new_status = rel.get("status")
        if STATUS_RANK.get(new_status, -1) > STATUS_RANK.get(existing.get("status"), -1):
            existing["status"] = new_status
            existing["status_changed"] = stamp
        existing["score"] = max(existing.get("score", 0), int(rel.get("score") or 0))
        existing["updated"] = stamp
        target = existing
    else:
        target = {
            "id": hashlib.sha1(f"{norm(rel['brand'])}|{norm(rel['model'])}".encode()).hexdigest()[:12],
            "brand": rel["brand"].strip(),
            "model": rel["model"].strip(),
            "reference": rel.get("reference"),
            "is_limited": bool(rel.get("is_limited")),
            "pieces": rel.get("pieces"),
            "limited_type": rel.get("limited_type") or "unknown",
            "price": rel.get("price"),
            "currency": rel.get("currency"),
            "price_aed": price_aed,
            "status": rel.get("status") or "announced",
            "launch_date": rel.get("launch_date"),
            "launch_date_text": rel.get("launch_date_text"),
            "launch_time": rel.get("launch_time"),
            "launch_timezone": rel.get("launch_timezone"),
            "buy_url": rel.get("buy_url"),
            "book_url": rel.get("book_url"),
            "dial": rel.get("dial"),
            "variant_count": rel.get("variant_count"),
            "article_images": rel.get("article_images") or [],
            "sale_method": rel.get("sale_method") or "unknown",
            "how_to_buy": rel.get("how_to_buy"),
            "reservation_possible": rel.get("reservation_possible"),
            "availability_notes": rel.get("availability_notes"),
            "specs": rel.get("specs"),
            "score": int(rel.get("score") or 0),
            "score_reason": rel.get("score_reason"),
            "allocation_likely": bool(rel.get("allocation_likely")),
            "image": rel.get("image"),
            "sources": [source],
            "first_seen": stamp,
            "updated": stamp,
            "status_changed": None,
        }
        releases.append(target)

    aed = target.get("price_aed")
    target["over_budget"] = bool(aed and aed > cfg["max_price_aed"])



# ---------------------------------------------------------------- collect

def collect(cfg, state):
    """Check sources and update the release list. Returns (releases, new_or_changed_ids)."""
    seen = load_json(SEEN_PATH, {})
    store = load_secure(RELEASES_PATH, RELEASES_ENC_PATH, {"releases": []})
    releases = store.get("releases", [])
    before = {r["id"]: (r.get("status"), r.get("launch_date")) for r in releases}

    articles, feed_errors = collect_articles(cfg, seen)
    articles = articles[: cfg.get("max_articles_per_run", 150)]
    print(f"New articles: {len(articles)}  |  feed problems: {len(feed_errors)}")

    ai_errors, candidates = 0, []
    if articles:
        try:
            candidates = triage(cfg, articles)
            keep = {a["link"] for a in candidates}
            for a in articles:
                if a["link"] not in keep:
                    seen[a["link"]] = now_utc().isoformat()
        except Exception as exc:
            print(f"Triage failed: {exc}")
            ai_errors += 1
    print(f"Articles about new releases: {len(candidates)}")

    for article in candidates:
        try:
            for rel in extract(cfg, article):
                upsert(cfg, releases, rel, article)
            seen[article["link"]] = now_utc().isoformat()
        except Exception as exc:
            print(f"Extraction failed for {article['link']}: {exc}")
            ai_errors += 1

    seen = dict(sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:8000])
    keep_after = now_utc() - timedelta(days=550)
    releases = [r for r in releases if (parse_iso(r["first_seen"]) or now_utc()) > keep_after]
    releases = consolidate(releases)
    for r in releases:
        normalise_time(cfg, r)
        build_where_to_buy(r)
        build_variants(r)
    releases.sort(key=lambda r: r["first_seen"], reverse=True)

    changed = {r["id"] for r in releases
               if before.get(r["id"]) != (r.get("status"), r.get("launch_date"))}
    state["last_collect"] = now_utc().isoformat()
    state["feed_errors"] = feed_errors
    state["ai_errors"] = state.get("ai_errors", 0) + ai_errors
    save_json(SEEN_PATH, seen)
    return releases, changed


def save_releases(cfg, releases):
    save_secure(RELEASES_PATH, RELEASES_ENC_PATH, {
        "generated": now_utc().isoformat(),
        "max_price_aed": cfg["max_price_aed"],
        "min_score": cfg["min_score_for_digest"],
        "breakeven_pct": round(((1 + cfg.get("purchase_tax_pct", 5) / 100)
                                / (1 - cfg.get("selling_cost_pct", 10) / 100) - 1) * 100, 1),
        "selling_cost_pct": cfg.get("selling_cost_pct", 10),
        "purchase_tax_pct": cfg.get("purchase_tax_pct", 5),
        "releases": releases,
    }, stub={"encrypted": True, "note": "Release data is encrypted. Open the dashboard and unlock it with your passphrase."})


# --------------------------------------------------------------- messages

def esc(value):
    return html.escape(str(value or ""), quote=False)


def fmt_price(rel):
    if rel.get("price_aed"):
        text = f"AED {rel['price_aed']:,}"
        if rel.get("currency") and rel["currency"].upper() != "AED" and rel.get("price"):
            text += f" (≈ {rel['currency'].upper()} {float(rel['price']):,.0f})"
        return text
    return "Price TBA"


def fmt_day(day, today):
    if day == today:
        return "TODAY"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    return day.strftime("%a %d %b")


def hype_line(rel):
    h = rel.get("hype")
    if not h:
        return None
    bits = [f"{h['media_outlets']} outlet" + ("" if h["media_outlets"] == 1 else "s")]
    if "youtube_views" in h:
        bits.append(f"{h['youtube_videos']} YouTube videos, {h['youtube_views']:,} views")
    if "search_vs_brand_pct" in h:
        bits.append(f"search {h['search_vs_brand_pct']}% of brand ({h['search_momentum']})")
    if "reddit_engagement" in h:
        bits.append(f"Reddit {h['reddit_engagement']:,} engagement")
    trend = "" if h.get("trend") in (None, "new") else f", {h['trend']}"
    return f"🔥 Hype {h['index']}/100{trend}: " + " | ".join(bits)


def resale_line(rel):
    r = rel.get("resale")
    if not r:
        return None
    text = (f"📈 Resale {r['low_pct']:+d}% to {r['high_pct']:+d}% vs retail ({r['confidence']} confidence). "
            f"{r['verdict']}; break-even {r['breakeven_pct']:+.0f}%")
    if r.get("est_profit_aed") is not None:
        text += f", est. net AED {r['est_profit_aed']:+,}"
    if r.get("roi_pct") is not None:
        text += f" (ROI {r['roi_pct']:+.0f}% after costs; range AED {r['worst_net_aed']:+,} to {r['best_net_aed']:+,})"
    text += f". Based on {r['basis']}"
    if r.get("adjustments"):
        text += "; " + ", ".join(r["adjustments"])
    return text + "."


def link(url, label):
    return f'<a href="{html.escape(url)}">{esc(label)}</a>'


def buy_row(rel):
    """One compact line with direct online links, e.g. 🛒 Buy online | 📝 Reserve."""
    w = rel.get("where_to_buy") or {}
    bits = []
    if w.get("buy_url"):
        bits.append("🛒 " + link(w["buy_url"], "Buy online" if rel.get("status") != "preorder_open" else "Pre-order online"))
    if w.get("book_url") and w.get("book_url") != w.get("buy_url"):
        bits.append("📝 " + link(w["book_url"], "Reserve / book"))
    if not bits and rel.get("how_to_buy"):
        return "🛒 " + esc(rel["how_to_buy"])
    if w.get("ships_to_uae") is True:
        bits.append("ships to UAE")
    elif w.get("ships_to_uae") is False and w.get("buy_url"):
        bits.append("⚠️ may not ship to UAE")
    return " | ".join(bits)


def stores_quote(rel, store):
    """Collapsed list of UAE stores plus your own contacts (expands on tap in Telegram)."""
    w = rel.get("where_to_buy") or {}
    contacts = private.contacts_for(store, rel["brand"])
    stores = w.get("stores") or []
    if not stores and not contacts:
        return None
    lines = [f"🇦🇪 <b>Where to buy in the UAE ({len(stores)})</b>"]
    for c in contacts:
        lines.append("👤 Your contact: " + esc(", ".join(x for x in (c.get("name"), c.get("store"), c.get("phone")) if x)))
    for st in stores:
        where = ", ".join(x for x in (st.get("mall"), st.get("city")) if x)
        kind = "boutique" if st.get("type") == "boutique" else "dealer"
        extra = [link(st["maps"], "Map")] if st.get("maps") else []
        if st.get("url"):
            extra.append(link(st["url"], "Store page"))
        line = f"• <b>{esc(st['name'])}</b> ({kind})" + (f", {esc(where)}" if where else "")
        if st.get("phone"):
            line += f"\n   ☎️ {esc(st['phone'])}"
        if extra:
            line += "\n   " + " | ".join(extra)
        lines.append(line)
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def variant_line(rel):
    vs = rel.get("variants") or []
    if not vs:
        return None
    top = vs[0]
    bits = [f"💎 Most desirable: <b>{esc(top['name'])}</b>"]
    if top.get("pieces"):
        bits.append(f"{top['pieces']:,} pcs" if isinstance(top["pieces"], int) else f"{esc(top['pieces'])} pcs")
    bits.append("this one ✅" if top.get("is_this_entry") else f"of {len(vs)} versions")
    if rel.get("collection_url"):
        bits.append(link(rel["collection_url"], f"See all {len(vs)}"))
    return " | ".join(bits)


def variants_quote(rel):
    vs = rel.get("variants") or []
    if len(vs) < 2:
        return None
    lines = [f"🎨 <b>All {len(vs)} versions, ranked</b>"]
    for v in vs:
        name = link(v["page_url"], v["name"]) if v.get("page_url") else esc(v["name"])
        tags = []
        if v.get("is_this_entry"):
            tags.append("this entry")
        if v.get("limited") and v.get("pieces"):
            tags.append(f"limited {v['pieces']}")
        elif v.get("limited"):
            tags.append("limited")
        if v.get("price_aed"):
            tags.append(f"AED {v['price_aed']:,}")
        line = f"{v.get('rank', '•')}. {name}" + (f" ({esc(', '.join(tags))})" if tags else "")
        if v.get("why"):
            line += f"\n   {esc(v['why'])}"
        lines.append(line)
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def analysis_quote(rel):
    lines = ["📊 <b>Analysis</b>"]
    for extra in (hype_line(rel), resale_line(rel)):
        if extra:
            lines.append(esc(extra))
    if rel.get("score_reason"):
        lines.append(f"💡 {esc(rel['score_reason'])}")
    lines.append("🔗 " + " / ".join(link(s["url"], s["name"]) for s in rel["sources"][:3]))
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def release_block(rel, today, store, index=None):
    head = f"{index}. " if index else ""
    lines = [f"<b>{head}{esc(rel['brand'])} {esc(rel['model'])}</b>   ⭐ {rel.get('score', '?')}/10"]
    facts = [fmt_price(rel)]
    if rel.get("pieces"):
        facts.append(f"{rel['pieces']:,} pcs" if isinstance(rel["pieces"], int) else f"{rel['pieces']} pcs")
    elif rel.get("limited_type") not in (None, "unknown", "not_limited"):
        facts.append(rel["limited_type"].replace("_", " "))
    when = fmt_launch(rel, today)
    if when:
        if launch_day(rel) and launch_day(rel) < today and rel.get("status") in ("available_now", "preorder_open"):
            when = "since " + when
        facts.append(f"📅 {when}")
    lines.append("💰 " + esc(" | ".join(facts)))
    x = rel.get("resale")
    quick = [STATUS_LABEL.get(rel.get("status"), "")]
    if x and x.get("roi_pct") is not None:
        quick.append(f"ROI {x['roi_pct']:+.0f}% (AED {x['est_profit_aed']:+,}), {x['verdict'].lower()}")
    if rel.get("hype"):
        quick.append(f"🔥 {rel['hype']['index']}")
    lines.append("📈 " + esc(" | ".join(q for q in quick if q)))
    if rel.get("dial") and norm(rel["dial"]) not in norm(rel["model"]):
        lines.append(f"🎨 Dial: {esc(rel['dial'])}")
    vline = variant_line(rel)
    if vline:
        lines.append(vline)
    row = buy_row(rel)
    if row:
        lines.append(row)
    if rel.get("allocation_likely"):
        lines.append("🤝 Likely allocated: contact your boutique early")
    for quote in (variants_quote(rel), stores_quote(rel, store), analysis_quote(rel)):
        if quote:
            lines.append(quote)
    return "\n".join(lines)


def release_buttons(rel):
    """Tap buttons for single-release messages (urgent alerts)."""
    w = rel.get("where_to_buy") or {}
    row = []
    if w.get("buy_url"):
        row.append({"text": "🛒 Buy online", "url": w["buy_url"]})
    if w.get("book_url") and w.get("book_url") != w.get("buy_url"):
        row.append({"text": "📝 Reserve", "url": w["book_url"]})
    stores = w.get("stores") or []
    if stores and stores[0].get("maps"):
        row.append({"text": "📍 Nearest store", "url": stores[0]["maps"]})
    return [row] if row else None


def dashboard_url(cfg):
    repo = os.environ.get("GITHUB_REPOSITORY")
    return cfg.get("dashboard_url") or (
        f"https://{repo.split('/')[0].lower()}.github.io/{repo.split('/')[1]}/" if repo else None)


def eligible(cfg, r):
    if r.get("over_budget"):
        return False
    if not r.get("price_aed") and not cfg.get("include_price_unknown", True):
        return False
    return r.get("score", 0) >= cfg["min_score_for_digest"]


# ----------------------------------------------------------- urgent alerts

def urgent_alerts(cfg, releases, changed_ids, state, store, wc):
    tz = ZoneInfo(cfg["timezone"])
    local = datetime.now(tz)
    today = local.date()
    last_digest = parse_iso(state.get("last_digest")) or (now_utc() - timedelta(days=1))
    sent = set(state.get("urgent_sent", []))
    u = cfg.get("urgent", {})
    if not u.get("enabled", True):
        return

    def reason(r):
        if not eligible(cfg, r) or r.get("status") == "sold_out":
            return None
        d = launch_day(r)
        soon = d and 0 <= (d - today).days <= u.get("within_hours", 48) // 24
        stale = d and (today - d).days > 2  # launched days ago: leave it to the digest
        is_new = (parse_iso(r["first_seen"]) or last_digest) > last_digest
        opened = (r["id"] in changed_ids and r.get("status") in ("preorder_open", "available_now"))
        if opened and not stale:
            return STATUS_LABEL[r["status"]]
        if is_new and soon:
            return "Launching " + fmt_day(d, today).lower()
        return None

    candidates = [(r, why) for r in releases if (why := reason(r))
                  and f"{r['id']}:{r.get('status')}:{r.get('launch_date')}" not in sent]
    if not candidates:
        return

    signals.enrich(cfg, releases, only_ids={r["id"] for r, _ in candidates}, wc=wc)
    for r, _ in candidates:
        build_where_to_buy(r)
        build_variants(r)

    quiet_start, quiet_end = u.get("quiet_start_hour", 23), u.get("quiet_end_hour", 7)
    quiet = local.hour >= quiet_start or local.hour < quiet_end
    for r, why in candidates:
        hype = (r.get("hype") or {}).get("index", 0)
        if r.get("score", 0) < u.get("min_score", 7) and hype < u.get("min_hype", 70):
            continue
        if quiet and launch_day(r) != today:
            continue  # hold until morning unless the drop is today
        photo = r.get("photo")
        if photo and signals.image_ok(photo):
            cap = f"🚨 <b>{esc(r['brand'])} {esc(r['model'])}</b>" + (f"\nPictured: {esc(r['photo_caption'])}" if r.get("photo_caption") else "")
            telegram_photos([(photo, cap)])
        telegram_send([f"🚨 <b>Urgent: {esc(why)}</b>", release_block(r, today, store)], buttons=release_buttons(r))
        sent.add(f"{r['id']}:{r.get('status')}:{r.get('launch_date')}")
        print(f"Urgent alert sent: {r['brand']} {r['model']}")
    state["urgent_sent"] = sorted(sent)[-500:]


# ------------------------------------------------------------------ digest

def send_digest_photos(cfg, releases, state):
    """A photo album of today's new releases (top variant where known), shown above the digest."""
    since = parse_iso(state.get("last_digest")) or (now_utc() - timedelta(days=1))
    new = [r for r in releases if eligible(cfg, r) and (parse_iso(r["first_seen"]) or since) > since and r.get("photo")]
    new.sort(key=lambda r: r.get("score", 0), reverse=True)
    items = []
    for i, r in enumerate(new[:10], 1):
        if not signals.image_ok(r["photo"]):
            continue
        cap = f"{i}. <b>{esc(r['brand'])} {esc(r['model'])}</b>"
        if r.get("photo_caption"):
            top = (r.get("variants") or [{}])[0]
            cap += f"\nPictured: {esc(r['photo_caption'])}" + (" (most desirable)" if top.get("name") == r["photo_caption"] else "")
        items.append((r["photo"], cap))
    if items:
        telegram_photos(items)


def build_digest(cfg, releases, state, store):
    tz = ZoneInfo(cfg["timezone"])
    today = datetime.now(tz).date()
    since = parse_iso(state.get("last_digest")) or (now_utc() - timedelta(days=1))

    new = [r for r in releases if eligible(cfg, r) and (parse_iso(r["first_seen"]) or since) > since]
    new.sort(key=lambda r: r.get("score", 0), reverse=True)
    new_ids = {r["id"] for r in new}
    horizon = today + timedelta(days=cfg.get("digest_upcoming_days", 7))
    upcoming = sorted([r for r in releases if eligible(cfg, r) and r.get("status") != "sold_out"
                       and (d := launch_day(r)) and today <= d <= horizon],
                      key=lambda r: (launch_day(r), (r.get("launch_uae") or {}).get("time", "")))
    changed = [r for r in releases if eligible(cfg, r) and r["id"] not in new_ids
               and (parse_iso(r.get("status_changed")) or since) > since]
    below = sum(1 for r in releases if (parse_iso(r["first_seen"]) or since) > since and r["id"] not in new_ids)

    parts = [f"⌚ <b>Watch launch digest</b>\n{today.strftime('%A %d %B %Y')}"]
    if new:
        parts.append(f"🆕 <b>New releases ({len(new)})</b>")
        parts.extend(release_block(r, today, store, i) for i, r in enumerate(new, 1))
    else:
        parts.append("🆕 No new releases matched your criteria in the last day.")
    if changed:
        parts.append("🔔 <b>Status updates</b>\n" + "\n".join(
            f"• {esc(r['brand'])} {esc(r['model'])}: now <b>{esc(STATUS_LABEL.get(r['status'], r['status']))}</b>"
            f" (<a href=\"{html.escape(r['sources'][-1]['url'])}\">details</a>)" for r in changed))
    if upcoming:
        lines = [f"📅 <b>Coming up in the next {cfg.get('digest_upcoming_days', 7)} days</b>"]
        for r in upcoming:
            hype = f", hype {r['hype']['index']}" if r.get("hype") else ""
            resale = f", resale {r['resale']['mid_pct']:+d}%" if r.get("resale") else ""
            lines.append(f"• <b>{esc(fmt_launch(r, today))}</b>: {esc(r['brand'])} {esc(r['model'])}, "
                         f"{esc(fmt_price(r))}{hype}{resale}")
        parts.append("\n".join(lines))

    if store is not None and today.weekday() == cfg.get("portfolio_report_weekday", 0) and store.get("holdings"):
        parts.append(private.portfolio_summary(cfg, store))

    footer = []
    if below:
        footer.append(f"{below} other release(s) below your score or budget are on the dashboard.")
    if (url := dashboard_url(cfg)):
        footer.append(f'📊 <a href="{html.escape(url)}">Open dashboard</a>')
    if state.get("feed_errors"):
        footer.append("⚠️ Sources not reachable: " + esc(", ".join(e.split(" (")[0] for e in state["feed_errors"])))
    if state.get("ai_errors"):
        footer.append(f"⚠️ {state['ai_errors']} AI processing error(s) since the last digest; check the Actions log.")
    if footer:
        parts.append("\n".join(footer))
    return parts


# ------------------------------------------------------------------- modes

def run_collect(cfg, digest=False):
    state = load_json(STATE_PATH, {})
    first_run = not state.get("last_collect")
    wc = signals.WatchCharts()
    try:
        private.process_commands(cfg, state, wc)
    except Exception as exc:
        print(f"Telegram command processing failed: {exc}")
    releases, changed_ids = collect(cfg, state)
    store = None
    try:
        store = private.load_private()
    except Exception as exc:
        print(f"Could not open private data (check PORTFOLIO_PASSPHRASE): {exc}")

    if digest:
        signals.enrich(cfg, releases, wc=wc)
        for r in releases:
            build_where_to_buy(r)
            build_variants(r)
        if store is not None and store.get("holdings") and \
                datetime.now(ZoneInfo(cfg["timezone"])).weekday() == cfg.get("portfolio_report_weekday", 0):
            private.value_holdings(cfg, store, wc)
            private.save_private(store)
        save_releases(cfg, releases)
        send_digest_photos(cfg, releases, state)
        telegram_send(build_digest(cfg, releases, state, store))
        state["last_digest"] = now_utc().isoformat()
        state["ai_errors"] = 0
        print("Digest sent.")
    else:
        if first_run:
            print("First run: urgent alerts skipped (everything is new); the digest covers it.")
        else:
            urgent_alerts(cfg, releases, changed_ids, state, store, wc)
        save_releases(cfg, releases)
    wc.save()
    save_json(STATE_PATH, state)
    print(f"Releases stored: {len(releases)}")


def run_test(cfg):
    report = []
    ask_claude(cfg, 'Reply with ONLY JSON: {"ok": true}', "Test", max_tokens=20)
    report.append("✅ Claude API")
    checks = {
        "WATCHCHARTS_API_KEY": "WatchCharts (resale and portfolio values)",
        "SERPAPI_KEY": "SerpApi (Google search interest)",
        "YOUTUBE_API_KEY": "YouTube (video views)",
        "REDDIT_CLIENT_ID": "Reddit (optional)",
        "PORTFOLIO_PASSPHRASE": "Private collection and contacts",
    }
    for key, label in checks.items():
        report.append(("✅ " if os.environ.get(key) else "➖ not set: ") + label)
    wc = signals.WatchCharts()
    if wc.enabled:
        probe = wc.lookup("Rolex", "126610LN")
        report.append("✅ WatchCharts responded" if probe else "⚠️ WatchCharts key set but lookup returned nothing; run mode 'probe'")
    telegram_send("⌚ <b>Watch tracker check</b>\n" + "\n".join(report) +
                  "\n\nSend /help to see collection commands.")
    print("\n".join(report))


def run_probe(cfg):
    key = os.environ.get("WATCHCHARTS_API_KEY")
    if not key:
        sys.exit("WATCHCHARTS_API_KEY is not set")
    base = signals.WatchCharts.BASE
    res = HTTP.get(f"{base}/search/watch", params={"brand_name": "rolex", "reference": "126610LN"},
                   headers={"x-api-key": key}, timeout=30)
    print("search/watch", res.status_code, json.dumps(res.json(), indent=2)[:3000])
    uuid = signals.WatchCharts._find(res.json(), lambda k, v: k == "uuid" and isinstance(v, str))
    if uuid:
        info = HTTP.get(f"{base}/watch/info", params={"uuid": uuid}, headers={"x-api-key": key}, timeout=30)
        print("watch/info", info.status_code, json.dumps(info.json(), indent=2)[:4000])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["collect", "digest", "test", "probe"], default="collect")
    args = parser.parse_args()
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit("config.json is missing or not valid JSON")
    if args.mode == "collect":
        run_collect(cfg)
    elif args.mode == "digest":
        run_collect(cfg, digest=True)
    elif args.mode == "test":
        run_test(cfg)
    else:
        run_probe(cfg)


if __name__ == "__main__":
    main()

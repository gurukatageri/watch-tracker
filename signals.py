"""Market intelligence: Hype index, resale outlook and UAE buying research.

Data sources (each optional, used when its key is set):
  WATCHCHARTS_API_KEY  secondary-market and retail prices by reference
  SERPAPI_KEY          Google search interest (Google Trends via SerpApi)
  YOUTUBE_API_KEY      YouTube videos and views
  REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET   Reddit discussion (needs Reddit approval)
Claude with web search is always used for comparables and UAE buying options.
"""

import base64
import math
import os
import re
import statistics
from datetime import timedelta

from common import (
    CACHE_ENC_PATH, CACHE_PATH, HTTP, ask_claude, load_json, load_secure, norm, now_utc, parse_day,
    parse_iso, save_json, save_secure, to_aed,
)

STOPWORDS = {"the", "and", "edition", "limited", "watch", "new", "with", "for", "of",
             "ref", "reference", "automatic", "mm", "x", "in", "a"}


# ------------------------------------------------------------ WatchCharts

class WatchCharts:
    """Small client for the official WatchCharts API (v3), with a 7-day cache."""

    BASE = "https://api.watchcharts.com/v3"

    def __init__(self):
        self.key = os.environ.get("WATCHCHARTS_API_KEY")
        self.cache = load_secure(CACHE_PATH, CACHE_ENC_PATH, {})
        self.calls = 0

    @property
    def enabled(self):
        return bool(self.key)

    def _get(self, path, params):
        self.calls += 1
        resp = HTTP.get(f"{self.BASE}{path}", params=params,
                        headers={"x-api-key": self.key}, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"WatchCharts {path} HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    @staticmethod
    def _find(obj, predicate):
        """Depth-first search for the first value whose key matches predicate."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if predicate(k, v):
                    return v
            for v in obj.values():
                found = WatchCharts._find(v, predicate)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = WatchCharts._find(v, predicate)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def lookup(self, brand, reference):
        """Return {'market_aed', 'retail_aed', 'premium_pct', 'uuid'} or None."""
        if not self.enabled or not brand or not reference:
            return None
        ck = f"wc|{norm(brand)}|{norm(reference)}"
        hit = self.cache.get(ck)
        if hit and (parse_iso(hit.get("at")) or now_utc()) > now_utc() - timedelta(days=7):
            return hit.get("data")

        data = None
        try:
            uuid = None
            for brand_name in dict.fromkeys([brand.lower(), brand.lower().replace(" ", "-"),
                                             brand.lower().replace(" ", "")]):
                res = self._get("/search/watch", {"brand_name": brand_name, "reference": reference})
                uuid = self._find(res, lambda k, v: k == "uuid" and isinstance(v, str))
                if uuid:
                    break
            if uuid:
                info = self._get("/watch/info", {"uuid": uuid})
                market = self._num(self._find(info, lambda k, v: "market" in k.lower() and "price" in k.lower()
                                              and self._num(v) is not None))
                retail = self._num(self._find(info, lambda k, v: "retail" in k.lower() and "price" in k.lower()
                                              and self._num(v) is not None))
                currency = self._find(info, lambda k, v: k.lower() == "currency" and isinstance(v, str)) or "USD"
                if market:
                    data = {
                        "uuid": uuid,
                        "market_aed": to_aed(market, currency),
                        "retail_aed": to_aed(retail, currency) if retail else None,
                        "premium_pct": round((market / retail - 1) * 100, 1) if retail else None,
                    }
        except Exception as exc:
            print(f"WatchCharts lookup failed for {brand} {reference}: {exc}")
            return None
        self.cache[ck] = {"at": now_utc().isoformat(), "data": data}
        return data

    def save(self):
        save_secure(CACHE_PATH, CACHE_ENC_PATH, self.cache)


# ------------------------------------------------------------ hype signals

def _tokens(text):
    return [t for t in norm(text).split() if len(t) >= 3 and t not in STOPWORDS]


def _title_matches(title, brand, model):
    t = norm(title)
    brand_hit = any(tok in t for tok in _tokens(brand)) or norm(brand) in t
    model_hits = sum(1 for tok in _tokens(model) if tok in t)
    return brand_hit and model_hits >= 1


def youtube_signal(rel):
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        return None
    since = (parse_iso(rel["first_seen"]) or now_utc()) - timedelta(days=3)
    query = f"{rel['brand']} {rel['model']}"
    res = HTTP.get("https://www.googleapis.com/youtube/v3/search", params={
        "part": "snippet", "q": query, "type": "video", "maxResults": 25,
        "publishedAfter": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "key": key,
    }, timeout=30)
    res.raise_for_status()
    ids = [it["id"]["videoId"] for it in res.json().get("items", [])
           if it.get("id", {}).get("videoId")
           and _title_matches(it["snippet"].get("title", ""), rel["brand"], rel["model"])]
    if not ids:
        return {"videos": 0, "views": 0}
    stats = HTTP.get("https://www.googleapis.com/youtube/v3/videos", params={
        "part": "statistics", "id": ",".join(ids), "key": key}, timeout=30)
    stats.raise_for_status()
    views = sum(int(v.get("statistics", {}).get("viewCount", 0)) for v in stats.json().get("items", []))
    return {"videos": len(ids), "views": views}


def trends_signal(rel):
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        return None
    term = re.sub(r"[,\"]", " ", f"{rel['brand']} {rel['model']}")[:90].strip()
    brand = re.sub(r"[,\"]", " ", rel["brand"]).strip()
    res = HTTP.get("https://serpapi.com/search.json", params={
        "engine": "google_trends", "q": f"{term},{brand}", "data_type": "TIMESERIES",
        "date": "today 1-m", "api_key": key}, timeout=60)
    res.raise_for_status()
    timeline = res.json().get("interest_over_time", {}).get("timeline_data", [])
    model_vals, brand_vals = [], []
    for point in timeline:
        vals = point.get("values", [])
        if len(vals) >= 2:
            model_vals.append(float(vals[0].get("extracted_value") or 0))
            brand_vals.append(float(vals[1].get("extracted_value") or 0))
    if not model_vals:
        return {"vs_brand_pct": 0, "momentum": "too low to measure"}
    recent_m = statistics.mean(model_vals[-7:])
    recent_b = statistics.mean(brand_vals[-7:]) or 1
    earlier_m = statistics.mean(model_vals[:-7]) if len(model_vals) > 7 else recent_m
    if recent_m < 1:
        momentum = "too low to measure"
    elif recent_m > earlier_m * 1.3:
        momentum = "rising"
    elif recent_m < earlier_m * 0.7:
        momentum = "falling"
    else:
        momentum = "steady"
    return {"vs_brand_pct": round(recent_m / recent_b * 100), "momentum": momentum}


def reddit_signal(rel):
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        return None
    ua = "watch-launch-tracker/2.0 (personal use)"
    tok = HTTP.post("https://www.reddit.com/api/v1/access_token",
                    data={"grant_type": "client_credentials"},
                    headers={"User-Agent": ua,
                             "Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()},
                    timeout=30)
    tok.raise_for_status()
    res = HTTP.get("https://oauth.reddit.com/search", params={
        "q": f"{rel['brand']} {rel['model']}", "sort": "new", "t": "month", "limit": 50},
        headers={"User-Agent": ua, "Authorization": f"Bearer {tok.json()['access_token']}"}, timeout=30)
    res.raise_for_status()
    posts = [c["data"] for c in res.json().get("data", {}).get("children", [])
             if _title_matches(c["data"].get("title", ""), rel["brand"], rel["model"])]
    return {"posts": len(posts),
            "engagement": sum(p.get("score", 0) + p.get("num_comments", 0) for p in posts)}


def compute_hype(rel):
    """Combine all available signals into a 0-100 Hype index."""
    parts, weights = {}, {"media": 0.25, "youtube": 0.35, "search": 0.25, "reddit": 0.15}
    outlets = len({s["name"] for s in rel.get("sources", [])})
    parts["media"] = min(1.0, math.log1p(outlets) / math.log1p(15))
    info = {"media_outlets": outlets}

    for name, fn in (("youtube", youtube_signal), ("search", trends_signal), ("reddit", reddit_signal)):
        try:
            sig = fn(rel)
        except Exception as exc:
            print(f"{name} signal failed for {rel['brand']} {rel['model']}: {exc}")
            sig = None
        if sig is None:
            continue
        if name == "youtube":
            parts[name] = 0.8 * min(1.0, math.log10(1 + sig["views"]) / 6) + 0.2 * min(1.0, sig["videos"] / 20)
            info.update(youtube_videos=sig["videos"], youtube_views=sig["views"])
        elif name == "search":
            parts[name] = min(1.0, sig["vs_brand_pct"] / 50)
            info.update(search_vs_brand_pct=sig["vs_brand_pct"], search_momentum=sig["momentum"])
        else:
            parts[name] = min(1.0, math.log10(1 + sig["engagement"]) / 4)
            info.update(reddit_posts=sig["posts"], reddit_engagement=sig["engagement"])

    total_w = sum(weights[k] for k in parts)
    index = int(round(sum(parts[k] * weights[k] for k in parts) / total_w * 100))

    history = (rel.get("hype") or {}).get("history", [])
    today = now_utc().date().isoformat()
    history = [h for h in history if h["date"] != today] + [{"date": today, "index": index}]
    older = [h for h in history if h["date"] <= (now_utc().date() - timedelta(days=2)).isoformat()]
    trend = "new"
    if older:
        diff = index - older[-1]["index"]
        trend = "rising" if diff >= 8 else "falling" if diff <= -8 else "steady"

    info.update(index=index, trend=trend, history=history[-30:],
                signals_used=sorted(parts), updated=now_utc().isoformat())
    return info


# ------------------------------------------------ research (Claude + web)

RESEARCH_SYSTEM = """You are a watch-market researcher helping a UAE-based collector decide whether to buy a new release
to hold and possibly resell. Use web search. Be factual; use null when you cannot find something.

Find:
1. 3-5 COMPARABLES: earlier limited or special editions from the same brand and model family (or the closest
   regular-production reference if no limited ones exist). For each, give the reference number, original retail
   price and current secondary-market price (prefer WatchCharts, Chrono24 price data, reputable dealer listings).
2. How well this BRAND generally holds value on the secondary market.
3. How to BUY or RESERVE this release:
   a) ONLINE: the direct URL of the product or pre-order page for THIS watch on the brand's official site or an
      authorized e-shop, and a URL to reserve / register interest / join the waitlist or raffle / book a boutique
      appointment. Only give URLs that appeared in your search results; never guess a URL. Say whether the online
      store sells or ships to the UAE.
   b) IN STORE in the UAE: the brand's own boutiques and authorized retailers that carry the brand (e.g. Ahmed
      Seddiqi & Sons, Rivoli, Tanagra, Al Manara, depending on the brand), with mall, city and phone number. Use the
      brand's store locator where possible. Only list stores you found evidence for.
4. Early market signals for THIS release: waitlists, sold out quickly, pre-delivery listings above retail, etc.
5. VARIANTS: if the launch comes in several dials, colours, materials, sizes or models, list every variant and rank
   them by collector desirability and resale potential (1 = most desirable). Consider: limited vs permanent, piece
   count, unusual materials, media and collector reaction. Mark which variant THIS entry refers to. For each variant
   give its official product page URL if it appeared in your search results, and pick its photo ONLY from the
   CANDIDATE IMAGES list (match by caption/alt text); use null if none clearly matches. Also give the URL of a page
   showing the whole collection (brand collection page or the best hands-on article).

Reply with ONLY this JSON after researching:
{"comparables": [{"name": str, "reference": str|null, "year": int|null, "pieces": int|null,
                  "retail_price": number|null, "market_price": number|null, "currency": "USD"|"CHF"|"EUR"|"GBP"|"AED"|...,
                  "source": "site name"}],
 "brand_retention": "strong"|"average"|"weak",
 "brand_retention_note": "one sentence",
 "online": {"buy_url": str|null, "book_url": str|null, "ships_to_uae": true|false|null, "note": "one short sentence, e.g. 'online drop, one per customer' or null"},
 "uae_stores": [{"name": str, "type": "boutique"|"authorized_dealer", "mall": str|null, "city": "Dubai"|"Abu Dhabi"|...,
                 "phone": str|null, "url": "store page URL or null"}],
 "variants": [{"name": "e.g. Ice Agate", "dial": "short description", "reference": str|null, "pieces": int|null,
               "limited": true|false, "price": number|null, "currency": str|null, "rank": int, "why": "one sentence",
               "is_this_entry": true|false, "page_url": str|null, "image_url": "from CANDIDATE IMAGES or null"}],
 "collection_url": str|null,
 "early_signal": "positive"|"neutral"|"negative"|"unknown",
 "early_signal_note": "one sentence or null"}"""


RESEARCH_VERSION = 3


def og_image(page_url):
    """The main (og:image) photo of a product page."""
    try:
        resp = HTTP.get(page_url, timeout=15)
        if resp.status_code >= 400:
            return None
        from bs4 import BeautifulSoup
        meta = BeautifulSoup(resp.text, "html.parser").find("meta", property="og:image")
        url = meta.get("content") if meta else None
        return ("https:" + url) if url and url.startswith("//") else url
    except Exception:
        return None


def image_ok(url):
    if not url or not str(url).startswith("http"):
        return False
    try:
        resp = HTTP.get(url, timeout=12, stream=True)
    except Exception:
        return False
    ctype = resp.headers.get("content-type", "") if hasattr(resp, "headers") else "image/"
    try:
        resp.close()
    except Exception:
        pass
    return resp.status_code < 400 and ctype.startswith("image/")


def link_alive(url):
    """Drop links that clearly don't exist; keep ones that merely block robots."""
    if not url or not str(url).startswith("http"):
        return False
    try:
        resp = HTTP.get(url, timeout=12, allow_redirects=True, stream=True)
    except Exception:
        return False
    try:
        resp.close()
    except Exception:
        pass
    return resp.status_code not in (404, 410)


def research_release(cfg, rel):
    user = (f"Release: {rel['brand']} {rel['model']}\nReference: {rel.get('reference')}\n"
            f"Pieces: {rel.get('pieces')}\nRetail: {rel.get('price')} {rel.get('currency')}\n"
            f"Status: {rel.get('status')}, launch: {rel.get('launch_date') or rel.get('launch_date_text')}\n"
            f"Sources: {', '.join(s['url'] for s in rel['sources'][:3])}\n\n"
            "CANDIDATE IMAGES (caption: url):\n" +
            ("\n".join(f"- {i.get('alt') or '(no caption)'}: {i['url']}" for i in rel.get("article_images", [])[:20]) or "(none)"))
    data = ask_claude(cfg, RESEARCH_SYSTEM, user, max_tokens=3500,
                      model=cfg.get("research_model"), web_searches=cfg.get("research_web_searches", 6))
    online = data.get("online") or {}
    for key in ("buy_url", "book_url"):
        if online.get(key) and not link_alive(online[key]):
            print(f"Dropped dead link: {online[key]}")
            online[key] = None
    data["online"] = online
    for store in data.get("uae_stores") or []:
        if store.get("url") and not link_alive(store["url"]):
            store["url"] = None
    data["variants"] = clean_variants(rel, data.get("variants") or [])
    if data.get("collection_url") and not link_alive(data["collection_url"]):
        data["collection_url"] = None
    return data


def clean_variants(rel, variants):
    """Verify links and attach a real photo to each variant."""
    candidates = {i["url"] for i in rel.get("article_images", [])}
    out, budget = [], 8  # cap page fetches per release
    for v in sorted(variants, key=lambda x: x.get("rank") or 99)[:12]:
        if not v.get("name"):
            continue
        if v.get("page_url") and not link_alive(v["page_url"]):
            v["page_url"] = None
        img = v.get("image_url") if v.get("image_url") in candidates else None
        if not img and v.get("page_url") and budget > 0:
            budget -= 1
            img = og_image(v["page_url"])
        if img and not image_ok(img):
            img = None
        v["image_url"] = img
        v["price_aed"] = to_aed(v.get("price"), v.get("currency")) if v.get("price") else None
        out.append(v)
    return out


# ---------------------------------------------------------- resale outlook

def compute_resale(cfg, rel, research, wc):
    tax = cfg.get("purchase_tax_pct", 5) / 100
    sell = cfg.get("selling_cost_pct", 10) / 100
    breakeven = round(((1 + tax) / (1 - sell) - 1) * 100, 1)
    out = {"breakeven_pct": breakeven, "updated": now_utc().isoformat()}

    # 1. Live market for THIS watch (once it trades second-hand), via WatchCharts.
    live = wc.lookup(rel["brand"], rel.get("reference")) if rel.get("reference") else None
    if live and live.get("market_aed") and rel.get("price_aed"):
        out["live_premium_pct"] = round((live["market_aed"] / rel["price_aed"] - 1) * 100, 1)
        out["live_market_aed"] = live["market_aed"]

    # 2. Comparables: WatchCharts figures preferred, web-researched figures as fallback.
    comps, wc_count = [], 0
    for c in (research or {}).get("comparables", [])[:5]:
        premium, source = None, c.get("source")
        data = wc.lookup(rel["brand"], c.get("reference")) if c.get("reference") else None
        if data and data.get("premium_pct") is not None:
            premium, source = data["premium_pct"], "WatchCharts"
            wc_count += 1
        else:
            try:
                if c.get("retail_price") and c.get("market_price"):
                    premium = round((float(c["market_price"]) / float(c["retail_price"]) - 1) * 100, 1)
            except (TypeError, ValueError, ZeroDivisionError):
                premium = None
        if premium is not None:
            comps.append({"name": c.get("name"), "reference": c.get("reference"),
                          "premium_pct": premium, "source": source})
    out["comparables"] = comps

    # 3. Base estimate.
    retention = (research or {}).get("brand_retention", "average")
    baseline = {"strong": 10, "average": -15, "weak": -35}.get(retention, -15)
    if len(comps) >= 2:
        base = statistics.median(c["premium_pct"] for c in comps)
        basis = f"median of {len(comps)} comparable watches"
    elif comps:
        # One data point is too thin to trust alone: blend it with the brand's general record.
        base = (comps[0]["premium_pct"] + baseline) / 2
        basis = f"1 comparable watch blended with the brand's general value retention ({retention})"
    else:
        base = baseline
        basis = f"the brand's general value retention ({retention}) only"

    # 4. Adjustments: scarcity vs demand, and early market signals.
    adj, reasons = 0, []
    hype = (rel.get("hype") or {}).get("index")
    pieces = rel.get("pieces")
    if hype is not None and pieces:
        if hype >= 70 and pieces <= 2000:
            adj += 10; reasons.append("high hype on a small run")
        elif hype >= 50 and pieces <= 500:
            adj += 5; reasons.append("solid hype on a very small run")
        elif hype < 30 and pieces > 2000:
            adj -= 5; reasons.append("low hype for the run size")
    if pieces and pieces > 5000:
        adj -= 10; reasons.append("large production")
    if not rel.get("is_limited"):
        adj -= 5; reasons.append("not limited")
    signal = (research or {}).get("early_signal")
    if signal == "positive":
        adj += 10; reasons.append("early market signals positive")
    elif signal == "negative":
        adj -= 10; reasons.append("early market signals negative")

    if "live_premium_pct" in out:
        mid, spread, confidence = out["live_premium_pct"], 5, "high"
        basis = "actual secondary-market price of this watch (WatchCharts)"
        reasons = []
    else:
        mid = base + adj
        if wc_count >= 3:
            spread, confidence = 10, "medium" if adj else "medium-high"
        elif len(comps) >= 2:
            spread, confidence = 15, "medium"
        else:
            spread, confidence = 25, "low"

    low, high = round(mid - spread), round(mid + spread)
    out.update(low_pct=low, high_pct=high, mid_pct=round(mid), confidence=confidence,
               basis=basis, adjustments=reasons, brand_retention=retention,
               brand_retention_note=(research or {}).get("brand_retention_note"),
               early_signal_note=(research or {}).get("early_signal_note"))

    price = rel.get("price_aed")
    if price:
        outlay = price * (1 + tax)
        net = lambda pct: price * (1 + pct / 100) * (1 - sell) - outlay
        out["outlay_aed"] = int(round(outlay))
        out["est_profit_aed"] = int(round(net(mid)))
        out["worst_net_aed"] = int(round(net(low)))
        out["best_net_aed"] = int(round(net(high)))
        out["roi_pct"] = round(net(mid) / outlay * 100, 1)
    out["wc_backed"] = "live_premium_pct" in out or any(c.get("source") == "WatchCharts" for c in comps)
    if confidence == "low":
        out["verdict"] = ("Leaning profit" if low > breakeven else
                          "Leaning loss" if high < breakeven else "Too uncertain to call")
    elif low > breakeven:
        out["verdict"] = "Likely profitable"
    elif mid > breakeven:
        out["verdict"] = "Possible profit"
    elif high > breakeven:
        out["verdict"] = "Unlikely profit"
    else:
        out["verdict"] = "Expect a loss"
    return out


# ------------------------------------------------------------ orchestration

def enrich(cfg, releases, only_ids=None, wc=None):
    """Refresh hype daily and research/resale once per release (re-run near launch)."""
    wc = wc or WatchCharts()
    today = now_utc().date()
    min_score = cfg.get("research_min_score", 6)
    hype_budget = cfg.get("max_hype_checks_per_day", 15)
    research_budget = cfg.get("max_research_per_day", 8)

    def active(r):
        if r.get("over_budget") or r.get("status") == "sold_out":
            return False
        d = parse_day(r.get("launch_date"))
        recent = (parse_iso(r["first_seen"]) or now_utc()) > now_utc() - timedelta(days=45)
        return recent or (d and d >= today)

    candidates = [r for r in releases if active(r) and r.get("score", 0) >= cfg["min_score_for_digest"]
                  and (only_ids is None or r["id"] in only_ids)]
    candidates.sort(key=lambda r: r.get("score", 0), reverse=True)

    for rel in candidates:
        if hype_budget > 0 and ((rel.get("hype") or {}).get("history") or [{}])[-1].get("date") != today.isoformat():
            rel["hype"] = compute_hype(rel)
            hype_budget -= 1

        if rel.get("score", 0) < min_score:
            continue
        last = parse_iso((rel.get("research") or {}).get("at"))
        launch = parse_day(rel.get("launch_date"))
        near_launch = launch and 0 <= (launch - today).days <= 3
        outdated = (rel.get("research") or {}).get("v") != RESEARCH_VERSION
        stale = last is None or outdated or (near_launch and last < now_utc() - timedelta(days=3))
        if stale and research_budget > 0:
            try:
                rel["research"] = {"at": now_utc().isoformat(), "v": RESEARCH_VERSION, "data": research_release(cfg, rel)}
                research_budget -= 1
            except Exception as exc:
                print(f"Research failed for {rel['brand']} {rel['model']}: {exc}")
        if rel.get("research"):
            rel["resale"] = compute_resale(cfg, rel, rel["research"].get("data"), wc)
    wc.save()
    print(f"Enriched {len(candidates)} release(s); WatchCharts calls: {wc.calls}")
    return wc

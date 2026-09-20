"""Private data: your collection and boutique contacts.

Stored encrypted (AES-256-GCM, key derived from PORTFOLIO_PASSPHRASE) in
docs/private.enc.json, so it is unreadable in the public repository. The
dashboard decrypts it in your browser when you enter the passphrase.

Telegram commands (processed on each hourly run):
  /bought <free text>   e.g. /bought Tudor BB58 79030N for 17,900 AED on 12 Sep
  /sold <free text>     e.g. /sold the Tudor for 21,000 AED
  /contact <free text>  e.g. /contact Tudor: Ahmed, Seddiqi Dubai Mall, +971 50 123 4567
  /portfolio            current value of your collection
  /contacts             your saved boutique contacts
  /help
"""

import base64
import hashlib
import html
import json
import os
import secrets
from datetime import timedelta

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from common import (
    HTTP, PRIVATE_PATH, ask_claude, load_json, norm, now_utc, parse_iso, save_json,
    telegram_send, to_aed,
)

ITERATIONS = 250_000


# ------------------------------------------------------------- encryption

def _key(passphrase, salt):
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, ITERATIONS, dklen=32)


def load_private():
    passphrase = os.environ.get("PORTFOLIO_PASSPHRASE")
    empty = {"holdings": [], "contacts": []}
    if not passphrase:
        return None
    blob = load_json(PRIVATE_PATH, None)
    if not blob:
        return empty
    salt, iv, data = (base64.b64decode(blob[k]) for k in ("salt", "iv", "data"))
    plain = AESGCM(_key(passphrase, salt)).decrypt(iv, data, None)
    return json.loads(plain)


def save_private(store):
    passphrase = os.environ.get("PORTFOLIO_PASSPHRASE")
    if not passphrase:
        return
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    data = AESGCM(_key(passphrase, salt)).encrypt(iv, json.dumps(store).encode(), None)
    save_json(PRIVATE_PATH, {
        "v": 1, "kdf": "PBKDF2-SHA256", "iterations": ITERATIONS,
        "salt": base64.b64encode(salt).decode(), "iv": base64.b64encode(iv).decode(),
        "data": base64.b64encode(data).decode(),
    })


# -------------------------------------------------------------- valuation

VALUE_SYSTEM = """Estimate the current secondary-market value (private sale, pre-owned with box and papers) of each
watch using web search (WatchCharts, Chrono24 price data, reputable dealers). Reply with ONLY JSON:
{"values": [{"id": str, "value": number|null, "currency": "USD"|"AED"|..., "source": "site name"}]}"""


def value_holdings(cfg, store, wc, force=False):
    tax = cfg.get("purchase_tax_pct", 5)
    need_web = []
    for h in store["holdings"]:
        if h.get("sold_price_aed"):
            continue
        last = parse_iso(h.get("valued_at"))
        if not force and last and last > now_utc() - timedelta(days=6):
            continue
        data = wc.lookup(h["brand"], h.get("reference")) if wc and wc.enabled else None
        if data and data.get("market_aed"):
            h.update(value_aed=data["market_aed"], value_source="WatchCharts", valued_at=now_utc().isoformat())
        else:
            need_web.append(h)
    if need_web:
        listing = "\n".join(f"id={h['id']}: {h['brand']} {h['model']} ref {h.get('reference')}" for h in need_web)
        try:
            reply = ask_claude(cfg, VALUE_SYSTEM, listing, max_tokens=1500,
                               model=cfg.get("research_model"), web_searches=min(8, 2 * len(need_web)))
            by_id = {v.get("id"): v for v in reply.get("values", [])}
            for h in need_web:
                v = by_id.get(h["id"])
                if v and v.get("value"):
                    h.update(value_aed=to_aed(v["value"], v.get("currency") or "USD"),
                             value_source=f"web estimate ({v.get('source')})", valued_at=now_utc().isoformat())
        except Exception as exc:
            print(f"Web valuation failed: {exc}")
    return tax


def portfolio_summary(cfg, store):
    sell = cfg.get("selling_cost_pct", 10) / 100
    held = [h for h in store["holdings"] if not h.get("sold_price_aed")]
    sold = [h for h in store["holdings"] if h.get("sold_price_aed")]
    if not held and not sold:
        return "📁 Your collection is empty. Add a watch with:\n<code>/bought Tudor BB58 79030N for 17,900 AED</code>"
    lines = ["📁 <b>Your collection</b>"]
    paid_total = value_total = 0
    for h in held:
        paid, value = h.get("price_aed") or 0, h.get("value_aed")
        paid_total += paid
        if value:
            value_total += value
            net = value * (1 - sell) - paid
            pct = (value / paid - 1) * 100 if paid else 0
            lines.append(f"• <b>{html.escape(h['brand'])} {html.escape(h['model'])}</b>\n"
                         f"  Paid AED {paid:,} → worth ~AED {value:,} ({pct:+.0f}%)\n"
                         f"  If sold now, net after costs: AED {net:+,.0f}\n"
                         f"  <i>{html.escape(h.get('value_source') or '')}</i>")
        else:
            value_total += paid
            lines.append(f"• <b>{html.escape(h['brand'])} {html.escape(h['model'])}</b>: paid AED {paid:,}, value not yet available")
    if held:
        lines.append(f"\nTotal paid AED {paid_total:,} | est. value AED {value_total:,}")
    if sold:
        realised = sum(h["sold_price_aed"] - (h.get("price_aed") or 0) for h in sold)
        lines.append(f"Sold: {len(sold)} watch(es), realised AED {realised:+,}")
    return "\n".join(lines)


# ------------------------------------------------------- Telegram commands

PARSE_SYSTEM = """Parse a collector's Telegram command into JSON. Today is {today}.
For "bought": {{"brand": str, "model": str, "reference": str|null, "price": number|null, "currency": "AED" unless stated,
  "date": "YYYY-MM-DD"|null, "notes": str|null}}
For "sold": {{"id": the id of the matching holding from the list, "price": number|null, "currency": "AED" unless stated,
  "date": "YYYY-MM-DD"|null}}
For "contact": {{"brand": str, "name": str|null, "store": str|null, "phone": str|null, "notes": str|null}}
Reply with ONLY the JSON object."""

HELP = ("⌚ <b>Commands</b>\n"
        "/bought Tudor BB58 79030N for 17,900 AED on 12 Sep\n"
        "/sold the Tudor for 21,000 AED\n"
        "/contact Tudor: Ahmed, Seddiqi Dubai Mall, +971 50 123 4567\n"
        "/portfolio: value of your collection\n"
        "/contacts: your boutique contacts\n\n"
        "Commands are processed within the hour.")


def process_commands(cfg, state, wc):
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    resp = HTTP.get(f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"offset": state.get("tg_offset", 0), "timeout": 0}, timeout=30)
    updates = resp.json().get("result", []) if resp.status_code == 200 else []
    if not updates:
        return
    state["tg_offset"] = updates[-1]["update_id"] + 1

    store = load_private()
    changed = False
    today = now_utc().date().isoformat()
    for upd in updates:
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat_id):
            continue  # ignore anyone else who finds the bot
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        try:
            if cmd in ("/help", "/start"):
                telegram_send(HELP)
                continue
            if store is None:
                telegram_send("Add the PORTFOLIO_PASSPHRASE secret on GitHub to use collection and contact commands.")
                continue
            system = PARSE_SYSTEM.format(today=today)
            if cmd == "/bought" and arg:
                d = ask_claude(cfg, system, f'Command "bought": {arg}', max_tokens=400)
                h = {"id": secrets.token_hex(4), "brand": d.get("brand"), "model": d.get("model"),
                     "reference": d.get("reference"), "price_aed": to_aed(d.get("price"), d.get("currency") or "AED"),
                     "purchase_date": d.get("date") or today, "notes": d.get("notes")}
                store["holdings"].append(h)
                value_holdings(cfg, {"holdings": [h]}, wc, force=True)
                changed = True
                telegram_send(f"✅ Added <b>{html.escape(h['brand'] or '')} {html.escape(h['model'] or '')}</b>"
                              f" (AED {h['price_aed'] or 0:,}).")
            elif cmd == "/sold" and arg:
                listing = "\n".join(f"id={h['id']}: {h['brand']} {h['model']} {h.get('reference') or ''}"
                                    for h in store["holdings"] if not h.get("sold_price_aed"))
                d = ask_claude(cfg, system, f'Command "sold": {arg}\nHoldings:\n{listing}', max_tokens=300)
                h = next((x for x in store["holdings"] if x["id"] == d.get("id")), None)
                if not h:
                    telegram_send("I couldn't match that to a watch in your collection. Try including the model name.")
                    continue
                h.update(sold_price_aed=to_aed(d.get("price"), d.get("currency") or "AED"),
                         sold_date=d.get("date") or today)
                changed = True
                gain = (h["sold_price_aed"] or 0) - (h.get("price_aed") or 0)
                telegram_send(f"✅ Marked <b>{html.escape(h['brand'])} {html.escape(h['model'])}</b> as sold"
                              f" for AED {h['sold_price_aed']:,} (AED {gain:+,} vs purchase).")
            elif cmd == "/contact" and arg:
                d = ask_claude(cfg, system, f'Command "contact": {arg}', max_tokens=300)
                store["contacts"] = [c for c in store["contacts"]
                                     if not (norm(c.get("brand")) == norm(d.get("brand"))
                                             and norm(c.get("name")) == norm(d.get("name")))]
                store["contacts"].append(d)
                changed = True
                telegram_send(f"✅ Saved contact for <b>{html.escape(d.get('brand') or '')}</b>.")
            elif cmd == "/portfolio":
                value_holdings(cfg, store, wc)
                changed = True
                telegram_send(portfolio_summary(cfg, store))
            elif cmd == "/contacts":
                if not store["contacts"]:
                    telegram_send("No contacts saved yet. Add one with /contact.")
                else:
                    telegram_send("👤 <b>Your contacts</b>\n" + "\n".join(
                        f"• <b>{html.escape(c.get('brand') or '')}</b>: " + html.escape(
                            ", ".join(x for x in (c.get("name"), c.get("store"), c.get("phone"), c.get("notes")) if x))
                        for c in store["contacts"]))
            else:
                telegram_send(HELP)
        except Exception as exc:
            print(f"Command failed: {text[:60]}: {exc}")
            telegram_send(f"⚠️ I couldn't process that command ({html.escape(str(exc)[:120])}).")
    if changed:
        save_private(store)


def contacts_for(store, brand):
    if not store:
        return []
    return [c for c in store.get("contacts", []) if norm(c.get("brand")) == norm(brand)]

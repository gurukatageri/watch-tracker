"""Shared helpers used by all tracker modules."""

import json
import os
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SEEN_PATH = ROOT / "data" / "seen.json"
STATE_PATH = ROOT / "data" / "state.json"
CACHE_PATH = ROOT / "data" / "market_cache.json"
RELEASES_PATH = ROOT / "docs" / "releases.json"
PRIVATE_PATH = ROOT / "docs" / "private.enc.json"

HTTP = requests.Session()
HTTP.headers["User-Agent"] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Approximate fallback rates (units of currency per 1 AED), used only if the
# live exchange-rate service is unreachable.
FALLBACK_RATES = {
    "AED": 1.0, "USD": 0.2723, "EUR": 0.235, "CHF": 0.218, "GBP": 0.203,
    "JPY": 40.0, "CNY": 1.95, "HKD": 2.12, "SGD": 0.35, "SAR": 1.021,
    "AUD": 0.41, "CAD": 0.37, "INR": 23.5,
}
_rates_cache = None


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def parse_day(value):
    try:
        return date.fromisoformat(value) if value else None
    except Exception:
        return None


def norm(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def get_rates():
    global _rates_cache
    if _rates_cache:
        return _rates_cache
    try:
        data = HTTP.get("https://open.er-api.com/v6/latest/AED", timeout=20).json()
        if data.get("result") == "success":
            _rates_cache = data["rates"]
            return _rates_cache
    except Exception:
        pass
    _rates_cache = FALLBACK_RATES
    return _rates_cache


def to_aed(amount, currency):
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    rate = get_rates().get((currency or "").upper())
    return int(round(amount / rate)) if rate else None


def parse_json_reply(text):
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object in reply")
    return json.loads(text[start:end + 1])


def ask_claude(cfg, system, user, max_tokens=2000, model=None, web_searches=0):
    """Call Claude and return the parsed JSON reply.

    web_searches > 0 lets Claude search the web (server-side tool) up to that many times.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY secret is missing")
    messages = [{"role": "user", "content": user}]
    body = {"model": model or cfg["model"], "max_tokens": max_tokens, "system": system}
    if web_searches:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": web_searches}]

    last_error = None
    for attempt in range(6):
        try:
            resp = HTTP.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={**body, "messages": messages},
                timeout=300,
            )
            if resp.status_code in (429, 500, 502, 503, 529):
                last_error = f"HTTP {resp.status_code}"
                time.sleep(10 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise RuntimeError(f"Claude API error {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            if data.get("stop_reason") == "pause_turn":
                # Long web-search turns can pause; continue where Claude left off.
                messages.append({"role": "assistant", "content": data["content"]})
                continue
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            return parse_json_reply(text)
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Claude API unavailable: {last_error}")


def telegram_send(parts, buttons=None):
    """Send one or more text parts (HTML) to the configured Telegram chat.

    buttons: optional inline keyboard rows [[{"text", "url"}]] attached to the last message.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret is missing")
    if isinstance(parts, str):
        parts = [parts]
    messages, current = [], ""
    for part in parts:
        part = part[:3800]
        if current and len(current) + len(part) + 2 > 3900:
            messages.append(current)
            current = part
        else:
            current = f"{current}\n\n{part}" if current else part
    if current:
        messages.append(current)
    for i, text in enumerate(messages):
        body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons and i == len(messages) - 1:
            body["reply_markup"] = {"inline_keyboard": buttons}
        resp = HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=body, timeout=30)
        if resp.status_code != 200 and "reply_markup" in body:
            body.pop("reply_markup")  # a rejected button URL shouldn't block the message
            resp = HTTP.post(f"https://api.telegram.org/bot{token}/sendMessage", json=body, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram error {resp.status_code}: {resp.text[:300]}")
        time.sleep(1)


def telegram_photos(items):
    """Send photos as one album (2-10) or a single photo. items: [(image_url, caption_html)]."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    items = [(u, c[:1000]) for u, c in items if u][:10]
    if not token or not chat_id or not items:
        return False
    try:
        if len(items) == 1:
            resp = HTTP.post(f"https://api.telegram.org/bot{token}/sendPhoto", timeout=60, json={
                "chat_id": chat_id, "photo": items[0][0], "caption": items[0][1], "parse_mode": "HTML"})
        else:
            resp = HTTP.post(f"https://api.telegram.org/bot{token}/sendMediaGroup", timeout=90, json={
                "chat_id": chat_id,
                "media": [{"type": "photo", "media": u, "caption": c, "parse_mode": "HTML"} for u, c in items]})
        if resp.status_code != 200:
            print(f"Photo send failed ({resp.status_code}): {resp.text[:200]}")
            return False
        time.sleep(1)
        return True
    except Exception as exc:
        print(f"Photo send failed: {exc}")
        return False

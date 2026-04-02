from __future__ import annotations

import json
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any

from config import TELEGRAM_CHAT_ID, TELEGRAM_TOKEN, WEBHOOK_URL


def send_telegram(text: str) -> None:
    """Send a message to the paired Telegram user (non-blocking)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    import threading
    def _post():
        try:
            payload = json.dumps({
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       text,
                "parse_mode": "Markdown",
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data=payload, method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"  \u26a0 Telegram failed: {e}")
    threading.Thread(target=_post, daemon=True).start()


def send_telegram_sync(text: str) -> None:
    """Send a Telegram message synchronously (blocking). Used before polling replies."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        payload = json.dumps({
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "Markdown",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"  \u26a0 Telegram sync send failed: {e}")


def telegram_get_updates(offset: int, timeout_sec: int) -> list[dict[str, Any]]:
    """Long-poll Telegram getUpdates. Returns list of update dicts."""
    url = (f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
           f"?offset={offset}&timeout={timeout_sec}")
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout_sec + 5) as r:
            return json.loads(r.read()).get("result", [])
    except Exception as e:
        print(f"  \u26a0 Telegram poll failed: {e}")
        return []


def wait_telegram_confirm(symbol: str, timeout: int = 120) -> bool:
    """Send a CONFIRM/SKIP prompt then long-poll for the user's reply."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    updates = telegram_get_updates(offset=0, timeout_sec=0)
    offset = (updates[-1]["update_id"] + 1) if updates else 0

    send_telegram_sync(
        f"Reply *CONFIRM* to place `{symbol}` order or *SKIP* to skip\n"
        f"_(expires in {timeout}s)_"
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        poll_sec = min(30, int(deadline - time.time()))
        if poll_sec <= 0:
            break
        for upd in telegram_get_updates(offset=offset, timeout_sec=poll_sec):
            offset = upd["update_id"] + 1
            msg     = upd.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text    = msg.get("text", "").strip().upper()
            if chat_id == str(TELEGRAM_CHAT_ID):
                if text == "CONFIRM":
                    return True
                elif text == "SKIP":
                    send_telegram_sync(f"\u23ed `{symbol}` \u2014 skipped.")
                    return False
                else:
                    send_telegram_sync("Reply *CONFIRM* to buy or *SKIP* to skip.")

    send_telegram_sync(f"\u23f0 `{symbol}` \u2014 timed out, no order placed.")
    return False


def call_webhook(signal: dict[str, Any]) -> None:
    """POST signal data to Claude Terminal webhook (non-blocking)."""
    if not WEBHOOK_URL:
        return
    import threading
    def _post():
        try:
            payload = json.dumps(signal).encode()
            req = urllib.request.Request(WEBHOOK_URL, data=payload, method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"  \u26a0 Webhook call failed: {e}")
    threading.Thread(target=_post, daemon=True).start()


def notify_mac(title: str, message: str) -> None:
    """Send a native macOS notification."""
    title   = title.replace("\\", "\\\\").replace('"', '\\"')
    message = message.replace("\\", "\\\\").replace('"', '\\"')
    script  = f'display notification "{message}" with title "{title}" sound name "Ping"'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def markup_escape(text: Any) -> str:
    """Escape Rich markup characters in arbitrary strings."""
    s = str(text)
    for ch in ("[", "]", "{", "}"):
        s = s.replace(ch, "\\" + ch)
    return s


def _escape_md(text: Any) -> str:
    """Escape Telegram Markdown special characters in arbitrary strings."""
    for ch in ("*", "_", "`", "[", "]"):
        text = str(text).replace(ch, "\\" + ch)
    return text

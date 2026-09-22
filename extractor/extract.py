"""
gp-ext-workflow extractor (daily-quota version)

- Scans your joined Telegram groups/channels for URLs.
- Extracts at most DAILY_LIMIT *new* (never-seen-before) URLs per run.
- Waits DELAY_SECONDS between each message it reads (be gentle on the API).
- Catches Telethon FloodWaitError and sleeps the exact time Telegram asks for.
- Remembers where it left off per-group (cursor) and which URLs were already
  sent (seen_urls), both persisted in Cloudflare KV, so the next run continues
  instead of re-scanning everything.
- Sends the newly found URLs to your Telegram Bot chat when done.

State lives in Cloudflare KV (via the REST API directly, no wrangler needed):
  key "state" -> { "cursors": {dialog_id: last_message_id}, "seen_urls": [...] }
  key "urls"  -> full dataset the Worker's /urls endpoint serves
"""

import os
import re
import json
import time
from datetime import datetime, timezone

import requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

# --- Telegram (user account) ---
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]

# --- Telegram Bot (to send results back) ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_CHAT_ID = os.environ["BOT_CHAT_ID"]

# --- Cloudflare KV (state + published dataset) ---
CF_ACCOUNT_ID = os.environ["CF_ACCOUNT_ID"]
CF_API_TOKEN = os.environ["CF_API_TOKEN"]
CF_KV_NAMESPACE_ID = os.environ["CF_KV_NAMESPACE_ID"]

# --- Tunables ---
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "50"))
DELAY_SECONDS = float(os.environ.get("DELAY_SECONDS", "2"))
MAX_SCAN_PER_DIALOG = int(os.environ.get("MAX_SCAN_PER_DIALOG", "300"))

URL_REGEX = re.compile(
    r'(?:https?://|www\.|t\.me/|telegram\.me/)[^\s<>"\')\]]+',
    re.IGNORECASE,
)

KV_BASE = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}"
KV_HEADERS = {"Authorization": f"Bearer {CF_API_TOKEN}"}


def clean_url(u: str) -> str:
    return u.rstrip(".,)]}\u3002\uff0c")


def extract_urls_from_text(text):
    if not text:
        return []
    return [clean_url(u) for u in URL_REGEX.findall(text)]


def kv_get(key, default):
    r = requests.get(f"{KV_BASE}/values/{key}", headers=KV_HEADERS)
    if r.status_code == 404:
        return default
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return default


def kv_put(key, value: dict):
    r = requests.put(
        f"{KV_BASE}/values/{key}",
        headers=KV_HEADERS,
        data=json.dumps(value, ensure_ascii=False).encode("utf-8"),
    )
    r.raise_for_status()


def send_to_bot(new_urls):
    if not new_urls:
        text = "Today's run: no new URLs found."
        chunks = [text]
    else:
        header = f"🎯 {len(new_urls)} new URL(s) found today:\n\n"
        body = "\n".join(new_urls)
        full = header + body
        # Telegram message limit is 4096 chars; split if needed
        chunks = [full[i:i + 4000] for i in range(0, len(full), 4000)]

    for chunk in chunks:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": BOT_CHAT_ID, "text": chunk, "disable_web_page_preview": True},
        )
        resp.raise_for_status()
        time.sleep(1)


def safe_sleep(seconds):
    if seconds > 0:
        time.sleep(seconds)


def main():
    state = kv_get("state", {"cursors": {}, "seen_urls": []})
    cursors = state.get("cursors", {})
    seen_urls = set(state.get("seen_urls", []))

    full_dataset = kv_get("urls", {"groups": {}})
    groups_data = full_dataset.get("groups", {})

    new_urls_this_run = []

    with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
        for dialog in client.iter_dialogs():
            if len(new_urls_this_run) >= DAILY_LIMIT:
                break
            if not (dialog.is_group or dialog.is_channel):
                continue

            gid = str(dialog.id)
            min_id = cursors.get(gid, 0)
            scanned = 0
            last_seen_id = min_id

            while len(new_urls_this_run) < DAILY_LIMIT and scanned < MAX_SCAN_PER_DIALOG:
                try:
                    messages = list(
                        client.iter_messages(
                            dialog,
                            min_id=last_seen_id,
                            reverse=True,
                            limit=20,
                        )
                    )
                except FloodWaitError as e:
                    print(f"FloodWait: sleeping {e.seconds}s")
                    safe_sleep(e.seconds)
                    continue

                if not messages:
                    break

                for message in messages:
                    scanned += 1
                    last_seen_id = max(last_seen_id, message.id)

                    for u in extract_urls_from_text(message.raw_text):
                        if u not in seen_urls:
                            seen_urls.add(u)
                            new_urls_this_run.append(u)

                            g = groups_data.setdefault(gid, {"group_name": dialog.name, "urls": [], "count": 0})
                            g["urls"].append(u)
                            g["count"] = len(g["urls"])

                        if len(new_urls_this_run) >= DAILY_LIMIT:
                            break

                    safe_sleep(DELAY_SECONDS)

                    if len(new_urls_this_run) >= DAILY_LIMIT:
                        break

                if scanned >= MAX_SCAN_PER_DIALOG or len(new_urls_this_run) >= DAILY_LIMIT:
                    break

            cursors[gid] = last_seen_id

    # persist state + dataset
    state = {"cursors": cursors, "seen_urls": sorted(seen_urls)}
    kv_put("state", state)

    full_dataset = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_groups": len(groups_data),
        "total_urls": len(seen_urls),
        "groups": groups_data,
    }
    kv_put("urls", full_dataset)

    send_to_bot(new_urls_this_run)

    print(f"DONE: {len(new_urls_this_run)} new urls this run (cap {DAILY_LIMIT}). "
          f"Total seen all-time: {len(seen_urls)}")


if __name__ == "__main__":
    main()

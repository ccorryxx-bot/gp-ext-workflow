"""
gp-ext-workflow extractor (Command & Control version)

- Triggered either by GitHub Actions cron OR by a Telegram Bot command
  ("/extract") relayed through the Cloudflare Worker webhook.
- Extracts at most DAILY_LIMIT *new* URLs per run, resuming per-group from
  where it left off (cursor persisted in Cloudflare KV).
- 2s delay between messages read.
- FloodWaitError handling:
    * wait <= FLOOD_ABORT_SECONDS (default 4h): sleep it out, notify if long.
    * wait  > FLOOD_ABORT_SECONDS: save progress, notify with resume time,
      and abort the whole run (exit 1) instead of blocking the runner.
- Any unhandled error: notify the bot with the error, then exit 1 so the
  GitHub Actions run is also marked failed.
- Sends a start ping and a final summary (with the new URLs) to the bot.
"""

import os
import re
import sys
import json
import time
from datetime import datetime, timedelta, timezone

import requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_CHAT_ID = os.environ["BOT_CHAT_ID"]

CF_ACCOUNT_ID = os.environ["CF_ACCOUNT_ID"]
CF_API_TOKEN = os.environ["CF_API_TOKEN"]
CF_KV_NAMESPACE_ID = os.environ["CF_KV_NAMESPACE_ID"]

DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "50"))
DELAY_SECONDS = float(os.environ.get("DELAY_SECONDS", "2"))
MAX_SCAN_PER_DIALOG = int(os.environ.get("MAX_SCAN_PER_DIALOG", "300"))
FLOOD_ABORT_SECONDS = int(os.environ.get("FLOOD_ABORT_HOURS", "4")) * 3600

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


def notify(text: str):
    """Fire-and-forget short status ping to the bot chat."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": BOT_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"notify() failed: {e}")


def send_results(new_urls, total_seen):
    if not new_urls:
        notify(f"✅ Run complete. No new urls found this time. Total all-time: {total_seen}.")
        return

    header = f"✅ Done: {len(new_urls)} new URL(s) (cap {DAILY_LIMIT}). Total all-time: {total_seen}\n\n"
    body = "\n".join(new_urls)
    full = header + body
    chunks = [full[i:i + 4000] for i in range(0, len(full), 4000)]
    for chunk in chunks:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": BOT_CHAT_ID, "text": chunk, "disable_web_page_preview": True},
                timeout=15,
            )
            time.sleep(1)
        except requests.RequestException as e:
            print(f"send_results() failed: {e}")


def safe_sleep(seconds):
    if seconds > 0:
        time.sleep(seconds)


def run():
    state = kv_get("state", {"cursors": {}, "seen_urls": []})
    cursors = state.get("cursors", {})
    seen_urls = set(state.get("seen_urls", []))

    full_dataset = kv_get("urls", {"groups": {}})
    groups_data = full_dataset.get("groups", {})

    new_urls_this_run = []

    def persist():
        kv_put("state", {"cursors": cursors, "seen_urls": sorted(seen_urls)})
        kv_put("urls", {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_groups": len(groups_data),
            "total_urls": len(seen_urls),
            "groups": groups_data,
        })

    notify(f"🚀 Extraction started. Target: {DAILY_LIMIT} new urls.")

    with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
        for dialog in client.iter_dialogs():
            if len(new_urls_this_run) >= DAILY_LIMIT:
                break
            if not (dialog.is_group or dialog.is_channel):
                continue

            gid = str(dialog.id)
            last_seen_id = cursors.get(gid, 0)
            scanned = 0

            while len(new_urls_this_run) < DAILY_LIMIT and scanned < MAX_SCAN_PER_DIALOG:
                try:
                    messages = list(
                        client.iter_messages(dialog, min_id=last_seen_id, reverse=True, limit=20)
                    )
                except FloodWaitError as e:
                    if e.seconds > FLOOD_ABORT_SECONDS:
                        resume_at = datetime.now(timezone.utc) + timedelta(seconds=e.seconds)
                        notify(
                            f"⚠️ FloodWait {e.seconds}s (~{e.seconds/3600:.1f}h) ကြုံရပါတယ်.\n"
                            f"{FLOOD_ABORT_SECONDS // 3600}h ကျော်လို့ workflow ကို ရပ်လိုက်ပါပြီ။\n"
                            f"ခန့်မှန်း resume time: {resume_at.strftime('%Y-%m-%d %H:%M UTC')}"
                        )
                        cursors[gid] = last_seen_id
                        persist()
                        sys.exit(1)
                    else:
                        if e.seconds > 30:
                            notify(f"⏳ FloodWait {e.seconds}s ကြုံရလို့ စောင့်နေပါတယ်...")
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

    persist()
    send_results(new_urls_this_run, len(seen_urls))
    print(f"DONE: {len(new_urls_this_run)} new urls this run. Total all-time: {len(seen_urls)}")


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        raise
    except Exception as e:
        notify(f"❌ Error ကြောင့် workflow ရပ်သွားပါတယ်:\n{type(e).__name__}: {e}")
        raise

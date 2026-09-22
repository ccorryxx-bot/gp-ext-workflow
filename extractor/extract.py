"""
gp-ext-workflow extractor (Command & Control version)

- Triggered either by GitHub Actions cron OR by a Telegram Bot command
  ("/extract") relayed through the Cloudflare Worker webhook.
- Extracts at most DAILY_LIMIT *new* URLs per run, resuming per-group from
  where it left off (cursor persisted in Cloudflare KV).
- 2s delay between messages read.
- FloodWaitError handling (has a known wait time):
    * wait <= FLOOD_ABORT_SECONDS (default 4h): sleep it out, notify if long.
    * wait  > FLOOD_ABORT_SECONDS: save progress, notify with resume time,
      and abort the whole run (exit 1) instead of blocking the runner.
- PeerFloodError handling (NO known wait time -- account got anti-spam
  flagged for too many peer/history requests): save progress, notify that
  there's no ETA, and abort immediately. Retrying soon will not help.
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
from telethon.tl import functions, types
from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UsernameNotOccupiedError,
    UsernameInvalidError,
    ChannelPrivateError,
)

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
MAX_NEW_URLS_PER_DIALOG = int(os.environ.get("MAX_NEW_URLS_PER_DIALOG", "5"))
FLOOD_ABORT_SECONDS = int(os.environ.get("FLOOD_ABORT_HOURS", "4")) * 3600
VALIDATE_URLS = os.environ.get("VALIDATE_URLS", "1") != "0"

URL_REGEX = re.compile(
    r'(?:https?://|www\.|t\.me/|telegram\.me/)[^\s<>"\')\]]+',
    re.IGNORECASE,
)

# t.me/joinchat/HASH or t.me/+HASH -- private invite links (need CheckChatInviteRequest)
TG_INVITE_HASH_REGEX = re.compile(
    r'(?:t\.me|telegram\.me)/(?:joinchat/|\+)([A-Za-z0-9_-]+)', re.IGNORECASE
)
# t.me/username -- public group/channel links (need get_entity)
TG_USERNAME_REGEX = re.compile(
    r'(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,32})(?:[/?]|$)', re.IGNORECASE
)
# path segments that are t.me features, not a group/channel username -- never resolve these
TG_RESERVED_PATHS = {
    "joinchat", "share", "addstickers", "addemoji", "addtheme", "proxy",
    "socks", "iv", "s", "c", "bg", "login", "confirmphone", "setlanguage",
}

KV_BASE = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}"
KV_HEADERS = {"Authorization": f"Bearer {CF_API_TOKEN}"}


def clean_url(u: str) -> str:
    return u.rstrip(".,)]}\u3002\uff0c")


def extract_urls_from_text(text):
    if not text:
        return []
    return [clean_url(u) for u in URL_REGEX.findall(text)]


def classify_telegram_url(client, url):
    """Resolve a Telegram link against the live API and classify it.

    Returns one of:
      'group'        -- confirmed group/megagroup, not expired -- KEEP
      'channel'       -- confirmed broadcast channel -- DROP
      'expired'       -- private invite link, expired/revoked -- DROP
      'invalid'       -- link doesn't resolve to anything real -- DROP
      'not_telegram'  -- not a t.me/telegram.me link at all -- KEEP as-is (unvalidated)
      'unknown'       -- resolution failed for a transient reason (flood, etc) -- KEEP, unverified

    Costs exactly one extra Telegram API call per *new* candidate URL (skipped
    entirely for URLs already in `seen_urls`, and skippable globally via
    VALIDATE_URLS=0).
    """
    m = TG_INVITE_HASH_REGEX.search(url)
    if m:
        invite_hash = m.group(1)
        try:
            result = client(functions.messages.CheckChatInviteRequest(hash=invite_hash))
        except FloodWaitError as e:
            if e.seconds <= 60:
                safe_sleep(e.seconds)
                try:
                    result = client(functions.messages.CheckChatInviteRequest(hash=invite_hash))
                except Exception:
                    return "unknown"
            else:
                return "unknown"
        except (InviteHashExpiredError,):
            return "expired"
        except (InviteHashInvalidError,):
            return "invalid"
        except Exception as e:
            print(f"::warning::classify_telegram_url invite check failed for {url}: {type(e).__name__}: {e}")
            return "unknown"

        if isinstance(result, (types.ChatInviteAlready, types.ChatInvitePeek)):
            chat = result.chat
            return "channel" if getattr(chat, "broadcast", False) else "group"
        if isinstance(result, types.ChatInvite):
            return "channel" if getattr(result, "broadcast", False) else "group"
        return "unknown"

    m2 = TG_USERNAME_REGEX.search(url)
    if m2:
        username = m2.group(1)
        if username.lower() in TG_RESERVED_PATHS:
            return "not_telegram"
        try:
            entity = client.get_entity(username)
        except FloodWaitError as e:
            if e.seconds <= 60:
                safe_sleep(e.seconds)
                try:
                    entity = client.get_entity(username)
                except Exception:
                    return "unknown"
            else:
                return "unknown"
        except (UsernameNotOccupiedError, UsernameInvalidError, ValueError):
            return "invalid"
        except ChannelPrivateError:
            return "invalid"
        except Exception as e:
            print(f"::warning::classify_telegram_url username lookup failed for {url}: {type(e).__name__}: {e}")
            return "unknown"

        if isinstance(entity, types.Channel):
            return "channel" if entity.broadcast else "group"
        if isinstance(entity, types.Chat):
            return "group"
        return "invalid"  # resolved to a User or something else, not a group/channel

    return "not_telegram"


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


def notify(text: str) -> bool:
    """Status ping to the bot chat. Returns True only if Telegram confirmed
    delivery (ok:true). Any failure -- network OR Telegram API rejection --
    is surfaced as a GitHub Actions ::error:: annotation, so a silent bot
    failure still shows up loudly in the run summary."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": BOT_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not r.ok or not data.get("ok"):
            print(f"::error::Bot ဆီကို message ပို့ မရပါ။ HTTP {r.status_code}: {r.text[:500]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"::error::Bot ဆီကို message ပို့ မရပါ (connection error): {type(e).__name__}: {e}")
        return False


def send_results(new_urls, total_seen, rejected=None):
    rejected = rejected or {}
    filtered_note = ""
    if any(rejected.values()):
        filtered_note = (
            f"\n🧹 Filtered out -- channel: {rejected.get('channel', 0)}, "
            f"expired: {rejected.get('expired', 0)}, invalid: {rejected.get('invalid', 0)}"
        )

    if not new_urls:
        notify(f"✅ Run complete. No new urls found this time. Total all-time: {total_seen}.{filtered_note}")
        return

    header = (
        f"✅ Done: {len(new_urls)} new URL(s) (cap {DAILY_LIMIT}). "
        f"Total all-time: {total_seen}{filtered_note}\n\n"
    )
    body = "\n".join(new_urls)
    full = header + body
    chunks = [full[i:i + 4000] for i in range(0, len(full), 4000)]
    failed = 0
    for idx, chunk in enumerate(chunks):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": BOT_CHAT_ID, "text": chunk, "disable_web_page_preview": True},
                timeout=15,
            )
            try:
                data = r.json()
            except ValueError:
                data = {}
            if not r.ok or not data.get("ok"):
                failed += 1
                print(f"::error::Bot ဆီကို result chunk {idx+1}/{len(chunks)} ပို့ မရပါ။ HTTP {r.status_code}: {r.text[:500]}")
            time.sleep(1)
        except requests.RequestException as e:
            failed += 1
            print(f"::error::Bot ဆီကို result chunk {idx+1}/{len(chunks)} ပို့ မရပါ (connection error): {type(e).__name__}: {e}")
    if failed:
        # URLs were still found and persisted to KV -- only the Telegram delivery
        # failed. Fail the Actions run too, so it doesn't look green when the
        # bot never got the results.
        print(f"::error::{failed}/{len(chunks)} chunk(s) Bot ဆီ ပို့ မရပါ -- KV ထဲ url တွေက ရေးထားပြီးသား ဖြစ်ပေမယ့် Bot notify သာ fail တာပါ။")
        sys.exit(1)


def safe_sleep(seconds):
    if seconds > 0:
        time.sleep(seconds)


def run():
    state = kv_get("state", {"cursors": {}, "seen_by_group": {}, "url_classifications": {}})
    cursors = state.get("cursors", {})
    # seen_by_group: gid -> set of urls already recorded FOR THAT GROUP.
    # The same url can exist under multiple groups -- it's only a duplicate
    # if it was already recorded under *this* group.
    seen_by_group = {gid: set(urls) for gid, urls in state.get("seen_by_group", {}).items()}
    # url_classifications: url -> 'group'/'channel'/'expired'/'invalid'/'unknown'.
    # Cached across groups AND across runs, so the same url shared into 5
    # different groups only ever costs 1 Telegram validation call, not 5.
    url_classifications = dict(state.get("url_classifications", {}))

    full_dataset = kv_get("urls", {"groups": {}})
    groups_data = full_dataset.get("groups", {})

    new_urls_this_run = []
    rejected = {"channel": 0, "expired": 0, "invalid": 0}
    validation_calls = [0]  # mutable box, just for the final log line

    def persist():
        kv_put("state", {
            "cursors": cursors,
            "seen_by_group": {gid: sorted(urls) for gid, urls in seen_by_group.items()},
            "url_classifications": url_classifications,
        })
        kv_put("urls", {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_groups": len(groups_data),
            "total_urls": sum(len(g.get("urls", [])) for g in groups_data.values()),
            "groups": groups_data,
        })

    def classify_cached(client, u):
        cached = url_classifications.get(u)
        if cached is not None:
            return cached  # no API call -- already known, from this run or a past one
        kind = classify_telegram_url(client, u)
        url_classifications[u] = kind
        validation_calls[0] += 1
        safe_sleep(DELAY_SECONDS)  # only throttle actual new API calls
        return kind

    notify(f"🚀 Extraction started. Target: {DAILY_LIMIT} new urls, max {MAX_NEW_URLS_PER_DIALOG}/group.")

    current_gid = [None]  # mutable box so the except block can see the in-progress group

    try:
        with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
            for dialog in client.iter_dialogs():
                if len(new_urls_this_run) >= DAILY_LIMIT:
                    break
                if not (dialog.is_group or dialog.is_channel):
                    continue

                gid = str(dialog.id)
                current_gid[0] = gid
                last_seen_id = cursors.get(gid, 0)
                gid_seen = seen_by_group.setdefault(gid, set())
                scanned = 0
                new_from_this_dialog = 0

                while (
                    len(new_urls_this_run) < DAILY_LIMIT
                    and new_from_this_dialog < MAX_NEW_URLS_PER_DIALOG
                    and scanned < MAX_SCAN_PER_DIALOG
                ):
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
                            if u in gid_seen:
                                continue  # already recorded for THIS group

                            if VALIDATE_URLS:
                                kind = classify_cached(client, u)
                            else:
                                kind = "not_telegram"

                            if kind in ("channel", "expired", "invalid"):
                                gid_seen.add(u)  # never re-check for this group again
                                rejected[kind] += 1
                                continue

                            # 'group', 'not_telegram', or 'unknown' -- keep it
                            gid_seen.add(u)
                            new_urls_this_run.append(u)
                            new_from_this_dialog += 1
                            g = groups_data.setdefault(gid, {"group_name": dialog.name, "urls": [], "count": 0})
                            g["urls"].append(u)
                            g["count"] = len(g["urls"])
                            if (
                                len(new_urls_this_run) >= DAILY_LIMIT
                                or new_from_this_dialog >= MAX_NEW_URLS_PER_DIALOG
                            ):
                                break

                        safe_sleep(DELAY_SECONDS)
                        if (
                            len(new_urls_this_run) >= DAILY_LIMIT
                            or new_from_this_dialog >= MAX_NEW_URLS_PER_DIALOG
                        ):
                            break

                    if (
                        scanned >= MAX_SCAN_PER_DIALOG
                        or len(new_urls_this_run) >= DAILY_LIMIT
                        or new_from_this_dialog >= MAX_NEW_URLS_PER_DIALOG
                    ):
                        break

                cursors[gid] = last_seen_id

    except PeerFloodError:
        # No wait-time given by Telegram for this one -- the account itself has
        # been rate-limited for too many peer/history requests. Retrying
        # immediately (or even in a few minutes) will not help; Telegram gives
        # no ETA. Save whatever progress we made and stop the whole run.
        if current_gid[0]:
            cursors[current_gid[0]] = cursors.get(current_gid[0], 0)
        persist()
        notify(
            "🚫 Peer Flood ကြုံရပါတယ်.\n"
            "Telegram က account ကို peer/history request များလွန်းလို့ temporarily flag တင်လိုက်ပါတယ်.\n"
            "ဒါက FloodWait လို တိတိကျကျ wait time မပါဘူး -- ရက်ချီနိုင်ပါတယ်.\n"
            "Workflow ကို ရပ်လိုက်ပါပြီ. ခဏနားပြီးမှ /extract ကို ပြန်စမ်းပါ (24h+ စောင့်ဖို့ recommend)."
        )
        sys.exit(1)

    persist()
    total_records = sum(len(urls) for urls in seen_by_group.values())
    send_results(new_urls_this_run, total_records, rejected)
    print(
        f"DONE: {len(new_urls_this_run)} new urls this run. Total (group,url) records: {total_records}. "
        f"Rejected: {rejected}. Telegram validation API calls this run: {validation_calls[0]}."
    )


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        raise
    except Exception as e:
        err_msg = f"{type(e).__name__}: {e}"
        # Print first -- this line shows up in the Actions run summary even
        # if the bot notify below also fails, so a fully silent failure
        # (no bot message AND nothing visible) is no longer possible.
        print(f"::error::Action ရပ်သွားခဲ့သည်, ဘာဖြစ်လို့ error: {err_msg}")
        notify(f"❌ Error ကြောင့် workflow ရပ်သွားပါတယ်:\n{err_msg}")
        raise

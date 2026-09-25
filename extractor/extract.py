"""
gp-ext-workflow extractor (Command & Control version)

- Triggered either by GitHub Actions cron OR by a Telegram Bot command
  ("/extract") relayed through the Cloudflare Worker webhook.
- Extracts at most DAILY_LIMIT *new* group URLs per run, capped per-group at
  MAX_NEW_URLS_PER_DIALOG (round-robin -- one busy group can't starve the
  rest), resuming per-group from where it left off (cursor in Cloudflare KV).
- Every candidate url is resolved against the live Telegram API and only
  kept if it's a group/megagroup (not a broadcast channel, not an
  expired/invalid invite). Duplicate tracking + the validation-result cache
  are per-group and cross-run (see classify_telegram_url / classify_cached).
- Randomized delay (DELAY_MIN_SECONDS-DELAY_MAX_SECONDS) between messages
  and validation calls.
- New urls are delivered to the bot in batches of BATCH_SIZE, with a long
  randomized rest (BATCH_REST_MIN_MINUTES-BATCH_REST_MAX_MINUTES) between
  batches, instead of one big dump at the end -- breaks up the request
  burst pattern. Delivered as a native-monospace, tap-to-copy bracketed
  list: [https://a,https://b,...]
- FloodWaitError handling (has a known wait time):
    * wait <= FLOOD_ABORT_SECONDS (default 4h): sleep it out, notify if long.
    * wait  > FLOOD_ABORT_SECONDS: save progress, notify with resume time,
      and abort the whole run (exit 1) instead of blocking the runner.
- PeerFloodError handling (NO known wait time -- account got anti-spam
  flagged for too many peer/history requests): save progress, notify that
  there's no ETA, and abort immediately. Retrying soon will not help.
- Any unhandled error: notify the bot with the error, then exit 1 so the
  GitHub Actions run is also marked failed.
- Sends a start ping, one message per delivered batch, and a final summary.
"""

import os
import re
import sys
import json
import time
import html
import random
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

# Captured at module load, i.e. essentially script start -- used to report
# how long the run has been going in every terminal/abort notice, so "0
# new urls found" is distinguishable from "the job never actually ran".
RUN_START_TIME = datetime.now(timezone.utc)

# The GH Actions job itself is killed at timeout-minutes: 330 (an OS-level
# process kill our own try/except can never catch, so no bot notice can go
# out when THAT happens -- see the incident this constant exists to
# prevent). Stopping ourselves comfortably before that always leaves time
# for a clean flush + persist + notify.
MAX_RUN_SECONDS = int(os.environ.get("MAX_RUN_MINUTES", "300")) * 60

CF_ACCOUNT_ID = os.environ["CF_ACCOUNT_ID"]
CF_API_TOKEN = os.environ["CF_API_TOKEN"]
CF_KV_NAMESPACE_ID = os.environ["CF_KV_NAMESPACE_ID"]

# Which Telegram account this run is for (e.g. "vsn", "nch"). Multiple
# accounts can share one KV namespace + one bot chat: this prefixes every
# KV key ("<account>:state", "<account>:urls") so their data never
# collides, and every bot message gets tagged "[VSN]"/"[NCH]" so it's
# obvious which account it came from.
ACCOUNT = os.environ.get("ACCOUNT", "default")
KV_STATE_KEY = f"{ACCOUNT}:state"
KV_URLS_KEY = f"{ACCOUNT}:urls"
# Lightweight, frequently-overwritten progress snapshot for the bot's
# /status command -- separate from KV_STATE_KEY (which only gets written
# at persist(), i.e. batch/end/interrupt) so a mid-run /status check can
# see live progress with a single KV read, no extra Telegram/GH API calls.
KV_LIVE_STATUS_KEY = f"{ACCOUNT}:live_status"
# Id of the single bot message that gets EDITED in place with live progress
# throughout the run (see update_live_message in run()) -- module level so
# the top-level exception handler at the bottom of this file can also
# finalize it on an unhandled error, not just code paths inside run().
LIVE_MESSAGE_ID = [None]

DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "50"))
# Per-message/per-validation-call pacing is randomized within this range
# instead of a fixed delay, so the request rhythm doesn't look scripted.
DELAY_MIN_SECONDS = float(os.environ.get("DELAY_MIN_SECONDS", os.environ.get("DELAY_SECONDS", "1")))
DELAY_MAX_SECONDS = float(os.environ.get("DELAY_MAX_SECONDS", "4"))
MAX_SCAN_PER_DIALOG = int(os.environ.get("MAX_SCAN_PER_DIALOG", "300"))
MAX_NEW_URLS_PER_DIALOG = int(os.environ.get("MAX_NEW_URLS_PER_DIALOG", "5"))
FLOOD_ABORT_SECONDS = int(os.environ.get("FLOOD_ABORT_HOURS", "4")) * 3600
VALIDATE_URLS = os.environ.get("VALIDATE_URLS", "1") != "0"
# Only keep group urls with MORE than this many members. None/unknown member
# counts (lookup failed) are kept rather than dropped -- see get_member_count.
MIN_GROUP_MEMBERS = int(os.environ.get("MIN_GROUP_MEMBERS", "1500"))

# Delivery pacing: new urls are pushed to the bot in batches, with a long
# human-like rest between batches, instead of one big dump at the end.
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))
BATCH_REST_MIN_SECONDS = int(os.environ.get("BATCH_REST_MIN_MINUTES", "15")) * 60
BATCH_REST_MAX_SECONDS = int(os.environ.get("BATCH_REST_MAX_MINUTES", "30")) * 60

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


def get_group_details(client, obj):
    """obj is either a types.ChatInvite (not-yet-joined invite -- title,
    about, and participants_count are all already embedded in it, free) or
    a resolved Chat/Channel entity. Chat has participants_count embedded
    but not `about` (would need a further GetFullChatRequest -- skipped,
    rare case, falls back to title-only language detection). Channel needs
    one GetFullChannelRequest for BOTH participants_count and about
    together -- one extra call covers both the member-count and the
    language filters, not two.

    Returns (members, about) -- either can be None if undeterminable."""
    if isinstance(obj, types.ChatInvite):
        return getattr(obj, "participants_count", None), getattr(obj, "about", None)
    if isinstance(obj, types.Chat):
        return getattr(obj, "participants_count", None), None
    if isinstance(obj, types.Channel):
        try:
            full = client(functions.channels.GetFullChannelRequest(obj))
            return full.full_chat.participants_count, full.full_chat.about
        except FloodWaitError as e:
            if e.seconds <= 60:
                safe_sleep(e.seconds)
                try:
                    full = client(functions.channels.GetFullChannelRequest(obj))
                    return full.full_chat.participants_count, full.full_chat.about
                except Exception:
                    return None, None
            return None, None
        except Exception:
            return None, None
    return None, None


def _decide_kind(members):
    """Shared decision for every 'confirmed group' branch below: member
    threshold only. A None/unknown member count (lookup failed) can't
    produce a false DROP -- it always falls through to keeping the url,
    same policy as before (see classify_telegram_url docstring)."""
    if members is not None and members <= MIN_GROUP_MEMBERS:
        return "small_group"
    return "group"


def classify_telegram_url(client, url):
    """Resolve a Telegram link against the live API and classify it.

    Returns a dict {"kind": <str>, "members": <int|None>}. kind is one of:
      'group'        -- confirmed group/megagroup, passes the member filter -- KEEP
      'small_group'   -- confirmed group/megagroup, <= MIN_GROUP_MEMBERS -- DROP
      'channel'       -- confirmed broadcast channel -- DROP
      'expired'       -- private invite link, expired/revoked -- DROP
      'invalid'       -- link doesn't resolve to anything real -- DROP
      'not_telegram'  -- not a t.me/telegram.me link at all -- KEEP as-is (unvalidated)
      'unknown'       -- resolution failed for a transient reason (flood, etc) -- KEEP, unverified

    A member count that couldn't be determined (missing data / a lookup
    call itself failed) is treated as passing rather than dropped -- we'd
    rather keep an unverified url than lose real data to a transient/
    missing-data issue. Only a *confirmed* small count gets dropped.

    Costs one extra Telegram API call per *new* candidate URL for the
    group/channel check, PLUS one more (GetFullChannelRequest) for the
    member-count check, unless it's a not-yet-joined private invite link
    (that response already has everything for free). Both are skipped
    entirely for URLs already in `seen_urls`, and skippable globally via
    VALIDATE_URLS=0.
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
                    return {"kind": "unknown", "members": None}
            else:
                return {"kind": "unknown", "members": None}
        except (InviteHashExpiredError,):
            return {"kind": "expired", "members": None}
        except (InviteHashInvalidError,):
            return {"kind": "invalid", "members": None}
        except Exception as e:
            print(f"::warning::classify_telegram_url invite check failed for {url}: {type(e).__name__}: {e}")
            return {"kind": "unknown", "members": None}

        if isinstance(result, (types.ChatInviteAlready, types.ChatInvitePeek)):
            chat = result.chat
            if getattr(chat, "broadcast", False):
                return {"kind": "channel", "members": None}
            jitter_sleep()
            members, about = get_group_details(client, chat)
            title = getattr(chat, "title", None)
            kind = _decide_kind(members)
            return {"kind": kind, "members": members, "title": title, "about": about}
        if isinstance(result, types.ChatInvite):
            if getattr(result, "broadcast", False):
                return {"kind": "channel", "members": None}
            members, about = get_group_details(client, result)  # free -- already in the response
            title = getattr(result, "title", None)
            kind = _decide_kind(members)
            return {"kind": kind, "members": members, "title": title, "about": about}
        return {"kind": "unknown", "members": None}

    m2 = TG_USERNAME_REGEX.search(url)
    if m2:
        username = m2.group(1)
        if username.lower() in TG_RESERVED_PATHS:
            return {"kind": "not_telegram", "members": None}
        try:
            entity = client.get_entity(username)
        except FloodWaitError as e:
            if e.seconds <= 60:
                safe_sleep(e.seconds)
                try:
                    entity = client.get_entity(username)
                except Exception:
                    return {"kind": "unknown", "members": None}
            else:
                return {"kind": "unknown", "members": None}
        except (UsernameNotOccupiedError, UsernameInvalidError, ValueError):
            return {"kind": "invalid", "members": None}
        except ChannelPrivateError:
            return {"kind": "invalid", "members": None}
        except Exception as e:
            print(f"::warning::classify_telegram_url username lookup failed for {url}: {type(e).__name__}: {e}")
            return {"kind": "unknown", "members": None}

        if isinstance(entity, types.Channel):
            if entity.broadcast:
                return {"kind": "channel", "members": None}
            jitter_sleep()
            members, about = get_group_details(client, entity)
            title = getattr(entity, "title", None)
            kind = _decide_kind(members)
            return {"kind": kind, "members": members, "title": title, "about": about}
        if isinstance(entity, types.Chat):
            members, about = get_group_details(client, entity)  # free -- embedded on Chat
            title = getattr(entity, "title", None)
            kind = _decide_kind(members)
            return {"kind": kind, "members": members, "title": title, "about": about}
        return {"kind": "invalid", "members": None}  # resolved to a User or something else

    return {"kind": "not_telegram", "members": None}


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


def _send_telegram_message(text: str, parse_mode: str | None = None) -> int | None:
    """Low-level sender. Every message is tagged with the account (e.g.
    "[VSN] ...") when ACCOUNT is set, so multiple accounts sharing one bot
    chat stay distinguishable. Returns the sent message's message_id on
    success (still truthy for existing `if not ok:` callers, AND usable by
    callers that want to edit this exact message later -- see
    _edit_telegram_message), or None if Telegram did not confirm delivery.
    Any failure -- network OR Telegram API rejection -- is surfaced as a
    GitHub Actions ::error:: annotation, so a silent bot failure still
    shows up loudly in the run summary."""
    if ACCOUNT and ACCOUNT != "default":
        text = f"[{ACCOUNT.upper()}] {text}"
    payload = {"chat_id": BOT_CHAT_ID, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=15)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not r.ok or not data.get("ok"):
            print(f"::error::Bot ဆီကို message ပို့ မရပါ။ HTTP {r.status_code}: {r.text[:500]}")
            return None
        return data.get("result", {}).get("message_id")
    except requests.RequestException as e:
        print(f"::error::Bot ဆီကို message ပို့ မရပါ (connection error): {type(e).__name__}: {e}")
        return None


def _edit_telegram_message(message_id: int, text: str) -> bool:
    """Edit an already-sent message in place -- used to keep ONE live-status
    message current throughout a run instead of sending a new ping every
    time (see update_live_message in run()). Same account-tag prefixing as
    _send_telegram_message. "message is not modified" is Telegram's
    response when the edit text is byte-identical to what's already there
    -- harmless, not a real failure, so it's treated as success rather than
    logged as an error."""
    if ACCOUNT and ACCOUNT != "default":
        text = f"[{ACCOUNT.upper()}] {text}"
    payload = {"chat_id": BOT_CHAT_ID, "message_id": message_id, "text": text, "disable_web_page_preview": True}
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText", json=payload, timeout=15)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not r.ok or not data.get("ok"):
            if "message is not modified" in str(data.get("description", "")).lower():
                return True
            print(f"::warning::Live status message edit failed. HTTP {r.status_code}: {r.text[:300]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"::warning::Live status message edit failed (connection error): {type(e).__name__}: {e}")
        return False


def notify(text: str) -> int | None:
    """Plain-text status ping to the bot chat."""
    return _send_telegram_message(text)


# Unified status taxonomy for run-level notices (start / success / failed /
# flood / error). Batch-delivery messages (📦) are a separate visual
# language and don't go through this -- this is specifically for "how did
# the run go" notices.
_STATUS_EMOJI = {
    "start": "🚀",
    "success": "✅",
    "failed": "❌",
    "flood": "🌊",
    "error": "⚠️",
    "timeout": "⏰",
}


def notify_status(status: str, text: str) -> int | None:
    emoji = _STATUS_EMOJI.get(status, "")
    return notify(f"{emoji} {text}".strip())


def format_elapsed(start_time=None):
    """Human-readable elapsed time since start_time (defaults to
    RUN_START_TIME, i.e. script start). Used in every terminal/abort notice
    so 'the run found nothing' and 'the run never really ran' are never
    confused with each other."""
    delta = datetime.now(timezone.utc) - (start_time or RUN_START_TIME)
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _chunk_urls_by_length(urls, max_len=3500):
    """Group urls into chunks whose bracketed-list rendering stays under
    Telegram's 4096-char message cap (max_len leaves headroom for the
    <code> wrapper and any header text)."""
    chunks, current, current_len = [], [], 2  # 2 == the "[" "]"
    for u in urls:
        add_len = len(u) + (1 if current else 0)  # +1 for the joining comma
        if current and current_len + add_len > max_len:
            chunks.append(current)
            current, current_len = [], 2
        current.append(u)
        current_len += add_len
    if current:
        chunks.append(current)
    return chunks or [[]]


def send_url_batch(urls, header: str | None = None) -> bool:
    """Push a batch of urls to the bot as a native-monospace, tap-to-copy
    bracketed list: [https://a,https://b,https://c]. Returns False (and
    logs ::error::) if ANY chunk fails to deliver -- caller decides whether
    that should fail the run."""
    if not urls:
        if header:
            return notify(header)
        return True

    ok_all = True
    chunks = _chunk_urls_by_length(urls)
    for idx, chunk in enumerate(chunks):
        body = "[" + ",".join(chunk) + "]"
        text = f"<code>{html.escape(body)}</code>"
        if header and idx == 0:
            text = f"{html.escape(header)}\n\n{text}"
        ok = _send_telegram_message(text, parse_mode="HTML")
        if not ok:
            print(f"::error::Url batch chunk {idx+1}/{len(chunks)} ({len(chunk)} url(s)) Bot ဆီ ပို့ မရပါ။")
        ok_all = ok_all and ok
        time.sleep(1)
    return ok_all


def safe_sleep(seconds):
    if seconds > 0:
        time.sleep(seconds)


def jitter_sleep():
    """Randomized pacing for message/validation calls -- avoids a fixed,
    scriptable rhythm between requests."""
    safe_sleep(random.uniform(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS))


def batch_rest_sleep():
    """Long human-like pause between delivered batches."""
    seconds = random.uniform(BATCH_REST_MIN_SECONDS, BATCH_REST_MAX_SECONDS)
    print(f"Resting {seconds/60:.1f} min before next batch...")
    time.sleep(seconds)


def time_budget_exceeded():
    return (datetime.now(timezone.utc) - RUN_START_TIME).total_seconds() > MAX_RUN_SECONDS


def push_live_status(**fields):
    """Best-effort progress snapshot, overwritten in place at KV_LIVE_STATUS_KEY
    -- read back by the Worker's /status handler with a single KV GET (no
    Telegram/GitHub API calls needed to see what a run is currently doing).
    Never allowed to break the run: a KV write failure here is logged and
    swallowed, since live status is a visibility nice-to-have, not core to
    the extraction itself."""
    try:
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "run_started_at": RUN_START_TIME.isoformat(),
        }
        payload.update(fields)
        kv_put(KV_LIVE_STATUS_KEY, payload)
    except Exception as e:
        print(f"::warning::live_status push failed (non-fatal): {type(e).__name__}: {e}")


def run():
    state = kv_get(KV_STATE_KEY, {"cursors": {}, "seen_by_group": {}, "url_classifications": {}})
    cursors = state.get("cursors", {})
    # seen_by_group: gid -> set of urls already recorded FOR THAT GROUP.
    # The same url can exist under multiple groups -- it's only a duplicate
    # if it was already recorded under *this* group.
    seen_by_group = {gid: set(urls) for gid, urls in state.get("seen_by_group", {}).items()}
    # url_classifications: url -> {"kind": ..., "members": int|None}.
    # Cached across groups AND across runs, so the same url shared into 5
    # different groups only ever costs its API call(s) once, not 5 times.
    url_classifications = dict(state.get("url_classifications", {}))

    full_dataset = kv_get(KV_URLS_KEY, {"groups": {}})
    groups_data = full_dataset.get("groups", {})

    new_urls_this_run = []
    rejected = {"channel": 0, "expired": 0, "invalid": 0, "small_group": 0}
    validation_calls = [0]  # mutable box, just for the final log line

    def persist():
        kv_put(KV_STATE_KEY, {
            "cursors": cursors,
            "seen_by_group": {gid: sorted(urls) for gid, urls in seen_by_group.items()},
            "url_classifications": url_classifications,
        })
        kv_put(KV_URLS_KEY, {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_groups": len(groups_data),
            "total_urls": sum(len(g.get("urls", [])) for g in groups_data.values()),
            "groups": groups_data,
        })

    def classify_cached(client, u):
        cached = url_classifications.get(u)
        if cached is not None:
            return cached  # no API call -- already known, from this run or a past one
        result = classify_telegram_url(client, u)
        url_classifications[u] = result
        validation_calls[0] += 1
        jitter_sleep()  # only throttle actual new API calls
        return result

    pending_batch = []
    pending_batch_members = []  # parallel list, member counts for the header stat -- not sent in the copyable body
    batch_counter = [0]

    def flush_pending_batch():
        """Push whatever's queued to the bot right now, as its own batch."""
        if not pending_batch:
            return
        batch_counter[0] += 1
        known = [m for m in pending_batch_members if m is not None]
        members_note = f", avg {sum(known)//len(known):,} members" if known else ""
        header = (
            f"📦 Batch {batch_counter[0]} -- {len(pending_batch)} url(s){members_note} "
            f"(running total: {len(new_urls_this_run)}/{DAILY_LIMIT})"
        )
        ok = send_url_batch(pending_batch, header=header)
        if not ok:
            print(
                f"::error::Batch {batch_counter[0]} ({len(pending_batch)} url(s)) Bot ဆီ ပို့ မရပါ -- "
                f"KV ထဲ ရေးထားပြီးသားပါ, Bot notify သာ fail တာပါ။"
            )
        pending_batch.clear()
        pending_batch_members.clear()
        snapshot_live_status()  # batch pushes are a natural, already-existing cadence to piggyback a KV update on

    # -- Live progress snapshot (KV push_live_status + edited bot message via
    # update_live_message, both fed from the same `live` state below) --
    # live: mutable, overwritten in place as scanning moves group to group.
    # total_scanned_box: cumulative messages scanned across the WHOLE run
    # (every group), used with elapsed wall-clock time to get an observed
    # messages/second pace -- this naturally bakes in jitter sleeps and
    # batch rests too, since both count as elapsed time, not just raw scan
    # speed, so the pace is real rather than theoretical.
    live = {
        "current_group": None, "current_group_members": None,
        "dialog_number": 0, "dialog_start_time": None,
        "messages_scanned_this_group": 0,
    }
    total_scanned_box = [0]

    def eta_next_group():
        """Best-effort estimate of when scanning will move on to the next
        group, from this run's observed pace so far. None if there's not
        yet enough data (first few messages of the run) to estimate from.
        This is an estimate, not a guarantee -- pace can shift a lot group
        to group (URL density, flood waits, validation-call mix)."""
        scanned_total = total_scanned_box[0]
        if scanned_total == 0 or live["dialog_start_time"] is None:
            return None
        elapsed = (datetime.now(timezone.utc) - RUN_START_TIME).total_seconds()
        avg_seconds_per_message = elapsed / scanned_total
        remaining = max(0, MAX_SCAN_PER_DIALOG - live["messages_scanned_this_group"])
        return datetime.now(timezone.utc) + timedelta(seconds=avg_seconds_per_message * remaining)

    start_text = f"Extraction started. Target: {DAILY_LIMIT} new urls, max {MAX_NEW_URLS_PER_DIALOG}/group, >{MIN_GROUP_MEMBERS} members."

    _LIVE_LABEL = {
        "start": "starting",
        "scanning": "🔎 scanning",
        "flood": "🌊 flood-paused / aborted",
        "timeout": "⏰ self-stopped (time budget)",
        "idle": "✅ finished",
        "failed": "❌ failed",
    }

    def build_live_text(status):
        """The 🚀 start line stays as a permanent header (same text the
        old static notice always showed) -- everything below it is live
        and gets rewritten on every edit, per the explicit ask that this
        info live IN the start message, not only in a separate /status
        pull. Kept as one continuously-edited message rather than a new
        ping per update -- see update_live_message for why."""
        eta = eta_next_group()
        lines = [f"{_STATUS_EMOJI['start']} {start_text}", "", f"📊 Live: {_LIVE_LABEL.get(status, status)}"]
        if live["current_group"]:
            members = live["current_group_members"]
            members_txt = f"{members:,} members" if members is not None else "members unknown"
            lines.append(f"Group #{live['dialog_number']}: {live['current_group']} ({members_txt})")
            lines.append(f"Scanned in this group: {live['messages_scanned_this_group']}")
        lines.append(f"Total messages scanned: {total_scanned_box[0]}")
        lines.append(f"Total urls found: {len(new_urls_this_run)}")
        if eta:
            lines.append(f"Est. next group swap: ~{eta.strftime('%H:%M UTC')} (pace estimate, not exact)")
        lines.append(f"⏱ Run duration: {format_elapsed()}")
        return "\n".join(lines)

    def update_live_message(status):
        text = build_live_text(status)
        if LIVE_MESSAGE_ID[0] is None:
            # First call of the run -- this SEND is the start notice itself
            # (same 🚀 text as before, now with live fields already attached).
            LIVE_MESSAGE_ID[0] = notify(text)
            return
        ok = _edit_telegram_message(LIVE_MESSAGE_ID[0], text)
        if not ok:
            # Message likely deleted by the user, or a genuine edit failure --
            # fall back to one fresh message rather than silently losing
            # live visibility for the rest of the run.
            LIVE_MESSAGE_ID[0] = notify(text)

    def snapshot_live_status(status="scanning"):
        eta = eta_next_group()
        push_live_status(
            status=status,
            current_group=live["current_group"],
            current_group_members=live["current_group_members"],
            dialog_number=live["dialog_number"],
            messages_scanned_this_group=live["messages_scanned_this_group"],
            total_messages_scanned=total_scanned_box[0],
            total_urls_found_this_run=len(new_urls_this_run),
            rejected_this_run=dict(rejected),
            estimated_next_group_at=eta.isoformat() if eta else None,
            eta_note="estimate based on this run's average pace so far -- not exact" if eta else None,
        )
        update_live_message(status)

    snapshot_live_status(status="start")

    current_gid = [None]  # mutable box so the except block can see the in-progress group
    stopped_for_time_budget = False

    try:
        with TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH) as client:
            for dialog in client.iter_dialogs():
                if len(new_urls_this_run) >= DAILY_LIMIT:
                    break
                if time_budget_exceeded():
                    stopped_for_time_budget = True
                    break
                if not (dialog.is_group or dialog.is_channel):
                    continue

                gid = str(dialog.id)
                current_gid[0] = gid
                last_seen_id = cursors.get(gid, 0)
                gid_seen = seen_by_group.setdefault(gid, set())
                scanned = 0
                new_from_this_dialog = 0

                live["current_group"] = dialog.name
                # Free -- already embedded in the dialog entity Telethon loaded for
                # iter_dialogs, no extra API call. Only reliably populated for Chat;
                # Channel usually needs GetFullChannelRequest to know this, which we
                # don't pay for here just to report a status number.
                live["current_group_members"] = getattr(dialog.entity, "participants_count", None)
                live["dialog_number"] += 1
                live["dialog_start_time"] = datetime.now(timezone.utc)
                live["messages_scanned_this_group"] = 0
                snapshot_live_status()

                while (
                    len(new_urls_this_run) < DAILY_LIMIT
                    and new_from_this_dialog < MAX_NEW_URLS_PER_DIALOG
                    and scanned < MAX_SCAN_PER_DIALOG
                    and not time_budget_exceeded()
                ):
                    try:
                        messages = list(
                            client.iter_messages(dialog, min_id=last_seen_id, reverse=True, limit=20)
                        )
                    except FloodWaitError as e:
                        # A flood wait doesn't just have to fit under FLOOD_ABORT_SECONDS
                        # to be safe to sleep out -- it also has to fit under what's LEFT
                        # of MAX_RUN_SECONDS. Blind-sleeping a 3-4h flood wait can carry
                        # the run straight through the self-imposed time budget and into
                        # GitHub's hard timeout-minutes kill -- an OS-level kill that no
                        # try/except/notify can react to (that silent-cancel failure mode
                        # is exactly what MAX_RUN_MINUTES exists to prevent elsewhere, so
                        # it has to be honored here too, not just at the outer loop checks).
                        elapsed_now = (datetime.now(timezone.utc) - RUN_START_TIME).total_seconds()
                        would_exceed_budget = elapsed_now + e.seconds > MAX_RUN_SECONDS
                        if e.seconds > FLOOD_ABORT_SECONDS or would_exceed_budget:
                            resume_at = datetime.now(timezone.utc) + timedelta(seconds=e.seconds)
                            flush_pending_batch()
                            reason = (
                                f"{FLOOD_ABORT_SECONDS // 3600}h ကျော်လို့"
                                if e.seconds > FLOOD_ABORT_SECONDS
                                else "ဒီ wait ကို အပြည့်စောင့်ရင် run time budget ကျော်သွားမှာမို့"
                            )
                            notify_status(
                                "flood",
                                f"Flood wait -{e.seconds}s (~{e.seconds/3600:.1f}h) ကြုံရပါတယ်.\n"
                                f"{reason} workflow ကို ရပ်လိုက်ပါပြီ။🎯\n"
                                f"Resume ဖြစ်မည့် အချိန်: {resume_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
                                f"⏱ Run duration: {format_elapsed()}"
                            )
                            cursors[gid] = last_seen_id
                            persist()
                            snapshot_live_status(status="flood")
                            sys.exit(1)
                        else:
                            if e.seconds > 30:
                                notify_status("flood", f"FloodWait {e.seconds}s ကြုံရလို့ စောင့်နေပါတယ်...")
                            safe_sleep(e.seconds)
                            continue

                    if not messages:
                        break

                    for message in messages:
                        scanned += 1
                        total_scanned_box[0] += 1
                        live["messages_scanned_this_group"] = scanned
                        last_seen_id = max(last_seen_id, message.id)
                        did_fresh_classify = False  # true if any url below triggered a real API call

                        # Groups with zero matching urls (all filtered, or genuinely
                        # none posted) can otherwise go the whole MAX_SCAN_PER_DIALOG
                        # without a single batch flush -- that's exactly the "run for
                        # hours, no visibility into what's happening" gap. Piggyback a
                        # cheap update every 25 messages so both /status AND the live
                        # bot message (see update_live_message) stay current even then.
                        if total_scanned_box[0] % 25 == 0:
                            snapshot_live_status()

                        for u in extract_urls_from_text(message.raw_text):
                            if u in gid_seen:
                                continue  # already recorded for THIS group

                            if VALIDATE_URLS:
                                if u not in url_classifications:
                                    did_fresh_classify = True  # classify_cached is about to jitter for this one itself
                                result = classify_cached(client, u)
                            else:
                                result = {"kind": "not_telegram", "members": None}
                            kind, members = result["kind"], result["members"]
                            title = result.get("title")
                            about = result.get("about")
                            if about:
                                about = about.strip()[:200]  # keep the KV dataset lean

                            if kind in ("channel", "expired", "invalid", "small_group"):
                                gid_seen.add(u)  # never re-check for this group again
                                rejected[kind] += 1
                                continue

                            # 'group', 'not_telegram', or 'unknown' -- keep it
                            gid_seen.add(u)
                            new_urls_this_run.append(u)
                            new_from_this_dialog += 1
                            g = groups_data.setdefault(gid, {"group_name": dialog.name, "urls": [], "count": 0})
                            entry = {"url": u, "members": members}
                            if title:
                                entry["title"] = title
                            if about:
                                entry["about"] = about
                            g["urls"].append(entry)
                            g["count"] = len(g["urls"])

                            pending_batch.append(u)
                            pending_batch_members.append(members)
                            if len(pending_batch) >= BATCH_SIZE:
                                flush_pending_batch()
                                if len(new_urls_this_run) < DAILY_LIMIT:
                                    batch_rest_sleep()

                            if (
                                len(new_urls_this_run) >= DAILY_LIMIT
                                or new_from_this_dialog >= MAX_NEW_URLS_PER_DIALOG
                            ):
                                break

                        if not did_fresh_classify:
                            # No API call happened for this message (no urls, or all
                            # cached) -- still pace the plain message-scanning cadence.
                            # If a fresh classify DID happen, classify_cached already
                            # jittered for it -- an extra sleep here would just stack.
                            jitter_sleep()
                        if (
                            len(new_urls_this_run) >= DAILY_LIMIT
                            or new_from_this_dialog >= MAX_NEW_URLS_PER_DIALOG
                        ):
                            break

                    if (
                        scanned >= MAX_SCAN_PER_DIALOG
                        or len(new_urls_this_run) >= DAILY_LIMIT
                        or new_from_this_dialog >= MAX_NEW_URLS_PER_DIALOG
                        or time_budget_exceeded()
                    ):
                        break

                cursors[gid] = last_seen_id

                if time_budget_exceeded():
                    stopped_for_time_budget = True
                    break

    except PeerFloodError:
        # No wait-time given by Telegram for this one -- the account itself has
        # been rate-limited for too many peer/history requests. Retrying
        # immediately (or even in a few minutes) will not help; Telegram gives
        # no ETA. Save whatever progress we made and stop the whole run.
        if current_gid[0]:
            cursors[current_gid[0]] = cursors.get(current_gid[0], 0)
        flush_pending_batch()
        persist()
        notify_status(
            "flood",
            "Peer Flood ကြုံရပါတယ်.\n"
            "Telegram က account ကို peer/history request များလွန်းလို့ temporarily flag တင်လိုက်ပါတယ်.\n"
            "ဒါက FloodWait လို တိတိကျကျ wait time မပါဘူး -- ရက်ချီနိုင်ပါတယ်.\n"
            "Workflow ကို ရပ်လိုက်ပါပြီ. ခဏနားပြီးမှ /extract ကို ပြန်စမ်းပါ (24h+ စောင့်ဖို့ recommend).\n"
            f"⏱ Run duration: {format_elapsed()}"
        )
        snapshot_live_status(status="flood")
        sys.exit(1)

    flush_pending_batch()
    persist()
    total_records = sum(len(urls) for urls in seen_by_group.values())
    filtered_note = ""
    if any(rejected.values()):
        filtered_note = (
            f"\n🧹 Filtered out -- channel: {rejected.get('channel', 0)}, "
            f"small_group (≤{MIN_GROUP_MEMBERS}): {rejected.get('small_group', 0)}, "
            f"expired: {rejected.get('expired', 0)}, invalid: {rejected.get('invalid', 0)}"
        )
    if stopped_for_time_budget:
        notify_status(
            "timeout",
            f"Time budget ({MAX_RUN_SECONDS // 60}min) ရောက်လို့ run ကို ကိုယ်တိုင် ရပ်လိုက်ပါပြီ "
            f"(GitHub ရဲ့ job timeout မမီခင်, clean notify ပို့ခွင့်ရအောင်).\n"
            f"{len(new_urls_this_run)} new url(s) sent across {batch_counter[0]} batch(es). "
            f"Total (group,url) records: {total_records}.{filtered_note}\n"
            f"Cursor state save ပြီးသားမို့ နောက် run ကျရင် ရပ်ခဲ့တဲ့နေရာကနေ ဆက်မယ်.\n"
            f"⏱ Run duration: {format_elapsed()}"
        )
        snapshot_live_status(status="timeout")
    else:
        notify_status(
            "success",
            f"Run complete -- {len(new_urls_this_run)} new url(s) sent across "
            f"{batch_counter[0]} batch(es). Total (group,url) records: {total_records}."
            f"{filtered_note}\n"
            f"⏱ Run duration: {format_elapsed()}"
        )
        snapshot_live_status(status="idle")
    print(
        f"DONE: {len(new_urls_this_run)} new urls this run. Total (group,url) records: {total_records}. "
        f"Rejected: {rejected}. Telegram validation API calls this run: {validation_calls[0]}. "
        f"Elapsed: {format_elapsed()}."
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
        notify_status("failed", f"Error ကြောင့် workflow ရပ်သွားပါတယ်:\n{err_msg}\n⏱ Run duration: {format_elapsed()}")
        push_live_status(status="failed", error=err_msg)
        if LIVE_MESSAGE_ID[0] is not None:
            _edit_telegram_message(
                LIVE_MESSAGE_ID[0],
                f"❌ failed -- {err_msg}\n⏱ Run duration: {format_elapsed()}",
            )
        raise

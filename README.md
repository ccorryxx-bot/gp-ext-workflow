# gp-ext-workflow

Extracts URLs from your own Telegram groups/channels, admin-triggered on demand,
stores them in Cloudflare KV, and serves them via a Cloudflare Worker HTTP endpoint.

## Architecture

```
GitHub Actions (workflow_dispatch, admin-triggered via Bot /extract -- no cron)
    -> extractor/extract.py  (Telethon, logs in with STRING_SESSION)
       - reads "state" (cursors + seen_by_group + url_classifications) from
         Cloudflare KV via REST API
       - scans groups from where it left off last time, round-robin (see below)
       - for each new candidate url, resolves it against the live Telegram
         API (CheckChatInviteRequest / get_entity) and classifies it:
         group (kept), channel (dropped), expired/invalid invite (dropped)
       - stops once 50 NEW *group* urls are found (DAILY_LIMIT)
       - randomized delay between messages/validation calls, catches FloodWaitError
       - writes updated "state" and "urls" back to KV via REST API
       - delivers new urls to your Telegram Bot chat in batches, with a long
         rest between batches (see "Delivery pacing" below)

Cloudflare Worker (worker/src/index.js)
    -> GET /urls             reads "urls" key from KV, serves JSON
    -> GET /urls?q=...       filter by substring
    -> GET /urls?group=...   filter by group name
```

The extractor runs on GitHub's runners, not inside the Worker, because the Worker
runtime cannot hold the persistent MTProto connection Telegram's client API needs.

KV is used for two things:
- `state` — cursor per group (last message id processed), the set of urls
  already recorded per group, and a url→classification cache, so each run
  only pulls what's new and never re-sends a duplicate.
- `urls` — the full published dataset the Worker serves over HTTP.

The extractor talks to Cloudflare KV directly over its REST API (`requests`),
so no `wrangler` CLI is needed in that workflow. `wrangler` is only used by
`deploy-worker.yml` to ship the Worker code itself.

## Delivery pacing

New urls are pushed to the bot in batches of `BATCH_SIZE` (default 10) as
they're found, with a randomized rest of `BATCH_REST_MIN_MINUTES` to
`BATCH_REST_MAX_MINUTES` (default 15–30 min) before the next batch --
instead of scanning everything first and dumping all 50 at the end. This
breaks up the request burst pattern; Telegram's flood detection cares more
about volume-in-a-short-window than sub-second timing. Per-message and
per-validation-call pacing is also randomized (`DELAY_MIN_SECONDS`–
`DELAY_MAX_SECONDS`, default 1–4s) instead of a fixed delay -- and only
applied once per message: if a message's url(s) already triggered a fresh
(non-cached) classification call, that call's own jitter already paced
this message, so the generic per-message jitter is skipped rather than
stacking a redundant sleep on top.

A full `DAILY_LIMIT=50` run normally takes roughly 1.5–2h (mostly the
batch rests). The pathological case is real, though, and hit in practice:
many groups the account is in can have zero matching urls (all filtered
out, or genuinely none posted), each still scanned to its full
`MAX_SCAN_PER_DIALOG` -- across enough such groups, a run can genuinely
take hours without ever reaching DAILY_LIMIT.

That matters because `timeout-minutes: 330` (job's hard cap, under
GitHub's 360min/6h ceiling) is an **OS-level kill** when it fires --
nothing in the script's own try/except can catch it, so no bot notice can
go out. `MAX_RUN_MINUTES` (default 300) exists specifically to prevent
that: the run checks its own elapsed time between dialogs (and inside a
dialog's scan loop) and, if exceeded, stops itself early -- flushing the
pending batch, persisting cursors, and sending a `timeout` (⏰) notice --
comfortably before the hard kill would otherwise take that chance away.
`MAX_RUN_MINUTES` should always stay meaningfully below `timeout-minutes`
(30min of buffer by default) so the cleanup itself has time to finish.

If a run is interrupted (FloodWait abort, PeerFlood, or the time budget),
whatever's already been found and validated is flushed to the bot and
persisted to KV before it exits, and cursors resume next run from exactly
where it stopped.

Each batch (and the final summary) is sent as a native-monospace,
tap-to-copy numbered list, one url per line:
```
1. https://t.me/a
2. https://t.me/b
```
(previously a comma-joined `[a,b,...]` blob -- that glued `[`/`]`/`,`
directly onto url edges with no separating whitespace, which made some
downstream tools mis-parse the first/last url as invalid; nothing is ever
flush against a url's edges now, only a leading `"N. "` and a trailing
newline.)

## Round-robin across groups

Each run visits every group (not just the first one it finds new urls in).
Per group, it stops early once it's collected `MAX_NEW_URLS_PER_DIALOG`
(default 5) new urls or scanned `MAX_SCAN_PER_DIALOG` (default 300) messages,
then moves to the next group -- so one very active group can't eat the
entire `DAILY_LIMIT` and starve quieter groups. Each group's scan position
(`cursors`) still persists in KV, so it always resumes where it left off.

## Duplicate detection

Duplicates are tracked **per group**, not globally by url text. The same
url can legitimately show up under several different groups' datasets --
only a repeat within the *same* group is skipped as a duplicate.

To avoid re-validating the same url once per group it's found in, url
classification results (`group` / `channel` / `expired` / `invalid`) are
cached in KV by url text and reused across groups and across runs -- so a
url shared into 5 groups still only costs 1 Telegram validation call, not 5.

## URL validation

Every new candidate URL is checked against the live Telegram API before being
kept (set `VALIDATE_URLS=0` as a GitHub Actions env var to skip this and go
back to raw regex extraction):

- `t.me/+hash` / `t.me/joinchat/hash` (private invite links) →
  `CheckChatInviteRequest`. Expired/revoked → dropped as `expired`, unresolvable → `invalid`.
- `t.me/username` (public links) → `get_entity`. Not found → `invalid`.
- Either way, if it resolves to a broadcast channel (not a group/megagroup) → dropped as `channel`.
- If it's a real group/megagroup, its member count is checked against
  `MIN_GROUP_MEMBERS` (default 1500) -- `<=` that many members → dropped as
  `small_group`. A count that couldn't be determined (transient API failure)
  is treated as passing, not dropped -- see the docstring on
  `classify_telegram_url` for the reasoning.
- Non-Telegram URLs pass through unvalidated (kept as-is).

Member counts and the about text both come free for not-yet-joined private
invite links (embedded in the `CheckChatInviteRequest` response); for
everything else, one `GetFullChannelRequest` call covers the member-count
check. All of this is cached per url in `url_classifications`, so repeats
-- within a run, across groups, across days -- cost nothing extra.

Kept urls also carry `title` and (up to 200 chars of) `about` when known --
both come from data already fetched for the member-count/language checks,
so this adds no extra API calls. Omitted from the entry entirely when
unknown (e.g. `not_telegram`/`unknown` kind, or the rare Chat-without-about
case) rather than stored as null.

The `urls` KV dataset stores each entry as `{"url": ..., "members": N}`
(not a bare string) so member counts are queryable later --
`GET /urls?account=vsn&min_members=5000` filters by it.

## Bot notifications

Run-level status notices (not the per-batch url pushes) go through a small
taxonomy in `notify_status(status, text)`:

| status    | emoji | when |
|-----------|-------|------|
| `start`   | 🚀    | run begins -- this message stays around and gets edited in place with live progress for the rest of the run, see "Live status" below |
| `success` | ✅    | run finished normally (with a filtered-out breakdown if anything was dropped) |
| `timeout` | ⏰    | self-stopped after `MAX_RUN_MINUTES` to leave time for a clean notice before the job's hard kill (see "Delivery pacing") |
| `flood`   | 🌊    | FloodWait abort, a long FloodWait sleep, or PeerFlood |
| `failed`  | ❌    | any unhandled exception |
| `error`   | ⚠️    | reserved for future use (mid-run degraded-but-continuing conditions) |

Batch url deliveries keep their own 📦 prefix -- they're data, not a status.

## Live status (mid-run visibility)

`KV_STATE_KEY`/`KV_URLS_KEY` only get written at `persist()` -- batch time,
end of run, or an abort -- so they can't show what a run is doing *right
now*. Two things fix that, updated together from the same snapshot (on
every dialog switch, every batch flush, and every 25 messages scanned
within a dialog -- so groups with zero matching urls still update it, not
just groups that produce a batch):

1. **KV** -- `<account>:live_status` is overwritten in place with `status`
   (`start`/`scanning`/`flood`/`timeout`/`idle`/`failed`), `current_group` +
   `current_group_members` + `dialog_number` (member count is whatever
   Telethon already loaded for the dialog list -- free, no extra API call;
   often `null` for channels, which need a separate call this doesn't pay
   for just to report a number), `messages_scanned_this_group` and
   `total_messages_scanned` (this run), `current_group_raw_url_count`,
   `total_urls_found_this_run`, and `estimated_next_group_at` (projected
   from this run's actual observed pace so far -- elapsed time ÷ messages
   scanned, so jitter sleeps and batch rests are already baked into the
   rate, not just raw scan speed; an estimate, not a promise). Reading it
   costs exactly one KV GET, no Telegram or GitHub Actions API calls -- the
   bot's `/status [vsn|nch]` command includes it automatically when a
   snapshot exists, and it's also exposed directly over HTTP as
   `GET /status?account=vsn|nch` on the Worker (same auth as `/urls`), for
   polling from anywhere else.

   Two url counts are tracked, deliberately kept separate because they
   answer different questions and conflating them is exactly what caused
   confusion once already (see git history): `current_group_raw_url_count`
   is how many messages in the group *currently being scanned* contain a
   url, still unscanned as of this run's cursor (`min_id`) -- one cheap
   `messages.search(filter=InputMessagesFilterUrl, limit=0)` call per group
   ENTERED (`count_raw_url_messages`, one extra API call, not per message;
   Telegram returns just the `.total` count, no message bodies fetched).
   It's a raw density signal, pre-validation -- a message with 2 links only
   counts once, and it says nothing about whether those links pass the
   member-count filter. `total_urls_found_this_run` is the opposite: fully
   validated, deduped, kept urls, summed across every group scanned SO FAR
   this run (matches the running total in batch headers). The live bot
   message shows the raw per-group count under "Total urls found" (per an
   explicit request that it reflect "what's in this group", not a
   cross-group cumulative); `/status` shows both, labeled separately.

2. **Bot chat** -- the same fields, formatted, are pushed live into the
   🚀 start notice itself via `editMessageText`, so progress is visible
   from the moment `/extract` is called without needing to ask `/status`.
   It edits ONE message in place rather than sending a fresh ping on every
   update (a zero-url group can hit this ~12 times over its
   `MAX_SCAN_PER_DIALOG`, and a full round-robin run can cover dozens of
   groups -- sending that many separate messages would flood the chat and
   risk Telegram's own rate limit, documented as ~1 message/sec sustained
   in a private chat or ~20/minute in a group chat for both `sendMessage`
   and `editMessageText`; our real cadence, paced by the same jitter/rest
   delays as everything else, stays well under either). This is entirely
   separate from the *scraping* account's Telegram MTProto flood-wait risk
   discussed elsewhere in this doc -- it's the bot account's own HTTP Bot
   API, unrelated rate limit, unrelated account.

## Skipping groups manually

The live message's `⏭ Skip this group` button always targets whichever
group is currently active in `live["current_group_id"]` (re-sent as part
of `reply_markup` on every edit, so it never goes stale). Tapping it:

- Writes `{gid: {name, excluded_at}}` into `<account>:excluded_groups` in KV
- The extractor loads that set once at the start of every future run and
  `continue`s past any dialog whose id is in it -- before doing any scan
  work, so a skipped group costs nothing going forward

This is a **manual curation decision, not an automatic one** -- "0 urls
found so far" in the live message is not by itself evidence a group is
worth skipping; it may simply not have been scanned yet this run (check
`messages_scanned_this_group` first). Nothing in the extractor ever writes
to this list on its own.

`/skipped [vsn|nch]` lists everything currently excluded for an account,
each with a `♻️ Unskip` button that removes it from the set (and future
runs will scan it again, from wherever its `cursors` entry last left off
-- skipping never touches or resets cursor state).

## Multiple Telegram accounts (VSN / NCH)

This repo can run extraction for more than one Telegram account against the
same bot chat and the same Cloudflare KV namespace:

- Each account gets its own workflow file (`extract-vsn.yml`, `extract-nch.yml`)
  and its own `<ACCOUNT>_API_ID` / `<ACCOUNT>_API_HASH` / `<ACCOUNT>_STRING_SESSION`
  GitHub secrets. `BOT_TOKEN`, `BOT_CHAT_ID`, and the `CF_*` KV secrets stay shared.
- `ACCOUNT` (set per-workflow, e.g. `vsn`/`nch`) prefixes every KV key
  (`vsn:state`, `vsn:urls`, `nch:state`, `nch:urls`) so the two accounts'
  data never collides, and tags every bot message (`[VSN] ...`) so they
  stay distinguishable in the shared chat.
- Cron times are staggered 30min apart (03:00 / 03:30 UTC) -- not required
  for flood-safety (different Telegram accounts, independent limits), just
  for cleaner monitoring.
- `/extract` in the bot now asks which account (VSN / NCH / Both) via
  inline buttons before dispatching; `/status [vsn|nch]` shows one or both.
- `GET /urls` now requires `?account=vsn` or `?account=nch`.

To add a third account: add its 3 `<KEY>_*` secrets, add a
`.github/workflows/extract-<key>.yml` (copy an existing one, change
`ACCOUNT`/secret names), and add it to the `ACCOUNTS` map at the top
of `worker/src/index.js`.

## One-time setup

1. Generate a session string locally (do NOT run this in CI):
   ```
   cd extractor
   pip install telethon
   python generate_session.py
   ```
   Paste the printed string into the `STRING_SESSION` GitHub secret.

2. KV namespace `GP_URLS` is already created (id baked into `worker/wrangler.toml`).
   Use that same id for the `CF_KV_NAMESPACE_ID` GitHub secret.

3. (Optional) protect the Worker endpoint:
   ```
   cd worker
   npx wrangler secret put WORKER_AUTH_TOKEN
   ```

## GitHub Secrets required

See the setup table shared separately.

## Manual run

Both workflows only run via `workflow_dispatch` -- no cron. Trigger them
with `/extract` in the bot (asks VSN / NCH / Both), or by hand from the
Actions tab.

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
`DELAY_MAX_SECONDS`, default 1–4s) instead of a fixed delay.

A full `DAILY_LIMIT=50` run therefore normally takes roughly 1.5–2h
(mostly the batch rests), well inside the job's `timeout-minutes: 330`
safety cap (GitHub's hard limit is 360min/6h). Note: in a pathological case
-- many groups with zero matching urls, each scanned to its full
`MAX_SCAN_PER_DIALOG` -- the scanning phase itself could still run long;
the job timeout exists as a backstop for that.

If a run is interrupted (FloodWait abort, PeerFlood, or the job timeout),
whatever's already been found and validated is flushed to the bot and
persisted to KV before it exits.

Each batch (and the final summary) is sent as a native-monospace,
tap-to-copy bracketed list: `[https://t.me/a,https://t.me/b,...]`.

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
- If `MYANMAR_ONLY=1` (default), its title+about are checked for either
  Myanmar script (Unicode U+1000-U+109F etc, catches Zawgyi too -- it
  reuses codepoints in the same block) OR an English Myanmar-indicator
  keyword (myanmar/burma/burmese/yangon/mandalay/naypyidaw, see
  `MYANMAR_KEYWORD_RE`) -- neither found → dropped as `not_myanmar`. The
  keyword fallback exists because plenty of genuine Myanmar groups (trading/
  business ones especially) name and describe themselves entirely in Latin
  script -- "Myanmar Trading Group" has zero Myanmar Unicode characters
  despite being a Myanmar group; script-only detection would wrongly drop
  it. No title/about text to judge from → treated as passing, not dropped,
  same reasoning as the member-count case. Set `MYANMAR_ONLY=0` to turn
  this off (member-count filtering still applies). This is still script/
  keyword matching, not statistical language detection -- Myanmar's script
  has zero overlap with Latin/Cyrillic, so a character-range + keyword
  check is both faster and more reliable here than a language-ID library,
  and it's deliberately biased toward keeping a url when uncertain rather
  than dropping a real Myanmar group.
- Non-Telegram URLs pass through unvalidated (kept as-is).

Member counts and the about text both come free for not-yet-joined private
invite links (embedded in the `CheckChatInviteRequest` response); for
everything else, one `GetFullChannelRequest` call covers BOTH the
member-count and language checks together -- not two separate calls. All
of this (including the Myanmar-script result) is cached per url in
`url_classifications`, so repeats -- within a run, across groups, across
days -- cost nothing extra.

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
| `start`   | 🚀    | run begins |
| `success` | ✅    | run finished normally (with a filtered-out breakdown if anything was dropped) |
| `flood`   | 🌊    | FloodWait abort, a long FloodWait sleep, or PeerFlood |
| `failed`  | ❌    | any unhandled exception |
| `error`   | ⚠️    | reserved for future use (mid-run degraded-but-continuing conditions) |

Batch url deliveries keep their own 📦 prefix -- they're data, not a status.

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

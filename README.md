# gp-ext-workflow

Extracts URLs from your own Telegram groups/channels on a schedule, stores them in
Cloudflare KV, and serves them via a Cloudflare Worker HTTP endpoint.

## Architecture

```
GitHub Actions (cron, once daily)
    -> extractor/extract.py  (Telethon, logs in with STRING_SESSION)
       - reads "state" (cursors + seen_urls) from Cloudflare KV via REST API
       - scans groups from where it left off last time
       - for each new candidate url, resolves it against the live Telegram
         API (CheckChatInviteRequest / get_entity) and classifies it:
         group (kept), channel (dropped), expired/invalid invite (dropped)
       - stops once 50 NEW *group* urls are found (DAILY_LIMIT)
       - 2s delay between messages, and 2s delay after each validation call
       - catches FloodWaitError and sleeps
       - writes updated "state" and "urls" back to KV via REST API
       - sends the day's new urls to your Telegram Bot chat, with a summary
         of how many channel/expired/invalid links were filtered out

Cloudflare Worker (worker/src/index.js)
    -> GET /urls             reads "urls" key from KV, serves JSON
    -> GET /urls?q=...       filter by substring
    -> GET /urls?group=...   filter by group name
```

The extractor runs on GitHub's runners, not inside the Worker, because the Worker
runtime cannot hold the persistent MTProto connection Telegram's client API needs.

KV is used for two things:
- `state` — cursor per group (last message id processed) + the full set of
  URLs already seen, so each run only pulls what's new and never re-sends
  a duplicate.
- `urls` — the full published dataset the Worker serves over HTTP.

The extractor talks to Cloudflare KV directly over its REST API (`requests`),
so no `wrangler` CLI is needed in that workflow. `wrangler` is only used by
`deploy-worker.yml` to ship the Worker code itself.

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
- Non-Telegram URLs pass through unvalidated (kept as-is).

This costs one extra Telegram API call per *new* URL, so it's worth keeping
`DAILY_LIMIT` reasonable -- it's on top of the message-scanning calls.

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

Both workflows support `workflow_dispatch`, so you can trigger them by hand from
the Actions tab instead of waiting for the cron schedule.

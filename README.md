# gp-ext-workflow

Extracts URLs from your own Telegram groups/channels on a schedule, stores them in
Cloudflare KV, and serves them via a Cloudflare Worker HTTP endpoint.

## Architecture

```
GitHub Actions (cron, once daily)
    -> extractor/extract.py  (Telethon, logs in with STRING_SESSION)
       - reads "state" (cursors + seen_urls) from Cloudflare KV via REST API
       - scans groups from where it left off last time
       - stops once 50 NEW urls are found (DAILY_LIMIT)
       - 2s delay between messages, catches FloodWaitError and sleeps
       - writes updated "state" and "urls" back to KV via REST API
       - sends the day's new urls to your Telegram Bot chat

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

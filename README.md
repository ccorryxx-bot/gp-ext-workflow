# gp-ext-workflow

Extracts URLs from your own Telegram groups/channels on a schedule, stores them in
Cloudflare KV, and serves them via a Cloudflare Worker HTTP endpoint.

## Architecture

```
GitHub Actions (cron, every 6h)
    -> extractor/extract.py  (Telethon, logs in with STRING_SESSION)
    -> writes extractor/output/urls.json
    -> wrangler kv key put   (pushes json into Cloudflare KV)

Cloudflare Worker (worker/src/index.js)
    -> GET /urls             reads from KV, serves JSON
    -> GET /urls?q=...       filter by substring
    -> GET /urls?group=...   filter by group name
```

The extractor runs on GitHub's runners, not inside the Worker, because the Worker
runtime cannot hold the persistent MTProto connection Telegram's client API needs.

## One-time setup

1. Generate a session string locally (do NOT run this in CI):
   ```
   cd extractor
   pip install telethon
   python generate_session.py
   ```
   Paste the printed string into the `STRING_SESSION` GitHub secret.

2. Create a KV namespace:
   ```
   npx wrangler kv namespace create GP_URLS
   ```
   Copy the returned `id` into `worker/wrangler.toml`, and also save it as the
   `CF_KV_NAMESPACE_ID` GitHub secret.

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

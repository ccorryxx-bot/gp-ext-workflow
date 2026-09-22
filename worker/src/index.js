const REPO = "ccorryxx-bot/gp-ext-workflow";
const WORKFLOW_FILE = "extract.yml";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/telegram-webhook") {
      return handleWebhook(request, env);
    }

    if (url.pathname === "/urls") {
      return handleUrls(request, env);
    }

    return new Response(
      "gp-ext-workflow worker is running.\n\nEndpoints:\n  POST /telegram-webhook  (Telegram only)\n  GET  /urls              (extracted urls)",
      { status: 200, headers: { "content-type": "text/plain" } }
    );
  },
};

async function handleWebhook(request, env) {
  // Verify the request really came from Telegram
  const secretHeader = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
  if (env.WEBHOOK_SECRET && secretHeader !== env.WEBHOOK_SECRET) {
    return new Response("Forbidden", { status: 403 });
  }

  let update;
  try {
    update = await request.json();
  } catch {
    return new Response("ok");
  }

  const message = update.message;
  if (!message || !message.text) {
    return new Response("ok");
  }

  const chatId = String(message.chat.id);
  // Only respond to commands from the configured chat/account
  if (env.BOT_CHAT_ID && chatId !== String(env.BOT_CHAT_ID)) {
    return new Response("ok");
  }

  const text = message.text.trim();

  if (text === "/extract") {
    try {
      await triggerWorkflow(env);
      await reply(env, chatId, "🚀 Extraction workflow triggered. GitHub Actions run started.");
    } catch (e) {
      await reply(env, chatId, `❌ Could not trigger workflow: ${e.message}`);
    }
  } else if (text === "/status") {
    const statusText = await getStatus(env);
    await reply(env, chatId, statusText);
  } else if (text === "/help" || text === "/start") {
    await reply(
      env,
      chatId,
      "Commands:\n/extract - run extraction now\n/status - latest run status + total urls\n/help - this message"
    );
  } else {
    await reply(env, chatId, "Unknown command. Try /help");
  }

  return new Response("ok");
}

async function triggerWorkflow(env) {
  const resp = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `token ${env.GH_PAT}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "gp-ext-worker",
      },
      body: JSON.stringify({ ref: "main" }),
    }
  );
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`GitHub dispatch ${resp.status}: ${body.slice(0, 200)}`);
  }
}

async function getStatus(env) {
  let runLine = "Run info unavailable.";
  try {
    const resp = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW_FILE}/runs?per_page=1`,
      {
        headers: {
          Authorization: `token ${env.GH_PAT}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "gp-ext-worker",
        },
      }
    );
    const data = await resp.json();
    const run = data.workflow_runs && data.workflow_runs[0];
    if (run) {
      const conclusion = run.conclusion || "in progress";
      runLine = `Last run: ${run.status} (${conclusion})\nStarted: ${run.run_started_at}\n${run.html_url}`;
    }
  } catch (e) {
    runLine = `Could not fetch run status: ${e.message}`;
  }

  let urlLine = "No dataset yet.";
  try {
    const data = await env.GP_URLS.get("urls", "json");
    if (data) {
      urlLine = `Total urls: ${data.total_urls} across ${data.total_groups} groups\nLast updated: ${data.generated_at}`;
    }
  } catch (e) {
    urlLine = `KV read failed: ${e.message}`;
  }

  return `📊 Status\n\n${runLine}\n\n${urlLine}`;
}

async function reply(env, chatId, text) {
  await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, disable_web_page_preview: true }),
  });
}

async function handleUrls(request, env) {
  const url = new URL(request.url);

  if (env.WORKER_AUTH_TOKEN) {
    const auth = request.headers.get("Authorization");
    if (auth !== `Bearer ${env.WORKER_AUTH_TOKEN}`) {
      return new Response("Unauthorized", { status: 401 });
    }
  }

  const data = await env.GP_URLS.get("urls", "json");
  if (!data) {
    return Response.json({ error: "No data yet. Run the extractor first." }, { status: 404 });
  }

  const q = url.searchParams.get("q");
  const group = url.searchParams.get("group");
  let groups = data.groups;

  if (group) {
    groups = Object.fromEntries(
      Object.entries(groups).filter(([, g]) => g.group_name.toLowerCase().includes(group.toLowerCase()))
    );
  }
  if (q) {
    const filtered = {};
    for (const [gid, g] of Object.entries(groups)) {
      const matches = g.urls.filter((u) => u.toLowerCase().includes(q.toLowerCase()));
      if (matches.length) filtered[gid] = { ...g, urls: matches, count: matches.length };
    }
    groups = filtered;
  }

  return Response.json({
    generated_at: data.generated_at,
    total_groups: Object.keys(groups).length,
    total_urls: Object.values(groups).reduce((sum, g) => sum + g.count, 0),
    groups,
  });
}

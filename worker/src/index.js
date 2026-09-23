const REPO = "ccorryxx-bot/gp-ext-workflow";

// One entry per Telegram account this worker manages. Add a new account by
// adding a key here + its own <KEY>_API_ID/<KEY>_API_HASH/<KEY>_STRING_SESSION
// GitHub secrets + a matching .github/workflows/extract-<key>.yml.
const ACCOUNTS = {
  vsn: { label: "VSN", workflowFile: "extract-vsn.yml" },
  nch: { label: "NCH", workflowFile: "extract-nch.yml" },
};

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
      "gp-ext-workflow worker is running.\n\nEndpoints:\n  POST /telegram-webhook  (Telegram only)\n  GET  /urls?account=vsn|nch  (extracted urls)",
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

  if (update.callback_query) {
    await handleCallbackQuery(update.callback_query, env);
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

  const parts = message.text.trim().split(/\s+/);
  const cmd = parts[0];
  const arg = (parts[1] || "").toLowerCase();

  if (cmd === "/extract") {
    await reply(env, chatId, "ဘယ် account ကို extract run မလဲ?", {
      inline_keyboard: [
        [
          { text: "🟦 VSN", callback_data: "extract:vsn" },
          { text: "🟩 NCH", callback_data: "extract:nch" },
        ],
        [{ text: "🔀 Both", callback_data: "extract:both" }],
      ],
    });
  } else if (cmd === "/status") {
    if (arg === "vsn" || arg === "nch") {
      await reply(env, chatId, await getStatus(env, arg));
    } else {
      const vsn = await getStatus(env, "vsn");
      const nch = await getStatus(env, "nch");
      await reply(env, chatId, `${vsn}\n\n----------\n\n${nch}`);
    }
  } else if (cmd === "/help" || cmd === "/start") {
    await reply(
      env,
      chatId,
      "Commands:\n" +
        "/extract - run extraction now (asks VSN / NCH / Both)\n" +
        "/status [vsn|nch] - latest run status + total urls (both if no arg)\n" +
        "/help - this message"
    );
  } else {
    await reply(env, chatId, "Unknown command. Try /help");
  }

  return new Response("ok");
}

async function handleCallbackQuery(cq, env) {
  const chatId = String(cq.message?.chat?.id || "");
  if (env.BOT_CHAT_ID && chatId !== String(env.BOT_CHAT_ID)) {
    await answerCallback(env, cq.id, "Not authorized");
    return;
  }

  const data = cq.data || "";
  const [action, target] = data.split(":");

  if (action !== "extract") {
    await answerCallback(env, cq.id, "Unknown action");
    return;
  }

  const targets = target === "both" ? ["vsn", "nch"] : [target];
  const validTargets = targets.filter((t) => ACCOUNTS[t]);

  if (!validTargets.length) {
    await answerCallback(env, cq.id, "Unknown account");
    return;
  }

  await answerCallback(env, cq.id, "Triggering...");

  const results = [];
  for (const t of validTargets) {
    try {
      await triggerWorkflow(env, ACCOUNTS[t].workflowFile);
      results.push(`✅ ${ACCOUNTS[t].label}: workflow triggered`);
    } catch (e) {
      results.push(`❌ ${ACCOUNTS[t].label}: ${e.message}`);
    }
  }

  await reply(env, chatId, `🚀 Extraction request:\n\n${results.join("\n")}`);
}

async function triggerWorkflow(env, workflowFile) {
  const resp = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${workflowFile}/dispatches`,
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

async function getStatus(env, account) {
  const acc = ACCOUNTS[account];
  if (!acc) {
    return `❓ Unknown account: ${account}`;
  }

  let runLine = "Run info unavailable.";
  try {
    const resp = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/${acc.workflowFile}/runs?per_page=1`,
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
    const data = await env.GP_URLS.get(`${account}:urls`, "json");
    if (data) {
      urlLine = `Total urls: ${data.total_urls} across ${data.total_groups} groups\nLast updated: ${data.generated_at}`;
    }
  } catch (e) {
    urlLine = `KV read failed: ${e.message}`;
  }

  return `📊 ${acc.label} Status\n\n${runLine}\n\n${urlLine}`;
}

async function reply(env, chatId, text, inlineKeyboard) {
  const body = { chat_id: chatId, text, disable_web_page_preview: true };
  if (inlineKeyboard) {
    body.reply_markup = inlineKeyboard;
  }
  await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function answerCallback(env, callbackQueryId, text) {
  await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/answerCallbackQuery`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ callback_query_id: callbackQueryId, text }),
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

  const account = url.searchParams.get("account");
  if (!account || !ACCOUNTS[account]) {
    return Response.json(
      { error: "Missing or unknown ?account=. Valid values: " + Object.keys(ACCOUNTS).join(", ") },
      { status: 400 }
    );
  }

  const data = await env.GP_URLS.get(`${account}:urls`, "json");
  if (!data) {
    return Response.json({ error: "No data yet. Run the extractor first." }, { status: 404 });
  }

  const q = url.searchParams.get("q");
  const group = url.searchParams.get("group");
  const minMembers = url.searchParams.get("min_members");
  let groups = data.groups;

  const urlOf = (entry) => (typeof entry === "string" ? entry : entry.url);

  if (group) {
    groups = Object.fromEntries(
      Object.entries(groups).filter(([, g]) => g.group_name.toLowerCase().includes(group.toLowerCase()))
    );
  }
  if (q) {
    const filtered = {};
    for (const [gid, g] of Object.entries(groups)) {
      const matches = g.urls.filter((entry) => urlOf(entry).toLowerCase().includes(q.toLowerCase()));
      if (matches.length) filtered[gid] = { ...g, urls: matches, count: matches.length };
    }
    groups = filtered;
  }
  if (minMembers) {
    const min = Number(minMembers);
    const filtered = {};
    for (const [gid, g] of Object.entries(groups)) {
      const matches = g.urls.filter((entry) => typeof entry === "object" && entry.members != null && entry.members >= min);
      if (matches.length) filtered[gid] = { ...g, urls: matches, count: matches.length };
    }
    groups = filtered;
  }

  return Response.json({
    account,
    generated_at: data.generated_at,
    total_groups: Object.keys(groups).length,
    total_urls: Object.values(groups).reduce((sum, g) => sum + g.count, 0),
    groups,
  });
}

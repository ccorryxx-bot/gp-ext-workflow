const REPO = "ccorryxx-bot/gp-ext-workflow";

// One entry per Telegram account this worker manages. Add a new account by
// adding a key here + its own <KEY>_API_ID/<KEY>_API_HASH/<KEY>_STRING_SESSION
// GitHub secrets + a matching .github/workflows/extract-<key>.yml.
const ACCOUNTS = {
  vsn: { label: "VSN", workflowFile: "extract-vsn.yml" },
  nch: { label: "NCH", workflowFile: "extract-nch.yml" },
};

// Single source of truth for every command this bot understands. Telegram's
// slash-command menu (the "Menu" button popup) is NOT auto-detected from this
// webhook code -- it's a separate piece of config on Telegram's side, only
// updated when we explicitly call the setMyCommands Bot API method. Adding a
// command here does nothing to the menu by itself; run /sync_menu afterward
// (or via BotFather) to actually push this list to Telegram.
const BOT_COMMANDS = [
  { command: "start", description: "Show available commands" },
  { command: "help", description: "Show available commands" },
  { command: "extract", description: "Run extraction (asks VSN / NCH / Both)" },
  { command: "status", description: "Latest run status + total urls" },
  { command: "skipped", description: "List manually-skipped groups" },
  { command: "sync_menu", description: "Re-sync this command list to Telegram's menu" },
];

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/telegram-webhook") {
      return handleWebhook(request, env);
    }

    if (url.pathname === "/urls") {
      return handleUrls(request, env);
    }

    if (url.pathname === "/status") {
      return handleLiveStatus(request, env);
    }

    return new Response(
      "gp-ext-workflow worker is running.\n\nEndpoints:\n  POST /telegram-webhook  (Telegram only)\n  GET  /urls?account=vsn|nch  (extracted urls)\n  GET  /status?account=vsn|nch  (live run progress)",
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
  } else if (cmd === "/skipped") {
    const accts = arg === "vsn" || arg === "nch" ? [arg] : ["vsn", "nch"];
    for (const a of accts) {
      await sendSkippedList(env, chatId, a);
    }
  } else if (cmd === "/help" || cmd === "/start") {
    const list = BOT_COMMANDS.filter((c) => c.command !== "start")
      .map((c) => `/${c.command} - ${c.description}`)
      .join("\n");
    await reply(
      env,
      chatId,
      `Commands:\n${list}\n\n` +
        "During a run, the live status message has a '⏭ Skip this group' button -- " +
        "tap it to permanently exclude whatever group is currently being scanned " +
        "from all future runs (manual decision, not automatic)."
    );
  } else if (cmd === "/sync_menu") {
    try {
      await syncBotCommands(env);
      await reply(env, chatId, `✅ Menu synced -- ${BOT_COMMANDS.length} commands pushed to Telegram.`);
    } catch (e) {
      await reply(env, chatId, `❌ Menu sync failed: ${e.message}`);
    }
  } else {
    await reply(env, chatId, "Unknown command. Try /help");
  }

  return new Response("ok");
}

async function sendSkippedList(env, chatId, account) {
  const key = `${account}:excluded_groups`;
  const excluded = (await env.GP_URLS.get(key, "json")) || {};
  const entries = Object.entries(excluded);
  const label = ACCOUNTS[account].label;
  if (!entries.length) {
    await reply(env, chatId, `${label}: skip လုပ်ထားတဲ့ group မရှိသေးပါဘူး။`);
    return;
  }
  const lines = entries.map(
    ([gid, info], i) => `${i + 1}. ${info.name || gid} (skipped ${(info.excluded_at || "").slice(0, 10) || "?"})`
  );
  const buttons = entries.map(([gid, info]) => [
    { text: `♻️ Unskip: ${(info.name || gid).slice(0, 30)}`, callback_data: `unskip:${account}:${gid}` },
  ]);
  await reply(env, chatId, `${label} skipped groups:\n\n${lines.join("\n")}`, { inline_keyboard: buttons });
}

async function handleCallbackQuery(cq, env) {
  const chatId = String(cq.message?.chat?.id || "");
  if (env.BOT_CHAT_ID && chatId !== String(env.BOT_CHAT_ID)) {
    await answerCallback(env, cq.id, "Not authorized");
    return;
  }

  const data = cq.data || "";
  const [action] = data.split(":");

  if (action === "extract") {
    await handleExtractCallback(cq, env, chatId, data);
  } else if (action === "skip") {
    await handleSkipCallback(cq, env, chatId, data, true);
  } else if (action === "unskip") {
    await handleSkipCallback(cq, env, chatId, data, false);
  } else {
    await answerCallback(env, cq.id, "Unknown action");
  }
}

async function handleExtractCallback(cq, env, chatId, data) {
  const [, target] = data.split(":");
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

async function handleSkipCallback(cq, env, chatId, data, excluding) {
  // callback_data: "skip:<account>:<gid>" or "unskip:<account>:<gid>". gid
  // itself is a plain signed integer (Telegram chat id), no colons in it,
  // so a straight split is safe.
  const parts = data.split(":");
  const account = parts[1];
  const gid = parts[2];

  if (!ACCOUNTS[account] || !gid) {
    await answerCallback(env, cq.id, "Bad request");
    return;
  }

  const key = `${account}:excluded_groups`;
  const current = (await env.GP_URLS.get(key, "json")) || {};

  if (excluding) {
    // Best-effort label from whatever the live snapshot currently shows for
    // this group -- purely cosmetic for /skipped, not load-bearing (the
    // extractor only ever checks gid membership, never the name).
    let label = gid;
    try {
      const live = await env.GP_URLS.get(`${account}:live_status`, "json");
      if (live && String(live.current_group_id) === String(gid) && live.current_group) {
        label = live.current_group;
      }
    } catch {
      // non-fatal -- fall back to the bare gid as the label
    }
    current[gid] = { name: label, excluded_at: new Date().toISOString() };
    await env.GP_URLS.put(key, JSON.stringify(current));
    await answerCallback(env, cq.id, `⏭ Skipped: ${label}`);
    await reply(env, chatId, `⏭ "${label}" ကို နောက် run တွေမှာ scan မလုပ်တော့ပါဘူး (/skipped ${account} နဲ့ ပြန်ကြည့်/ပြန်ဖျက်လို့ရတယ်)။`);
  } else {
    const label = current[gid]?.name || gid;
    delete current[gid];
    await env.GP_URLS.put(key, JSON.stringify(current));
    await answerCallback(env, cq.id, `♻️ Unskipped: ${label}`);
    // Refresh the list message in place so removed entries disappear immediately.
    await sendSkippedList(env, chatId, account);
  }
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

  let liveLine = "";
  try {
    // Single extra KV read -- written far more often than KV_STATE_KEY/KV_URLS_KEY
    // (which only get persisted at batch/end/interrupt), so this is what makes a
    // mid-run /status show what's happening RIGHT NOW instead of stale totals.
    const live = await env.GP_URLS.get(`${account}:live_status`, "json");
    if (live) liveLine = formatLiveStatus(live);
  } catch (e) {
    liveLine = `Live status read failed: ${e.message}`;
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

  return `📊 ${acc.label} Status\n\n${runLine}${liveLine ? "\n\n" + liveLine : ""}\n\n${urlLine}`;
}

const LIVE_STATUS_LABEL = {
  start: "🚀 starting",
  scanning: "🔎 scanning",
  flood: "🌊 flood-paused / aborted",
  timeout: "⏰ self-stopped (time budget)",
  idle: "✅ finished",
  failed: "❌ failed",
};

function formatLiveStatus(live) {
  const label = LIVE_STATUS_LABEL[live.status] || live.status || "unknown";
  const lines = [`Live: ${label}`];
  if (live.current_group) {
    const members =
      live.current_group_members != null ? `${live.current_group_members.toLocaleString()} members` : "members unknown";
    lines.push(`Group #${live.dialog_number ?? "?"}: ${live.current_group} (${members})`);
    lines.push(`Scanned in this group: ${live.messages_scanned_this_group ?? 0}`);
    // Raw url-bearing message count still unscanned in THIS group (not the
    // validated/kept count below, which is cumulative for the whole run) --
    // see extract.py's count_raw_url_messages for why these are kept separate.
    lines.push(
      `Total urls found: ${live.current_group_raw_url_count != null ? live.current_group_raw_url_count.toLocaleString() : "unknown"}`
    );
  }
  lines.push(`Total messages scanned this run: ${live.total_messages_scanned ?? 0}`);
  lines.push(`Total urls kept so far this run (validated): ${live.total_urls_found_this_run ?? 0}`);
  if (live.estimated_next_group_at) {
    lines.push(`Est. next group swap: ~${new Date(live.estimated_next_group_at).toUTCString()} (rough estimate, not exact)`);
  }
  if (live.updated_at) {
    lines.push(`(as of ${new Date(live.updated_at).toUTCString()})`);
  }
  return lines.join("\n");
}

async function handleLiveStatus(request, env) {
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

  // One KV read, nothing else -- no Telegram/GitHub API calls needed to see
  // what an in-progress run is currently doing.
  const live = await env.GP_URLS.get(`${account}:live_status`, "json");
  if (!live) {
    return Response.json({ error: "No live status yet. Run the extractor first." }, { status: 404 });
  }
  return Response.json({ account, ...live });
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

async function syncBotCommands(env) {
  const resp = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/setMyCommands`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ commands: BOT_COMMANDS }),
  });
  const data = await resp.json();
  if (!data.ok) {
    throw new Error(data.description || `HTTP ${resp.status}`);
  }
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

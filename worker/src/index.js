export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/urls") {
      // Optional bearer-token protection. Set WORKER_AUTH_TOKEN via:
      //   wrangler secret put WORKER_AUTH_TOKEN
      if (env.WORKER_AUTH_TOKEN) {
        const auth = request.headers.get("Authorization");
        if (auth !== `Bearer ${env.WORKER_AUTH_TOKEN}`) {
          return new Response("Unauthorized", { status: 401 });
        }
      }

      const data = await env.GP_URLS.get("urls", "json");
      if (!data) {
        return Response.json({ error: "No data yet. Run the extractor workflow first." }, { status: 404 });
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
          if (matches.length) {
            filtered[gid] = { ...g, urls: matches, count: matches.length };
          }
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

    return new Response(
      "gp-ext-workflow worker is running.\n\nEndpoints:\n  GET /urls            -> all extracted urls\n  GET /urls?q=xxx      -> filter urls containing xxx\n  GET /urls?group=xxx  -> filter by group name",
      { status: 200, headers: { "content-type": "text/plain" } }
    );
  },
};

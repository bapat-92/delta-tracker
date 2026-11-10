// Serves the static page and proxies public ticker requests to Delta India.
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) {
      const path = url.pathname.slice(5);
      if (request.method !== "GET" || !path.startsWith("v2/tickers")) {
        return new Response("Not allowed", { status: 403 });
      }
      const up = await fetch("https://api.india.delta.exchange/" + path + url.search, {
        headers: { Accept: "application/json", "User-Agent": "delta-tracker/1.0" },
      });
      return new Response(up.body, {
        status: up.status,
        headers: { "content-type": "application/json", "cache-control": "public, max-age=2" },
      });
    }
    return env.ASSETS.fetch(request);
  },
};

// Serves the static page, stores your tracker data in Cloudflare KV (behind a passphrase),
// and keeps the old ticker proxy route.
const KEYS = new Set(["legs", "journal", "series", "cfg"]);
const HEAD = { "content-type": "application/json", "cache-control": "no-store" };
const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: HEAD });

// Compare secrets by hashing both, so the comparison time doesn't leak anything.
async function sameSecret(a, b) {
  const enc = new TextEncoder();
  const [x, y] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  const u = new Uint8Array(x), v = new Uint8Array(y);
  let d = 0;
  for (let i = 0; i < u.length; i++) d |= u[i] ^ v[i];
  return d === 0;
}

async function handleState(request, env, url) {
  const missing = [!env.STORE && "KV binding STORE", !env.APP_PASSPHRASE && "secret APP_PASSPHRASE"].filter(Boolean);
  if (missing.length) return json({ error: "Worker is missing: " + missing.join(" and ") }, 503);
  const auth = request.headers.get("Authorization") || "";
  const given = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!(await sameSecret(given, env.APP_PASSPHRASE))) {
    await new Promise((r) => setTimeout(r, 400));   // slow down guessing
    return json({ error: "unauthorized" }, 401);
  }
  if (request.method === "GET" && url.pathname === "/api/state") {
    const out = {};
    await Promise.all([...KEYS].map(async (k) => {
      const v = await env.STORE.get("state:" + k);
      out[k] = v === null ? null : JSON.parse(v);
    }));
    return json(out);
  }
  const key = url.pathname.slice("/api/state/".length);
  if (!KEYS.has(key)) return json({ error: "not found" }, 404);
  if (request.method === "GET") {
    const v = await env.STORE.get("state:" + key);
    return json(v === null ? null : JSON.parse(v));
  }
  if (request.method === "PUT") {
    const text = await request.text();
    if (text.length > 900000) return json({ error: "too large" }, 413);
    try { JSON.parse(text); } catch (e) { return json({ error: "bad json" }, 400); }
    await env.STORE.put("state:" + key, text);
    return json({ ok: true });
  }
  return json({ error: "method not allowed" }, 405);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/api/state" || url.pathname.startsWith("/api/state/")) {
      return handleState(request, env, url);
    }
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

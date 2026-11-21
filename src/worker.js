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

async function authorize(request, env) {
  const missing = [!env.STORE && "KV binding STORE", !env.APP_PASSPHRASE && "secret APP_PASSPHRASE"].filter(Boolean);
  if (missing.length) return json({ error: "Worker is missing: " + missing.join(" and ") }, 503);
  const auth = request.headers.get("Authorization") || "";
  const given = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!(await sameSecret(given, env.APP_PASSPHRASE))) {
    await new Promise((r) => setTimeout(r, 400));   // slow down guessing
    return json({ error: "unauthorized" }, 401);
  }
  return null;
}

// Saved option-price history, one KV key per expiry (public Delta prices; same passphrase as the rest).
async function handleCandles(request, env, url) {
  const bad = await authorize(request, env);
  if (bad) return bad;
  const code = url.pathname.slice("/api/candles/".length);
  if (!/^\d{6}$/.test(code)) return json({ error: "bad expiry code" }, 400);
  if (request.method === "GET") {
    const v = await env.STORE.get("candles:" + code);          // returned as stored: parsing a multi-MB value would use too much CPU
    return new Response(v === null ? "null" : v, { headers: HEAD });
  }
  if (request.method === "PUT") {
    const text = await request.text();
    if (text.length > 20000000) return json({ error: "too large" }, 413);
    if (text[0] !== "{") return json({ error: "bad json" }, 400);
    await env.STORE.put("candles:" + code, text);
    return json({ ok: true });
  }
  return json({ error: "method not allowed" }, 405);
}

// Daily backtest report written by the GitHub Actions job (small JSON), read by the page.
async function handleBacktest(request, env, url) {
  const bad = await authorize(request, env);
  if (bad) return bad;
  const name = (url.searchParams.get("name") || "latest").replace(/[^0-9A-Za-z_-]/g, "").slice(0, 40) || "latest";   // "latest" or a date range like 2026-05-01_2026-05-31
  if (request.method === "GET") {
    const v = await env.STORE.get("backtest:" + name);
    return new Response(v === null ? "null" : v, { headers: HEAD });
  }
  if (request.method === "PUT") {
    const text = await request.text();
    if (text.length > 900000) return json({ error: "too large" }, 413);
    try { JSON.parse(text); } catch (e) { return json({ error: "bad json" }, 400); }
    await env.STORE.put("backtest:" + name, text);
    return json({ ok: true });
  }
  return json({ error: "method not allowed" }, 405);
}

// Starts the GitHub "daily-backtest" workflow with the settings typed on the page.
// Needs the Worker secret GH_TOKEN (fine-grained token, only this repo, permission Actions: read and write). Optional variable GH_REPO (default below).
const RUN_FIELDS = { from_date: /^\d{4}-\d{2}-\d{2}$/, to_date: /^\d{4}-\d{2}-\d{2}$/, capital: /^\d{1,9}(\.\d+)?$/, share: /^\d{1,3}(\.\d+)?$/, ratio: /^\d{1,2}(\.\d+)?$/, spread: /^\d{1,2}(\.\d+)?$/, minpct: /^\d{1,3}(\.\d+)?$/, hours: /^\d{1,2}(\.\d+)?(,\d{1,2}(\.\d+)?){0,11}$/ };
async function handleRun(request, env, url) {
  const bad = await authorize(request, env);
  if (bad) return bad;
  if (!env.GH_TOKEN) return json({ error: "Worker is missing: secret GH_TOKEN (see the setup note)" }, 503);
  const token = String(env.GH_TOKEN).trim();
  if (url.searchParams.get("check")) {              // diagnosis only: shows what the Worker holds (never the token itself) and what GitHub says to it
    const g = await fetch("https://api.github.com/rate_limit", { headers: { Authorization: "Bearer " + token, Accept: "application/vnd.github+json", "User-Agent": "delta-tracker" } });
    return json({ token_length: token.length, token_starts_with: token.slice(0, 11), had_spaces_or_newlines: token !== String(env.GH_TOKEN), github_status: g.status, github_says: g.status === 200 ? "token accepted" : (await g.text()).slice(0, 120) });
  }
  if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
  let body; try { body = await request.json(); } catch (e) { return json({ error: "bad json" }, 400); }
  const inputs = {};
  for (const [k, re] of Object.entries(RUN_FIELDS)) {
    const v = String(body[k] ?? "").trim();
    if (v === "") continue;
    if (!re.test(v)) return json({ error: "bad value for " + k }, 400);
    inputs[k] = v;
  }
  const repo = env.GH_REPO || "bapat-92/delta-tracker";
  const r = await fetch("https://api.github.com/repos/" + repo + "/actions/workflows/daily-backtest.yml/dispatches", {
    method: "POST",
    headers: { Authorization: "Bearer " + token, Accept: "application/vnd.github+json", "User-Agent": "delta-tracker", "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json" },
    body: JSON.stringify({ ref: env.GH_REF || "main", inputs }),
  });
  if (r.status === 204) return json({ ok: true });
  return json({ error: "GitHub answered " + r.status + ": " + (await r.text()).slice(0, 200) }, 502);
}

async function handleState(request, env, url) {
  const bad = await authorize(request, env);
  if (bad) return bad;
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
    if (url.pathname === "/api/run") return handleRun(request, env, url);
    if (url.pathname === "/api/backtest") {
      return handleBacktest(request, env, url);
    }
    if (url.pathname.startsWith("/api/candles/")) {
      return handleCandles(request, env, url);
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

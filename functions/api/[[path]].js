// Cloudflare Pages Function: forwards public ticker requests to Delta India.
// Served from the same site as the page, so no CORS setup is needed.
export async function onRequestGet({ request, params }) {
  const path = Array.isArray(params.path) ? params.path.join("/") : (params.path || "");
  if (!path.startsWith("v2/tickers")) {
    return new Response("Not allowed", { status: 403 });
  }
  const search = new URL(request.url).search;
  const up = await fetch("https://api.india.delta.exchange/" + path + search, {
    headers: { Accept: "application/json" },
  });
  return new Response(up.body, {
    status: up.status,
    headers: { "content-type": "application/json", "cache-control": "public, max-age=2" },
  });
}

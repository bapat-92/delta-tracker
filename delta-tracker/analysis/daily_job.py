#!/usr/bin/env python3
"""
Daily backtest job (run by GitHub Actions, see .github/workflows/daily-backtest.yml).

1. Downloads the 1-minute option mark-price candles for the last N finished expiries from Delta (only what the cache doesn't already have).
2. Runs the frozen strategy (strategy_config.json) with batman_backtest.py.
3. Sends the summary to your tracker (PUT /api/backtest, behind your passphrase) so the page can show it.

Environment: TRACKER_URL, TRACKER_PASSPHRASE (GitHub secrets); optional CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET if the site sits behind Cloudflare Access;
optional FROM_DATE / TO_DATE (YYYY-MM-DD, a fixed range; blank = the last DAYS days), optional DELTA_BASE (default https://api.india.delta.exchange), DAYS (default 30), CACHE_DIR (default .cache/candles).
Nothing here places orders or needs an exchange key: it only reads public prices.
"""
import os, sys, json, gzip, time, math, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import requests, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import batman_backtest as B

BASE = os.environ.get("DELTA_BASE", "https://api.india.delta.exchange").rstrip("/")
DAYS = int(os.environ.get("DAYS", "30")); CACHE = os.environ.get("CACHE_DIR", ".cache/candles"); STEP = 200
HERE = os.path.dirname(os.path.abspath(__file__)); CFG = json.load(open(os.path.join(HERE, "strategy_config.json")))
S = requests.Session(); S.headers["User-Agent"] = "delta-tracker-backtest/1.0"

def get(path, params, tries=5):
    for k in range(tries):
        try:
            r = S.get(BASE + path, params=params, timeout=30)
            if r.status_code == 200: return r.json().get("result") or []
            if r.status_code not in (429, 500, 502, 503, 504): return None
        except requests.RequestException: pass
        time.sleep(1.5 * (k + 1))
    raise RuntimeError("download failed: " + path + " " + str(params.get("symbol")))

def candles(sym, a, b, res="1m"):
    out = get("/v2/history/candles", dict(resolution=res, symbol="MARK:" + sym, start=a, end=b))
    if out is not None and len(out) < 3: out = get("/v2/history/candles", dict(resolution=res, symbol=sym, start=a, end=b)) or out
    return [[int(c["time"]), float(c["open"]), float(c["close"])] for c in sorted(out or [], key=lambda c: c["time"]) if float(c["close"]) > 0]

def fetch_expiry(day):
    """All strikes within +-10% of BTC at the start of the window (12:00 UTC the day before) for one expiry."""
    d = dt.date.fromisoformat(day); code = d.strftime("%d%m%y"); tx = int(dt.datetime(d.year, d.month, d.day, 12, tzinfo=dt.timezone.utc).timestamp()); wa = tx - 86400
    path = os.path.join(CACHE, code + ".json.gz")
    if os.path.exists(path):
        with gzip.open(path, "rt") as f: return json.load(f)
    btc = get("/v2/history/candles", dict(resolution="5m", symbol="BTCUSD", start=wa, end=wa + 3600))
    if not btc: raise RuntimeError("no BTC price for " + day)
    spot = float(btc[0]["open"]); lo = math.floor(spot * 0.9 / STEP) * STEP; hi = math.ceil(spot * 1.1 / STEP) * STEP
    syms = [f"{t}-BTC-{k}-{code}" for k in range(lo, hi + 1, STEP) for t in "CP"]
    with ThreadPoolExecutor(3) as ex: res = list(ex.map(lambda s: (s, candles(s, wa, tx + 60)), syms))
    series = {s: c for s, c in res if len(c) >= 3}
    if len(series) < 20: raise RuntimeError(f"too few option series for {day}: {len(series)}")
    os.makedirs(CACHE, exist_ok=True)
    with gzip.open(path, "wt") as f: json.dump(series, f)
    return series

def main():
    now = int(time.time()); today = dt.datetime.now(dt.timezone.utc).date()
    f, t = os.environ.get("FROM_DATE", "").strip(), os.environ.get("TO_DATE", "").strip(); days = []
    if f or t:                                   # a chosen date range, e.g. 2026-05-01 to 2026-05-31
        d0 = dt.date.fromisoformat(f or t); d1 = dt.date.fromisoformat(t or f)
        if d1 < d0: raise SystemExit("TO_DATE is before FROM_DATE")
        if (d1 - d0).days > 120: raise SystemExit("range too long (max 120 days per run)")
        d = d0
        while d <= d1: days.append(d.isoformat()); d += dt.timedelta(days=1)
    else:
        for k in range(DAYS, -1, -1): days.append((today - dt.timedelta(days=k)).isoformat())
        days = days[-DAYS:]
    days = [d for d in days if int(dt.datetime.fromisoformat(d).replace(hour=12, tzinfo=dt.timezone.utc).timestamp()) < now - 1800]
    name = f"{days[0]}_{days[-1]}" if (f or t) and days else "latest"; E = {}; failed = []
    for day in days:
        try: E[day] = B.E_from_series(day, fetch_expiry(day)); print("ok", day, len(E[day]["syms"]), "series", flush=True)
        except Exception as e: failed.append(day); print("FAILED", day, e, flush=True)
    if not E: raise SystemExit("no data downloaded; check that this runner can reach " + BASE)
    hours = [float(x) for x in str(CFG["hours"]).split(",")]
    cap = dict(capital=CFG["capital"], share=CFG["share"], ratio=CFG["ratio"]) if CFG.get("capital") else None
    rows = []
    for day, e in E.items(): rows += B.work((day, e, hours, 0, 0, CFG["lev"], CFG["minpct"], CFG["spread"] / 100, cap))
    import pandas as pd
    df = pd.DataFrame(rows); ok = df[df["final"].notna()] if "final" in df else df.iloc[0:0]
    summ = []
    for h, g in ok.groupby("hours_before"):
        summ.append(dict(hours_before=h, days=int(len(g)), win_pct=round((g.final > 0).mean() * 100), total=round(g.final.sum()), avg=round(g.final.mean()), worst_final=round(g.final.min()), worst_open=round(g.worst.min()),
                         hit10=int((g.hit10 > 0).sum()), hit20=int((g.hit20 > 0).sum()), hit30=int((g.hit30 > 0).sum()), margin=round(g.margin.mean())))
    latest = ok.sort_values("expiry").tail(40)[["expiry", "hours_before", "spot", "settle", "credit", "final", "worst", "worst_pct"]].round(1).to_dict("records") if len(ok) else []
    report = dict(name=name, computed_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), config=CFG, expiries=sorted(E), failed=failed, by_hour=sorted(summ, key=lambda x: -x["hours_before"]), latest=latest)
    print(json.dumps(report["by_hour"], indent=1))
    url, pw = os.environ.get("TRACKER_URL", "").rstrip("/"), os.environ.get("TRACKER_PASSPHRASE", "")
    if not url or not pw: print("TRACKER_URL / TRACKER_PASSPHRASE not set: report not uploaded"); return
    h = {"Authorization": "Bearer " + pw, "Content-Type": "application/json"}
    if os.environ.get("CF_ACCESS_CLIENT_ID"): h.update({"CF-Access-Client-Id": os.environ["CF_ACCESS_CLIENT_ID"], "CF-Access-Client-Secret": os.environ["CF_ACCESS_CLIENT_SECRET"]})
    r = requests.put(url + "/api/backtest?name=" + name, data=json.dumps(report), headers=h, timeout=30)
    print("upload:", r.status_code, r.text[:200]); r.raise_for_status()

if __name__ == "__main__": main()

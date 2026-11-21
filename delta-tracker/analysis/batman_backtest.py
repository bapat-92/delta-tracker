#!/usr/bin/env python3
"""
Inverted Batman backtest on saved Delta 1-minute option candles (CSV exported from the tracker).

Replicates the tracker's batmanSearch():
  bought strikes: calls spot..+3%, puts -3%..spot;  sold strikes: calls +0..+8%, puts -8%..spot
  best = widest profit range (contiguous around spot at expiry) that still earns >= minpct% of margin; ties -> larger credit
  margin = sold * 2 * 0.001 * spot / leverage
Prices: the candle open at the entry minute, with a half-spread (buy at +spread, sell at -spread).
Spot is inferred from put-call parity on the marks (the CSV has option prices only).
Fees: Delta formula (1.18 * min(0.01% notional, 3.5% premium)) on each order, settlement fee on ITM expiry.

Usage:
  python3 batman_backtest.py --csv candles.csv --validate            # last 7 expiries, entry 14:00 UTC (= 15:00 CET) and 05:00 UTC
  python3 batman_backtest.py --csv candles.csv --scan                # every entry hour, all days
"""
import argparse, sys, os, time
import numpy as np, pandas as pd
from multiprocessing import Pool

Q = 0.001
def fee_opt(lots, px, spot): return 1.18 * min(0.0001 * lots * Q * spot, 0.035 * px * lots * Q)
def settle_fee(qb, iv, S): return 1.18 * min(0.00015 * qb * S, 0.05 * iv * qb)

# ---------------------------------------------------------------- loading
def load(csv):
    df = pd.read_csv(csv, usecols=["expiry_day", "symbol", "type", "strike", "time_epoch", "open", "close"])
    out = {}
    for day, g in df.groupby("expiry_day"):
        t0, t1 = int(g.time_epoch.min()), int(g.time_epoch.max())
        times = np.arange(t0, t1 + 60, 60)
        op = g.pivot_table(index="time_epoch", columns="symbol", values="open", aggfunc="last").reindex(times)
        cl = g.pivot_table(index="time_epoch", columns="symbol", values="close", aggfunc="last").reindex(times)
        cl = cl.ffill(); op = op.fillna(cl.shift(1)).ffill()
        meta = g.drop_duplicates("symbol").set_index("symbol")[["type", "strike"]]
        syms = list(cl.columns)
        out[day] = dict(times=times, op=op.values, cl=cl.values, syms=syms, typ=np.array([meta.loc[s, "type"] for s in syms]),
                        strike=np.array([meta.loc[s, "strike"] for s in syms], float), tx=int(pd.Timestamp(day + " 12:00", tz="UTC").timestamp()))
    return out

def E_from_series(day, series):
    """series: {symbol: [[t, open, close], ...]} -> the same structure load() builds from the CSV."""
    syms = sorted(series, key=lambda x: (x[0], int(x.split("-")[2])))
    t0 = min(series[s][0][0] for s in syms if series[s]); t1 = max(series[s][-1][0] for s in syms if series[s])
    times = np.arange(t0, t1 + 60, 60); n = len(times)
    op = np.full((n, len(syms)), np.nan); cl = np.full((n, len(syms)), np.nan)
    for j, sy in enumerate(syms):
        a = np.array(series[sy], float)
        if not len(a): continue
        i = ((a[:, 0] - t0) // 60).astype(int); keep = (i >= 0) & (i < n)
        op[i[keep], j] = a[keep, 1]; cl[i[keep], j] = a[keep, 2]
    cl = pd.DataFrame(cl).ffill(); op = pd.DataFrame(op).fillna(cl.shift(1)).ffill()
    return dict(times=times, op=op.values, cl=cl.values, syms=syms, typ=np.array([s[0] for s in syms]), strike=np.array([float(s.split("-")[2]) for s in syms]),
                tx=int(pd.Timestamp(day + " 12:00", tz="UTC").timestamp()))

def spot_series(E):
    """Spot per minute from put-call parity: S = K + C - P at the strike where C ~ P (marks, end-of-minute close)."""
    cl, typ, K = E["cl"], E["typ"], E["strike"]
    cs = {k: i for i, (t, k) in enumerate(zip(typ, K)) if t == "C"}; ps = {k: i for i, (t, k) in enumerate(zip(typ, K)) if t == "P"}
    ks = sorted(set(cs) & set(ps)); ci = [cs[k] for k in ks]; pi = [ps[k] for k in ks]
    C, P, KK = cl[:, ci], cl[:, pi], np.array(ks)
    d = np.abs(C - P); d = np.where(np.isnan(d), 1e18, d)
    j = d.argmin(axis=1); r = np.arange(len(C))
    return KK[j] + C[r, j] - P[r, j]          # spot at the END of each minute row (candle close)

# ---------------------------------------------------------------- search (port of batmanSearch)
def search(E, row, S, sold, bought, lev, minpct, spr, balanced=False):
    typ, K, o = E["typ"], E["strike"], E["op"][row]
    ok = ~np.isnan(o) & (o > 0)
    bid, ask = o * (1 - spr), o * (1 + spr)
    gs = max(5, round(S * 0.0002 / 5) * 5)
    grid = np.arange(S * 0.85, S * 1.15 + 1e-9, gs); iS = int(np.abs(grid - S).argmin())
    isC, isP = typ == "C", typ == "P"
    lcs = np.where(ok & isC & (K >= S) & (K <= S * 1.03))[0]; scs = np.where(ok & isC & (K > S) & (K <= S * 1.08))[0]
    lps = np.where(ok & isP & (K <= S) & (K >= S * 0.97))[0]; sps = np.where(ok & isP & (K < S) & (K >= S * 0.92))[0]
    def pairs(lg_i, sh_i, call):
        res = []
        for lg in lg_i:
            for sh in sh_i:
                if (call and K[sh] > K[lg]) or ((not call) and K[sh] < K[lg]):
                    credit = (bid[sh] * sold - ask[lg] * bought) * Q
                    a = np.maximum(grid - K[sh], 0) if call else np.maximum(K[sh] - grid, 0)
                    b = np.maximum(grid - K[lg], 0) if call else np.maximum(K[lg] - grid, 0)
                    res.append((lg, sh, credit, credit - a * sold * Q + b * bought * Q))
        return res
    cps, pps = pairs(lcs, scs, True), pairs(lps, sps, False)
    if not cps or not pps: return None
    margin = sold * 2 * Q * S / lev; thr = margin * minpct / 100
    PA = np.stack([p[3] for p in pps]); G = len(grid); best = None
    for (clg, csh, ccr, carr) in cps:
        T = carr[None, :] + PA
        m = T >= thr
        good = m[:, iS]
        if not good.any(): continue
        idx = np.where(good)[0]; mm = m[idx]
        left = mm[:, :iS][:, ::-1]; right = mm[:, iS + 1:]
        lbad = np.where(left.all(axis=1), iS, np.argmax(~left, axis=1))      # steps we can walk left
        rbad = np.where(right.all(axis=1), G - 1 - iS, np.argmax(~right, axis=1))
        width = grid[iS + rbad] - grid[iS - lbad]
        for k, (q, w) in enumerate(zip(idx, width)):
            cr = ccr + pps[q][2]; bal = min(grid[iS] - grid[iS - lbad[k]], grid[iS + rbad[k]] - grid[iS])
            key = (bal, w, cr) if balanced else (w, cr)         # balanced: the smaller side as large as possible, then width, then credit
            if best is None or key > best["key"]:
                best = dict(key=key, width=w, credit=cr, bal=bal, c=(clg, csh), p=(pps[q][0], pps[q][1]), lo=grid[iS - lbad[k]], hi=grid[iS + rbad[k]], margin=margin)
    return best

# ---------------------------------------------------------------- simulate one entry
def simulate(E, spot, row, sold, bought, lev, minpct, spr, levels=(10, 20, 30), cap=None, balanced=False):
    S = spot[row - 1] if row > 0 else spot[row]        # spot at the entry minute (end of previous candle)
    if cap:                                            # size the lots from capital like the tracker: margin = capital * share%
        per = Q * S / lev; sold = int(cap["capital"] * cap["share"] / 100 / (2 * per) / 10) * 10
        if sold < 10: return None
        bought = max(1, round(sold * cap["ratio"]))
    b = search(E, row, S, sold, bought, lev, minpct, spr, balanced)
    if b is None: return None
    K, typ, o, cl = E["strike"], E["typ"], E["op"][row], E["cl"]
    legs = [(b["c"][1], -1, sold, o[b["c"][1]] * (1 - spr)), (b["c"][0], 1, bought, o[b["c"][0]] * (1 + spr)),
            (b["p"][1], -1, sold, o[b["p"][1]] * (1 - spr)), (b["p"][0], 1, bought, o[b["p"][0]] * (1 + spr))]
    fees = sum(fee_opt(n, e, S) for _, _, n, e in legs)
    idx = [l[0] for l in legs]; side = np.array([l[1] for l in legs]); n = np.array([l[2] for l in legs]); e = np.array([l[3] for l in legs])
    rows = np.arange(row + 1, len(E["times"]))                       # marks at the end of each following minute
    marks = cl[rows][:, idx]
    pnl = ((marks - e) * side * n * Q).sum(axis=1) - fees
    Sx = spot[-1]; intr = np.array([max(Sx - K[i], 0) if typ[i] == "C" else max(K[i] - Sx, 0) for i in idx])
    fin = float(((intr - e) * side * n * Q).sum() - fees - sum(settle_fee(nn * Q, v, Sx) for nn, v in zip(n, intr) if v > 0))
    path = pnl[:-1] if len(pnl) > 1 else pnl                         # before expiry
    margin = b["margin"]; res = dict(sold=sold, bought=bought, spot=S, settle=Sx, cp=K[b["p"][1]], cpl=K[b["p"][0]], cc=K[b["c"][0]], ccs=K[b["c"][1]], lo=b["lo"], hi=b["hi"], credit=b["credit"], margin=margin, fees=fees, final=fin,
                                     worst=float(path.min()), worst_pct=float(path.min() / margin * 100), worst_min_after=int(path.argmin()) + 1)
    for L in levels:
        hit = np.where(path <= -L / 100 * margin)[0]; res[f"hit{L}"] = int(hit[0]) + 1 if len(hit) else -1
    # exit rule: close all legs the first minute spot is outside the profit range
    sp = spot[rows[:-1]] if len(rows) > 1 else spot[rows]
    out = np.where((sp < b["lo"]) | (sp > b["hi"]))[0]
    if len(out):
        i = out[0]; ex = 0.0
        for j, (li, sd, nn, ee) in enumerate(legs):
            px = marks[i, j] * (1 + spr) if sd == -1 else marks[i, j] * (1 - spr)
            ex += sd * (px - ee) * nn * Q - fee_opt(nn, px, sp[i])
        res["exit_final"] = float(ex - fees); res["exit_min"] = i + 1
    else:
        res["exit_final"] = fin; res["exit_min"] = -1
    return res

def work(args):
    day, E, hours, sold, bought, lev, minpct, spr, cap = args[:9]; balanced = bool(args[9]) if len(args) > 9 else False
    spot = spot_series(E); res = []
    for h in hours:            # h = entry time as hours before expiry (12:00 UTC)
        t0 = E["tx"] - int(h * 3600); row = int((t0 - E["times"][0]) // 60)
        if row < 1 or row >= len(E["times"]) - 5: continue
        r = simulate(E, spot, row, sold, bought, lev, minpct, spr, cap=cap, balanced=balanced)
        res.append(dict(expiry=day, hours_before=h, entry_utc=pd.Timestamp(t0, unit="s").strftime("%Y-%m-%d %H:%M"), **(r or {"none": True})))
    return res

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None, help="JSON file with the frozen parameters (see strategy_config.json); command-line flags override it")
    ap.add_argument("--summary", default=None, help="also write a per-entry-hour summary CSV here")
    ap.add_argument("--csv", required=True); ap.add_argument("--sold", type=int, default=5000); ap.add_argument("--bought", type=int, default=1500)
    ap.add_argument("--lev", type=float, default=100); ap.add_argument("--spread", type=float, default=2.0); ap.add_argument("--minpct", type=float, default=2.0)
    ap.add_argument("--hours", default="22,7", help="comma list of hours before expiry (12:00 UTC) or 'all'")
    ap.add_argument("--capital", type=float, default=0, help="size lots from this capital (USD), like the tracker's Auto-size")
    ap.add_argument("--share", type=float, default=25, help="margin share of capital in %% (tracker default 25)"); ap.add_argument("--ratio", type=float, default=0.30, help="bought / sold")
    ap.add_argument("--balanced", action="store_true", help="prefer the smaller side of the profit range as large as possible"); ap.add_argument("--last", type=int, default=0, help="only the last N expiries"); ap.add_argument("--out", default="/tmp/bt_out.csv"); ap.add_argument("--procs", type=int, default=4)
    a = ap.parse_args()
    if a.config:                                   # frozen parameters from the file, unless given on the command line
        import json; C = json.load(open(a.config)); given = {x.lstrip("-").split("=")[0].replace("-", "_") for x in sys.argv[1:] if x.startswith("--")}
        for k_json, k_arg in (("capital", "capital"), ("share", "share"), ("ratio", "ratio"), ("lev", "lev"), ("spread", "spread"), ("minpct", "minpct"), ("hours", "hours")):
            if k_json in C and k_arg not in given: setattr(a, k_arg, C[k_json])
    t = time.time(); D = load(a.csv); print("loaded", len(D), "expiries in", round(time.time() - t, 1), "s", flush=True)
    days = sorted(D)[-a.last:] if a.last else sorted(D)
    hours = list(range(22, 1, -1)) if a.hours == "all" else [float(x) for x in a.hours.split(",")]
    cap = dict(capital=a.capital, share=a.share, ratio=a.ratio) if a.capital else None
    jobs = [(d, D[d], hours, a.sold, a.bought, a.lev, a.minpct, a.spread / 100, cap, a.balanced) for d in days]
    with Pool(a.procs) as p: rs = [r for chunk in p.imap(work, jobs) for r in chunk]
    df = pd.DataFrame(rs); df.to_csv(a.out, index=False); print("wrote", a.out, len(rs), "rows in", round(time.time() - t, 1), "s")
    if a.summary and "final" in df:
        ok = df[df["final"].notna()]
        g = ok.groupby("hours_before").agg(days=("final", "size"), win_pct=("final", lambda x: round((x > 0).mean() * 100)), total=("final", "sum"), avg=("final", "mean"), worst_final=("final", "min"),
                                           worst_open=("worst", "min"), hit10=("hit10", lambda x: int((x > 0).sum())), hit20=("hit20", lambda x: int((x > 0).sum())), hit30=("hit30", lambda x: int((x > 0).sum())))
        g.round(1).sort_index(ascending=False).to_csv(a.summary); print(g.round(0).sort_index(ascending=False).to_string())

if __name__ == "__main__": main()

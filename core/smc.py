"""SMC / ICT structures: swings, BOS/CHoCH, FVG, Order Blocks, Breaker Blocks,
liquidity and the dealing range. Measurement only - the agent decides."""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

import config


def rnd(x, ref: float):
    if x is None:
        return None
    x = float(x)
    if not np.isfinite(x):
        return None
    p = abs(ref) or 1.0
    nd = 2 if p >= 1000 else 3 if p >= 100 else 4 if p >= 1 else 6 if p >= 0.01 else 8
    return round(x, nd)


def arrays(df: pd.DataFrame):
    return (df["open"].to_numpy(float), df["high"].to_numpy(float),
            df["low"].to_numpy(float), df["close"].to_numpy(float),
            df["volume"].to_numpy(float))


def swings(high, low, k: int = 2):
    n = high.size
    if n < 2 * k + 2:
        return np.array([], int), np.array([], int)
    hl = sliding_window_view(high, k).max(axis=1)
    hr = sliding_window_view(high, k).max(axis=1)
    ll = sliding_window_view(low, k).min(axis=1)
    lr = sliding_window_view(low, k).min(axis=1)
    idx = np.arange(k, n - k)
    sh = idx[(high[idx] >= hl[idx - k]) & (high[idx] > hr[idx + 1])]
    sl = idx[(low[idx] <= ll[idx - k]) & (low[idx] < lr[idx + 1])]
    return sh, sl


def structure(high, low, close, k: int = 2):
    """Forward walk producing BOS / CHoCH events and the current bias."""
    n = close.size
    sh, sl = swings(high, low, k)
    hmap = {int(i): float(high[i]) for i in sh}
    lmap = {int(i): float(low[i]) for i in sl}
    bias, events = "flat", []
    ph = pl = None
    for i in range(n):
        c = i - k
        if c >= 0:
            if c in hmap:
                ph = (c, hmap[c])
            if c in lmap:
                pl = (c, lmap[c])
        if ph and close[i] > ph[1]:
            events.append(("BOS" if bias == "bull" else "CHoCH", "bull", i, ph[1]))
            bias, ph = "bull", None
        if pl and close[i] < pl[1]:
            events.append(("BOS" if bias == "bear" else "CHoCH", "bear", i, pl[1]))
            bias, pl = "bear", None
    return bias, events, sh, sl, ph, pl


def resting_quote(high, low, vol, typ, start: int, lo: float, hi: float) -> float:
    """Quote-currency volume actually traded inside [lo,hi] since `start`.

    Each candle contributes its notional pro-rated by range overlap. This is the
    'money resting in the zone' figure.
    """
    if start + 1 >= high.size or hi <= lo:
        return 0.0
    h, l = high[start + 1:], low[start + 1:]
    v, p = vol[start + 1:], typ[start + 1:]
    rng = np.maximum(h - l, 1e-12)
    ov = np.maximum(0.0, np.minimum(h, hi) - np.maximum(l, lo))
    return float(np.sum(v * p * np.clip(ov / rng, 0, 1)))


def find_fvg(o, h, l, c, v, typ, atr: float, keep: int):
    n = c.size
    price = float(c[-1])
    out = []
    for i in range(max(2, n - config.SCAN_WINDOW), n):
        if l[i] > h[i - 2]:
            k, top, bot = "bull", float(l[i]), float(h[i - 2])
        elif h[i] < l[i - 2]:
            k, top, bot = "bear", float(l[i - 2]), float(h[i])
        else:
            continue
        size = top - bot
        if size <= 0:
            continue
        fill, mit = 0.0, False
        if i + 1 < n:
            if k == "bull":
                pen = np.clip((top - np.minimum(l[i + 1:], top)) / size, 0, 1)
                mit = bool((l[i + 1:] <= bot).any())
            else:
                pen = np.clip((np.maximum(h[i + 1:], bot) - bot) / size, 0, 1)
                mit = bool((h[i + 1:] >= top).any())
            fill = float(pen.max()) if pen.size else 0.0
        if mit:
            continue                                   # only live gaps are useful
        inside = bot <= price <= top
        if config.FRESH_ONLY and not inside:
            if fill * 100 > config.MAX_FVG_FILL:
                continue                               # mostly filled = spent
            if n - 1 - i > config.MAX_ZONE_AGE:
                continue                               # stale
        out.append({"k": k, "_i": i, "top": top, "bot": bot,
                    "ce": (top + bot) / 2,
                    "atr": round(size / atr, 2) if atr else None,
                    "age": int(n - 1 - i), "fill": round(fill * 100),
                    "fq": int(v[i - 2] * typ[i - 2] + v[i - 1] * typ[i - 1]
                              + v[i] * typ[i]),
                    "rq": int(resting_quote(h, l, v, typ, i, bot, top))})
    return sorted(out, key=lambda z: -z["_i"])[:keep]


def find_ob(o, h, l, c, v, typ, atr: float, sh, sl, keep: int):
    n = c.size
    price = float(c[-1])
    if n < 40:
        return [], []
    body = np.abs(c - o)
    bm = pd.Series(body).rolling(20).mean().to_numpy()
    bm = np.nan_to_num(bm, nan=float(np.nanmean(body) or 1.0))
    obs, broken = [], []
    for i in range(max(config.OB_LOOKBACK + 2, n - config.SCAN_WINDOW), n):
        if bm[i] <= 0 or body[i] < config.DISPLACEMENT * bm[i]:
            continue
        up = c[i] > o[i] and l[i] > h[i - 2]
        dn = c[i] < o[i] and h[i] < l[i - 2]
        if not (up or dn):
            continue
        k = "bull" if up else "bear"
        j = None
        for x in range(i - 1, max(i - config.OB_LOOKBACK - 1, 0), -1):
            if (k == "bull" and c[x] < o[x]) or (k == "bear" and c[x] > o[x]):
                j = x
                break
        if j is None or h[j] <= l[j]:
            continue
        lo, hi = float(l[j]), float(h[j])
        psh, psl = sh[sh < i], sl[sl < i]
        bos = bool((k == "bull" and psh.size and c[i] > h[psh[-1]]) or
                   (k == "bear" and psl.size and c[i] < l[psl[-1]]))
        after_c = c[i + 1:]
        vio = np.where(after_c < lo)[0] if k == "bull" else np.where(after_c > hi)[0]
        vio_i = int(i + 1 + vio[0]) if vio.size else None
        tapped = bool((l[i + 1:] <= hi).any() if k == "bull" else (h[i + 1:] >= lo).any())
        z = {"k": k, "_i": j, "hi": hi, "lo": lo,
             "bhi": float(max(o[j], c[j])), "blo": float(min(o[j], c[j])),
             "disp": round(body[i] / atr, 2) if atr else None,
             "bos": int(bos), "age": int(n - 1 - j), "tap": int(tapped),
             "qv": int(v[j] * typ[j]), "iq": int(v[i] * typ[i]),
             "rq": int(resting_quote(h, l, v, typ, j, lo, hi))}
        # several displacement legs can point at the same OB candle - keep the
        # one with the strongest displacement rather than emitting it twice
        if vio_i is None and config.FRESH_ONLY:
            inside = lo <= price <= hi
            if not inside and (tapped or n - 1 - j > config.MAX_ZONE_AGE):
                continue          # already mitigated or stale -> not a fresh OB
        target = broken if vio_i is not None else obs
        if vio_i is not None:
            z["_v"] = vio_i
        dup = next((x for x in target if x["_i"] == j), None)
        if dup is None:
            target.append(z)
        elif (z.get("disp") or 0) > (dup.get("disp") or 0):
            target[target.index(dup)] = z
    return (sorted(obs, key=lambda z: -z["_i"])[:keep],
            sorted(broken, key=lambda z: -z["_v"]))


def find_breakers(broken_obs, h, l, c, v, typ, keep: int):
    """An order block that got violated flips polarity and becomes a breaker."""
    n = c.size
    out = []
    for ob in broken_obs:
        x = ob["_v"]
        flip = "bear" if ob["k"] == "bull" else "bull"
        lo, hi = ob["lo"], ob["hi"]
        if flip == "bear":
            rt = np.where(h[x + 1:] >= lo)[0]
        else:
            rt = np.where(l[x + 1:] <= hi)[0]
        ri = int(x + 1 + rt[0]) if rt.size else None
        out.append({"k": flip, "_i": x, "hi": hi, "lo": lo,
                    "age": int(n - 1 - x),
                    "rt": int(ri is not None),
                    "rq": int(resting_quote(h, l, v, typ, x, lo, hi)),
                    "bq": int(v[x] * typ[x])})
    return sorted(out, key=lambda z: -z["_i"])[:keep]


def liquidity(h, l, c, v, typ, sh, sl, atr: float, keep: int):
    n, price = c.size, float(c[-1])
    tol = max(atr * 0.25, price * 0.0005) if atr else price * 0.0005

    def cluster(idxs, arr):
        lv = []
        for i in idxs:
            p = float(arr[i])
            for g in lv:
                if abs(g["l"] - p) <= tol:
                    g["l"] = (g["l"] * g["n"] + p) / (g["n"] + 1)
                    g["n"] += 1
                    g["i"] = int(i)
                    break
            else:
                lv.append({"l": p, "n": 1, "i": int(i), "f": int(i)})
        return lv

    up, dn, swept = [], [], []
    for side, groups, arr in (("u", cluster(sh, h), h), ("d", cluster(sl, l), l)):
        for g in groups:
            k = g["i"]
            if side == "u":
                hit = np.where(h[k + 1:] > g["l"])[0]
            else:
                hit = np.where(l[k + 1:] < g["l"])[0]
            si = int(k + 1 + hit[0]) if hit.size else None
            item = {"lvl": g["l"], "t": ("EQH" if side == "u" else "EQL") if g["n"] > 1
                    else "sw", "n": g["n"], "age": int(n - 1 - g["i"]),
                    "d": round((g["l"] - price) / price * 100, 2),
                    "qv": int(resting_quote(h, l, v, typ, max(0, g["f"] - 1),
                                            g["l"] - tol, g["l"] + tol))}
            if si is None:
                (up if side == "u" else dn).append(item)
            elif n - 1 - si <= 20:
                item["ago"] = int(n - 1 - si)
                item["rec"] = int((c[si] < g["l"]) if side == "u" else (c[si] > g["l"]))
                swept.append(item)
    up = [x for x in up if x["lvl"] > price]
    dn = [x for x in dn if x["lvl"] < price]
    up.sort(key=lambda x: x["lvl"])
    dn.sort(key=lambda x: -x["lvl"])
    swept.sort(key=lambda x: x["ago"])
    return {"u": up[:keep], "d": dn[:keep], "sw": swept[:keep]}


def dealing_range(h, l, c, sh, sl):
    if not sh.size or not sl.size:
        return None
    hi, lo = float(h[sh[-1]]), float(l[sl[-1]])
    if hi <= lo:
        hi, lo = float(h[sh[-3:]].max()), float(l[sl[-3:]].min())
    if hi <= lo:
        return None
    price = float(c[-1])
    pos = (price - lo) / (hi - lo)
    return {"hi": hi, "lo": lo, "eq": (hi + lo) / 2,
            "pos": round(pos * 100, 1),
            "z": "prem" if pos > .55 else "disc" if pos < .45 else "eq",
            "ote": [lo + (hi - lo) * .21, lo + (hi - lo) * .38] if pos < .5
                   else [lo + (hi - lo) * .62, lo + (hi - lo) * .79]}


def support_resistance(h, l, c, v, typ, atr: float, keep: int):
    sh, sl = swings(h, l, 3)
    price = float(c[-1])
    tol = max(atr * .5, price * .0015) if atr else price * .0015
    pts = sorted([(int(i), float(h[i])) for i in sh] +
                 [(int(i), float(l[i])) for i in sl])
    cl = []
    for i, p in pts:
        for g in cl:
            if abs(g["l"] - p) <= tol:
                g["l"] = (g["l"] * g["n"] + p) / (g["n"] + 1)
                g["n"] += 1
                g["i"] = i
                break
        else:
            cl.append({"l": p, "n": 1, "i": i, "f": i})
    items = [{"lvl": g["l"], "n": g["n"],
              "d": round((g["l"] - price) / price * 100, 2),
              "qv": int(resting_quote(h, l, v, typ, max(0, g["f"] - 1),
                                      g["l"] - tol, g["l"] + tol))} for g in cl]
    res = sorted([x for x in items if x["lvl"] > price], key=lambda x: x["lvl"])[:keep]
    sup = sorted([x for x in items if x["lvl"] <= price], key=lambda x: -x["lvl"])[:keep]
    return {"s": sup, "r": res}


# ---------------------------------------------------------------------------
# Volume profile and swing sequence
# ---------------------------------------------------------------------------
def volume_profile(h, l, c, v, typ, bins: int = 24):
    """POC / value area from quote volume spread across each candle's range."""
    lo, hi = float(l.min()), float(h.max())
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    mids = (edges[:-1] + edges[1:]) / 2
    hist = np.zeros(bins)
    width = (hi - lo) / bins
    for i in range(c.size):
        a, b = l[i], h[i]
        rng = max(b - a, 1e-12)
        overlap = np.maximum(0.0, np.minimum(edges[1:], b) - np.maximum(edges[:-1], a))
        hist += (overlap / rng) * v[i] * typ[i]
    total = hist.sum()
    if total <= 0:
        return None
    poc = int(np.argmax(hist))
    order = np.argsort(-hist)
    acc, chosen = 0.0, []
    for idx in order:
        chosen.append(int(idx))
        acc += hist[idx]
        if acc >= total * 0.70:
            break
    return {"poc": float(mids[poc]),
            "vah": float(mids[max(chosen)] + width / 2),
            "val": float(mids[min(chosen)] - width / 2)}


def swing_sequence(sh, sl, h, l, n: int = 4) -> str:
    """Label the last few swings HH / LH / HL / LL - the trend read at a glance."""
    pts = sorted([(int(i), "H", float(h[i])) for i in sh]
                 + [(int(i), "L", float(l[i])) for i in sl])
    if len(pts) < 2:
        return ""
    out, prev_h, prev_l = [], None, None
    for _, kind, price in pts:
        if kind == "H":
            out.append("HH" if prev_h is not None and price > prev_h
                       else "LH" if prev_h is not None else "H")
            prev_h = price
        else:
            out.append("HL" if prev_l is not None and price > prev_l
                       else "LL" if prev_l is not None else "L")
            prev_l = price
    return ",".join(out[-n:])

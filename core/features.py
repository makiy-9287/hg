"""pandas_ta indicators + the single-timeframe snapshot handed to the agent.

One timeframe per payload. The 4h scan sends 4h only; a 1h drill-down is
requested per coin through a tool. Zones are emitted as positional arrays with
the legend in the (cached) system prompt.
"""
from __future__ import annotations

from math import floor, log10

import numpy as np
import pandas as pd

if not hasattr(np, "NaN"):          # pandas_ta still imports numpy.NaN
    np.NaN = np.nan
import pandas_ta as ta  # noqa: E402

import config  # noqa: E402
from core import smc  # noqa: E402

KILLZONES = [("asia", 0, 5), ("london", 7, 10), ("ny_am", 12, 15), ("ny_pm", 18, 20)]


# --------------------------------------------------------------------- helpers
def _sig(x, n: int = None):
    if x is None:
        return None
    x = float(x)
    if x == 0 or not np.isfinite(x):
        return 0
    n = n or config.SIG_DIGITS
    d = n - 1 - floor(log10(abs(x)))
    return round(x, d) if d > 0 else int(round(x, d))


def _m(x) -> float:
    """Quote volume in millions: 143009593 is four tokens, 143.01 is two."""
    return round((x or 0) / 1e6, 3)


def _d(level, price) -> float:
    """Signed distance from CMP in %, so the agent never has to compute it."""
    return round((level - price) / price * 100, 2)


def indicators(df: pd.DataFrame) -> dict:
    close = df["close"]

    def last(s):
        if s is None or not len(s):
            return None
        s = s.dropna()
        return float(s.iloc[-1]) if len(s) else None

    price = float(close.iloc[-1])
    rsi = ta.rsi(close, length=14)
    ev = [last(ta.ema(close, length=p)) for p in (20, 50, 200)]
    a = last(ta.atr(df["high"], df["low"], close, length=14)) or 0.0
    vm = last(ta.sma(df["volume"], length=20)) or 0.0
    r = last(rsi)
    rprev = None
    if rsi is not None and len(rsi.dropna()) > 5:
        rprev = float(rsi.dropna().iloc[-6])
    stack = ("b" if all(x is not None for x in ev) and ev[0] >= ev[1] >= ev[2]
             else "e" if all(x is not None for x in ev) and ev[0] <= ev[1] <= ev[2]
             else "m")
    return {"rsi": round(r, 1) if r else None,
            "rsi_d": round(r - rprev, 1) if r and rprev else None,
            "ema": [round((price - x) / price * 100, 2) if x else None for x in ev],
            "stack": stack,
            "atr_pct": round(a / price * 100, 3) if price else None,
            "rvol": round(float(df["volume"].iloc[-1]) / vm, 2) if vm else None,
            "atr": a}


def sessions(df: pd.DataFrame) -> dict:
    ts = pd.to_datetime(df["ts"], unit="ms", utc=True)
    now = ts.iloc[-1]
    hour = now.hour + now.minute / 60
    out = {"kz": next((n for n, s, e in KILLZONES if s <= hour < e), "off")}
    today = now.normalize()
    y = df[(ts >= today - pd.Timedelta(days=1)) & (ts < today)]
    if len(y):
        out["pdh"] = float(y["high"].max())
        out["pdl"] = float(y["low"].min())
    d0 = df[ts >= today]
    w0 = df[ts >= today - pd.Timedelta(days=int(now.dayofweek))]
    out["do"] = float(d0["open"].iloc[0]) if len(d0) else None
    out["wo"] = float(w0["open"].iloc[0]) if len(w0) else None
    return out


# ------------------------------------------------------------------ build view
def build_view(meta: dict, df: pd.DataFrame, tf: str) -> dict | None:
    """Full structural read of ONE timeframe for ONE coin."""
    if df is None or len(df) < 80:
        return None
    price = float(df["close"].iloc[-1])
    if price <= 0:
        return None

    o, h, l, c, v = smc.arrays(df)
    typ = (h + l + c) / 3.0
    ind = indicators(df)
    atr = ind.pop("atr")

    bias, events, sh, sl, ph, pl = smc.structure(h, l, c, config.SWING)
    fvg = smc.find_fvg(o, h, l, c, v, typ, atr, config.KEEP_FVG)
    obs, broken = smc.find_ob(o, h, l, c, v, typ, atr, sh, sl, config.KEEP_OB)
    bb = smc.find_breakers(broken, h, l, c, v, typ, config.KEEP_BB)
    liq = smc.liquidity(h, l, c, v, typ, sh, sl, atr, config.KEEP_LIQ)
    rng = smc.dealing_range(h, l, c, sh, sl)
    sr = smc.support_resistance(h, l, c, v, typ, atr, config.KEEP_SR)
    vp = smc.volume_profile(h, l, c, v, typ)

    return {
        "symbol": meta["symbol"], "tf": tf, "price": price,
        "qv24": meta.get("qv24", 0), "chg": meta.get("chg", 0.0),
        "bias": bias, "seq": smc.swing_sequence(sh, sl, h, l),
        "events": [(t, d, int(c.size - 1 - i), lv)
                   for t, d, i, lv in events[-config.KEEP_EVENTS:]],
        "ind": ind, "atr": atr,
        "fvg": fvg, "ob": obs, "bb": bb, "liq": liq, "rng": rng,
        "sr": sr, "vp": vp, "sess": sessions(df),
        "c3": [[float(r.open), float(r.high), float(r.low), float(r.close)]
               for r in df.iloc[-2:].itertuples()],
    }


# ----------------------------------------------------------------- wire format
def encode_view(view: dict, extras: dict | None = None,
                header: bool = True) -> dict:
    """Rich view -> compact positional arrays. header=False for a nested block."""
    p = view["price"]
    near = config.ZONE_NEAR_PCT.get(view["tf"], 6.0)

    def ok(level) -> bool:
        return abs(level - p) / p * 100 <= near

    i = view["ind"]
    out = {}
    if header:
        out.update({"s": view["symbol"].split(":")[0], "tf": view["tf"],
                    "p": _sig(p), "v": _m(view["qv24"]),
                    "ch": round(view["chg"], 1)})
    out.update({
        "b": view["bias"], "q": view["seq"],
        "e": [f"{t} {d} {n}b @{_sig(lv)}" for t, d, n, lv in view["events"]],
        "i": [i["rsi"], i["rsi_d"], i["ema"][0], i["ema"][1], i["ema"][2],
              i["stack"], i["atr_pct"], i["rvol"], _sig(view["atr"])],
    })

    f = [[z["k"][0], _sig(z["top"]), _sig(z["bot"]), z["age"], z["fill"],
          _m(z["rq"]), _d((z["top"] + z["bot"]) / 2, p)]
         for z in view["fvg"] if ok((z["top"] + z["bot"]) / 2)]
    ob = [[z["k"][0], _sig(z["hi"]), _sig(z["lo"]), z["age"], z["bos"], z["tap"],
           z["disp"], _m(z["qv"]), _m(z["rq"]), _d((z["hi"] + z["lo"]) / 2, p)]
          for z in view["ob"] if ok((z["hi"] + z["lo"]) / 2)]
    bb = [[z["k"][0], _sig(z["hi"]), _sig(z["lo"]), z["age"], z["rt"],
           _m(z["rq"]), _d((z["hi"] + z["lo"]) / 2, p)]
          for z in view["bb"] if ok((z["hi"] + z["lo"]) / 2)]
    if f:
        out["f"] = f
    if ob:
        out["o"] = ob
    if bb:
        out["k"] = bb

    lq = {}
    for side in ("u", "d", "sw"):
        arr = [[_sig(z["lvl"]), z["t"], z["n"], z["age"], _m(z["qv"]),
                _d(z["lvl"], p)] + ([z.get("ago"), z.get("rec")] if side == "sw" else [])
               for z in view["liq"].get(side, [])[:config.KEEP_LIQ]]
        if arr:
            lq[side] = arr
    if lq:
        out["l"] = lq

    r = view["rng"]
    if r:
        out["r"] = [_sig(r["hi"]), _sig(r["lo"]), r["pos"], r["z"][0],
                    _sig(r["ote"][0]), _sig(r["ote"][1])]
    vp = view["vp"]
    if vp:
        out["vp"] = [_sig(vp["poc"]), _sig(vp["vah"]), _sig(vp["val"])]
    out["sr"] = [[_sig(x["lvl"]), x["n"], _m(x["qv"]), _d(x["lvl"], p)]
                 for x in view["sr"]["s"][:config.KEEP_SR]
                 + view["sr"]["r"][:config.KEEP_SR]]
    se = view["sess"]
    out["z"] = [se.get("kz"), _sig(se.get("pdh")), _sig(se.get("pdl")),
                _sig(se.get("do")), _sig(se.get("wo"))]
    out["c3"] = [[_sig(x) for x in row] for row in view["c3"]]

    extras = extras or {}
    if extras.get("rs") is not None:
        out["rs"] = extras["rs"]
    mk = extras.get("mkt")
    if mk:
        out["m"] = [mk.get("fr"), mk.get("oi"), mk.get("oi1"), mk.get("oi4"),
                    mk.get("ls")]
    return out


def nearest_poi_pct(view: dict) -> float | None:
    """Distance from CMP to the closest live POI edge, in % - what the 4h scan
    is really screening on."""
    p = view["price"]
    best = None
    zones = ([(z["bot"], z["top"]) for z in view["fvg"]]
             + [(z["lo"], z["hi"]) for z in view["ob"]]
             + [(z["lo"], z["hi"]) for z in view["bb"]])
    for lo, hi in zones:
        d = 0.0 if lo <= p <= hi else min(abs(lo - p), abs(hi - p)) / p * 100
        best = d if best is None else min(best, d)
    return round(best, 3) if best is not None else None


# --------------------------------------------------------- multi-timeframe wire
def digest(view: dict) -> dict:
    """Compact HTF summary for the 15m loop.

    1d and 4h structure cannot change between 15-minute ticks, so resending the
    full block 64 times a day is waste. This keeps the narrative and the levels
    that matter and drops the rest.
    """
    p = view["price"]
    out = {"b": view["bias"], "q": view["seq"],
           "e": [f"{t} {d} {n}b @{_sig(lv)}" for t, d, n, lv in view["events"][-1:]]}
    r = view["rng"]
    if r:
        out["r"] = [_sig(r["hi"]), _sig(r["lo"]), r["pos"], r["z"][0]]
    poi = ([("f", z["top"], z["bot"], z["k"], z["rq"]) for z in view["fvg"]]
           + [("o", z["hi"], z["lo"], z["k"], z["rq"]) for z in view["ob"]]
           + [("k", z["hi"], z["lo"], z["k"], z["rq"]) for z in view["bb"]])
    poi.sort(key=lambda z: abs((z[1] + z[2]) / 2 - p))
    out["poi"] = [[k, kind[0], _sig(hi), _sig(lo), _m(rq), _d((hi + lo) / 2, p)]
                  for k, hi, lo, kind, rq in poi[:2]]
    liq = view["liq"]
    tg = {}
    for side in ("u", "d"):
        if liq.get(side):
            z = liq[side][0]
            tg[side] = [_sig(z["lvl"]), z["t"], _d(z["lvl"], p)]
    if tg:
        out["l"] = tg
    if view.get("vp"):
        out["poc"] = _sig(view["vp"]["poc"])
    return out


def _header(views: dict, extras: dict | None) -> dict:
    any_v = next(iter(views.values()))
    extras = extras or {}
    out = {"s": any_v["symbol"].split(":")[0], "p": _sig(any_v["price"]),
           "v": _m(any_v["qv24"]), "ch": round(any_v["chg"], 1)}
    if extras.get("rs") is not None:
        out["rs"] = extras["rs"]
    mk = extras.get("mkt")
    if mk:
        out["m"] = [mk.get("fr"), mk.get("oi"), mk.get("oi1"), mk.get("oi4"),
                    mk.get("ls")]
    se = any_v["sess"]
    out["z"] = [se.get("kz"), _sig(se.get("pdh")), _sig(se.get("pdl")),
                _sig(se.get("do")), _sig(se.get("wo"))]
    return out


def encode_full(views: dict, extras: dict | None = None) -> dict:
    """Every timeframe in full - the 4h bulk scan payload."""
    out = _header(views, extras)
    out["tf"] = {tf: encode_view(views[tf], header=False)
                 for tf in config.MAIN_TFS if tf in views}
    return out


def encode_active(views: dict, extras: dict | None = None) -> dict:
    """15m and 1h in full, 4h and 1d digested - the 15-minute loop payload."""
    out = _header(views, extras)
    out["tf"] = {tf: encode_view(views[tf], header=False)
                 for tf in config.ACTIVE_TFS if tf in views}
    out["htf"] = {tf: digest(views[tf]) for tf in config.DIGEST_TFS if tf in views}
    return out


def nearest_poi_any(views: dict) -> float | None:
    """Closest live POI across all timeframes, in % from CMP."""
    best = None
    for v in views.values():
        d = nearest_poi_pct(v)
        if d is not None:
            best = d if best is None else min(best, d)
    return best

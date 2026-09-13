"""Minute-by-minute SL/TP tracking on live websocket prices.

Runs 24/7, including outside the scan window - open trades never go unwatched.
Between checks it tracks the running high/low from the ticker stream, so a wick
that touches a level and retraces inside the same minute is still caught.
"""
from __future__ import annotations

import asyncio
import logging

import config
from storage import db
from tg import send as tg

log = logging.getLogger("monitor")


def pnl(row, exit_price: float) -> tuple[float, float]:
    entry = row["entry_price"] or (row["entry_low"] + row["entry_high"]) / 2
    risk = abs(entry - row["stop_loss"]) or 1e-12
    sign = 1.0 if row["direction"] == "LONG" else -1.0
    legs, w = [], []
    for i, key in enumerate(("tp1", "tp2", "tp3")):
        if row[f"tp{i + 1}_hit"]:
            legs.append(row[key])
            w.append(config.TP_SPLIT[i])
    rest = max(0.0, 1.0 - sum(w))
    if rest > 0:
        legs.append(exit_price)
        w.append(rest)
    p = sum(x * sign * (lv - entry) / entry * 100 for lv, x in zip(legs, w))
    r = sum(x * sign * (lv - entry) for lv, x in zip(legs, w)) / risk
    return round(p, 3), round(r, 3)


class Monitor:
    def __init__(self, stream):
        self.stream = stream
        self._ext: dict[str, list[float]] = {}   # symbol -> [min, max] this interval

    async def sample_forever(self):
        """Track intra-minute extremes from the ticker stream."""
        while True:
            for row in db.open_signals():
                sym = row["symbol"]
                p = self.stream.price(sym)
                if p is None:
                    continue
                e = self._ext.get(sym)
                self._ext[sym] = [min(e[0], p), max(e[1], p)] if e else [p, p]
            await asyncio.sleep(1)

    async def run_forever(self):
        while True:
            try:
                await self.tick()
            except Exception as ex:
                log.exception("monitor tick failed: %s", ex)
            await asyncio.sleep(config.MONITOR_SECONDS)

    async def tick(self):
        rows = db.open_signals()
        if not rows:
            self._ext.clear()
            return
        for row in rows:
            p = self.stream.price(row["symbol"])
            if p is None:
                continue
            lo, hi = self._ext.get(row["symbol"], [p, p])
            try:
                await self.check(row, p, min(lo, p), max(hi, p))
            except Exception as ex:
                log.exception("check #%s failed: %s", row["id"], ex)
        self._ext.clear()

    async def check(self, row, price: float, lo: float, hi: float):
        long = row["direction"] == "LONG"
        sid = row["id"]

        if row["status"] == "PENDING":
            if (long and lo <= row["stop_loss"]) or (not long and hi >= row["stop_loss"]):
                db.update(sid, status="CLOSED", closed_at=db.now(),
                          exit_price=row["stop_loss"], exit_reason="INVALID",
                          pnl_pct=0.0, r_mult=0.0)
                db.event(sid, "INVALID", row["stop_loss"])
                await tg.send(tg.event_text(row, "INVALID", row["stop_loss"],
                                            "never filled"))
                return
            if (db.now() - row["created_at"]) / 60 > config.PENDING_EXPIRY_MIN:
                db.update(sid, status="EXPIRED", closed_at=db.now(),
                          exit_reason="EXPIRED", pnl_pct=0.0, r_mult=0.0)
                db.event(sid, "EXPIRED", price)
                await tg.send(tg.event_text(row, "EXPIRED", price))
                return
            filled = lo <= row["entry_high"] if long else hi >= row["entry_low"]
            if not filled:
                return
            fill = (min(row["entry_high"], max(row["entry_low"], price)) if long
                    else max(row["entry_low"], min(row["entry_high"], price)))
            db.update(sid, status="ACTIVE", activated_at=db.now(), entry_price=fill)
            db.event(sid, "FILLED", fill)
            await tg.send(tg.event_text(row, "FILLED", fill))
            row = db.get(sid)

        if row["status"] != "ACTIVE":
            return

        if (long and lo <= row["stop_loss"]) or (not long and hi >= row["stop_loss"]):
            p, r = pnl(row, row["stop_loss"])
            db.update(sid, status="CLOSED", closed_at=db.now(),
                      exit_price=row["stop_loss"], exit_reason="SL",
                      pnl_pct=p, r_mult=r)
            db.event(sid, "SL", row["stop_loss"])
            await tg.send(tg.event_text(row, "SL", row["stop_loss"],
                                        f"{p:+.2f}% ({r:+.2f}R)"))
            return

        for i, key in enumerate(("tp1", "tp2", "tp3"), start=1):
            if row[f"tp{i}_hit"]:
                continue
            if not (hi >= row[key] if long else lo <= row[key]):
                break
            db.update(sid, **{f"tp{i}_hit": 1})
            db.event(sid, f"TP{i}", row[key])
            row = db.get(sid)
            if i < 3:
                await tg.send(tg.event_text(row, f"TP{i}", row[key]))
            else:
                p, r = pnl(row, row["tp3"])
                db.update(sid, status="CLOSED", closed_at=db.now(),
                          exit_price=row["tp3"], exit_reason="TP3",
                          pnl_pct=p, r_mult=r)
                await tg.send(tg.event_text(row, "TP3", row["tp3"],
                                            f"{p:+.2f}% ({r:+.2f}R)"))
                return

    async def force_close(self, sid: int, price: float) -> bool:
        row = db.get(sid)
        if not row or row["status"] not in ("PENDING", "ACTIVE"):
            return False
        p, r = pnl(row, price) if row["status"] == "ACTIVE" else (0.0, 0.0)
        db.update(sid, status="CLOSED", closed_at=db.now(), exit_price=price,
                  exit_reason="MANUAL", pnl_pct=p, r_mult=r)
        db.event(sid, "CLOSED", price, "manual")
        await tg.send(tg.event_text(row, "CLOSED", price, f"{p:+.2f}% ({r:+.2f}R)"))
        return True

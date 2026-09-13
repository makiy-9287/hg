"""Tools the agent can call.

  fetch_candles    -> CCXT Pro websocket store (no REST, no weight)
  compute_indicators -> pandas_ta on demand
  get_zones        -> full OB / Breaker / FVG detail for one timeframe
  send_signal      -> python-telegram-bot + database

Only send_signal has side effects. The read tools exist so the snapshot can stay
tiny: the agent pays tokens for detail only on coins it actually cares about.
"""
from __future__ import annotations

import logging

import config
from core import smc
from storage import db
from tg import send as tg

log = logging.getLogger("tools")

SIGNAL_FN = {"type": "function", "function": {
    "name": "send_signal",
    "description": "Dispatch one validated multi-timeframe sniper entry to Telegram.",
    "parameters": {"type": "object", "properties": {
        "symbol": {"type": "string"},
        "direction": {"type": "string", "enum": ["LONG", "SHORT"]},
        "entry_type": {"type": "string", "enum": ["CMP", "LIMIT"]},
        "entry_low": {"type": "number"}, "entry_high": {"type": "number"},
        "stop_loss": {"type": "number"},
        "tp1": {"type": "number"}, "tp2": {"type": "number"},
        "tp3": {"type": "number"},
        "confidence": {"type": "integer", "description": "1-10, send only >=7"},
        "htf_bias": {"type": "string", "description": "the 1d/4h narrative"},
        "entry_tf": {"type": "string", "enum": ["15m", "1h", "4h", "1d"]},
        "poi": {"type": "string",
                "enum": ["FVG", "OB", "BREAKER", "OTE", "SWEEP"]},
        "confirmations": {"type": "array", "items": {"type": "string"},
                          "description": "at least 3, spanning timeframes"},
        "reasoning": {"type": "string",
                      "description": "1d context, 4h narrative, 1h shift, 15m trigger"}},
        "required": ["symbol", "direction", "entry_type", "entry_low",
                     "entry_high", "stop_loss", "tp1", "tp2", "tp3",
                     "confidence", "htf_bias", "entry_tf", "poi",
                     "confirmations", "reasoning"]}}}

FLAG_FN = {"type": "function", "function": {
    "name": "flag_setup",
    "description": ("Put a coin on the 15-minute active list. It will be "
                    "re-read every 15 minutes until it fires or expires."),
    "parameters": {"type": "object", "properties": {
        "symbol": {"type": "string"},
        "bias": {"type": "string", "description": "the 1d/4h direction"},
        "poi": {"type": "string", "description": "the exact zone, with prices"},
        "trigger": {"type": "string", "description": "concrete entry condition"},
        "invalidation": {"type": "string", "description": "concrete price level"}},
        "required": ["symbol", "bias", "poi", "trigger", "invalidation"]}}}

KEEP_FN = {"type": "function", "function": {
    "name": "keep_setup",
    "description": "Still valid, trigger not fired. Stay on the active list.",
    "parameters": {"type": "object", "properties": {
        "symbol": {"type": "string"}, "note": {"type": "string"}},
        "required": ["symbol", "note"]}}}

DROP_FN = {"type": "function", "function": {
    "name": "drop_setup",
    "description": "Invalidated or dead. Remove from the active list.",
    "parameters": {"type": "object", "properties": {
        "symbol": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["symbol", "reason"]}}}

MAIN_TOOLS = [FLAG_FN, SIGNAL_FN]
ACTIVE_TOOLS = [SIGNAL_FN, KEEP_FN, DROP_FN]


class Rejected(Exception):
    pass


def _validate(a: dict, known: set[str]) -> dict:
    sym = str(a.get("symbol", "")).strip()
    if sym not in known:
        short = sym.split(":")[0]
        if short in known:
            sym = short
        else:
            raise Rejected(f"unknown symbol {sym!r}")
    d = str(a.get("direction", "")).upper()
    if d not in ("LONG", "SHORT"):
        raise Rejected("bad direction")
    et = str(a.get("entry_type", "")).upper()
    if et not in ("CMP", "LIMIT"):
        raise Rejected("bad entry_type")
    try:
        lo, hi = float(a["entry_low"]), float(a["entry_high"])
        sl = float(a["stop_loss"])
        t1, t2, t3 = float(a["tp1"]), float(a["tp2"]), float(a["tp3"])
    except (KeyError, TypeError, ValueError) as ex:
        raise Rejected(f"bad number: {ex}")
    if lo > hi:
        lo, hi = hi, lo
    if min(lo, hi, sl, t1, t2, t3) <= 0:
        raise Rejected("non-positive price")
    if d == "LONG" and not (sl < lo <= hi < t1 < t2 < t3):
        raise Rejected("LONG level order invalid")
    if d == "SHORT" and not (sl > hi >= lo > t1 > t2 > t3):
        raise Rejected("SHORT level order invalid")
    ref = (lo + hi) / 2
    risk = abs(ref - sl)
    if risk <= 0:
        raise Rejected("zero risk")
    if risk / ref > 0.15:
        raise Rejected("stop wider than 15%")
    rr = abs(t1 - ref) / risk
    if rr < config.MIN_RR:
        raise Rejected(f"TP1 R:R {rr:.2f} < {config.MIN_RR}")
    conf = int(a.get("confidence") or 0)
    if conf < config.MIN_CONFIDENCE:
        raise Rejected(f"confidence {conf} < {config.MIN_CONFIDENCE}")
    confirms = [str(x) for x in (a.get("confirmations") or [])][:6]
    if len(confirms) < 3:
        raise Rejected("fewer than 3 confirmations")
    return {"symbol": sym, "direction": d, "mode": config.MODE, "entry_type": et,
            "entry_low": lo, "entry_high": hi, "stop_loss": sl,
            "tp1": t1, "tp2": t2, "tp3": t3, "confidence": conf,
            "htf_bias": str(a.get("htf_bias", ""))[:160],
            "entry_tf": str(a.get("entry_tf", ""))[:6],
            "poi": str(a.get("poi", ""))[:16],
            "confirmations": confirms,
            "reasoning": str(a.get("reasoning", ""))[:900],
            "rr": round(rr, 2)}


class ToolBox:
    """Binds tool names to the live market stream."""

    def __init__(self, stream):
        self.stream = stream
        self.known: set[str] = set()
        self.symbols: dict[str, str] = {}
        self.flagged: list[str] = []
        
        
        

    def resolve(self, name: str) -> str:
        name = (name or "").strip()
        return self.symbols.get(name, self.symbols.get(name.split(":")[0], name))

    # ------------------------------------------------------- active list
    def flag(self, a: dict) -> dict:
        sym = self.resolve(a.get("symbol", ""))
        if not sym or self.stream.get(sym, "15m") is None:
            return {"error": "unknown symbol"}
        if len(db.setups()) >= config.MAX_ACTIVE:
            return {"error": "active list is full - flag only your strongest"}
        if len(self.flagged) >= config.MAX_FLAGS_PER_SCAN:
            return {"error": f"already flagged {config.MAX_FLAGS_PER_SCAN} this "
                             f"scan, which is the cap - keep only the best"}
        db.add_setup(sym, config.SETUP_MAX_HOURS,
                     bias=str(a.get("bias", ""))[:120],
                     poi=str(a.get("poi", ""))[:140],
                     trigger=str(a.get("trigger", ""))[:200],
                     invalidation=str(a.get("invalidation", ""))[:120])
        self.flagged.append(sym)
        log.info("FLAG   %s · %s · trigger: %s", sym.split(":")[0],
                 str(a.get("bias", ""))[:24], str(a.get("trigger", ""))[:70])
        return {"status": "on the 15m active list",
                "expires_hours": config.SETUP_MAX_HOURS}

    def keep(self, a: dict) -> dict:
        sym = self.resolve(a.get("symbol", ""))
        db.bump_setup(sym)
        log.info("  keep  %s · %s", sym.split(":")[0], str(a.get("note", ""))[:70])
        return {"status": "still active"}

    def drop(self, a: dict) -> dict:
        sym = self.resolve(a.get("symbol", ""))
        db.drop_setup(sym)
        log.info("  drop  %s · %s", sym.split(":")[0], str(a.get("reason", ""))[:70])
        return {"status": "dropped"}

    # ------------------------------------------------------------ write tool
    async def send_signal(self, args: dict) -> dict:
        try:
            s = _validate(args, self.known)
        except Rejected as ex:
            log.warning("rejected %s: %s", args.get("symbol"), ex)
            return {"status": "rejected", "reason": str(ex)}

        s["symbol"] = self.resolve(s["symbol"])
        if db.open_for(s["symbol"]):
            return {"status": "skipped", "reason": "already open on this symbol"}
        if db.recent(s["symbol"], s["direction"], config.DUP_COOLDOWN_MIN):
            return {"status": "skipped", "reason": "duplicate within cooldown"}

        price = self.stream.price(s["symbol"])
        if s["entry_type"] == "CMP":
            s["status"] = "ACTIVE"
            s["entry_price"] = price or (s["entry_low"] + s["entry_high"]) / 2
            s["activated_at"] = db.now()
        else:
            s["status"] = "PENDING"

        sid = db.insert(s)
        db.event(sid, "CREATED", s.get("entry_price"), s["poi"])
        db.drop_setup(s["symbol"])
        mid = await tg.send(tg.signal_text(s, sid))
        if mid:
            db.update(sid, msg_id=mid)
        log.info("SIGNAL #%d %s %s conf=%d rr=%.2f", sid,
                 s["symbol"].split(":")[0], s["direction"],
                 s["confidence"], s["rr"])
        return {"status": "sent", "signal_id": sid}

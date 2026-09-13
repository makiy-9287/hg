"""Telegram commands."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import NetworkError
from telegram.ext import Application, CommandHandler, ContextTypes

import config
from storage import db
from tg import send as sender
from tg.send import e, fmt

log = logging.getLogger("tgbot")

STATE: dict = {}


def ok(u: Update) -> bool:
    return not config.TG_CHAT or str(u.effective_chat.id) == str(config.TG_CHAT)


async def reply(u: Update, t: str):
    await u.effective_message.reply_text(t, parse_mode=ParseMode.HTML,
                                         disable_web_page_preview=True)


def window(arg):
    tbl = {"24h": 86400, "today": 86400, "7d": 604800, "week": 604800,
           "30d": 2592000, "all": 315360000}
    a = (arg or "24h").lower()
    return int(time.time()) - tbl.get(a, 86400), a


async def start(u, c):
    if ok(u):
        await reply(u, f"<b>SMC/ICT agent</b>\nchat id <code>{u.effective_chat.id}</code>"
                       f"\n/help for commands")


async def help_(u, c):
    if ok(u):
        await reply(u, "/active — open &amp; pending\n/pnl [24h|7d|30d|all]\n"
                       "/report [24h|7d]\n/last [n]\n/cost — token spend\n"
                       "/setups — coins on the 15m active list\n"
                       "/status — scanner state\n/close ID\n/pause /resume")


async def active(u, c):
    if not ok(u):
        return
    rows = db.open_signals()
    if not rows:
        await reply(u, "No open signals.")
        return
    st = STATE.get("stream")
    out = [f"<b>Open ({len(rows)})</b>"]
    for r in rows:
        p = st.price(r["symbol"]) if st else None
        entry = r["entry_price"] or (r["entry_low"] + r["entry_high"]) / 2
        live = ""
        if p:
            sign = 1 if r["direction"] == "LONG" else -1
            live = f" · {sign * (p - entry) / entry * 100:+.2f}%"
        tps = "".join("✅" if r[f"tp{i}_hit"] else "▫️" for i in (1, 2, 3))
        out.append(f"\n<code>#{r['id']}</code> <b>{e(r['symbol'].split(':')[0])}</b> "
                   f"{r['direction']} {r['status']}{live}\n"
                   f"  E {fmt(r['entry_low'])}-{fmt(r['entry_high'])} · "
                   f"SL {fmt(r['stop_loss'])} {tps}")
    await reply(u, "\n".join(out))


async def pnl(u, c):
    if not ok(u):
        return
    ts, lab = window(c.args[0] if c.args else None)
    p = db.performance(ts)
    if not p["n"]:
        await reply(u, f"No closed signals in {lab}.")
        return
    await reply(u, f"<b>PnL — {lab}</b>\nClosed <b>{p['n']}</b> · "
                   f"W/L <b>{p['wins']}/{p['n'] - p['wins']}</b> · "
                   f"WR <b>{p['wr']}%</b>\n"
                   f"Total <b>{p['pnl']:+.2f}%</b> ({p['r']:+.2f}R)\n"
                   f"<i>equal thirds at TP1/2/3</i>")


async def report(u, c):
    if not ok(u):
        return
    ts, lab = window(c.args[0] if c.args else "24h")
    p, created = db.performance(ts), db.since(ts)
    out = [f"<b>Report — {lab}</b>", f"Generated <b>{len(created)}</b> · "
           f"open <b>{len(db.open_signals())}</b>",
           f"Closed <b>{p['n']}</b> · WR <b>{p['wr']}%</b> · "
           f"<b>{p['pnl']:+.2f}%</b> ({p['r']:+.2f}R)"]
    by_poi = {}
    for r in db.since(ts):
        if r["closed_at"]:
            k = r["poi"] or "?"
            d = by_poi.setdefault(k, {"n": 0, "w": 0, "pnl": 0.0})
            d["n"] += 1
            d["w"] += 1 if (r["pnl_pct"] or 0) > 0 else 0
            d["pnl"] += r["pnl_pct"] or 0
    if by_poi:
        out.append("\n<b>By POI type</b>")
        for k, v in sorted(by_poi.items(), key=lambda x: -x[1]["n"]):
            out.append(f"  {k}: {v['n']} · {v['w'] / v['n'] * 100:.0f}% WR · "
                       f"{v['pnl']:+.2f}%")
    row = db.usage()
    if row:
        out.append(f"\nToday: {row['requests']} requests · "
                   f"${db.cost_of(row):.3f}")
    note = db.get_meta("last_note")
    if note:
        out.append(f"\n<b>Agent's last read</b>\n<i>{e(note[:700])}</i>")
    await reply(u, "\n".join(out))


async def last(u, c):
    if not ok(u):
        return
    n = 10
    if c.args:
        try:
            n = max(1, min(25, int(c.args[0])))
        except ValueError:
            pass
    rows = db.last(n)
    if not rows:
        await reply(u, "No signals yet.")
        return
    out = [f"<b>Last {len(rows)}</b>"]
    for r in rows:
        t = datetime.fromtimestamp(r["created_at"], tz=timezone.utc).strftime("%m-%d %H:%M")
        pl = f" {r['pnl_pct']:+.2f}%" if r["pnl_pct"] is not None else ""
        out.append(f"<code>#{r['id']}</code> {t} {e(r['symbol'].split(':')[0])} "
                   f"{r['direction']} {r['status']}{pl}")
    await reply(u, "\n".join(out))


async def cost(u, c):
    if not ok(u):
        return
    rows = db.usage_all()
    if not rows:
        await reply(u, "No API usage recorded yet.")
        return
    out = ["<b>Token spend</b>"]
    tot = 0.0
    for r in rows:
        cst = db.cost_of(r)
        tot += cst
        out.append(f"{r['day']}: {r['requests']} req · "
                   f"{r['hit'] / 1e6:.2f}M hit + {r['miss'] / 1e6:.2f}M miss in · "
                   f"{r['out'] / 1e3:.0f}k out · <b>${cst:.3f}</b>")
    out.append(f"\n{len(rows)}-day total <b>${tot:.2f}</b>")
    out.append(f"<i>rates: ${config.PRICE_IN_HIT}/M hit, "
               f"${config.PRICE_IN_MISS}/M miss, ${config.PRICE_OUT}/M out</i>")
    await reply(u, "\n".join(out))


async def watch(u, c):
    if not ok(u):
        return
    rows = db.setups()
    if not rows:
        await reply(u, "No active setups.")
        return
    st = STATE.get("stream")
    out = [f"<b>Active setups ({len(rows)})</b>"]
    for r in rows:
        p = st.price(r["symbol"]) if st else None
        mins = (int(time.time()) - r["created_at"]) // 60
        out.append(f"\n<b>{e(r['symbol'].split(':')[0])}</b> · {r['bias']} · "
                   f"{mins//60}h{mins%60:02d}m · {r['checks']} checks"
                   + (f" · now {fmt(p)}" if p else "") +
                   f"\n  POI {e(r['poi'])}\n  trigger: {e(r['trigger'])}"
                   f"\n  invalid: {e(r['invalidation'])}")
    await reply(u, "\n".join(out))


async def status(u, c):
    if not ok(u):
        return
    st = STATE.get("stream")
    now = datetime.now(config.LOCAL_TZ)
    import datetime as _dt
    sysnow = _dt.datetime.now()
    out = ["<b>Status</b>",
           f"Trading clock: {now:%Y-%m-%d %H:%M} ({config.LOCAL_TZ.key})",
           f"Server clock:  {sysnow:%H:%M} ({sysnow.astimezone().tzinfo}) — "
           f"schedule ignores this",
           f"Window {config.ACTIVE_START:%H:%M}–{config.ACTIVE_END:%H:%M} · "
           f"{'ACTIVE' if STATE.get('in_window') else 'IDLE'}",
           f"Dispatch {'PAUSED' if STATE.get('paused') else 'ON'}",
           f"Watchlist: top {len(st.universe) if st else '-'} by 24h volume",
           f"Bulk scan every 4h ({'/'.join(config.MAIN_TFS)}) · "
           f"active loop every 15m",
           f"POI threshold {config.POI_MAX_DIST_PCT}% from CMP",
           f"Active setups: {len(db.setups())}",
           f"Telegram: {'online' if sender.online() else 'DEGRADED'}"
           + (f" · {sender.queued()} queued" if sender.queued() else ""),
           f"Open signals {len(db.open_signals())}"]
    if STATE.get("last_scan"):
        out.append(f"Last scan {STATE['last_scan']}")
    await reply(u, "\n".join(out))


async def close(u, c):
    if not ok(u):
        return
    if not c.args:
        await reply(u, "Usage: /close 12")
        return
    try:
        sid = int(c.args[0].lstrip("#"))
    except ValueError:
        await reply(u, "Signal id must be a number.")
        return
    row = db.get(sid)
    if not row:
        await reply(u, "No such signal.")
        return
    st = STATE.get("stream")
    price = (st.price(row["symbol"]) if st else None) or row["entry_price"] \
        or (row["entry_low"] + row["entry_high"]) / 2
    mon = STATE.get("monitor")
    done = await mon.force_close(sid, price) if mon else False
    await reply(u, f"#{sid} closed." if done else f"#{sid} is not open.")


async def pause(u, c):
    if ok(u):
        STATE["paused"] = True
        await reply(u, "Dispatch paused. Monitoring continues.")


async def resume(u, c):
    if ok(u):
        STATE["paused"] = False
        await reply(u, "Dispatch resumed.")


_LAST_NET_ERR = [0.0, 0]


async def on_error(update, context):
    """Collapse Telegram network errors into one line a minute.

    Left alone, python-telegram-bot logs a full traceback for every failed
    getUpdates poll - roughly one every four seconds - which buries the
    scanner's own output completely.
    """
    err = context.error
    if isinstance(err, NetworkError):
        now = time.time()
        _LAST_NET_ERR[1] += 1
        if now - _LAST_NET_ERR[0] >= 60:
            log.warning("Telegram unreachable — %d polling error(s) in the last "
                        "minute (%s). Commands are down; signals are queued and "
                        "retried. Set TELEGRAM_PROXY if this persists.",
                        _LAST_NET_ERR[1], type(err).__name__)
            _LAST_NET_ERR[0] = now
            _LAST_NET_ERR[1] = 0
        return
    log.error("telegram handler error: %s", str(err).split("\n")[0][:200])


def build() -> Application:
    b = (Application.builder().token(config.TG_TOKEN)
         .connect_timeout(config.TG_TIMEOUT)
         .read_timeout(config.TG_TIMEOUT)
         .get_updates_read_timeout(config.TG_TIMEOUT))
    if config.TG_PROXY:
        b = b.proxy(config.TG_PROXY).get_updates_proxy(config.TG_PROXY)
    app = b.build()
    app.add_error_handler(on_error)
    for name, fn in {"start": start, "help": help_, "active": active, "pnl": pnl,
                     "report": report, "last": last, "cost": cost,
                     "status": status, "close": close, "pause": pause,
                     "setups": watch, "watch": watch,
                     "resume": resume}.items():
        app.add_handler(CommandHandler(name, fn))
    return app

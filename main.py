#!/usr/bin/env python3
"""SMC/ICT DeepSeek futures agent — hourly scan, 30m active watch.

  05:00 local   rebuild the top-80 watchlist
  hourly        scan all coins on 4h/1h/15m/5m → agent screens → flags
  30m           re-check every flagged coin on 5m/15m for entry trigger
  always        open signals checked every 60s for SL/TP
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal as sig
import time
from datetime import datetime, timezone

import config
from agent.client import Agent
from agent.tools import ToolBox
from core import features
from core.stream import MarketStream
from monitor.tracker import Monitor
from storage import db
from tg import bot as tgbot
from tg import send as tg

C = {"g": "\033[32m", "y": "\033[33m", "c": "\033[36m", "d": "\033[2m",
     "b": "\033[1m", "0": "\033[0m"}


class LocalTime(logging.Formatter):
    """Timestamp every log line in LOCAL_TZ, not the server's timezone.

    An Alibaba box defaults to Asia/Shanghai, so without this the log reads
    08:17 while the trader's clock says 05:47 and the schedule looks broken
    when it is not.
    """

    def formatTime(self, record, datefmt=None):
        # At interpreter shutdown strftime can fail with "sys.meta_path is
        # None" because the import system is already gone. A logger must never
        # be the thing that crashes teardown, so fall back to plain seconds.
        try:
            dt = datetime.fromtimestamp(record.created, config.LOCAL_TZ)
            return dt.strftime(datefmt or "%H:%M:%S")
        except Exception:
            return str(int(record.created))


def setup_logging():
    fmt = (f"{C['d']}%(asctime)s{C['0']} %(levelname)-5s "
           f"{C['c']}%(name)-7s{C['0']} %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(LocalTime(fmt, datefmt="%H:%M:%S"))
    root.addHandler(sh)
    fh = logging.handlers.RotatingFileHandler(config.LOG_DIR / "agent.log",
                                              maxBytes=8_000_000, backupCount=3)
    fh.setFormatter(LocalTime("%(asctime)s %(levelname)-5s %(name)-7s %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(fh)
    for n in ("httpx", "httpcore", "ccxt", "openai", "asyncio"):
        logging.getLogger(n).setLevel(logging.WARNING)
    # the updater logs a full traceback per failed poll; our own error handler
    # reports these once a minute instead
    for n in ("telegram", "telegram.ext.Updater", "telegram.ext._updater",
              "telegram.ext.ExtBot", "telegram.bot"):
        logging.getLogger(n).setLevel(logging.CRITICAL)


log = logging.getLogger("main")


def now_local() -> datetime:
    return datetime.now(config.LOCAL_TZ)


def in_window(now: datetime | None = None) -> bool:
    t = (now or now_local()).time()
    if config.ACTIVE_START <= config.ACTIVE_END:
        return config.ACTIVE_START <= t < config.ACTIVE_END
    return t >= config.ACTIVE_START or t < config.ACTIVE_END


def next_close(step: int) -> float:
    """Seconds until the next candle close of `step` seconds, plus the offset."""
    now = time.time()
    nxt = (now // step + 1) * step + config.ALIGN_OFFSET
    if nxt - now < 5:
        nxt += step
    return nxt - now


def clear_line():
    print(" " * 78, end="\r", flush=True)


async def heartbeat(seconds: float, label: str):
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            clear_line()
            return
        m, s = divmod(int(left), 60)
        print(f"{C['d']}[{now_local():%Y-%m-%d %H:%M:%S}] {label} — "
              f"{m}m {s:02d}s{C['0']}", end="\r", flush=True)
        await asyncio.sleep(min(config.HEARTBEAT_SECONDS, max(1, left)))


def upcoming_closes(n: int = 6) -> list[datetime]:
    out, t = [], time.time()
    step = config.HTF_SECONDS
    nxt = (t // step + 1) * step
    while len(out) < n:
        out.append(datetime.fromtimestamp(nxt, config.LOCAL_TZ))
        nxt += step
    return out


class Engine:
    def __init__(self, stream: MarketStream, agent: Agent):
        self.stream = stream
        self.agent = agent
        self.scan_no = 0
        self._last_refresh_day = None

    # ------------------------------------------------------------- payloads
    def views_for(self, sym: str, meta: dict) -> dict:
        out = {}
        for tf in config.TIMEFRAMES:
            try:
                v = features.build_view(meta, self.stream.get(sym, tf), tf)
            except Exception as ex:
                log.debug("build failed %s %s: %s", sym, tf, ex)
                continue
            if v:
                out[tf] = v
        return out

    def build_all(self):
        out = []
        for u in self.stream.universe:
            views = self.views_for(u["symbol"], u)
            if len(views) == len(config.TIMEFRAMES):
                out.append((u["symbol"], views))
        return out

    # ------------------------------------------------------------ 4h scan
    async def scan(self):
        self.scan_no += 1
        t0 = time.monotonic()
        clear_line()
        log.info("%s4h bulk scan #%d — %d coins x %s%s", C["b"], self.scan_no,
                 len(self.stream.universe), "/".join(config.MAIN_TFS), C["0"])

        built = await asyncio.get_running_loop().run_in_executor(None, self.build_all)
        if not built:
            log.warning("no data ready — websockets may still be warming up")
            return

        extras = {}
        try:
            extras = await self.stream.fetch_extras([s for s, _ in built])
        except Exception as ex:
            log.warning("extras unavailable: %s", ex)

        payloads = []
        for sym, views in built:
            ex = {"rs": self.stream.relative_strength(sym), "mkt": extras.get(sym)}
            payloads.append(features.encode_full(views, ex))
        self.agent.tb.symbols = {p["s"]: s for p, (s, _) in zip(payloads, built)}

        near = sum(1 for _, v in built
                   if (features.nearest_poi_any(v) or 99) <= config.POI_MAX_DIST_PCT)
        log.info("built %d coins in %.1fs · %d with a POI within %.1f%% of CMP",
                 len(payloads), time.monotonic() - t0, near, config.POI_MAX_DIST_PCT)

        if tgbot.STATE.get("paused"):
            log.info("dispatch paused — scan discarded")
            return

        sent = await self.agent.main_scan(payloads)
        db.set_meta("last_scan_ts", db.now())
        row = db.usage()
        log.info("%sscan #%d done in %.0fs — %d flagged, %d signal(s), "
                 "%d active · today $%.4f%s", C["g"] if sent else C["y"],
                 self.scan_no, time.monotonic() - t0, len(self.agent.tb.flagged),
                 sent, len(db.setups()), db.cost_of(row), C["0"])
        tgbot.STATE["last_scan"] = (f"#{self.scan_no} {now_local():%H:%M} "
                                    f"({len(self.agent.tb.flagged)} flagged, "
                                    f"{sent} signals)")

    # ------------------------------------------------------- 15-minute loop
    async def active(self):
        db.purge_setups()
        rows = db.setups()
        if not rows:
            return
        clear_line()
        t0 = time.monotonic()
        meta, payloads = [], []
        for r in rows:
            sym = r["symbol"]
            u = next((x for x in self.stream.universe if x["symbol"] == sym),
                     {"symbol": sym, "qv24": 0, "chg": 0.0})
            views = self.views_for(sym, u)
            if not all(tf in views for tf in config.ACTIVE_TFS):
                continue
            enc = features.encode_active(
                views, {"rs": self.stream.relative_strength(sym)})
            self.agent.tb.symbols[enc["s"]] = sym
            payloads.append(enc)
            meta.append({"symbol": enc["s"], "bias": r["bias"], "poi": r["poi"],
                         "trigger": r["trigger"],
                         "invalidation": r["invalidation"],
                         "checks": r["checks"],
                         "age_min": (db.now() - r["created_at"]) // 60})
            db.bump_setup(sym)
        if not payloads:
            return
        log.info("%s15m check — %d active setup(s): %s%s", C["b"], len(payloads),
                 ", ".join(m["symbol"] for m in meta), C["0"])
        sent = await self.agent.active_run(meta, payloads)
        log.info("15m check done in %.0fs — %d signal(s), %d still active",
                 time.monotonic() - t0, sent, len(db.setups()))

    # ---------------------------------------------------------------- loops
    async def startup_scan(self):
        if not config.SCAN_ON_START:
            return
        if not in_window():
            log.info("%sstartup outside %s-%s %s — waiting for the window%s",
                     C["y"], config.ACTIVE_START.strftime("%H:%M"),
                     config.ACTIVE_END.strftime("%H:%M"), config.LOCAL_TZ.key, C["0"])
            return
        mins = db.minutes_since("last_scan_ts")
        if mins < config.MIN_RESCAN_MINUTES:
            log.info("last scan %.0f min ago (< %d) — skipping startup scan, "
                     "next at %s", mins, config.MIN_RESCAN_MINUTES,
                     upcoming_closes(1)[0].strftime("%H:%M"))
            return
        log.info("%sstartup scan (last one %s)%s", C["b"],
                 "never" if mins > 1e8 else f"{mins:.0f} min ago", C["0"])
        try:
            await self.scan()
        except Exception as ex:
            log.exception("startup scan failed: %s", ex)

    async def scan_loop(self):
        await self.startup_scan()
        while True:
            await heartbeat(next_close(config.MAIN_SECONDS), "next 4h scan in")
            if not in_window():
                log.info("%s4h close outside the window — skipping bulk scan%s",
                         C["y"], C["0"])
                continue
            try:
                await self.scan()
            except Exception as ex:
                log.exception("scan failed: %s", ex)

    async def active_loop(self):
        while True:
            await asyncio.sleep(next_close(config.ACTIVE_SECONDS))
            if not in_window():
                continue
            try:
                await self.active()
            except Exception as ex:
                log.exception("15m check failed: %s", ex)

    async def refresh_loop(self):
        while True:
            now = now_local()
            if (self._last_refresh_day != now.date()
                    and now.time() >= config.WATCHLIST_REFRESH):
                self._last_refresh_day = now.date()
                try:
                    log.info("%sdaily watchlist refresh%s", C["b"], C["0"])
                    await self.stream.refresh_watchlist()
                except Exception as ex:
                    log.exception("watchlist refresh failed: %s", ex)
            await asyncio.sleep(60)


async def retry_loop():
    """Re-send anything Telegram refused while it was unreachable."""
    while True:
        await asyncio.sleep(config.TG_RETRY_SECONDS)
        try:
            if tg.queued():
                await tg.flush()
        except Exception:
            pass


async def amain():
    setup_logging()
    db.conn()
    missing = [k for k, v in (("DEEPSEEK_API_KEY", config.API_KEY),
                              ("TELEGRAM_BOT_TOKEN", config.TG_TOKEN),
                              ("TELEGRAM_CHAT_ID", config.TG_CHAT)) if not v]
    if missing:
        raise SystemExit("Missing in .env: " + ", ".join(missing))

    log.info("model=%s effort=%s · top %d coins · %s x%d each · "
             "bulk scan 4h, active loop 15m", config.MODEL,
             config.REASONING_EFFORT, config.WATCHLIST_SIZE,
             "/".join(config.TIMEFRAMES), config.CANDLES)

    utc = datetime.now(timezone.utc)
    sysname = str(datetime.now().astimezone().tzinfo)
    log.info("clocks: server %s (%s) · UTC %s · %s %s  <- all logs use %s",
             datetime.now().strftime("%H:%M"), sysname, utc.strftime("%H:%M"),
             now_local().strftime("%H:%M"), config.LOCAL_TZ.key, config.LOCAL_TZ.key)
    upcoming = [d for d in upcoming_closes(6) if in_window(d)][:4]
    log.info("window %s-%s %s · next scans: %s",
             config.ACTIVE_START.strftime("%H:%M"),
             config.ACTIVE_END.strftime("%H:%M"), config.LOCAL_TZ.key,
             ", ".join(d.strftime("%a %H:%M") for d in upcoming) or "none in window")

    stream = MarketStream()
    try:
        await stream.start()
    except Exception:
        # start() failing left an open aiohttp session; close it here or the
        # interpreter tears down mid-socket and buries the real traceback
        # under pages of ccxt __del__ and "Unclosed connector" noise.
        log.error("startup failed - closing sockets before exit")
        await stream.close()
        raise
    tb = ToolBox(stream)
    agent = Agent(tb)
    monitor = Monitor(stream)
    engine = Engine(stream, agent)

    app = tgbot.build()
    tg.set_bot(app.bot)
    reachable, detail = await tg.check()
    if reachable:
        log.info("telegram: connected as %s%s", detail,
                 f" via proxy {config.TG_PROXY}" if config.TG_PROXY else "")
    else:
        log.error("%stelegram UNREACHABLE: %s%s", C["y"], detail, C["0"])
        log.error("  signals will be queued and retried every %ds, and printed "
                  "here so nothing is lost.", config.TG_RETRY_SECONDS)
        log.error("  api.telegram.org is blocked from some regions — set "
                  "TELEGRAM_PROXY=http://user:pass@host:port in .env")
    tgbot.STATE.update({"stream": stream, "monitor": monitor, "engine": engine,
                        "paused": False})
    await app.initialize()
    await app.start()
    if config.TG_POLLING:
        await app.updater.start_polling(drop_pending_updates=True)
    else:
        log.info("telegram polling disabled (TELEGRAM_POLLING=0) — "
                 "outbound signals only")
    await tg.send(f"🤖 <b>SMC/ICT agent online</b>\nTop {len(stream.universe)} coins · "
                  f"4h bulk scan + 15m active loop · {config.ACTIVE_START:%H:%M}–"
                  f"{config.ACTIVE_END:%H:%M} {config.LOCAL_TZ.key}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (sig.SIGINT, sig.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass

    tasks = [asyncio.create_task(engine.scan_loop()),
             asyncio.create_task(engine.active_loop()),
             asyncio.create_task(engine.refresh_loop()),
             asyncio.create_task(monitor.run_forever()),
             asyncio.create_task(monitor.sample_forever()),
             asyncio.create_task(retry_loop())]
    try:
        await stop.wait()
    finally:
        log.info("shutting down")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if config.TG_POLLING:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            pass
        await stream.close()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass

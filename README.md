# SMC / ICT DeepSeek Futures Agent — 4H scan, 1H drill-down, hourly watch

```
05:00 SLST      rebuild the top-50 watchlist by 24h volume
startup + 4h    BULK SCAN: 50 coins x 1d/4h/1h/15m, 400 candles each
                        │
                        ├─► flag_setup ──► the 15m active list
                        └─► send_signal (if already triggering)
every 15m       ACTIVE LOOP: flagged coins only, 15m+1h full, 4h/1d digest
                        │
                  ┌─────┼─────┐
             send_signal keep drop
                  │
             Telegram + SQLite
always          open signals checked every 60s for SL / TP
```

## Timezone — you do not need to change the server clock

The schedule is anchored to `LOCAL_TZ` (Asia/Colombo) and to UTC candle
boundaries, never to the server's clock. An Alibaba box defaults to
Asia/Shanghai; the bot works correctly on it either way.

What *was* confusing: log lines used to print in the server's timezone, so the
log read 08:17 while your clock said 05:47 and the schedule looked broken when
it was not. **Every log line now prints in `LOCAL_TZ`**, and startup prints all
three clocks side by side plus the next four scan times:

```
clocks: server 08:17 (CST) · UTC 00:47 · 05:47 Asia/Colombo  <- all logs use Asia/Colombo
window 05:00-21:00 Asia/Colombo · next scans: Sat 09:30, Sat 13:30, Sat 17:30, Sun 05:30
```

Changing the server timezone with `timedatectl set-timezone Asia/Colombo` is
optional and makes no difference to behaviour.

## Startup scan

`SCAN_ON_START=1` (default) runs one scan immediately on boot rather than
waiting for the next 4h close — starting at 05:47 no longer means idling until
09:30. `MIN_RESCAN_MINUTES=60` guards it, so restarting the process repeatedly
cannot trigger repeated scans. Outside the window it waits as normal.

## Schedule

The 4h scan fires on the 4h candle close. In Sri Lanka time those land at
**05:30, 09:30, 13:30, 17:30** — exactly four inside the 05:00–21:00 window.
The 21:30 close falls just outside, which is why it is four scans and not five.

## The two loops

**Bulk scan — every 4h close, plus once on startup.** All 50 coins, all four
timeframes in full: 1d, 4h, 1h, 15m, 400 candles each. ~1,740 tokens per coin.
The agent's job here is triage — which coins have a live POI within
`POI_MAX_DIST_PCT` (1.5%) of CMP that all four timeframes agree on. Those get
`flag_setup` with a bias, an exact POI zone, a concrete trigger and a concrete
invalidation. If a setup is already triggering it can `send_signal` straight
away.

**Active loop — every 15m close.** Only the flagged coins. 15m and 1h in full,
4h and 1d **digested** (~1,160 tokens per coin): 1d and 4h structure cannot
change between 15-minute ticks, so resending them in full 64 times a day is
waste. The digest keeps the narrative, the dealing range, the two nearest POIs,
the nearest unswept pool each side, and the POC. For each coin the agent then
does exactly one of `send_signal`, `keep_setup`, or `drop_setup`. A coin stays
on the list until it fires, is dropped, or hits `SETUP_MAX_HOURS` (8).

There is no day/swing split. One multi-timeframe read: 1d gives direction, 4h
the narrative and the POI, 1h the structure shift with displacement, 15m the
trigger. All four must agree.

## How the agent chooses

The prompt grades every candidate on a 10-point rubric — HTF agreement, correct
half of the range, a reclaimed sweep, an untapped POI, real resting liquidity,
displacement on the origin leg, unswept liquidity to target, a tight
invalidation, momentum agreement, and session/positioning context. 8+ is A+,
6-7 is decent, below 6 is left alone.

It must then rank the whole batch and flag **at most `MAX_FLAGS_PER_SCAN` (4)**,
strongest first — the tool refuses past that, so the slots have to be spent on
its best work. Each flag states its score and top three reasons in the log, so
the choice is auditable.

Alignment is defined properly: a trade must agree with the **1d/4h draw on
liquidity**, not with all four bias labels. Price retracing down into a discount
POI with 1h and 15m bearish is the pullback — required, not disqualifying.

## Token cost

| | per call | per day | tokens/day |
|---|---|---|---|
| bulk scan | 1,740 × 50 coins | 4 scans | **0.36M** |
| active loop | 1,160 × N coins | 64 runs | 0.22M (N=3) … 0.59M (N=8) |

| active coins | tokens/day | $/day at $0.28/M | $2 lasts |
|---|---|---|---|
| 3 | 0.58M | $0.16 | 12 days |
| 5 | 0.73M | $0.21 | 10 days |
| 8 | 0.95M | $0.27 | 7.5 days |

`MAX_ACTIVE=8` caps the active list, which caps the bill. `/cost` reports real
spend from the API's own cache-hit / cache-miss token counts — set
`PRICE_IN_MISS` from your DeepSeek dashboard first.

## If Telegram is unreachable

Some hosting regions cannot reach `api.telegram.org`. The bot handles it rather
than falling over:

- startup runs `getMe` and says plainly whether Telegram is reachable
- failed sends are **queued and retried** every `TELEGRAM_RETRY_SECONDS`, and
  printed to the terminal marked `--- UNDELIVERED ---`, so a signal is never
  lost to a network fault
- polling errors are collapsed into one summary line a minute instead of a
  full traceback every four seconds
- `/status` shows `Telegram: online | DEGRADED · N queued`

If it stays down, set `TELEGRAM_PROXY=http://user:pass@host:port` in `.env`.
`TELEGRAM_POLLING=0` keeps outbound signals while disabling the command
interface.

## Install

```bash
bash scripts/install.sh
nano .env                       # DeepSeek key, Telegram token + chat id
./venv/bin/python scripts/preflight.py
./venv/bin/python main.py
```

## Telegram

`/active` `/watch` `/pnl [24h|7d|30d|all]` `/report` `/last [n]` `/cost`
`/status` `/close ID` `/pause` `/resume`

`/watch` shows every coin under hourly watch with its POI, trigger,
invalidation, age and check count.

## Layout

```
config.py            all tunables
main.py              4h scan loop, 1h watch loop, 05:00 watchlist refresh
core/stream.py       CCXT Pro websockets, top-50 watchlist, funding / OI / L-S
core/smc.py          OB, breaker, FVG, liquidity, structure, range, POC
core/features.py     pandas_ta, single-timeframe view, wire encoding
agent/prompt.py      scan / drill / watch prompts + schema legend
agent/tools.py       get_1h_context, watch_hourly, keep_watching, drop_watch, send_signal
agent/client.py      three-stage tool loop, token accounting
monitor/tracker.py   minute-by-minute SL/TP
storage/db.py        signals, events, watches, usage
tg/                  sender + commands
```

## Note

These are automated trade *ideas* from a model, not advice. Leveraged futures
can lose more than you put in. Paper-trade it and check `/pnl` first.

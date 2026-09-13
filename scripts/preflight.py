#!/usr/bin/env python3
"""Verify the box before running main.py.  python3 scripts/preflight.py"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402


async def main() -> int:
    ok = True
    print("== config ==")
    print(f"  model        : {config.MODEL} (effort={config.REASONING_EFFORT})")
    print(f"  api key      : {'set' if config.API_KEY else 'MISSING'}")
    print(f"  telegram     : {'set' if config.TG_TOKEN and config.TG_CHAT else 'MISSING'}")
    print(f"  watchlist    : top {config.WATCHLIST_SIZE}, rebuilt at "
          f"{config.WATCHLIST_REFRESH:%H:%M} {config.LOCAL_TZ.key}")
    print(f"  window       : {config.ACTIVE_START:%H:%M}-{config.ACTIVE_END:%H:%M}")
    print(f"  data         : {'/'.join(config.TIMEFRAMES)} x{config.CANDLES} each")
    print(f"  POI threshold: {config.POI_MAX_DIST_PCT}% from CMP")
    if not (config.API_KEY and config.TG_TOKEN and config.TG_CHAT):
        ok = False

    scans = 0
    print("\n== 4h closes inside your window ==")
    for h in (0, 4, 8, 12, 16, 20):
        t = datetime(2026, 1, 1, h, tzinfo=timezone.utc).astimezone(config.LOCAL_TZ)
        inside = config.ACTIVE_START <= t.time() < config.ACTIVE_END
        scans += inside
        print(f"  {h:02d}:00 UTC -> {t:%H:%M} local  {'SCAN' if inside else 'skip'}")
    print(f"  => {scans} scans/day")

    print("\n== exchange ==")
    from core.stream import MarketStream
    from core import features
    stream = MarketStream()
    kb4 = kb1 = 0.0
    try:
        t0 = time.time()
        uni = await stream.load_universe()
        print(f"  watchlist    : {len(uni)} coins in {time.time()-t0:.1f}s")
        print(f"  top 6        : "
              f"{', '.join(u['symbol'].split(':')[0] for u in uni[:6])}")
        print(f"  volume range : {uni[0]['qv24']/1e6:.0f}M - {uni[-1]['qv24']/1e6:.0f}M")
        stream.universe = uni[:3]
        t0 = time.time()
        await stream.seed()
        print(f"  seeded 3     : {time.time()-t0:.1f}s -> "
              f"~{len(uni)/3*(time.time()-t0):.0f}s for all {len(uni)}")

        sym = stream.universe[0]["symbol"]
        v4 = features.build_view(stream.universe[0], stream.get(sym, config.HTF),
                                 config.HTF)
        v1 = features.build_view(stream.universe[0], stream.get(sym, config.LTF),
                                 config.LTF)
        ex = await stream.fetch_extras([sym])
        print(f"  extras       : {'funding/OI/LS OK' if ex else 'UNAVAILABLE'} "
              f"{list(ex.get(sym, {}).keys())}")
        e4 = features.encode_view(v4, {"rs": stream.relative_strength(sym),
                                       "mkt": ex.get(sym)})
        e1 = features.encode_view(v1, {"rs": stream.relative_strength(sym)})
        kb4 = len(json.dumps(e4, separators=(",", ":"), default=float))
        kb1 = len(json.dumps(e1, separators=(",", ":"), default=float))
        print(f"  4h view      : {kb4/1.6:.0f} tokens   "
              f"nearest POI {features.nearest_poi_pct(v4)}%")
        print(f"  1h view      : {kb1/1.6:.0f} tokens")
    except Exception as e:
        print(f"  FAILED: {e}")
        ok = False
    finally:
        await stream.close()

    if kb4:
        per = config.WATCHLIST_SIZE * kb4 / 1.6
        print(f"\n== token projection ==")
        print(f"  stage 1      : {per/1000:.1f}k/scan x {scans} = {per*scans/1000:.0f}k/day")
        print(f"  + drills/watch (est)                     ~{(5*4+4*16)*kb1/1.6/1000:.0f}k/day")
        print(f"  => 1M input tokens lasts "
              f"~{1e6/(per*scans + (5*4+4*16)*kb1/1.6):.1f} days")

    print("\n== telegram ==")
    try:
        from tg import send as tg
        mid = await tg.send("✅ Preflight OK.")
        print(f"  sent (id {mid})" if mid else "  FAILED")
        ok = ok and bool(mid)
    except Exception as e:
        print(f"  FAILED: {e}")
        ok = False

    print("\n== deepseek ==")
    try:
        from openai import AsyncOpenAI
        c = AsyncOpenAI(api_key=config.API_KEY, base_url=config.BASE_URL, timeout=60)
        kw = dict(model=config.MODEL,
                  messages=[{"role": "user", "content": "Reply one word: READY"}])
        try:
            r = await c.chat.completions.create(
                **kw, extra_body={"reasoning_effort": config.REASONING_EFFORT,
                                  "thinking": {"type": "enabled"}})
            print("  reasoning params: accepted")
        except Exception as e:
            print(f"  reasoning params: REJECTED ({str(e).splitlines()[0][:80]})")
            print("                    the agent falls back automatically")
            r = await c.chat.completions.create(**kw)
        print(f"  reply        : {(r.choices[0].message.content or '').strip()[:30]}")
        u = r.usage
        print(f"  usage fields : hit={getattr(u,'prompt_cache_hit_tokens','n/a')} "
              f"miss={getattr(u,'prompt_cache_miss_tokens','n/a')}")
    except Exception as e:
        print(f"  FAILED: {str(e).splitlines()[0][:180]}")
        print("  -> check DEEPSEEK_MODEL matches what your account exposes")
        ok = False

    print("\n" + ("ALL CHECKS PASSED - python3 main.py" if ok else "FIX THE ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

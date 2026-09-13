"""DeepSeek agent: 4h scan -> 1h drill-down -> hourly watch."""
from __future__ import annotations

import asyncio
import json
import logging

from openai import AsyncOpenAI

import config
from agent import prompt as P
from agent import tools as T
from storage import db

log = logging.getLogger("agent")


def dumps(o) -> str:
    return json.dumps(o, separators=(",", ":"), default=float)


class Agent:
    def __init__(self, toolbox: T.ToolBox):
        self.tb = toolbox
        self.client = AsyncOpenAI(api_key=config.API_KEY, base_url=config.BASE_URL,
                                  timeout=config.REQUEST_TIMEOUT, max_retries=2)
        self._extra_ok = True

    # ------------------------------------------------------------------- call
    async def _chat(self, messages, tools):
        kw = dict(model=config.MODEL, messages=messages, tools=tools,
                  tool_choice="auto", temperature=config.TEMPERATURE)
        if self._extra_ok and config.REASONING_EFFORT:
            kw["extra_body"] = {"reasoning_effort": config.REASONING_EFFORT,
                                "thinking": {"type": "enabled"}}
        try:
            return await self.client.chat.completions.create(**kw)
        except Exception as ex:
            msg = str(ex)
            if self._extra_ok and any(k in msg for k in
                                      ("reasoning_effort", "thinking",
                                       "Unrecognized", "invalid_request")):
                log.warning("API rejected reasoning params, retrying without: %s",
                            msg.split("\n")[0][:110])
                self._extra_ok = False
                kw.pop("extra_body", None)
                return await self.client.chat.completions.create(**kw)
            raise

    def _account(self, resp, label: str):
        u = getattr(resp, "usage", None)
        if not u:
            return
        hit = getattr(u, "prompt_cache_hit_tokens", None)
        miss = getattr(u, "prompt_cache_miss_tokens", None)
        if hit is None and miss is None:
            miss, hit = getattr(u, "prompt_tokens", 0) or 0, 0
        out = getattr(u, "completion_tokens", 0) or 0
        db.add_usage(hit or 0, miss or 0, out)
        log.info("  %s | in %s hit + %s miss, out %s", label,
                 f"{hit or 0:,}", f"{miss or 0:,}", f"{out:,}")

    async def _run(self, system: str, user: str, payload, tools, label: str,
                   handlers: dict, rounds: int = 1) -> int:
        """One agent turn.

        `rounds` is deliberately 1 for the bulk scan: a second round would
        resend the entire 45k-token payload just to let the model react to its
        own tool results, which buys nothing. Models emit all their calls in a
        single response anyway.
        """
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user + dumps(payload)}]
        sent = 0
        for rnd in range(max(1, rounds)):
            try:
                resp = await self._chat(messages, tools)
            except Exception as ex:
                log.error("%s failed: %s", label, str(ex).split("\n")[0][:200])
                return sent
            self._account(resp, f"{label} r{rnd + 1}")
            msg = resp.choices[0].message
            calls = list(msg.tool_calls or [])
            if not calls:
                txt = (msg.content or "").strip()
                if txt and txt.upper() != "NONE":
                    log.info("  %s: %s", label, txt[:700])
                    db.set_meta("last_note", f"{label}: {txt[:900]}")
                break
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [
                                 {"id": c.id, "type": "function",
                                  "function": {"name": c.function.name,
                                               "arguments": c.function.arguments}}
                                 for c in calls]})
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                fn = handlers.get(c.function.name)
                if fn is None:
                    res = {"error": f"unknown tool {c.function.name}"}
                else:
                    try:
                        res = await fn(args) if asyncio.iscoroutinefunction(fn) \
                            else fn(args)
                    except Exception as ex:
                        log.exception("tool %s failed", c.function.name)
                        res = {"error": str(ex)[:200]}
                if c.function.name == "send_signal" and res.get("status") == "sent":
                    sent += 1
                messages.append({"role": "tool", "tool_call_id": c.id,
                                 "content": dumps(res)[:3000]})
            if rnd + 1 >= rounds and len(calls) >= config.BUSY_ROUND_WARN:
                log.info("  %s: %d tool call(s) in one round", label, len(calls))
        return sent

    # ------------------------------------------------------------ bulk scan
    async def main_scan(self, payloads: list[dict]) -> int:
        """4h scan over the whole watchlist. Returns signals sent directly."""
        self.tb.flagged = []
        self.tb.known = {c["s"] for c in payloads}
        n = config.COINS_PER_REQUEST
        batches = [payloads[i:i + n] for i in range(0, len(payloads), n)]
        system = (P.MAIN_SYSTEM
                  .replace("{poi}", str(config.POI_MAX_DIST_PCT))
                  .replace("{maxflags}", str(config.MAX_FLAGS_PER_SCAN)))
        handlers = {"flag_setup": self.tb.flag, "send_signal": self.tb.send_signal}
        log.info("4h scan: %d coins in %d request(s)", len(payloads), len(batches))
        sem = asyncio.Semaphore(config.AGENT_CONCURRENCY)

        async def one(i, b):
            async with sem:
                return await self._run(
                    system, P.main_user(i, len(batches), [x["s"] for x in b]),
                    {"coins": b}, T.MAIN_TOOLS, f"scan {i}/{len(batches)}",
                    handlers, rounds=1)

        res = await asyncio.gather(*[one(i, b) for i, b in enumerate(batches, 1)],
                                   return_exceptions=True)
        return sum(r for r in res if isinstance(r, int))

    # --------------------------------------------------------- 15-minute loop
    async def active_run(self, rows: list[dict], payloads: list[dict]) -> int:
        """One 15m check across the active setups."""
        if not payloads:
            return 0
        self.tb.known = {c["s"] for c in payloads}
        handlers = {"send_signal": self.tb.send_signal,
                    "keep_setup": self.tb.keep, "drop_setup": self.tb.drop}
        n = config.ACTIVE_BATCH
        sent = 0
        for i in range(0, len(payloads), n):
            chunk = payloads[i:i + n]
            meta = rows[i:i + n]
            sent += await self._run(P.ACTIVE_SYSTEM, P.active_user(meta),
                                    {"coins": chunk}, T.ACTIVE_TOOLS,
                                    f"active {i // n + 1}", handlers,
                                    rounds=config.ACTIVE_ROUNDS)
        return sent

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal

import httpx

from .activity import observe
from .config import Settings
from .db import Store
from .demo import DemoQuotes, seed
from .domain import D, Fill, Signal, now
from .engine import Engine
from .execution import LiveExecutor
from .history import History
from .ipc import Control
from .providers import Fomo, Relay, scrape_public_fomo
from .ranking import rank
from .rpc import Monitor


class Daemon:
    def __init__(self, cfg: Settings, demo=False):
        if demo and cfg.mode != "paper":
            raise ValueError("demo cannot run live")
        self.cfg, self.demo = cfg, demo
        cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if cfg.state_dir.is_symlink() or cfg.state_dir.stat().st_mode & 0o077:
            raise ValueError("state directory must be private (0700), not a symlink")
        self.lockfile = (cfg.state_dir / "daemon.lockfile").open("a")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lockfile.close()
            raise ValueError("another daemon owns this state directory") from exc
        self.store = Store(cfg.state_dir / "spy.sqlite")
        provenance = self.store.get("environment")
        expected = "synthetic-demo" if demo else "real"
        if provenance and provenance != expected:
            raise ValueError("demo and real data require separate state directories")
        self.store.put("environment", expected)
        self.http = httpx.AsyncClient(timeout=15, follow_redirects=False)
        live = LiveExecutor(cfg, self.store, self.http) if cfg.mode == "live" else None
        self.quotes = DemoQuotes(cfg) if demo else (live or Relay(cfg, self.http))
        self.engine = Engine(cfg, self.store, self.quotes, live, rpc_http=self.http)
        self.fomo = Fomo(cfg, self.store, self.http)
        self.history = History(cfg, self.store, self.http, self.engine.health)
        self.control = Control(self.engine, self.fomo.credits)
        self.stop = asyncio.Event()
        self.monitor = Monitor(
            cfg,
            self.store,
            self.http,
            self.wallets,
            self.engine.event,
            self.engine.reorg,
            self.engine.health,
        )

    def wallets(self, chain):
        result = {}
        for trader in self.engine.watched():
            row = self.store.get("trader:" + trader, {})
            wallet = row.get("wallets", {}).get("solana" if chain.kind == "solana" else "evm")
            if wallet:
                result[trader] = wallet
        return result

    async def discovery(self):
        if not self.fomo.key:
            self.engine.health(
                "discovery",
                {
                    "state": "unavailable",
                    "reason": "FOMO_API_KEY missing; configure and restart",
                    "at": now(),
                },
            )
            await self.stop.wait()
            return
        while not self.stop.is_set():
            retry_after = self.cfg.discovery_ttl
            try:
                traders = await self.fomo.discover()
                # Retain leaderboard order and candidates even if history calls
                # subsequently hit the monitoring credit reserve.
                for trader in traders:
                    if trader.get("userId"):
                        self.store.put("trader:" + trader["userId"], trader)
                for trader in traders:
                    uid = trader.get("userId")
                    if not uid:
                        continue
                    self.store.put("trader:" + uid, trader)
                    history = await self.fomo.history(uid)
                    self.store.put("history_raw:" + uid, history)
                    self.history.evaluate(trader)
                self.engine.health(
                    "discovery", {"state": "ready", "candidates": len(traders), "at": now()}
                )
            except Exception as exc:
                retry_after = 300
                self.engine.health(
                    "discovery", {"state": "unavailable", "error": type(exc).__name__, "at": now()}
                )
            for url in self.cfg.direct_fomo_urls:
                try:
                    capture = await scrape_public_fomo(url, self.http)
                    self.store.put("direct_capture:" + url, capture)
                    self.engine.health(
                        "direct_fomo",
                        {"state": "capture only", "documents": len(capture["documents"])},
                    )
                except Exception as exc:
                    self.engine.health(
                        "direct_fomo", {"state": "unavailable", "error": type(exc).__name__}
                    )
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=retry_after)
            except TimeoutError:
                pass

    def import_history(self):
        if not self.cfg.history_import:
            return
        data = json.loads(self.cfg.history_import.read_text())
        for item in data["traders"]:
            uid = item["userId"]
            fills = [Fill.model_validate(f) for f in item["fills"]]
            if any(f.trader != uid for f in fills):
                raise ValueError("history trader ID mismatch")
            if any(f.provenance.startswith("SYNTHETIC") for f in fills):
                raise ValueError("synthetic data may not be imported into real state")
            evidence = rank(fills, complete_30d=False)
            evidence["missing"] = [
                "imported coverage claims require independent chain reconstruction"
            ]
            evidence["provenance"] = item.get("provenance", "operator-imported history")
            self.store.put("ranking:" + uid, evidence)
            self.store.put("trader:" + uid, {k: item[k] for k in ("userId", "handle", "wallets")})
            self.store.put("evidence_import:" + uid, item)

    async def social(self, data, historical=False):
        uid, eid = data.get("userId"), data.get("eventId")
        if not uid or not eid or data.get("alertType") not in ("buy", "sell"):
            return
        ts = data.get("ts")
        ts = float(ts) / 1000 if isinstance(ts, (int, float)) and ts > 10**12 else ts
        ts = ts if isinstance(ts, (int, float)) else None
        lag = now() - ts if ts is not None else None
        watched = uid in self.engine.watched()
        token = data.get("token") or {}
        chain_id = data.get("chainId", data.get("networkId"))
        chain = next((c.name for c in self.cfg.chains if str(c.fomo_id) == str(chain_id)), "")
        reason = (
            "Replay: observation only"
            if historical
            else "Awaiting exact RPC transaction identity"
            if watched
            else "Trader not selected; observation only"
        )
        if not observe(
            self.store,
            "fomo:" + str(eid),
            kind="FOMO alert",
            trader=uid,
            chain=chain or str(data.get("chain") or ""),
            side=data["alertType"],
            token=data.get("tokenAddress")
            or (
                (token.get("address") or token.get("symbol") or "")
                if isinstance(token, dict)
                else str(token)
            ),
            timestamp=ts,
            status="observed",
            reason=reason,
        ):
            return
        self.engine.health(
            "social:" + uid if watched else "social_feed",
            {
                "state": "observed; awaiting RPC transaction identity"
                if watched
                else "receiving alerts; no selected trader required",
                "delay_seconds": lag,
                "at": now(),
                "historical": historical,
                "reason": "app feed lacks exact fills/tx hashes; never trade from positionValueUsd",
            },
        )

    async def reconstruct(self):
        while not self.stop.is_set():
            traders = sorted(
                self.store.items("trader:").values(), key=lambda t: t.get("rank", 10000)
            )
            for trader in traders:
                if self.stop.is_set():
                    return
                try:
                    await self.history.step(trader)
                except Exception as exc:
                    self.engine.health(
                        "history",
                        {"state": "data unavailable", "error": type(exc).__name__, "at": now()},
                    )
                await asyncio.sleep(0)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.cfg.history_refresh_seconds)
            except TimeoutError:
                pass

    async def maintenance(self):
        last_mark = 0
        while not self.stop.is_set():
            try:
                await self.engine.recover()
                if now() - last_mark >= 15:
                    await self.engine.mark_positions()
                    last_mark = now()
            except Exception as exc:
                self.engine.health(
                    "recovery", {"state": "degraded", "error": type(exc).__name__, "at": now()}
                )
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=2)
            except TimeoutError:
                pass

    async def demo_events(self):
        count = 0
        while not self.stop.is_set():
            count += 1
            sig = Signal(
                trader="demo-7",
                chain="base",
                token="DEMO",
                side="buy" if count % 3 == 1 else "sell",
                quantity=D(100 if count % 3 == 1 else 50),
                source_before=D(0 if count % 3 == 1 else (100 if count % 3 == 2 else 50)),
                source_after=D(100 if count % 3 == 1 else (50 if count % 3 == 2 else 0)),
                token_decimals=6,
                tx=f"demo-{self.started_demo}-{count}",
                block=count,
                block_hash=f"synthetic-{count}",
                timestamp=now(),
                finalized=True,
                provider="synthetic",
            )
            await self.engine.event(sig)
            self.engine.health("demo", {"state": "SYNTHETIC OFFLINE DATA", "at": now()})
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=3)
            except TimeoutError:
                pass

    async def run(self):
        os.umask(0o077)
        if self.demo:
            seed(self.store)
            self.started_demo = now()
        else:
            self.import_history()
        await self.engine.recover()
        server = await self.control.serve(self.cfg.socket_path)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)
        tasks = [asyncio.create_task(self.maintenance())]
        if self.demo:
            tasks.append(asyncio.create_task(self.demo_events()))
        else:
            tasks.extend(
                [
                    asyncio.create_task(self.discovery()),
                    asyncio.create_task(self.reconstruct()),
                    asyncio.create_task(
                        self.fomo.stream(self.social, self.engine.health, self.stop)
                    ),
                ]
            )
            for chain in self.cfg.chains:
                if chain.enabled:
                    tasks.append(asyncio.create_task(self.supervise_chain(chain)))
        try:
            await self.stop.wait()
        finally:
            self.monitor.stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            server.close()
            await server.wait_closed()
            self.cfg.socket_path.unlink(missing_ok=True)
            await self.http.aclose()
            self.store.close()
            self.lockfile.close()

    async def supervise_chain(self, chain):
        while not self.stop.is_set():
            try:
                await self.monitor.run_chain(chain)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.engine.health(
                    "rpc:" + chain.name,
                    {"state": "unavailable", "error": type(exc).__name__, "at": now()},
                )
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=30)
            except TimeoutError:
                pass

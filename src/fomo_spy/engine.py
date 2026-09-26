from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

from sqlalchemy import select

from .config import Settings
from .db import KV, Cash, Event, Ledger, Order, Position, Store
from .domain import D, Quote, Signal, now
from .providers import Unavailable


class Engine:
    def __init__(self, cfg: Settings, store: Store, quotes, live=None, rpc_http=None):
        self.cfg, self.store, self.quotes, self.live = cfg, store, quotes, live
        self.rpc_http = rpc_http
        self.lock = asyncio.Lock()
        self.started = now()
        enabled = [c for c in cfg.chains if c.enabled]
        with store.session() as s:
            existing_cash = list(s.scalars(select(Cash).where(Cash.id.startswith(cfg.mode + ":"))))
            if existing_cash and any(not s.get(Cash, cfg.mode + ":" + c.name) for c in enabled):
                raise ValueError(
                    "chain allocation is already initialized; use a fresh paper state or an explicit audited allocation migration"
                )
            for i, c in enumerate(enabled):
                key = f"{cfg.mode}:{c.name}"
                if not s.get(Cash, key):
                    # All chain allocations sum to one total, not $1000 on every chain.
                    share = cfg.limits.capital_usd / len(enabled)
                    if i == len(enabled) - 1:
                        share = cfg.limits.capital_usd - share * i
                    s.add(Cash(id=key, balance=str(share)))

    def health(self, provider, value):
        self.store.put("health:" + provider, value)

    def rankings(self):
        excluded = set(self.store.get("excluded", []))
        ranks = [
            {
                **value,
                "trader": key.removeprefix("ranking:"),
                "excluded_by_user": key.removeprefix("ranking:") in excluded,
            }
            for key, value in self.store.items("ranking:").items()
        ]
        return sorted(
            ranks,
            key=lambda r: r.get("score") if r.get("score") is not None else -1e99,
            reverse=True,
        )

    def selected(self):
        return [
            r["trader"]
            for r in self.rankings()
            if r["eligible"]
            and not r["excluded_by_user"]
            and now() - r["evaluated_at"] <= self.cfg.discovery_ttl
        ][: self.cfg.follow]

    def watched(self):
        with self.store.session() as s:
            positions = s.scalars(select(Position).where(Position.mode == self.cfg.mode)).all()
            return sorted(set(self.selected()) | {p.trader for p in positions if D(p.quantity) > 0})

    def position_id(self, trader, chain, token):
        return hashlib.sha256("|".join([self.cfg.mode, trader, chain, token]).encode()).hexdigest()[
            :24
        ]

    def _equity(self, s):
        cash = sum(
            (
                D(c.balance)
                for c in s.scalars(select(Cash).where(Cash.id.startswith(self.cfg.mode + ":")))
            ),
            D(0),
        )
        positions = s.scalars(select(Position).where(Position.mode == self.cfg.mode)).all()
        return cash + sum((D(p.mark) for p in positions if D(p.quantity) > 0), D(0))

    def risk_reason(self, s, chain, cost):
        limits = self.cfg.limits
        if self.store.get("paused:" + self.cfg.mode, False):
            return "entries paused"
        if self.store.get("chain_halt:" + chain, False):
            return "chain reconciliation required"
        positions = [
            p
            for p in s.scalars(select(Position).where(Position.mode == self.cfg.mode))
            if D(p.quantity) > 0
        ]
        pending = list(
            s.scalars(
                select(Order).where(
                    Order.mode == self.cfg.mode,
                    Order.state.in_(
                        [
                            "prepared",
                            "signed",
                            "broadcast",
                            "uncertain",
                            "approval_confirmed",
                            "reconcile",
                        ]
                    ),
                )
            )
        )
        if any(p.needs_reconcile for p in positions):
            return "inventory reconciliation required"
        if any(now() - p.marked_at > 60 for p in positions):
            return "position liquidation marks are stale"
        reserved = sum((D(o.data.get("reserve", "0")) for o in pending if o.side == "buy"), D(0))
        exposure = sum((max(D(p.cost), D(p.mark)) for p in positions), D(0)) + reserved
        if exposure + cost > limits.exposure_usd:
            return "maximum exposure"
        if len(positions) + sum(o.side == "buy" for o in pending) >= limits.positions:
            return "maximum positions"
        equity = self._equity(s)
        day = datetime.now(timezone.utc).date().isoformat()
        k = "daily:" + self.cfg.mode + ":" + day
        baseline = s.get(KV, k)
        if not baseline:
            # At process start today's ledger PnL restores losses already incurred today.
            midnight = (
                datetime.now(timezone.utc)
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .timestamp()
            )
            realized_today = sum(
                (
                    D(entry.realized)
                    for entry in s.scalars(
                        select(Ledger).where(
                            Ledger.mode == self.cfg.mode, Ledger.timestamp >= midnight
                        )
                    )
                ),
                D(0),
            )
            baseline = KV(key=k, value={"equity": str(equity - realized_today)})
            s.add(baseline)
        if D(baseline.value["equity"]) - equity >= limits.daily_loss_usd:
            return "daily equity loss halt"
        cash = s.get(Cash, self.cfg.mode + ":" + chain)
        chain_reserved = sum(
            (D(o.data.get("reserve", "0")) for o in pending if o.data.get("chain") == chain), D(0)
        )
        if not cash or D(cash.balance) - chain_reserved < cost:
            return "insufficient prefunded chain allocation"
        return ""

    async def event(self, sig: Signal):
        async with self.lock:
            with self.store.session() as s:
                existing = s.get(Event, sig.key)
                if existing:
                    if existing.data["block_hash"] != sig.block_hash:
                        existing.status, existing.reason = "reorg", "changed block identity"
                        self._halt_chain(s, sig.chain, sig.block)
                    return "duplicate"
                s.add(
                    Event(
                        key=sig.key,
                        data=sig.model_dump(mode="json"),
                        status="received",
                        reason="",
                        received=sig.received,
                    )
                )
            lag = sig.received - sig.timestamp
            self.store.put(
                "latency:" + sig.key,
                {
                    "chain": sig.chain,
                    "provider": sig.provider,
                    "chain_to_receive_ms": round(lag * 1000),
                    "at": sig.received,
                },
            )
            reason = ""
            if sig.historical or sig.timestamp < self.started:
                reason = "historical/replay event: never copies"
            elif not sig.finalized:
                reason = "not confirmed/finalized"
            elif sig.side == "transfer":
                reason = "transfer or undecoded route: reconcile inventory"
            elif lag < -2:
                reason = "future timestamp / clock skew"
            elif sig.side == "buy" and now() - sig.timestamp > self.cfg.max_signal_age:
                reason = "entry older than 30 seconds"
            elif not self.cfg.chain(sig.chain).enabled:
                reason = "chain disabled"
            elif sig.side == "buy" and sig.trader not in self.selected():
                reason = "trader not selected"
            pid = self.position_id(sig.trader, sig.chain, sig.token)
            if reason:
                with self.store.session() as s:
                    p = s.get(Position, pid)
                    if (
                        p
                        and D(p.quantity) > 0
                        and (sig.side in ("sell", "transfer") or sig.historical)
                    ):
                        p.needs_reconcile = True
                    e = s.get(Event, sig.key)
                    e.status, e.reason = "rejected", reason
                return reason
            if sig.side == "buy" and self.cfg.auto_bridge:
                await self.paper_bridge(sig.chain)
            with self.store.session() as s:
                p = s.get(Position, pid)
                if sig.side == "sell":
                    if not p or D(p.quantity) <= 0:
                        reason = "no copied inventory for this source trader"
                    elif (
                        sig.source_before is None
                        or sig.source_after is None
                        or sig.source_before <= 0
                        or sig.quantity > sig.source_before
                        or sig.source_before - sig.quantity != sig.source_after
                    ):
                        p.needs_reconcile = True
                        reason = "unknown/inconsistent source inventory; reconciliation required"
                    elif p.needs_reconcile:
                        reason = "copied inventory requires reconciliation"
                    if not reason:
                        fraction = sig.quantity / sig.source_before
                        amount = int(D(p.quantity) * fraction * 10**p.decimals)
                        if amount <= 0:
                            reason = "exit below token precision"
                else:
                    amount = int(self.cfg.limits.buy_usd * 10 ** self.cfg.chain(sig.chain).decimals)
                    reason = self.risk_reason(s, sig.chain, self.cfg.limits.buy_usd)
                if reason:
                    e = s.get(Event, sig.key)
                    e.status, e.reason = "rejected", reason
                    return reason
            return await self._execute(sig, pid, amount)

    async def paper_bridge(self, destination):
        if self.cfg.mode != "paper":
            raise Unavailable("automatic live bridging is not validated")
        needed = self.cfg.limits.buy_usd + self.cfg.limits.max_fee_usd
        with self.store.session() as s:
            target = s.get(Cash, "paper:" + destination)
            if not target or D(target.balance) >= needed:
                return
            if (
                self.risk_reason(s, destination, needed)
                != "insufficient prefunded chain allocation"
            ):
                return
            donors = sorted(
                s.scalars(select(Cash).where(Cash.id.startswith("paper:"))),
                key=lambda c: D(c.balance),
                reverse=True,
            )
            donor = next(
                (c for c in donors if c.id != target.id and D(c.balance) > needed * 2), None
            )
            if not donor:
                return
            source = donor.id.split(":", 1)[1]
            amount = min(self.cfg.bridge_max_usd, D(donor.balance) - needed)
        try:
            bridge = await self.quotes.bridge(source, destination, amount)
            spent, received, fee = map(D, (bridge["spend"], bridge["receive"], bridge["fee"]))
            if spent > self.cfg.bridge_max_usd or spent <= 0 or received <= 0 or fee < 0:
                raise Unavailable("bridge quote violates amount limits")
            oid = "paper:bridge:" + str(now())
            with self.store.session() as s:
                donor, target = s.get(Cash, "paper:" + source), s.get(Cash, "paper:" + destination)
                if D(donor.balance) < spent + fee:
                    raise Unavailable("bridge funding changed")
                donor.balance = str(D(donor.balance) - spent - fee)
                target.balance = str(D(target.balance) + received)
                s.add(
                    Order(
                        id=oid,
                        mode="paper",
                        event_key="",
                        position_id="",
                        side="bridge",
                        state="filled",
                        created=now(),
                        data={"chain": source, "destination": destination, **bridge},
                        tx_hash="",
                        signed="",
                        error="",
                    )
                )
                s.add(
                    Ledger(
                        id=oid + ":out",
                        mode="paper",
                        chain=source,
                        timestamp=now(),
                        cash_delta=str(-spent - fee),
                        realized=str(received - spent - fee),
                        details=bridge,
                    )
                )
                s.add(
                    Ledger(
                        id=oid + ":in",
                        mode="paper",
                        chain=destination,
                        timestamp=now(),
                        cash_delta=str(received),
                        realized="0",
                        details=bridge,
                    )
                )
            self.health(
                "bridge",
                {
                    "state": "paper simulated",
                    "at": now(),
                    "source": source,
                    "destination": destination,
                },
            )
        except Exception as exc:
            self.health(
                "bridge", {"state": "unavailable", "error": type(exc).__name__, "at": now()}
            )

    async def _execute(self, sig, pid, amount, manual=False):
        order_id = self.cfg.mode + ":" + sig.key
        try:
            quote = await self.quotes.quote(
                sig.chain, sig.token, sig.side, amount, sig.token_decimals
            )
            self.validate_quote(quote, sig, amount)
            if sig.side == "buy" and sig.trader not in self.selected():
                raise Unavailable("trader deselected while obtaining quote")
            if sig.side == "buy" and not manual and now() - sig.timestamp > self.cfg.max_signal_age:
                raise Unavailable("signal expired while obtaining quote")
            with self.store.session() as s:
                if sig.side == "buy":
                    reason = self.risk_reason(s, sig.chain, quote.usd + quote.fee_usd)
                    if reason:
                        raise Unavailable(reason)
                s.add(
                    Order(
                        id=order_id,
                        mode=self.cfg.mode,
                        event_key=sig.key,
                        position_id=pid,
                        side=sig.side,
                        state="prepared",
                        created=now(),
                        data={
                            "chain": sig.chain,
                            "trader": sig.trader,
                            "token": sig.token,
                            "decimals": sig.token_decimals,
                            "signal_at": sig.timestamp,
                            "manual": manual,
                            "reserve": str(quote.usd + quote.fee_usd) if sig.side == "buy" else "0",
                            "quote": quote.model_dump(mode="json"),
                        },
                        tx_hash="",
                        signed="",
                        error="",
                    )
                )
            if self.cfg.mode == "paper":
                # Pessimistic fill: minimum route output, then additional adverse adjustment.
                output = quote.minimum_output * (10000 - self.cfg.limits.adverse_bps) // 10000
                self.settle(order_id, output, quote.fee_usd)
            else:
                if not self.live:
                    raise Unavailable("live executor unavailable")
                await self.live.prepare_and_send(order_id, quote)
            with self.store.session() as s:
                e = s.get(Event, sig.key)
                if e:
                    e.status = "processed"
            self.store.put(
                "decision:" + sig.key,
                {
                    "chain": sig.chain,
                    "at": now(),
                    "receive_to_decision_ms": round((now() - sig.received) * 1000),
                    "chain_to_decision_ms": round((now() - sig.timestamp) * 1000),
                },
            )
            return "processed"
        except Exception as exc:
            # Do not destroy uncertain state if broadcast may have occurred.
            message = str(exc) if isinstance(exc, Unavailable) else type(exc).__name__
            with self.store.session() as s:
                o = s.get(Order, order_id)
                if o and o.state == "prepared":
                    o.state, o.error = "rejected", message
                e = s.get(Event, sig.key)
                if e:
                    e.status, e.reason = "rejected", message
                p = s.get(Position, pid)
                if sig.side == "sell" and p:
                    p.needs_reconcile = True
            return message

    def validate_quote(self, q, sig, amount):
        if (
            q.chain != sig.chain
            or q.token != sig.token
            or q.side != sig.side
            or q.input_amount != amount
            or q.token_decimals != sig.token_decimals
            or q.expires < now()
            or q.timestamp > now() + 2
            or now() - q.timestamp > 15
        ):
            raise Unavailable("quote mismatches intent or is stale")
        if q.fee_usd > self.cfg.limits.max_fee_usd:
            raise Unavailable("fee limit exceeded")
        if (
            q.side == "buy"
            and abs(q.usd - self.cfg.limits.buy_usd) > D("0.01") * self.cfg.limits.buy_usd
        ):
            raise Unavailable("settlement USD valuation outside 1% peg tolerance")
        if q.minimum_output < q.output_amount * (10000 - self.cfg.limits.slippage_bps) // 10000:
            raise Unavailable("slippage policy exceeded")

    def settle(self, order_id, output, fee):
        if output <= 0:
            raise Unavailable("zero output")
        with self.store.session() as s:
            o = s.get(Order, order_id)
            if o.state == "filled" or s.get(Ledger, order_id):
                return
            q = Quote.model_validate(o.data["quote"])
            p = s.get(Position, o.position_id)
            if not p:
                p = Position(
                    id=o.position_id,
                    mode=o.mode,
                    trader=o.data["trader"],
                    chain=q.chain,
                    token=q.token,
                    decimals=q.token_decimals,
                    quantity="0",
                    cost="0",
                    realized="0",
                    mark="0",
                    marked_at=0,
                    needs_reconcile=False,
                )
                s.add(p)
            cash = s.get(Cash, o.mode + ":" + q.chain)
            if o.side == "buy":
                quantity = D(output) / 10**q.token_decimals
                cost = q.usd + fee
                cash.balance = str(D(cash.balance) - cost)
                p.quantity = str(D(p.quantity) + quantity)
                p.cost = str(D(p.cost) + cost)
                # Conservative initial mark; subsequent liquidation-size quotes update it.
                p.mark = str(D(p.mark) + q.usd * D(output) / q.output_amount)
                delta, realized = -cost, D(0)
            else:
                quantity = D(q.input_amount) / 10**q.token_decimals
                if quantity > D(p.quantity):
                    raise Unavailable("exit exceeds source-attributed inventory")
                # USD output quote is scaled to actual/adversely adjusted token output.
                proceeds = q.usd * D(output) / q.output_amount - fee
                matched_cost = D(p.cost) * quantity / D(p.quantity)
                remaining_fraction = (D(p.quantity) - quantity) / D(p.quantity)
                p.quantity = str(D(p.quantity) - quantity)
                p.cost = str(D(p.cost) - matched_cost)
                p.mark = str(D(p.mark) * remaining_fraction)
                realized = proceeds - matched_cost
                p.realized = str(D(p.realized) + realized)
                cash.balance = str(D(cash.balance) + proceeds)
                delta = proceeds
            p.marked_at = now()
            p.needs_reconcile = False
            s.add(
                Ledger(
                    id=order_id,
                    mode=o.mode,
                    chain=q.chain,
                    timestamp=now(),
                    cash_delta=str(delta),
                    realized=str(realized),
                    details={
                        "fee_usd": str(fee),
                        "output_atomic": output,
                        "side": o.side,
                        "position": p.id,
                        "provider": q.provider,
                    },
                )
            )
            o.state = "filled"

    def _halt_chain(self, s, chain, height):
        s.merge(KV(key="chain_halt:" + chain, value={"height": height, "at": now()}))
        for p in s.scalars(
            select(Position).where(Position.chain == chain, Position.mode == self.cfg.mode)
        ):
            if D(p.quantity) > 0:
                p.needs_reconcile = True

    async def reorg(self, chain, height):
        async with self.lock:
            with self.store.session() as s:
                self._halt_chain(s, chain, height)
                for e in s.scalars(select(Event)):
                    if e.data["chain"] == chain and e.data["block"] >= height - 64:
                        e.status, e.reason = (
                            "reorg",
                            "canonical block changed; inventory reconciliation required",
                        )
            self.health("reorg:" + chain, {"state": "halted", "at": now(), "height": height})

    async def recover(self):
        async with self.lock:
            with self.store.session() as s:
                orders = list(
                    s.scalars(
                        select(Order).where(
                            Order.mode == self.cfg.mode,
                            Order.state.in_(
                                [
                                    "prepared",
                                    "signed",
                                    "broadcast",
                                    "uncertain",
                                    "approval_confirmed",
                                ]
                            ),
                        )
                    )
                )
            for order in orders:
                if order.state == "prepared":
                    # No signing occurred: paper settle is atomic; old unsigned signals expire.
                    with self.store.session() as s:
                        s.get(Order, order.id).state = "cancelled"
                    continue
                if not self.live:
                    continue
                if order.state == "approval_confirmed":
                    if (
                        order.side == "buy"
                        and now() - order.data["signal_at"] > self.cfg.max_signal_age
                    ):
                        with self.store.session() as s:
                            s.get(Order, order.id).state = "cancelled"
                        continue
                    old = Quote.model_validate(order.data["quote"])
                    fresh = await self.quotes.quote(
                        old.chain, old.token, old.side, old.input_amount, old.token_decimals
                    )
                    # Refresh quote without expanding reserved spending or fee policy.
                    if fresh.usd + fresh.fee_usd > D(order.data["reserve"]) and order.side == "buy":
                        with self.store.session() as s:
                            s.get(Order, order.id).state = "cancelled"
                        continue
                    await self.live.prepare_and_send(order.id, fresh)
                    continue
                result = await self.live.reconcile(order)
                if not result:
                    continue
                if result["state"] == "filled":
                    self.settle(order.id, result["output"], D(result["fee"]))
                else:
                    with self.store.session() as s:
                        o = s.get(Order, order.id)
                        o.state = result["state"]
                        o.error = result.get("reason", "")
                        if result["state"] in ("failed", "approval_confirmed"):
                            fee = D(result["fee"])
                            ledger_id = o.id + ":fee:" + o.tx_hash
                            if not s.get(Ledger, ledger_id):
                                cash = s.get(Cash, "live:" + o.data["chain"])
                                cash.balance = str(D(cash.balance) - fee)
                                s.add(
                                    Ledger(
                                        id=ledger_id,
                                        mode="live",
                                        chain=o.data["chain"],
                                        timestamp=now(),
                                        cash_delta=str(-fee),
                                        realized=str(-fee),
                                        details={"transaction": o.tx_hash},
                                    )
                                )
                            o.data = {
                                **o.data,
                                "previous_transactions": o.data.get("previous_transactions", [])
                                + [o.tx_hash],
                            }
                        if result["state"] == "reconcile":
                            self._halt_chain(s, order.data["chain"], 0)

    async def mark_positions(self):
        async with self.lock:
            with self.store.session() as s:
                positions = [
                    p
                    for p in s.scalars(select(Position).where(Position.mode == self.cfg.mode))
                    if D(p.quantity) > 0
                ]
            for p in positions:
                try:
                    q = await self.quotes.quote(
                        p.chain, p.token, "sell", int(D(p.quantity) * 10**p.decimals), p.decimals
                    )
                    ratio = D(q.minimum_output) / q.output_amount
                    mark = max(
                        D(0),
                        q.usd * ratio * (10000 - self.cfg.limits.adverse_bps) / 10000 - q.fee_usd,
                    )
                    with self.store.session() as s:
                        row = s.get(Position, p.id)
                        row.mark, row.marked_at = str(mark), now()
                except Exception as exc:
                    self.health(
                        "mark:" + p.id, {"state": "stale", "error": type(exc).__name__, "at": now()}
                    )

    async def reconcile_inventory(self, pid):
        from .rpc import RPC

        async with self.lock:
            with self.store.session() as s:
                p = s.get(Position, pid)
                if not p or p.mode != self.cfg.mode or D(p.quantity) <= 0:
                    raise Unavailable("no matching open position")
                pending = list(
                    s.scalars(
                        select(Order).where(
                            Order.mode == self.cfg.mode,
                            Order.state.in_(
                                [
                                    "signed",
                                    "broadcast",
                                    "uncertain",
                                    "approval_confirmed",
                                    "reconcile",
                                ]
                            ),
                        )
                    )
                )
                if any(o.data.get("chain") == p.chain for o in pending):
                    raise Unavailable(
                        "resolve uncertain transactions before inventory reconciliation"
                    )
                attributed = sum(
                    (
                        D(row.quantity)
                        for row in s.scalars(
                            select(Position).where(
                                Position.mode == self.cfg.mode,
                                Position.chain == p.chain,
                                Position.token == p.token,
                            )
                        )
                    ),
                    D(0),
                )
            c = self.cfg.chain(p.chain)
            source = (
                self.store.get("trader:" + p.trader, {})
                .get("wallets", {})
                .get("solana" if c.kind == "solana" else "evm")
            )
            if not source or self.rpc_http is None:
                raise Unavailable("verified source wallet and RPC required for reconciliation")
            rpc = RPC(c, self.rpc_http)
            await rpc.check_network()

            async def balance(wallet):
                if c.kind == "evm":
                    return D(await rpc.balance(p.token, wallet, "finalized")) / 10**p.decimals
                result = await rpc.call(
                    "getTokenAccountsByOwner",
                    [
                        wallet,
                        {"mint": p.token},
                        {"encoding": "jsonParsed", "commitment": "finalized"},
                    ],
                )
                total = 0
                for row in result["value"]:
                    info = row["account"]["data"]["parsed"]["info"]
                    if (
                        info["owner"] != wallet
                        or info["mint"] != p.token
                        or info["tokenAmount"]["decimals"] != p.decimals
                    ):
                        raise Unavailable("token account metadata mismatch")
                    total += int(info["tokenAmount"]["amount"])
                return D(total) / 10**p.decimals

            source_balance = await balance(source)
            bot_balance = await balance(c.wallet) if self.cfg.mode == "live" else attributed
            if bot_balance < attributed:
                raise Unavailable(
                    "bot balance below attributed inventory; manual ledger audit required"
                )
            with self.store.session() as s:
                s.get(Position, pid).needs_reconcile = False
                s.flush()
                unresolved = [
                    row
                    for row in s.scalars(
                        select(Position).where(
                            Position.chain == p.chain, Position.mode == self.cfg.mode
                        )
                    )
                    if row.needs_reconcile and D(row.quantity) > 0
                ]
                if not unresolved:
                    s.merge(KV(key="chain_halt:" + p.chain, value=False))
            result = {
                "position": pid,
                "source_quantity": str(source_balance),
                "bot_quantity": str(bot_balance),
                "attributed_quantity": str(attributed),
                "unattributed_quantity": str(bot_balance - attributed),
                "at": now(),
                "missed_sells": "not replayed; operator may close remaining copied inventory",
            }
            self.store.put("reconciliation:" + pid, result)
            return result

    async def close_position(self, pid):
        async with self.lock:
            with self.store.session() as s:
                p = s.get(Position, pid)
                if not p or p.mode != self.cfg.mode or D(p.quantity) <= 0:
                    raise Unavailable("position not open in current mode")
                if p.needs_reconcile:
                    raise Unavailable("reconcile inventory before close")
                pending = list(
                    s.scalars(
                        select(Order).where(
                            Order.position_id == pid,
                            Order.state.in_(
                                [
                                    "prepared",
                                    "signed",
                                    "broadcast",
                                    "uncertain",
                                    "approval_confirmed",
                                    "reconcile",
                                ]
                            ),
                        )
                    )
                )
                if pending:
                    raise Unavailable("position already has an unresolved order")
                sig = Signal(
                    trader=p.trader,
                    chain=p.chain,
                    token=p.token,
                    side="sell",
                    quantity=D(p.quantity),
                    source_before=D(p.quantity),
                    source_after=D(0),
                    token_decimals=p.decimals,
                    tx=f"manual:{now()}",
                    block=0,
                    block_hash="manual",
                    timestamp=now(),
                    finalized=True,
                )
                amount = int(D(p.quantity) * 10**p.decimals)
            return await self._execute(sig, pid, amount, manual=True)

    def snapshot(self):
        with self.store.session() as s:
            positions = list(s.scalars(select(Position).where(Position.mode == self.cfg.mode)))
            orders = list(
                s.scalars(
                    select(Order)
                    .where(Order.mode == self.cfg.mode)
                    .order_by(Order.created.desc())
                    .limit(100)
                )
            )
            events = list(s.scalars(select(Event).order_by(Event.received.desc()).limit(100)))
            equity = self._equity(s)
            cash = {
                c.id.split(":", 1)[1]: c.balance
                for c in s.scalars(select(Cash).where(Cash.id.startswith(self.cfg.mode + ":")))
            }
        latency = sorted(self.store.items("latency:").values(), key=lambda x: x["at"])[-1000:]
        lags = sorted(x["chain_to_receive_ms"] for x in latency)
        by_chain = {}
        for chain in self.cfg.chains:
            values = sorted(x["chain_to_receive_ms"] for x in latency if x["chain"] == chain.name)
            decisions = [
                x for x in self.store.items("decision:").values() if x["chain"] == chain.name
            ]
            by_chain[chain.name] = {
                "samples": len(values),
                "p50_ms": values[len(values) // 2] if values else None,
                "p95_ms": values[min(len(values) - 1, int(len(values) * 0.95))] if values else None,
                "latest_decision": max(decisions, key=lambda x: x["at"]) if decisions else None,
            }
        return {
            "mode": self.cfg.mode,
            "at": now(),
            "uptime": now() - self.started,
            "paused": self.store.get("paused:" + self.cfg.mode, False),
            "equity_usd": str(equity),
            "cash": cash,
            "selected": self.selected(),
            "watching": self.watched(),
            "rankings": self.rankings(),
            "positions": [
                {
                    k: getattr(p, k)
                    for k in (
                        "id",
                        "trader",
                        "chain",
                        "token",
                        "quantity",
                        "cost",
                        "realized",
                        "mark",
                        "marked_at",
                        "needs_reconcile",
                    )
                }
                for p in positions
                if D(p.quantity) > 0
            ],
            "orders": [
                {k: getattr(o, k) for k in ("id", "side", "state", "created", "tx_hash", "error")}
                for o in orders
            ],
            "activity": [{"status": e.status, "reason": e.reason, **e.data} for e in events],
            "health": self.store.items("health:"),
            "latency": {
                "by_chain": by_chain,
                "samples": len(lags),
                "p50_ms": lags[len(lags) // 2] if lags else None,
                "p95_ms": lags[min(len(lags) - 1, int(len(lags) * 0.95))] if lags else None,
            },
            "chains": [
                {
                    "name": c.name,
                    "enabled": c.enabled,
                    "paper": "quote verification required"
                    if c.settlement
                    else "settlement token unverified",
                    "live": (
                        "reviewed v2 configuration required"
                        if c.kind == "evm"
                        else "reviewed Raydium CPMM pools/program hash required"
                    ),
                    "halt": self.store.get("chain_halt:" + c.name, False),
                }
                for c in self.cfg.chains
            ],
        }

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import websockets

from .config import Settings
from .db import Store
from .domain import D, Quote, now

PAPER_SOLANA_ADDRESS = "A6o4enrB5QQWSuTJmxxZohYoxUuLa9DQ9HDZ8292L2Kb"
# Quote-only public address; its key was discarded. Never fund it.


class Unavailable(RuntimeError):
    pass


class QuotaExhausted(Unavailable):
    pass


class Credits:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings

    @property
    def key(self):
        return "credits:" + datetime.now(timezone.utc).strftime("%Y-%m")

    def status(self):
        return self.store.get(self.key, {"spent": 0, "remaining_header": None, "blocked": False})

    def reserve(self, cost: int, essential=False):
        state = self.status()
        limit = self.settings.monthly_credits - (0 if essential else self.settings.credit_reserve)
        if state["blocked"] or state["spent"] + cost > limit:
            raise QuotaExhausted("monthly credit budget exhausted; cached discovery retained")
        if state["remaining_header"] is not None and state["remaining_header"] < cost:
            raise QuotaExhausted("provider credit balance exhausted")
        state["spent"] += cost
        if state["remaining_header"] is not None:
            state["remaining_header"] -= cost
        self.store.put(self.key, state)

    def response(self, response: httpx.Response):
        state = self.status()
        # Docs: X-Credits-Remaining / X-Credits-Cost. Reserve is pessimistic on network error.
        if "x-credits-remaining" in response.headers:
            state["remaining_header"] = int(response.headers["x-credits-remaining"])
        if response.status_code == 402:
            state["blocked"] = True
        self.store.put(self.key, state)


class Fomo:
    def __init__(self, settings: Settings, store: Store, client: httpx.AsyncClient):
        self.cfg, self.store, self.http = settings, store, client
        self.credits = Credits(store, settings)
        self.key = settings.key()
        self.lock = asyncio.Lock()
        self.last_request = 0.0

    async def get(self, path, params=None, *, cost=250, ttl=0, essential=False):
        cache_key = (
            "cache:"
            + hashlib.sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()
        )
        cached = self.store.get(cache_key)
        if cached and cached["at"] + ttl > now():
            return cached["data"]
        if not self.key:
            raise Unavailable("FOMO_API_KEY or fomo_key_file required for data")
        async with self.lock:
            self.credits.reserve(cost, essential)
            await asyncio.sleep(
                max(0, self.cfg.fomo_min_request_interval - (time.monotonic() - self.last_request))
            )
            self.last_request = time.monotonic()
            response = await self.http.get(
                self.cfg.fomo_url + path,
                params=params,
                headers={"Authorization": "Bearer " + self.key},
            )
            self.credits.response(response)
        if response.status_code == 402:
            raise QuotaExhausted("provider returned 402; no automatic charged retries")
        if response.status_code in (401, 403, 429):
            raise Unavailable(f"FOMO HTTP {response.status_code}; check account/quota")
        response.raise_for_status()
        data = response.json()
        self.store.put(cache_key, {"at": now(), "data": data})
        return data

    async def discover(self):
        data = await self.get(
            "/v2/leaderboard/30d", {"limit": self.cfg.candidates}, ttl=self.cfg.discovery_ttl
        )
        if data.get("stale") or data.get("available") is False:
            raise Unavailable("leaderboard unavailable or stale")
        return data.get("traders", [])[: self.cfg.candidates]

    async def history(self, user_id: str):
        pages, cursor, seen = [], None, set()
        for _ in range(self.cfg.history_pages):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = await self.get(
                f"/v2/users/{quote(user_id, safe='')}/swaps", params, ttl=self.cfg.discovery_ttl
            )
            pages.append(page)
            cursor = page.get("nextCursor")
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        # Raw rows retained for diagnosis; undocumented fill fields are NOT guessed.
        return {
            "pages": pages,
            "complete_30d": False,
            "missing": [
                "provider does not document a full fill schema with historical fees",
                "RPC backfill or an audited normalized history import is required",
            ],
        }

    async def stream(self, callback, health, stop: asyncio.Event):
        if not self.key:
            health(
                "fomo_stream",
                {
                    "state": "unavailable",
                    "reason": "FOMO_API_KEY missing; configure and restart",
                    "at": now(),
                },
            )
            await stop.wait()
            return
        delay = 1
        while not stop.is_set():
            try:
                if not self.key:
                    raise Unavailable("FOMO data key missing")
                self.credits.reserve(250, essential=True)
                # Key-in-query is the provider's documented WS auth; never log URL/exceptions.
                url = self.cfg.fomo_ws + "?" + urlencode({"key": self.key})
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, max_size=2**20, open_timeout=15
                ) as ws:
                    health("fomo_stream", {"state": "connected", "at": now()})
                    # REST gap recovery is bounded and incomplete by contract; never replay entries.
                    cursor = self.store.get("social_cursor", {"ts": now()})["ts"]
                    try:
                        gap = await self.get(
                            "/v2/alerts", {"since": cursor, "limit": 100}, cost=125, essential=True
                        )
                        for alert in reversed(gap.get("alerts", [])):
                            await callback(alert, True)
                    except (QuotaExhausted, httpx.HTTPError, Unavailable) as exc:
                        # Retain an existing free-message socket when REST recovery is unavailable.
                        health(
                            "social_gap",
                            {"state": "unrecovered", "error": type(exc).__name__, "at": now()},
                        )
                    health(
                        "social_coverage",
                        {
                            "state": "partial",
                            "reason": "large trades only; REST gap limited to 100",
                        },
                    )
                    delay = 1
                    async for message in ws:
                        data = json.loads(message)
                        if data.get("type") == "welcome":
                            health(
                                "fomo_stream",
                                {
                                    "state": "connected",
                                    "at": now(),
                                    "delay_seconds": data.get("delaySeconds"),
                                    "realtime": data.get("realtime"),
                                },
                            )
                        elif data.get("type") == "alert":
                            await callback(data, bool(data.get("replay")))
                            self.store.put("social_cursor", {"ts": data.get("ts", now())})
                        if stop.is_set():
                            return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                health(
                    "fomo_stream",
                    {"state": "unavailable", "error": type(exc).__name__, "at": now()},
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, 300)


class Relay:
    def __init__(self, cfg: Settings, client: httpx.AsyncClient):
        self.cfg, self.http = cfg, client

    async def catalog(self):
        r = await self.http.get(self.cfg.relay_url + "/chains")
        r.raise_for_status()
        return {c["id"]: c for c in r.json()["chains"]}

    async def raw_quote(self, origin, destination, token_in, token_out, amount, user, recipient):
        r = await self.http.post(
            self.cfg.relay_url + "/quote/v2",
            json={
                "user": user,
                "recipient": recipient,
                "originChainId": origin,
                "destinationChainId": destination,
                "originCurrency": token_in,
                "destinationCurrency": token_out,
                "amount": str(amount),
                "tradeType": "EXACT_INPUT",
                "slippageTolerance": str(self.cfg.limits.slippage_bps),
            },
        )
        r.raise_for_status()
        return r.json()

    async def quote(self, chain_name, token, side, amount, decimals):
        c = self.cfg.chain(chain_name)
        if not c.settlement:
            raise Unavailable("settlement token is not verified/configured on this chain")
        # Non-funded public addresses for paper quotes; a configured wallet improves route accuracy.
        user = c.wallet or (
            PAPER_SOLANA_ADDRESS
            if c.kind == "solana"
            else "0x0000000000000000000000000000000000000001"
        )
        token_in, token_out = (c.settlement, token) if side == "buy" else (token, c.settlement)
        data = await self.raw_quote(c.relay_id, c.relay_id, token_in, token_out, amount, user, user)
        details = data["details"]
        ci, co = details["currencyIn"], details["currencyOut"]

        def equal(a, b):
            return a == b if c.kind == "solana" else a.lower() == b.lower()

        if (
            not equal(details["sender"], user)
            or not equal(details["recipient"], user)
            or ci["currency"]["chainId"] != c.relay_id
            or co["currency"]["chainId"] != c.relay_id
            or not equal(ci["currency"]["address"], token_in)
            or not equal(co["currency"]["address"], token_out)
            or int(ci["amount"]) != amount
        ):
            raise Unavailable("Relay quote does not match requested trade")
        expected_in_decimals = c.decimals if side == "buy" else decimals
        expected_out_decimals = decimals if side == "buy" else c.decimals
        if (
            ci["currency"]["decimals"] != expected_in_decimals
            or co["currency"]["decimals"] != expected_out_decimals
        ):
            raise Unavailable("quote token decimals mismatch")
        if not data.get("steps") or any(s.get("kind") != "transaction" for s in data["steps"]):
            raise Unavailable(
                "quote has no executable transaction route or requires unsupported signing"
            )
        out = int(co["amount"])
        minimum = int(co.get("minimumAmount") or 0)
        if minimum < out * (10000 - self.cfg.limits.slippage_bps) // 10000:
            raise Unavailable("quote minimum output exceeds slippage policy")
        gas = D(data["fees"]["gas"]["amountUsd"])
        if gas < 0 or gas > self.cfg.limits.max_fee_usd:
            raise Unavailable("gas estimate outside fee policy")
        # Relay's output is already net of route/service fees; only origin gas is added.
        usd = D(ci["amountUsd"] if side == "buy" else co["amountUsd"])
        if usd <= 0:
            raise Unavailable("missing USD valuation")
        return Quote(
            chain=chain_name,
            token=token,
            side=side,
            input_amount=amount,
            output_amount=out,
            minimum_output=minimum,
            token_decimals=decimals,
            usd=usd,
            fee_usd=gas,
            expires=now() + 10,
            provider="relay",
            raw=data,
        )

    async def bridge(self, source, destination, usd):
        a, b = self.cfg.chain(source), self.cfg.chain(destination)
        if not a.settlement or not b.settlement:
            raise Unavailable("bridge settlement assets not configured")

        def user(c):
            return c.wallet or (
                PAPER_SOLANA_ADDRESS
                if c.kind == "solana"
                else "0x0000000000000000000000000000000000000001"
            )

        amount = int(usd * 10**a.decimals)
        raw = await self.raw_quote(
            a.relay_id, b.relay_id, a.settlement, b.settlement, amount, user(a), user(b)
        )
        d = raw["details"]
        ci, co = d["currencyIn"], d["currencyOut"]
        if (
            ci["currency"]["chainId"] != a.relay_id
            or co["currency"]["chainId"] != b.relay_id
            or ci["currency"]["address"] != a.settlement
            or co["currency"]["address"] != b.settlement
            or d["sender"] != user(a)
            or d["recipient"] != user(b)
            or int(ci["amount"]) != amount
            or ci["currency"]["decimals"] != a.decimals
            or co["currency"]["decimals"] != b.decimals
            or not raw.get("steps")
        ):
            raise Unavailable("bridge quote does not match configured wallets/assets")
        expected, minimum = int(co["amount"]), int(co.get("minimumAmount") or 0)
        if (
            minimum <= 0
            or expected <= 0
            or minimum < expected * (10000 - self.cfg.limits.slippage_bps) // 10000
        ):
            raise Unavailable("bridge output outside slippage policy")
        fee = D(raw["fees"]["gas"]["amountUsd"])
        spend = D(ci["amountUsd"])
        if fee < 0 or fee > self.cfg.limits.max_fee_usd or abs(spend - usd) > usd * D(".01"):
            raise Unavailable("bridge gas/spend outside policy")
        out_usd = (
            D(co["amountUsd"])
            * D(minimum)
            / expected
            * (10000 - self.cfg.limits.adverse_bps)
            / 10000
        )
        if out_usd <= 0 or out_usd > usd * D("1.01"):
            raise Unavailable("invalid bridge output valuation")
        return {
            "spend": str(spend),
            "receive": str(out_usd),
            "fee": str(fee),
            "provider": "relay",
            "at": now(),
        }


class PublicJSON(HTMLParser):
    def __init__(self):
        super().__init__()
        self.collect = False
        self.chunks = []
        self.documents = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type") in (
            "application/json",
            "application/ld+json",
        ):
            self.collect, self.chunks = True, []

    def handle_data(self, data):
        if self.collect:
            self.chunks.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.collect:
            try:
                self.documents.append(json.loads("".join(self.chunks)))
            except ValueError:
                pass
            self.collect = False


async def scrape_public_fomo(url: str, http: httpx.AsyncClient) -> dict:
    """Opt-in public embedded JSON capture. No invented private endpoint or auth bypass."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ("fomo.family", "www.fomo.family"):
        raise Unavailable("direct capture only accepts public https://fomo.family pages")
    robots = await http.get("https://fomo.family/robots.txt")
    if robots.status_code not in (200, 404):
        raise Unavailable("cannot establish public crawling policy")
    policy = RobotFileParser()
    policy.parse(robots.text.splitlines() if robots.status_code == 200 else [])
    if robots.status_code == 200 and not policy.can_fetch("FomoTradeSpy", url):
        raise Unavailable("robots.txt disallows this page")
    r = await http.get(url, headers={"User-Agent": "FomoTradeSpy/0.1 (public discovery)"})
    r.raise_for_status()
    parser = PublicJSON()
    parser.feed(r.text)
    return {
        "url": url,
        "at": time.time(),
        "documents": parser.documents,
        "copy_ready": False,
        "reason": "public capture only; no verified direct FOMO trade contract",
    }

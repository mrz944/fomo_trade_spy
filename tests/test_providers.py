import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from fomo_spy.db import Store
from fomo_spy.domain import now
from fomo_spy.providers import Credits, Fomo, QuotaExhausted, Relay, Unavailable, scrape_public_fomo


def quote_payload(cfg, token="0x" + "a" * 40):
    c = cfg.chain("base")
    wallet = "0x0000000000000000000000000000000000000001"
    return {
        "steps": [{"kind": "transaction", "items": [{"data": {}}]}],
        "details": {
            "sender": wallet,
            "recipient": wallet,
            "currencyIn": {
                "currency": {"chainId": 8453, "address": c.settlement, "decimals": 6},
                "amount": "25000000",
                "amountUsd": "25",
            },
            "currencyOut": {
                "currency": {"chainId": 8453, "address": token, "decimals": 6},
                "amount": "100000000",
                "minimumAmount": "99000000",
                "amountUsd": "25",
            },
        },
        "fees": {"gas": {"amountUsd": ".01"}},
    }


async def test_relay_exact_size_same_chain_contract(cfg):
    def handler(req):
        body = json.loads(req.content)
        assert req.url.path == "/quote/v2"
        assert body["originChainId"] == body["destinationChainId"] == 8453
        assert body["amount"] == "25000000"
        assert body["slippageTolerance"] == "100"
        return httpx.Response(200, json=quote_payload(cfg))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        q = await Relay(cfg, http).quote("base", "0x" + "a" * 40, "buy", 25000000, 6)
        assert q.minimum_output == 99000000
        assert q.provider == "relay"


@pytest.mark.parametrize("tamper", ["recipient", "chain", "spend", "decimals", "minimum", "fees"])
async def test_relay_rejects_mutated_quote(cfg, tamper):
    data = quote_payload(cfg)
    if tamper == "recipient":
        data["details"]["recipient"] = "0xBAD"
    elif tamper == "chain":
        data["details"]["currencyOut"]["currency"]["chainId"] = 1
    elif tamper == "spend":
        data["details"]["currencyIn"]["amount"] = "26000000"
    elif tamper == "decimals":
        data["details"]["currencyOut"]["currency"]["decimals"] = 18
    elif tamper == "minimum":
        data["details"]["currencyOut"]["minimumAmount"] = "1"
    else:
        data["fees"]["gas"]["amountUsd"] = "100"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data))
    ) as http:
        with pytest.raises(Unavailable):
            await Relay(cfg, http).quote("base", "0x" + "a" * 40, "buy", 25000000, 6)


async def test_discovery_cached_and_credit_budget_survives_restart(cfg, monkeypatch):
    monkeypatch.setenv("FOMO_API_KEY", "test-key")
    store = Store(cfg.state_dir / "spy.sqlite")
    calls = []

    def handler(req):
        calls.append(req.url)
        assert req.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200, json={"traders": [{"userId": "uid"}]}, headers={"X-Credits-Remaining": "249750"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        fomo = Fomo(cfg, store, http)
        await fomo.discover()
        await fomo.discover()
        assert len(calls) == 1
        assert Credits(store, cfg).status()["spent"] == 250
        cfg.monthly_credits = 20000
        with pytest.raises(QuotaExhausted):
            fomo.credits.reserve(250)
    store.close()


async def test_402_blocks_repeated_charges(cfg, monkeypatch):
    monkeypatch.setenv("FOMO_API_KEY", "test-key")
    store = Store(cfg.state_dir / "spy.sqlite")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(402))
    ) as http:
        fomo = Fomo(cfg, store, http)
        with pytest.raises(QuotaExhausted):
            await fomo.discover()
        spent = fomo.credits.status()["spent"]
        with pytest.raises(QuotaExhausted):
            await fomo.discover()
        assert fomo.credits.status()["spent"] == spent
    store.close()


async def test_ws_welcome_replay_and_gap_recovery(cfg, monkeypatch):
    monkeypatch.setenv("FOMO_API_KEY", "test-key")
    store = Store(cfg.state_dir / "spy.sqlite")
    stop = asyncio.Event()
    received, health = [], {}

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            async def messages():
                yield json.dumps({"type": "welcome", "delaySeconds": 15, "realtime": False})
                yield json.dumps(
                    {"type": "alert", "eventId": "replayed", "replay": True, "ts": now()}
                )
                stop.set()
                yield json.dumps({"type": "alert", "eventId": "live", "ts": now()})

            return messages()

    async def consume(data, historic):
        received.append((data["eventId"], historic))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"alerts": [{"eventId": "gap"}]})
        )
    ) as http:
        with patch("fomo_spy.providers.websockets.connect", return_value=Socket()):
            await Fomo(cfg, store, http).stream(consume, lambda k, v: health.update({k: v}), stop)
    assert received == [("gap", True), ("replayed", True), ("live", False)]
    assert health["fomo_stream"]["delay_seconds"] == 15
    assert store.get("social_cursor")
    store.close()


async def test_direct_capture_is_opt_in_public_json_only():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(
            200, text='<script type="application/json">{"profile":"user"}</script>'
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await scrape_public_fomo("https://fomo.family/example", http)
        assert result["documents"] == [{"profile": "user"}]
        assert result["copy_ready"] is False
        with pytest.raises(Unavailable):
            await scrape_public_fomo("https://internal.invalid", http)


async def test_quota_exhausted_rest_does_not_close_active_ws(cfg, monkeypatch):
    monkeypatch.setenv("FOMO_API_KEY", "test-key")
    cfg.monthly_credits = 250
    cfg.credit_reserve = 0
    store = Store(cfg.state_dir / "spy.sqlite")
    stop = asyncio.Event()
    seen = []

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def __aiter__(self):
            async def messages():
                yield json.dumps({"type": "welcome", "delaySeconds": 15})
                stop.set()
                yield json.dumps({"type": "alert", "eventId": "still-connected", "ts": now()})

            return messages()

    async def callback(data, replay):
        seen.append(data["eventId"])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    ) as http:
        with patch("fomo_spy.providers.websockets.connect", return_value=Socket()):
            await Fomo(cfg, store, http).stream(callback, lambda *_: None, stop)
    assert seen == ["still-connected"]
    assert (
        store.get(
            "credits:"
            + __import__("datetime")
            .datetime.now(__import__("datetime").timezone.utc)
            .strftime("%Y-%m")
        )["spent"]
        == 250
    )
    store.close()

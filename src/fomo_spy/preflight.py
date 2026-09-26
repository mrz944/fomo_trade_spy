from __future__ import annotations

import asyncio
import platform
import shutil

import httpx

from .config import Settings
from .domain import now
from .providers import Relay, Unavailable
from .rpc import RPC


async def preflight(cfg: Settings, network=False, quotes=False):
    """Read-only JSON-RPC, GET health/catalog, optional unsigned executable quote requests.

    Does not instantiate a signer, read wallet secrets or call any broadcast method.
    """
    report = {
        "at": now(),
        "mode": cfg.mode,
        "network_checks": network,
        "funded_transactions_submitted": 0,
        "infrastructure": {
            "python": platform.python_version(),
            "platform": platform.system(),
            "podman": shutil.which("podman"),
            "systemctl": shutil.which("systemctl"),
        },
        "fomo_key_configured": bool(cfg.fomo_key_file or __import__("os").getenv("FOMO_API_KEY")),
        "chains": [],
        "limitations": [
            "FOMO authenticated schemas and streaming require an account; no complete history guarantee",
            "Social feed omits small trades and cannot establish exact fill inventory",
            "Live execution is restricted to reviewed EVM v2 routers and Solana Raydium CPMM pools; unverified with funded wallets",
            "Automatic live bridging is blocked pending validated bridge transaction decoding",
            "Direct FOMO public scraping captures embedded JSON only; no verified trade feed",
        ],
    }
    if not network:
        report["chains"] = [
            {"name": c.name, "enabled": c.enabled, "rpc": "not checked", "relay": "not checked"}
            for c in cfg.chains
        ]
        return report
    async with httpx.AsyncClient(timeout=20) as http:
        relay = Relay(cfg, http)
        try:
            r = await http.get(cfg.fomo_url + "/health")
            r.raise_for_status()
            report["fomo_health"] = r.json()
        except Exception as exc:
            report["fomo_health"] = {"error": type(exc).__name__}
        try:
            catalog = await relay.catalog()
        except Exception as exc:
            catalog = {}
            report["relay_catalog_error"] = type(exc).__name__

        async def check(c):
            row = {"name": c.name, "enabled": c.enabled}
            listed = catalog.get(c.relay_id)
            row["relay_listed"] = bool(listed and not listed.get("disabled"))
            row["relay_deposits"] = listed.get("depositEnabled") if listed else None
            try:
                row["rpc_network"] = await RPC(c, http).check_network()
            except Exception as exc:
                row["rpc_error"] = str(exc) if isinstance(exc, Unavailable) else type(exc).__name__
            if quotes and c.enabled and c.settlement and listed:
                native = listed["currency"]
                token = next(
                    (
                        t
                        for t in listed.get("featuredTokens", [])
                        if t.get("symbol") in ("WETH", "WBNB", "WMON")
                    ),
                    None,
                )
                if c.kind == "solana":
                    token = {
                        "address": "So11111111111111111111111111111111111111112",
                        "decimals": 9,
                    }
                if not token:
                    row["quote"] = "no verified non-settlement probe token"
                else:
                    try:
                        q = await relay.quote(
                            c.name,
                            token["address"],
                            "buy",
                            int(cfg.limits.buy_usd * 10**c.decimals),
                            token.get("decimals", native["decimals"]),
                        )
                        row["quote"] = {
                            "provider": q.provider,
                            "input": q.input_amount,
                            "output": q.output_amount,
                            "fee_usd": str(q.fee_usd),
                            "executable_steps": len(q.raw["steps"]),
                        }
                    except Exception as exc:
                        row["quote"] = {
                            "error": (
                                str(exc) if isinstance(exc, Unavailable) else type(exc).__name__
                            )
                        }
                        if isinstance(exc, httpx.HTTPStatusError):
                            row["quote"]["http_status"] = exc.response.status_code
                            row["quote"]["provider_response"] = exc.response.text[:500]
            return row

        report["chains"] = await asyncio.gather(*(check(c) for c in cfg.chains))
    return report

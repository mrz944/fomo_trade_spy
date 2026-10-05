"""Current-data admission probes; archive history is not an admission dependency."""

from .domain import now
from .history import failure
from .normalization import TRANSFER, WSOL, topic
from .providers import Unavailable
from .rpc import RPC


async def probe_current(chain, wallet, http, quotes, catalog):
    rpc = RPC(chain, http)
    rpc.priority = 30
    report = dict(
        chain=chain.name,
        at=now(),
        observe_ready=False,
        checks={},
        quote="per-token verification required",
        native="unverified",
    )
    try:
        await rpc.check_network()
        report["checks"]["network"] = "ok"
        listed = catalog.get(chain.relay_id)
        if not listed or listed.get("disabled"):
            raise Unavailable("chain is unavailable in executable quote catalog")
        if chain.kind == "evm":
            head = int(await rpc.call("eth_blockNumber"), 16) - chain.confirmations
            block = await rpc.call("eth_getBlockByNumber", [hex(head), False])
            if not block or int(block["timestamp"], 16) < now() - 120:
                raise Unavailable("confirmed RPC head is stale")
            await rpc.balance(chain.settlement, wallet, hex(head))
            await rpc.call(
                "eth_getLogs",
                [
                    {
                        "fromBlock": hex(head),
                        "toBlock": hex(head),
                        "topics": [TRANSFER, None, topic(wallet)],
                    }
                ],
            )
            report["checks"].update(recent_inventory="ok", recent_logs="ok")
            if block.get("transactions"):
                receipt = await rpc.call("eth_getTransactionReceipt", [block["transactions"][0]])
                if not receipt or receipt.get("blockHash") != block["hash"]:
                    raise Unavailable("canonical recent receipt unavailable")
                try:
                    await rpc.call(
                        "debug_traceTransaction",
                        [block["transactions"][0], {"tracer": "callTracer"}],
                    )
                    report["native"] = "trace available; route verified per transaction"
                except Exception as exc:
                    report["native"] = failure(exc)
        else:
            await rpc.call("getSlot", [{"commitment": "finalized"}])
            await rpc.call(
                "getSignaturesForAddress", [wallet, {"limit": 1, "commitment": "finalized"}]
            )
            await rpc.call(
                "getTokenAccountsByOwner",
                [
                    wallet,
                    {"mint": chain.settlement},
                    {"encoding": "jsonParsed", "commitment": "finalized"},
                ],
            )
            report["checks"].update(signatures="ok", current_token_accounts="ok")
            report["native"] = "canonical SOL and rent deltas supported"
        report["observe_ready"] = True
        token = (
            {"address": WSOL, "decimals": 9}
            if chain.kind == "solana"
            else next(
                (
                    t
                    for t in listed.get("featuredTokens", [])
                    if t.get("symbol") in ("WETH", "WBNB", "WMON")
                ),
                None,
            )
        )
        if token:
            try:
                buy = await quotes.quote(
                    chain.name,
                    token["address"],
                    "buy",
                    int(quotes.cfg.limits.buy_usd * 10**chain.decimals),
                    token["decimals"],
                )
                output = buy.minimum_output * (10000 - quotes.cfg.limits.adverse_bps) // 10000
                sell = await quotes.quote(
                    chain.name, token["address"], "sell", output, token["decimals"]
                )
                report["quote"] = {
                    "buy": "ok",
                    "liquidation": "ok",
                    "token": token["address"],
                    "at": now(),
                    "fees_usd": [str(buy.fee_usd), str(sell.fee_usd)],
                }
            except Exception as exc:
                report["quote"] = {
                    "probe_error": failure(exc),
                    "actual_token": "must pass before entry",
                }
    except Exception as exc:
        report["reason"] = failure(exc)
    return report

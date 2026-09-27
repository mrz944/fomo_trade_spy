"""Wallet net movements used by both reconstruction and fresh monitoring.

Consideration is already net of route fees. Gas is separate; unavailable gas
remains None. Native movements never include gas or account rent twice.
"""

from collections import defaultdict
from dataclasses import dataclass

from eth_utils import keccak

from .domain import D, Signal

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WSOL = "So11111111111111111111111111111111111111112"
DEPOSIT = "0x" + keccak(text="Deposit(address,uint256)").hex()
WITHDRAWAL = "0x" + keccak(text="Withdrawal(address,uint256)").hex()
WRAPPED = {
    "ethereum": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "base": "0x4200000000000000000000000000000000000006",
    "bsc": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
}
NATIVE_PRICE = {
    "ethereum": "coingecko:ethereum",
    "base": "coingecko:ethereum",
    "bsc": "coingecko:binancecoin",
    "solana": "coingecko:solana",
}


def topic(wallet):
    return "0x" + wallet.lower().removeprefix("0x").zfill(64)


def log_delta(log, wallet):
    if len(log.get("topics", [])) != 3 or log["topics"][0].lower() != TRANSFER:
        return 0
    value = int(log["data"], 16)
    return value * (
        (log["topics"][2].lower() == topic(wallet)) - (log["topics"][1].lower() == topic(wallet))
    )


@dataclass
class Movement:
    signal: Signal
    consideration: D | None
    price_asset: str | None
    gas_native: D | None
    reason: str = ""


def native_trace_delta(trace, wallet):
    """Reverted subtrees and delegate/static frames never transfer value."""
    if not isinstance(trace, dict) or "type" not in trace:
        raise ValueError("callTracer result missing")
    if trace.get("error"):
        return 0
    delta = 0
    if trace["type"].upper() in ("CALL", "CREATE", "CREATE2", "SELFDESTRUCT"):
        value = int(trace.get("value", "0x0"), 16)
        delta = value * (
            (trace.get("to", "").lower() == wallet.lower())
            - (trace.get("from", "").lower() == wallet.lower())
        )
    return delta + sum(native_trace_delta(c, wallet) for c in trace.get("calls", []))


def classify(deltas, chain, native=None):
    deltas = {t: d for t, d in deltas.items() if d}
    settlement = deltas.pop(chain.settlement, 0)
    wrapped = WRAPPED.get(chain.name)
    # Wrapped movements can only be netted when the native trace is known.
    if wrapped and wrapped in deltas and native is not None:
        native += deltas.pop(wrapped)
    quotes = []
    if settlement:
        quotes.append((D(settlement) / 10**chain.decimals, chain.name + ":" + chain.settlement))
    if native:
        quotes.append((D(native) / 10**18, NATIVE_PRICE.get(chain.name)))
    result = []
    for token, delta in deltas.items():
        quote = quotes[0] if len(quotes) == 1 else (None, None)
        side = (
            ("buy" if delta > 0 else "sell")
            if (len(deltas) == 1 and quote[0] is not None and delta * quote[0] < 0)
            else "transfer"
        )
        result.append((token, delta, side, abs(quote[0]) if quote[0] else None, quote[1]))
    return result


async def evm_movements(rpc, receipt, block, trader, wallet, logs, started, trace=None):
    c = rpc.chain
    if receipt["blockHash"] != block["hash"]:
        raise ValueError("noncanonical receipt")
    if int(receipt["status"], 16) != 1:
        return []
    deltas = defaultdict(int)
    for log in receipt["logs"]:
        deltas[log["address"].lower()] += log_delta(log, wallet)
        # WETH9/WBNB mint/burn use Deposit/Withdrawal instead of Transfer.
        if (
            log["address"].lower() == WRAPPED.get(c.name)
            and len(log.get("topics", [])) == 2
            and log["topics"][1].lower() == topic(wallet)
        ):
            event = log["topics"][0].lower()
            if event in (DEPOSIT, WITHDRAWAL):
                deltas[log["address"].lower()] += int(log["data"], 16) * (
                    1 if event == DEPOSIT else -1
                )
    native = native_trace_delta(trace, wallet) if trace is not None else None
    sender = receipt.get("from")
    gas = None
    if sender and sender.lower() != wallet.lower():
        gas = D(0)
    elif sender and receipt.get("gasUsed") and receipt.get("effectiveGasPrice"):
        gas = D(int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)) / 10**18
        if c.name == "base":
            gas = gas + D(int(receipt["l1Fee"], 16)) / 10**18 if "l1Fee" in receipt else None
        elif c.name not in ("ethereum", "bsc"):
            gas = None  # Unknown chain-specific fee components, never assume zero.
        if c.name == "ethereum" and receipt.get("type") == "0x3":
            gas = (
                gas + D(int(receipt["blobGasUsed"], 16) * int(receipt["blobGasPrice"], 16)) / 10**18
                if "blobGasUsed" in receipt and "blobGasPrice" in receipt
                else None
            )
    height = int(receipt["blockNumber"], 16)
    result = []
    for token, delta, side, amount, asset in classify(deltas, c, native):
        decimals = await rpc.decimals(token, hex(height))
        before = await rpc.balance(token, wallet, hex(max(0, height - 1)))
        for log in logs:
            if (
                int(log["blockNumber"], 16) == height
                and int(log["transactionIndex"], 16) < int(receipt["transactionIndex"], 16)
                and log["address"].lower() == token
            ):
                before += log_delta(log, wallet)
        ts = int(block["timestamp"], 16)
        sig = Signal(
            trader=trader,
            chain=c.name,
            token=token,
            side=side,
            quantity=D(abs(delta)) / 10**decimals,
            source_before=D(before) / 10**decimals,
            source_after=D(before + delta) / 10**decimals,
            token_decimals=decimals,
            tx=receipt["transactionHash"],
            block=height,
            block_hash=block["hash"],
            timestamp=ts,
            historical=ts < started,
            finalized=True,
        )
        result.append(
            Movement(sig, amount, asset, gas, "native trace unavailable" if trace is None else "")
        )
    return result


def solana_movements(tx, trader, wallet, signature, chain, started):
    meta = tx["meta"]
    if meta.get("err") or tx.get("blockTime") is None:
        return []
    pre, post, decimals, owned = defaultdict(int), defaultdict(int), {}, set()
    for field, values in [("preTokenBalances", pre), ("postTokenBalances", post)]:
        for row in meta.get(field, []):
            if row.get("owner") == wallet:
                token = row["mint"]
                values[token] += int(row["uiTokenAmount"]["amount"])
                decimals[token] = row["uiTokenAmount"]["decimals"]
                if "accountIndex" in row:
                    owned.add(row["accountIndex"])
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    gas, native = None, None
    if wallet in keys and "fee" in meta and "preBalances" in meta and "postBalances" in meta:
        index = keys.index(wallet)
        gas_lamports = meta["fee"] if index == 0 else 0
        gas = D(gas_lamports) / 10**9
        # Aggregate SOL across wallet + owned token accounts: moving rent into an
        # ATA or closing it is internal. Wrapped SOL is already in these lamports.
        native = (
            sum(meta["postBalances"][i] - meta["preBalances"][i] for i in owned | {index})
            + gas_lamports
        )
    deltas = {t: post[t] - pre[t] for t in pre.keys() | post.keys() if post[t] != pre[t]}
    settlement = deltas.pop(chain.settlement, 0)
    if native is not None:
        deltas.pop(WSOL, None)
    quotes = []
    if settlement:
        quotes.append((D(settlement) / 10**chain.decimals, chain.name + ":" + chain.settlement))
    if native:
        quotes.append((D(native) / 10**9, NATIVE_PRICE["solana"]))
    result = []
    for token, delta in deltas.items():
        amount, asset = quotes[0] if len(quotes) == 1 else (None, None)
        side = (
            ("buy" if delta > 0 else "sell")
            if (len(deltas) == 1 and amount is not None and delta * amount < 0)
            else "transfer"
        )
        sig = Signal(
            trader=trader,
            chain=chain.name,
            token=token,
            side=side,
            quantity=D(abs(delta)) / 10 ** decimals[token],
            source_before=D(pre[token]) / 10 ** decimals[token],
            source_after=D(post[token]) / 10 ** decimals[token],
            token_decimals=decimals[token],
            tx=signature,
            block=tx["slot"],
            block_hash=str(tx["slot"]) + ":finalized",
            timestamp=tx["blockTime"],
            historical=tx["blockTime"] < started,
            finalized=True,
        )
        result.append(Movement(sig, abs(amount) if amount is not None else None, asset, gas))
    return result

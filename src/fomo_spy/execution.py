"""Restricted live EVM execution. Relay's opaque transactions are never blindly signed."""

from __future__ import annotations

from decimal import Decimal

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address
from sqlalchemy import select

from .config import Chain, Settings, read_secret
from .db import Order, Store
from .domain import D, Quote, now
from .providers import Relay, Unavailable
from .rpc import RPC, log_delta

SWAP = keccak(text="swapExactTokensForTokens(uint256,uint256,address[],address,uint256)")[:4]
APPROVE = bytes.fromhex("095ea7b3")


def calldata(signature, types, values):
    return "0x" + (keccak(text=signature)[:4] + encode(types, values)).hex()


def validate_transaction(
    tx: dict,
    *,
    chain: Chain,
    wallet: str,
    token_in: str,
    token_out: str,
    amount: int,
    minimum: int,
    deadline: int,
    max_fee_native: Decimal,
    approval=False,
):
    allowed = {"from", "to", "chainId", "nonce", "value", "data", "gas", "gasPrice"}
    if set(tx) - allowed:
        raise Unavailable("unexpected transaction fields")
    if (
        tx["chainId"] != chain.fomo_id
        or tx["from"].lower() != wallet.lower()
        or int(tx["value"]) != 0
        or int(tx["nonce"]) < 0
    ):
        raise Unavailable("wrong chain, signer, value or nonce")
    if (
        int(tx["gas"]) <= 21000
        or int(tx["gasPrice"]) <= 0
        or D(int(tx["gas"]) * int(tx["gasPrice"])) / 10**18 > max_fee_native
    ):
        raise Unavailable("transaction gas exceeds explicit policy")
    data = bytes.fromhex(tx["data"].removeprefix("0x"))
    if approval:
        expected = APPROVE + encode(["address", "uint256"], [chain.router, amount])
        if tx["to"].lower() != token_in.lower() or data != expected:
            raise Unavailable("approval must be exact amount to reviewed router")
    else:
        expected = SWAP + encode(
            ["uint256", "uint256", "address[]", "address", "uint256"],
            [amount, minimum, [token_in, token_out], wallet, deadline],
        )
        if tx["to"].lower() != chain.router.lower() or data != expected:
            raise Unavailable("unexpected route, recipient, spend, minimum output or deadline")
        if not now() < deadline <= now() + 120:
            raise Unavailable("expired or excessive transaction deadline")


class LiveV2:
    """Real signing/broadcast/reconciliation for reviewed exact-input two-token v2 routes.

    Opaque Relay routes are rejected; Solana uses a separate locally built CPMM executor.
    """

    def __init__(self, cfg: Settings, store: Store, http):
        self.cfg, self.store, self.http = cfg, store, http
        self.relay = Relay(cfg, http)

    def ready(self, c):
        if c.kind != "evm":
            raise Unavailable("Solana must use the separate Raydium CPMM executor")
        if not all((c.router, c.router_code_hash, c.wallet, c.secret_file, c.max_gas_native)):
            raise Unavailable(
                "live requires reviewed v2 router/code hash, dedicated wallet/secret and gas cap"
            )

    async def quote(self, chain_name, token, side, amount, decimals):
        c = self.cfg.chain(chain_name)
        self.ready(c)
        rpc = RPC(c, self.http)
        await rpc.check_network()
        code = await rpc.call("eth_getCode", [c.router, "latest"])
        if "0x" + keccak(bytes.fromhex(code[2:])).hex() != c.router_code_hash.lower():
            raise Unavailable("router bytecode hash does not match reviewed configuration")
        ti, to = (c.settlement, token) if side == "buy" else (token, c.settlement)
        raw = await rpc.call(
            "eth_call",
            [
                {
                    "to": c.router,
                    "data": calldata(
                        "getAmountsOut(uint256,address[])",
                        ["uint256", "address[]"],
                        [amount, [ti, to]],
                    ),
                },
                "latest",
            ],
        )
        amounts = decode(["uint256[]"], bytes.fromhex(raw[2:]))[0]
        if len(amounts) != 2 or amounts[0] != amount or amounts[1] <= 0:
            raise Unavailable("invalid v2 output quote")
        out = amounts[-1]
        minimum = out * (10000 - self.cfg.limits.slippage_bps) // 10000
        # Independent executable quote supplies current USD valuation; no historical price reuse.
        comparison = await self.relay.quote(chain_name, token, side, amount, decimals)
        native = await self.relay.raw_quote(
            c.relay_id,
            c.relay_id,
            "0x0000000000000000000000000000000000000000",
            c.settlement,
            10**16,
            c.wallet,
            c.wallet,
        )
        native_price = D(native["details"]["currencyIn"]["amountUsd"]) * 100
        if native_price <= 0:
            raise Unavailable("native gas USD price unavailable")
        fee = c.max_gas_native * native_price * 2  # reserve approval + swap ceilings
        if fee > self.cfg.limits.max_fee_usd:
            raise Unavailable("configured gas ceiling exceeds USD fee limit")
        usd = (
            comparison.usd if side == "buy" else comparison.usd * D(out) / comparison.output_amount
        )
        return Quote(
            chain=chain_name,
            token=token,
            side=side,
            input_amount=amount,
            output_amount=out,
            minimum_output=minimum,
            token_decimals=decimals,
            usd=usd,
            fee_usd=fee,
            expires=now() + 10,
            provider="reviewed-v2",
            raw={"native_usd": str(native_price), "token_in": ti, "token_out": to},
        )

    async def prepare_and_send(self, order_id: str, quote: Quote):
        if self.cfg.mode != "live" or not self.cfg.live_enabled:
            raise Unavailable("live broadcast disabled")
        c = self.cfg.chain(quote.chain)
        self.ready(c)
        rpc = RPC(c, self.http)
        await rpc.check_network()
        code = await rpc.call("eth_getCode", [c.router, "latest"])
        if "0x" + keccak(bytes.fromhex(code[2:])).hex() != c.router_code_hash.lower():
            raise Unavailable("router changed since quote")
        if now() > quote.expires:
            raise Unavailable("quote expired before signing")
        key = read_secret(c.secret_file)
        account = Account.from_key(key)
        if account.address.lower() != c.wallet.lower():
            raise Unavailable("secret does not match dedicated wallet")
        # Never use another nonce while any transaction for this chain is unresolved.
        with self.store.session() as s:
            pending = list(
                s.scalars(
                    select(Order).where(
                        Order.mode == "live",
                        Order.state.in_(
                            ["signed", "broadcast", "uncertain", "approval_confirmed", "reconcile"]
                        ),
                    )
                )
            )
            if any(o.id != order_id and o.data.get("chain") == c.name for o in pending):
                raise Unavailable("unresolved order on chain; reconcile before another submission")
        ti, to = quote.raw["token_in"], quote.raw["token_out"]
        allowance_raw = await rpc.call(
            "eth_call",
            [
                {
                    "to": ti,
                    "data": calldata(
                        "allowance(address,address)", ["address", "address"], [c.wallet, c.router]
                    ),
                },
                "latest",
            ],
        )
        allowance = int(allowance_raw, 16)
        approval = allowance < quote.input_amount
        if approval and allowance != 0:
            raise Unavailable("nonzero insufficient allowance; revoke externally before use")
        deadline = int(now()) + 90
        data = (
            "0x" + (APPROVE + encode(["address", "uint256"], [c.router, quote.input_amount])).hex()
            if approval
            else "0x"
            + (
                SWAP
                + encode(
                    ["uint256", "uint256", "address[]", "address", "uint256"],
                    [quote.input_amount, quote.minimum_output, [ti, to], c.wallet, deadline],
                )
            ).hex()
        )
        nonce = int(await rpc.call("eth_getTransactionCount", [c.wallet, "pending"]), 16)
        gas_price = int(await rpc.call("eth_gasPrice"), 16)
        wire = {"from": c.wallet, "to": ti if approval else c.router, "value": "0x0", "data": data}
        gas = int(await rpc.call("eth_estimateGas", [wire]), 16) * 120 // 100
        tx = {
            **wire,
            "to": to_checksum_address(wire["to"]),
            "value": 0,
            "chainId": c.fomo_id,
            "nonce": nonce,
            "gas": gas,
            "gasPrice": gas_price,
        }
        native_balance = int(await rpc.call("eth_getBalance", [c.wallet, "pending"]), 16)
        if native_balance < gas * gas_price:
            raise Unavailable("insufficient separately prefunded native gas")
        validate_transaction(
            tx,
            chain=c,
            wallet=c.wallet,
            token_in=ti,
            token_out=to,
            amount=quote.input_amount,
            minimum=quote.minimum_output,
            deadline=deadline,
            max_fee_native=c.max_gas_native,
            approval=approval,
        )
        await rpc.call("eth_call", [wire, "latest"])
        with self.store.session() as session:
            current = session.get(Order, order_id)
            if current.side == "buy" and (
                self.store.get("paused:live", False)
                or self.store.get("chain_halt:" + c.name, False)
                or current.data.get("trader") in self.store.get("excluded", [])
            ):
                raise Unavailable("entry paused, excluded, or halted before signing")
            if (
                current.side == "buy"
                and now() - current.data["signal_at"] > self.cfg.max_signal_age
            ):
                raise Unavailable("entry expired before signing")
        if now() > quote.expires:
            raise Unavailable("quote expired during simulation")
        signed = account.sign_transaction({k: v for k, v in tx.items() if k != "from"})
        raw_tx = "0x" + signed.raw_transaction.hex()
        tx_hash = "0x" + signed.hash.hex()
        with self.store.session() as s:
            order = s.get(Order, order_id)
            if order.state not in ("prepared", "approval_confirmed"):
                raise Unavailable("order is not signable")
            order.state, order.signed, order.tx_hash = "signed", raw_tx, tx_hash
            order.data = {
                **order.data,
                "approval": approval,
                "nonce": nonce,
                "deadline": deadline,
                "quote": quote.model_dump(mode="json"),
                "gas_cap": str(c.max_gas_native),
            }
        # Durable signed bytes and hash exist before the first broadcast.
        try:
            result = await rpc.call("eth_sendRawTransaction", [raw_tx])
            if result.lower() != tx_hash.lower():
                raise Unavailable("broadcast returned an unexpected hash")
            state = "broadcast"
        except Exception:
            state = "uncertain"
        with self.store.session() as s:
            s.get(Order, order_id).state = state

    async def reconcile(self, order):
        c = self.cfg.chain(order.data["chain"])
        rpc = RPC(c, self.http)
        await rpc.check_network()
        if not order.tx_hash:
            return None
        receipt = await rpc.call("eth_getTransactionReceipt", [order.tx_hash])
        if not receipt:
            # No receipt is NOT proof of failure. Never allocate a fresh nonce or resubmit.
            return None
        height = int(receipt["blockNumber"], 16)
        head = int(await rpc.call("eth_blockNumber"), 16)
        block = await rpc.call("eth_getBlockByNumber", [receipt["blockNumber"], False])
        if head - height < c.confirmations or not block or block["hash"] != receipt["blockHash"]:
            return None
        quote = Quote.model_validate(order.data["quote"])
        gas_native = (
            D(
                int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
                + int(receipt.get("l1Fee", "0x0"), 16)
            )
            / 10**18
        )
        fee = gas_native * D(quote.raw["native_usd"])
        if int(receipt["status"], 16) != 1:
            return {"state": "failed", "fee": str(fee)}
        if order.data.get("approval"):
            return {"state": "approval_confirmed", "fee": str(fee)}
        deltas = {}
        for log in receipt["logs"]:
            token = log["address"].lower()
            deltas[token] = deltas.get(token, 0) + log_delta(log, c.wallet)
        spent = -deltas.get(quote.raw["token_in"].lower(), 0)
        output = deltas.get(quote.raw["token_out"].lower(), 0)
        if spent != quote.input_amount or output < quote.minimum_output:
            return {"state": "reconcile", "reason": "receipt token deltas violate signed intent"}
        if gas_native > c.max_gas_native:
            return {"state": "reconcile", "reason": "actual fee exceeded gas ceiling"}
        return {
            "state": "filled",
            "output": output,
            "fee": str(fee),
            "block": height,
            "block_hash": receipt["blockHash"],
        }


class LiveExecutor:
    def __init__(self, cfg, store, http):
        from .solana import SolanaCPMM

        self.cfg = cfg
        self.evm = LiveV2(cfg, store, http)
        self.solana = SolanaCPMM(cfg, store, http)

    def backend(self, chain):
        return self.solana if self.cfg.chain(chain).kind == "solana" else self.evm

    async def quote(self, chain, token, side, amount, decimals):
        return await self.backend(chain).quote(chain, token, side, amount, decimals)

    async def prepare_and_send(self, order_id, quote):
        return await self.backend(quote.chain).prepare_and_send(order_id, quote)

    async def reconcile(self, order):
        return await self.backend(order.data["chain"]).reconcile(order)

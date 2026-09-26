"""Restricted Raydium CPMM exact-input execution, built locally from verified pool state.

Layout source: raydium-io/raydium-cp-swap, instructions/swap_base_input.rs and
states/pool.rs. Only original SPL Token is accepted; no Token-2022 hooks or ALTs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from sqlalchemy import select

from .config import read_secret
from .db import Order
from .domain import D, Quote, now
from .providers import Relay, Unavailable
from .rpc import RPC

CPMM = Pubkey.from_string("CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C")
TOKEN = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM = Pubkey.default()
SWAP_DISCRIMINATOR = hashlib.sha256(b"global:swap_base_input").digest()[:8]
POOL_DISCRIMINATOR = hashlib.sha256(b"account:PoolState").digest()[:8]


def associated(wallet, mint):
    return Pubkey.find_program_address([bytes(wallet), bytes(TOKEN), bytes(mint)], ATA)[0]


def decode_pool(raw: bytes):
    raw = bytes(raw)
    if len(raw) != 637 or raw[:8] != POOL_DISCRIMINATOR:
        raise Unavailable("unsupported Raydium pool layout/discriminator")
    names = [
        "config",
        "creator",
        "vault0",
        "vault1",
        "lp",
        "mint0",
        "mint1",
        "program0",
        "program1",
        "observation",
    ]
    result = {
        name: Pubkey.from_bytes(raw[8 + i * 32 : 40 + i * 32]) for i, name in enumerate(names)
    }
    result.update(
        status=raw[329],
        decimals0=raw[331],
        decimals1=raw[332],
        open_time=struct.unpack_from("<Q", raw, 373)[0],
    )
    if result["program0"] != TOKEN or result["program1"] != TOKEN:
        raise Unavailable("only original SPL Token pools are supported")
    if result["status"] & 4 or result["open_time"] > now():
        raise Unavailable("pool is not open for swaps")
    return result


def token_account(raw: bytes, mint: Pubkey, owner: Pubkey):
    raw = bytes(raw)
    if (
        len(raw) != 165
        or Pubkey.from_bytes(raw[:32]) != mint
        or Pubkey.from_bytes(raw[32:64]) != owner
    ):
        raise Unavailable("unexpected SPL token account owner/mint/layout")
    if (
        raw[108] != 1
        or struct.unpack_from("<I", raw, 72)[0] != 0
        or struct.unpack_from("<I", raw, 129)[0] != 0
    ):
        raise Unavailable("frozen, delegated, or close-authorized token account rejected")
    return struct.unpack_from("<Q", raw, 64)[0]


def build_instructions(pool_address, pool, wallet, mint_in, mint_out, amount, minimum, priority=0):
    if amount <= 0 or minimum <= 0 or amount >= 2**64 or minimum >= 2**64:
        raise Unavailable("invalid swap amounts")
    if {mint_in, mint_out} != {pool["mint0"], pool["mint1"]}:
        raise Unavailable("pool token pair does not match intent")
    input_account, output_account = associated(wallet, mint_in), associated(wallet, mint_out)
    vault_in, vault_out = (
        (pool["vault0"], pool["vault1"])
        if mint_in == pool["mint0"]
        else (pool["vault1"], pool["vault0"])
    )
    authority = Pubkey.find_program_address([b"vault_and_lp_mint_auth_seed"], CPMM)[0]
    create = Instruction(
        ATA,
        b"\x01",
        [
            AccountMeta(wallet, True, True),
            AccountMeta(output_account, False, True),
            AccountMeta(wallet, False, False),
            AccountMeta(mint_out, False, False),
            AccountMeta(SYSTEM, False, False),
            AccountMeta(TOKEN, False, False),
        ],
    )
    accounts = [
        (wallet, True, True),
        (authority, False, False),
        (pool["config"], False, False),
        (pool_address, False, True),
        (input_account, False, True),
        (output_account, False, True),
        (vault_in, False, True),
        (vault_out, False, True),
        (TOKEN, False, False),
        (TOKEN, False, False),
        (mint_in, False, False),
        (mint_out, False, False),
        (pool["observation"], False, True),
    ]
    swap = Instruction(
        CPMM,
        SWAP_DISCRIMINATOR + struct.pack("<QQ", amount, minimum),
        [AccountMeta(*a) for a in accounts],
    )
    return [set_compute_unit_limit(300000), set_compute_unit_price(priority), create, swap]


def validate_message(message, expected_instructions, wallet, blockhash):
    expected = Message.new_with_blockhash(expected_instructions, wallet, blockhash)
    if bytes(message) != bytes(expected):
        raise Unavailable(
            "unexpected Solana instructions, recipient, account privileges or amounts"
        )
    if message.header.num_required_signatures != 1 or message.account_keys[0] != wallet:
        raise Unavailable("unexpected Solana signer")


class SolanaCPMM:
    def __init__(self, cfg, store, http):
        self.cfg, self.store, self.http = cfg, store, http
        self.relay = Relay(cfg, http)

    async def account(self, rpc, address):
        result = await rpc.call(
            "getAccountInfo", [str(address), {"encoding": "base64", "commitment": "finalized"}]
        )
        item = result["value"]
        if not item:
            return None, None
        return item, base64.b64decode(item["data"][0], validate=True)

    async def context(self, chain, token, side, amount):
        c = self.cfg.chain(chain)
        if not c.solana_program_data_hash or token not in c.solana_pools or not c.max_gas_native:
            raise Unavailable(
                "Solana live requires reviewed CPMM pool, program-data hash and gas cap"
            )
        rpc = RPC(c, self.http)
        await rpc.check_network()
        program, program_raw = await self.account(rpc, CPMM)
        if not program or not program["executable"] or program_raw[:4] != struct.pack("<I", 2):
            raise Unavailable("unexpected upgradeable CPMM program")
        program_data = Pubkey.from_bytes(program_raw[4:36])
        _, deployed = await self.account(rpc, program_data)
        if not deployed or hashlib.sha256(deployed).hexdigest() != c.solana_program_data_hash:
            raise Unavailable("CPMM deployed program-data hash mismatch")
        pool_address = Pubkey.from_string(c.solana_pools[token])
        info, raw = await self.account(rpc, pool_address)
        if not info or info["owner"] != str(CPMM):
            raise Unavailable("pool is not owned by Raydium CPMM")
        pool = decode_pool(raw)
        mint_in, mint_out = map(
            Pubkey.from_string, (c.settlement, token) if side == "buy" else (token, c.settlement)
        )
        wallet = Pubkey.from_string(c.wallet)
        input_address, output_address = associated(wallet, mint_in), associated(wallet, mint_out)
        for mint in (mint_in, mint_out):
            meta, mint_data = await self.account(rpc, mint)
            if (
                not meta
                or meta["owner"] != str(TOKEN)
                or len(mint_data) != 82
                or mint_data[45] != 1
            ):
                raise Unavailable("mint is not an initialized original SPL mint")
            # Avoid tokens with a freeze authority that can strand copied inventory.
            if struct.unpack_from("<I", mint_data, 46)[0] != 0 and str(mint) != c.settlement:
                raise Unavailable("target token has freeze authority")
        meta, raw = await self.account(rpc, input_address)
        if not meta or meta["owner"] != str(TOKEN) or token_account(raw, mint_in, wallet) < amount:
            raise Unavailable("insufficient prefunded input ATA balance")
        out_meta, out_raw = await self.account(rpc, output_address)
        before = 0
        if out_meta:
            if out_meta["owner"] != str(TOKEN):
                raise Unavailable("unexpected output token program")
            before = token_account(out_raw, mint_out, wallet)
        return c, rpc, pool_address, pool, wallet, mint_in, mint_out, before, out_meta is None

    async def quote(self, chain, token, side, amount, decimals):
        c, rpc, pa, pool, wallet, ti, to, before, needs_ata = await self.context(
            chain, token, side, amount
        )
        comparison = await self.relay.quote(chain, token, side, amount, decimals)
        bh = await rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])
        blockhash = Hash.from_string(bh["value"]["blockhash"])
        ixs = build_instructions(
            pa,
            pool,
            wallet,
            ti,
            to,
            amount,
            comparison.minimum_output,
            c.solana_priority_micro_lamports,
        )
        message = Message.new_with_blockhash(ixs, wallet, blockhash)
        unsigned = base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()
        sim = await rpc.call(
            "simulateTransaction",
            [
                unsigned,
                {
                    "encoding": "base64",
                    "sigVerify": False,
                    "commitment": "finalized",
                    "accounts": {"encoding": "base64", "addresses": [str(associated(wallet, to))]},
                },
            ],
        )
        if sim["value"]["err"]:
            raise Unavailable("local CPMM route simulation failed")
        simulated = sim["value"]["accounts"][0]
        after = token_account(base64.b64decode(simulated["data"][0]), to, wallet)
        output = after - before
        if output < comparison.minimum_output:
            raise Unavailable("simulated output below minimum")
        network_fee = (
            await rpc.call(
                "getFeeForMessage",
                [base64.b64encode(bytes(message)).decode(), {"commitment": "finalized"}],
            )
        )["value"]
        if network_fee is None:
            raise Unavailable("fee quote unavailable")
        rent = await rpc.call("getMinimumBalanceForRentExemption", [165]) if needs_ata else 0
        native = await self.relay.raw_quote(
            c.relay_id, c.relay_id, str(SYSTEM), c.settlement, 10**8, c.wallet, c.wallet
        )
        native_usd = D(native["details"]["currencyIn"]["amountUsd"]) * 10
        gas = D(network_fee + rent) / 10**9
        fee = gas * native_usd
        if gas > c.max_gas_native or fee > self.cfg.limits.max_fee_usd or native_usd <= 0:
            raise Unavailable("Solana fee/rent exceeds policy")
        minimum = output * (10000 - self.cfg.limits.slippage_bps) // 10000
        usd = (
            comparison.usd
            if side == "buy"
            else comparison.usd * D(output) / comparison.output_amount
        )
        return Quote(
            chain=chain,
            token=token,
            side=side,
            input_amount=amount,
            output_amount=output,
            minimum_output=minimum,
            token_decimals=decimals,
            usd=usd,
            fee_usd=fee,
            expires=now() + 10,
            provider="reviewed-raydium-cpmm",
            raw={"native_usd": str(native_usd), "token_in": str(ti), "token_out": str(to)},
        )

    async def prepare_and_send(self, order_id, q):
        if self.cfg.mode != "live" or not self.cfg.live_enabled:
            raise Unavailable("live broadcast disabled")
        c, rpc, pa, pool, wallet, ti, to, _, _ = await self.context(
            q.chain, q.token, q.side, q.input_amount
        )
        secret = read_secret(c.secret_file)
        signer = (
            Keypair.from_bytes(bytes(json.loads(secret)))
            if secret.startswith("[")
            else Keypair.from_base58_string(secret)
        )
        if signer.pubkey() != wallet:
            raise Unavailable("secret does not match dedicated Solana wallet")
        with self.store.session() as s:
            pending = list(
                s.scalars(
                    select(Order).where(
                        Order.mode == "live",
                        Order.state.in_(["signed", "broadcast", "uncertain", "reconcile"]),
                    )
                )
            )
            if any(o.id != order_id and o.data.get("chain") == c.name for o in pending):
                raise Unavailable("unresolved Solana transaction; reconcile first")
            order = s.get(Order, order_id)
            if order.side == "buy" and now() - order.data["signal_at"] > self.cfg.max_signal_age:
                raise Unavailable("signal expired before signing")
        latest = (await rpc.call("getLatestBlockhash", [{"commitment": "finalized"}]))["value"]
        blockhash = Hash.from_string(latest["blockhash"])
        ixs = build_instructions(
            pa,
            pool,
            wallet,
            ti,
            to,
            q.input_amount,
            q.minimum_output,
            c.solana_priority_micro_lamports,
        )
        tx = Transaction.new_signed_with_payer(ixs, wallet, [signer], blockhash)
        validate_message(tx.message, ixs, wallet, blockhash)
        raw = base64.b64encode(bytes(tx)).decode()
        sim = await rpc.call(
            "simulateTransaction",
            [raw, {"encoding": "base64", "sigVerify": True, "commitment": "finalized"}],
        )
        if sim["value"]["err"] or now() > q.expires:
            raise Unavailable("final Solana simulation failed or quote expired")
        with self.store.session() as s:
            order = s.get(Order, order_id)
            if order.side == "buy" and (
                self.store.get("paused:live", False)
                or self.store.get("chain_halt:" + c.name, False)
                or order.data.get("trader") in self.store.get("excluded", [])
            ):
                raise Unavailable("entry paused, excluded, or halted before signing")
            if order.side == "buy" and now() - order.data["signal_at"] > self.cfg.max_signal_age:
                raise Unavailable("entry expired during simulation")
            if order.state != "prepared":
                raise Unavailable("order is not signable")
            order.state, order.signed, order.tx_hash = "signed", raw, str(tx.signatures[0])
            order.data = {
                **order.data,
                "quote": q.model_dump(mode="json"),
                "last_valid_block_height": latest["lastValidBlockHeight"],
                "approval": False,
            }
        try:
            signature = await rpc.call(
                "sendTransaction",
                [
                    raw,
                    {
                        "encoding": "base64",
                        "skipPreflight": False,
                        "preflightCommitment": "finalized",
                        "maxRetries": 0,
                    },
                ],
            )
            if signature != str(tx.signatures[0]):
                raise Unavailable("RPC signature mismatch")
            state = "broadcast"
        except Exception:
            state = "uncertain"
        with self.store.session() as s:
            s.get(Order, order_id).state = state

    async def reconcile(self, order):
        c = self.cfg.chain(order.data["chain"])
        rpc = RPC(c, self.http)
        await rpc.check_network()
        status = (
            await rpc.call(
                "getSignatureStatuses", [[order.tx_hash], {"searchTransactionHistory": True}]
            )
        )["value"][0]
        if not status or status.get("confirmationStatus") != "finalized":
            return None
        tx = await rpc.call(
            "getTransaction",
            [
                order.tx_hash,
                {
                    "encoding": "jsonParsed",
                    "commitment": "finalized",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        if not tx:
            return None
        q = Quote.model_validate(order.data["quote"])
        meta = tx["meta"]
        # Input/output are SPL only: payer SOL delta is exclusively fee + ATA rent.
        fee_native = D(meta["preBalances"][0] - meta["postBalances"][0]) / 10**9
        fee = fee_native * D(q.raw["native_usd"])
        if status.get("err") or meta.get("err"):
            return {"state": "failed", "fee": str(fee)}
        amounts = []
        for field in ("preTokenBalances", "postTokenBalances"):
            values = {}
            for row in meta.get(field, []):
                if row.get("owner") == c.wallet:
                    values[row["mint"]] = values.get(row["mint"], 0) + int(
                        row["uiTokenAmount"]["amount"]
                    )
            amounts.append(values)
        pre, post = amounts
        ti, to = q.raw["token_in"], q.raw["token_out"]
        spent, output = pre.get(ti, 0) - post.get(ti, 0), post.get(to, 0) - pre.get(to, 0)
        if (
            spent != q.input_amount
            or output < q.minimum_output
            or fee_native > c.max_gas_native
            or fee_native < 0
        ):
            return {
                "state": "reconcile",
                "reason": "Solana receipt violates spend/output/fee policy",
            }
        return {
            "state": "filled",
            "output": output,
            "fee": str(fee),
            "block": tx["slot"],
            "block_hash": str(tx["slot"]) + ":finalized",
        }

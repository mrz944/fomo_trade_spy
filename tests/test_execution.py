from copy import deepcopy

import pytest
from eth_abi import encode

from fomo_spy.domain import D, now
from fomo_spy.execution import APPROVE, SWAP, validate_transaction
from fomo_spy.providers import Unavailable


@pytest.fixture
def intent(cfg):
    chain = cfg.chain("base")
    chain.router = "0x" + "1" * 40
    wallet, ti, to = ["0x" + x * 40 for x in ("2", "3", "4")]
    deadline = int(now()) + 60
    tx = {
        "chainId": 8453,
        "from": wallet,
        "to": chain.router,
        "value": 0,
        "nonce": 0,
        "gas": 200000,
        "gasPrice": 1000000,
        "data": "0x"
        + (
            SWAP
            + encode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                [25, 100, [ti, to], wallet, deadline],
            )
        ).hex(),
    }
    policy = dict(
        chain=chain,
        wallet=wallet,
        token_in=ti,
        token_out=to,
        amount=25,
        minimum=100,
        deadline=deadline,
        max_fee_native=D(".01"),
    )
    return tx, policy


def test_locally_constructed_swap_validates(intent):
    tx, policy = intent
    validate_transaction(tx, **policy)


@pytest.mark.parametrize(
    "field,value",
    [
        ("chainId", 1),
        ("from", "0xBAD"),
        ("to", "0xBAD"),
        ("value", 1),
        ("gas", 10**20),
        ("nonce", -1),
        ("data", "0x12345678"),
        ("authorizationList", []),
    ],
)
def test_transaction_mutations_rejected(intent, field, value):
    tx, policy = intent
    tx[field] = value
    with pytest.raises(Unavailable):
        validate_transaction(tx, **policy)


def test_exact_approval_only_and_no_unlimited_spender(intent):
    tx, policy = intent
    tx["to"] = policy["token_in"]
    tx["data"] = (
        "0x" + (APPROVE + encode(["address", "uint256"], [policy["chain"].router, 25])).hex()
    )
    validate_transaction(tx, **policy, approval=True)
    tx["data"] = (
        "0x"
        + (APPROVE + encode(["address", "uint256"], [policy["chain"].router, 2**256 - 1])).hex()
    )
    with pytest.raises(Unavailable):
        validate_transaction(tx, **policy, approval=True)


def test_minimum_recipient_path_and_deadline_bound_to_bytes(intent):
    tx, policy = intent
    for field, value in [
        ("minimum", 99),
        ("wallet", "0x" + "5" * 40),
        ("token_out", "0x" + "6" * 40),
        ("deadline", int(now()) - 1),
    ]:
        changed = deepcopy(policy)
        changed[field] = value
        with pytest.raises(Unavailable):
            validate_transaction(tx, **changed)


async def test_signed_transaction_committed_before_uncertain_broadcast(cfg, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from eth_account import Account
    from eth_utils import keccak

    from fomo_spy.config import Limits
    from fomo_spy.db import Order, Store
    from fomo_spy.domain import Quote
    from fomo_spy.execution import LiveV2

    account = Account.create()  # Ephemeral unfunded test wallet; RPC is fully mocked.
    c = cfg.chain("base")
    secret = tmp_path / "test-key"
    secret.write_text(account.key.hex())
    secret.chmod(0o600)
    c.wallet, c.secret_file = account.address, secret
    c.router = "0x" + "1" * 40
    c.router_code_hash = "0x" + keccak(bytes.fromhex("6000")).hex()
    c.max_gas_native = D(".01")
    cfg.live_enabled = True
    cfg.live = Limits(**Limits().model_dump())
    cfg.mode = "live"
    store = Store(cfg.state_dir / "live.sqlite")
    q = Quote(
        chain="base",
        token="0x" + "4" * 40,
        side="buy",
        input_amount=25,
        output_amount=101,
        minimum_output=100,
        token_decimals=6,
        usd=25,
        fee_usd=1,
        expires=now() + 10,
        provider="reviewed-v2",
        raw={"token_in": c.settlement, "token_out": "0x" + "4" * 40, "native_usd": "2000"},
    )
    with store.session() as s:
        s.add(
            Order(
                id="test",
                mode="live",
                event_key="test",
                position_id="pos",
                side="buy",
                state="prepared",
                created=now(),
                data={"chain": "base", "signal_at": now()},
                signed="",
                tx_hash="",
                error="",
            )
        )
    rpc = AsyncMock()

    async def call(method, params=None):
        if method == "eth_getCode":
            return "0x6000"
        if method == "eth_call":
            return hex(1000)
        if method == "eth_getBalance":
            return hex(10**18)
        if method == "eth_getTransactionCount":
            return "0x0"
        if method == "eth_gasPrice":
            return hex(1000000)
        if method == "eth_estimateGas":
            return hex(100000)
        if method == "eth_sendRawTransaction":
            with store.session() as s:
                o = s.get(Order, "test")
                assert o.state == "signed" and o.signed == params[0]
                assert o.tx_hash and o.data["nonce"] == 0
            raise TimeoutError("ambiguous network outcome")
        if method == "eth_getTransactionReceipt":
            return None
        raise AssertionError(method)

    rpc.call.side_effect = call
    monkeypatch.setattr("fomo_spy.execution.RPC", lambda *_: rpc)
    live = LiveV2(cfg, store, None)
    await live.prepare_and_send("test", q)
    with store.session() as s:
        order = s.get(Order, "test")
        assert order.state == "uncertain"
    assert await live.reconcile(order) is None
    assert sum(c.args[0] == "eth_sendRawTransaction" for c in rpc.call.call_args_list) == 1
    store.close()

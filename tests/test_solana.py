import struct

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey

from fomo_spy.providers import Unavailable
from fomo_spy.solana import (
    CPMM,
    POOL_DISCRIMINATOR,
    SWAP_DISCRIMINATOR,
    TOKEN,
    associated,
    build_instructions,
    decode_pool,
    token_account,
    validate_message,
)


def pool_fixture():
    keys = [Pubkey.new_unique() for _ in range(10)]
    keys[7] = keys[8] = TOKEN
    raw = bytearray(637)
    raw[:8] = POOL_DISCRIMINATOR
    for i, key in enumerate(keys):
        raw[8 + i * 32 : 40 + i * 32] = bytes(key)
    raw[331:333] = bytes([6, 6])
    return raw


def test_cpmm_layout_and_legacy_program_validation():
    raw = pool_fixture()
    pool = decode_pool(raw)
    assert pool["decimals0"] == 6
    for mutation in ["discriminator", "program", "status", "size"]:
        changed = bytearray(raw)
        if mutation == "discriminator":
            changed[0] ^= 1
        elif mutation == "program":
            changed[8 + 7 * 32] ^= 1
        elif mutation == "status":
            changed[329] = 4
        else:
            changed.append(0)
        with pytest.raises(Unavailable):
            decode_pool(changed)


def test_exact_solana_message_has_no_unreviewed_instructions():
    pool = decode_pool(pool_fixture())
    wallet = Keypair().pubkey()
    blockhash = Hash.default()
    ixs = build_instructions(
        Pubkey.new_unique(), pool, wallet, pool["mint0"], pool["mint1"], 25, 100
    )
    message = Message.new_with_blockhash(ixs, wallet, blockhash)
    validate_message(message, ixs, wallet, blockhash)
    swap = ixs[-1]
    assert swap.program_id == CPMM
    assert bytes(swap.data) == SWAP_DISCRIMINATOR + struct.pack("<QQ", 25, 100)
    assert swap.accounts[5].pubkey == associated(wallet, pool["mint1"])
    bad = Instruction(Pubkey.default(), b"drain", [AccountMeta(wallet, True, True)])
    with pytest.raises(Unavailable):
        validate_message(
            Message.new_with_blockhash([*ixs, bad], wallet, blockhash), ixs, wallet, blockhash
        )
    altered = list(ixs)
    altered[-1] = Instruction(CPMM, SWAP_DISCRIMINATOR + struct.pack("<QQ", 25, 1), swap.accounts)
    with pytest.raises(Unavailable):
        validate_message(
            Message.new_with_blockhash(altered, wallet, blockhash), ixs, wallet, blockhash
        )


def test_token_account_owner_mint_delegates_and_freeze():
    wallet, mint = Pubkey.new_unique(), Pubkey.new_unique()
    raw = bytearray(165)
    raw[:32], raw[32:64] = bytes(mint), bytes(wallet)
    struct.pack_into("<Q", raw, 64, 100)
    raw[108] = 1
    assert token_account(raw, mint, wallet) == 100
    for offset in [0, 32, 72, 108, 129]:
        bad = bytearray(raw)
        bad[offset] ^= 1
        with pytest.raises(Unavailable):
            token_account(bad, mint, wallet)

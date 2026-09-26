from __future__ import annotations

import hashlib
import time
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .config import Strict

ChainName = Literal["solana", "ethereum", "base", "bsc", "monad", "robinhood", "arc"]

D = Decimal


def now() -> float:
    return time.time()


def address(value: str, chain: str) -> str:
    return value if chain == "solana" else value.lower()


class Fill(Strict):
    """Normalized, evidenced spot fill; unknown cost/fees remain unknown."""

    id: str
    trader: str
    chain: ChainName
    token: str
    side: Literal["buy", "sell", "transfer", "airdrop"]
    quantity: Decimal = Field(gt=0)
    usd: Decimal | None = Field(None, ge=0)
    fee_usd: Decimal | None = Field(None, ge=0)
    timestamp: float
    tx: str = ""
    provenance: str

    @model_validator(mode="after")
    def canonical(self):
        self.__dict__["token"] = address(self.token, self.chain)
        self.__dict__["tx"] = address(self.tx, self.chain)
        return self


class Signal(Strict):
    trader: str
    chain: ChainName
    token: str
    side: Literal["buy", "sell", "transfer"]
    quantity: Decimal = Field(gt=0)
    source_before: Decimal | None = Field(None, ge=0)
    source_after: Decimal | None = Field(None, ge=0)
    token_decimals: int = Field(ge=0, le=18)
    tx: str
    block: int = Field(ge=0)
    block_hash: str
    timestamp: float
    received: float = Field(default_factory=now)
    provider: str = "rpc"
    historical: bool = False
    finalized: bool = False
    event_id: str = ""

    @model_validator(mode="after")
    def normalize(self):
        self.__dict__["token"] = address(self.token, self.chain)
        self.__dict__["tx"] = address(self.tx, self.chain)
        if not self.tx or not self.block_hash:
            raise ValueError("a copy signal requires chain transaction and block identity")
        return self

    @property
    def key(self):
        # Net wallet delta per token/transaction, not one event per transfer log.
        raw = "|".join((self.chain, self.tx, self.trader, self.token))
        return hashlib.sha256(raw.encode()).hexdigest()


class Quote(Strict):
    chain: ChainName
    token: str
    side: Literal["buy", "sell"]
    input_amount: int = Field(gt=0)
    output_amount: int = Field(gt=0)
    minimum_output: int = Field(gt=0)
    token_decimals: int = Field(ge=0, le=18)
    usd: Decimal = Field(gt=0)
    fee_usd: Decimal = Field(ge=0)
    timestamp: float = Field(default_factory=now)
    expires: float
    provider: str
    raw: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def amounts(self):
        if self.minimum_output > self.output_amount:
            raise ValueError("minimum output exceeds expected output")
        return self

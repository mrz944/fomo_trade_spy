from __future__ import annotations

import os
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class Chain(Strict):
    name: str
    fomo_id: int
    relay_id: int
    kind: Literal["evm", "solana"] = "evm"
    rpc: str
    historical_rpc: str | None = None
    ws: str = ""
    settlement: str
    decimals: int = Field(6, ge=0, le=18)
    enabled: bool = True
    wallet: str = ""
    secret_file: Path | None = None
    confirmations: int = Field(2, ge=1)
    solana_pools: dict[str, str] = Field(default_factory=dict)
    solana_program_data_hash: str = ""
    solana_priority_micro_lamports: int = Field(0, ge=0, le=100000)
    # Live v2 routes require reviewed router bytecode and a native gas USD price.
    router: str = ""
    router_code_hash: str = ""
    max_gas_native: Decimal | None = Field(None, gt=0)


def default_chains() -> list[Chain]:
    rows = [
        (
            "solana",
            1399811149,
            792703809,
            "solana",
            "https://api.mainnet-beta.solana.com",
            "wss://api.mainnet-beta.solana.com",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            6,
        ),
        (
            "ethereum",
            1,
            1,
            "evm",
            "https://ethereum.publicnode.com",
            "wss://ethereum.publicnode.com",
            "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            6,
        ),
        (
            "base",
            8453,
            8453,
            "evm",
            "https://base.drpc.org",
            "wss://base.drpc.org",
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            6,
        ),
        (
            "bsc",
            56,
            56,
            "evm",
            "https://bsc-rpc.publicnode.com",
            "wss://bsc-rpc.publicnode.com",
            "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
            18,
        ),
        (
            "monad",
            143,
            143,
            "evm",
            "https://rpc3.monad.xyz",
            "wss://rpc3.monad.xyz",
            "0x754704bc059f8c67012fed69bc8a327a5aafb603",
            6,
        ),
        (
            "robinhood",
            4663,
            4663,
            "evm",
            "https://rpc.mainnet.chain.robinhood.com",
            "",
            "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
            6,
        ),
        (
            "arc",
            5042,
            5042,
            "evm",
            "https://rpc.mainnet.arc.io",
            "",
            "0x3600000000000000000000000000000000000000",
            6,
        ),
    ]
    return [
        Chain(name=n, fomo_id=f, relay_id=r, kind=k, rpc=h, ws=w, settlement=s, decimals=d)
        for n, f, r, k, h, w, s, d in rows
    ]


class Limits(Strict):
    capital_usd: Decimal = Field(Decimal("1000"), gt=0)
    buy_usd: Decimal = Field(Decimal("25"), gt=0)
    exposure_usd: Decimal = Field(Decimal("250"), gt=0)
    positions: int = Field(10, gt=0)
    daily_loss_usd: Decimal = Field(Decimal("50"), gt=0)
    slippage_bps: int = Field(100, ge=0, le=1000)
    adverse_bps: int = Field(50, ge=0, le=1000)
    max_fee_usd: Decimal = Field(Decimal("5"), gt=0)


class Settings(Strict):
    mode: Literal["paper", "live"] = "paper"
    state_dir: Path = Path("state")
    socket: Path | None = None
    chains: list[Chain] = Field(default_factory=default_chains)
    paper: Limits = Field(default_factory=Limits)
    live: Limits | None = None
    live_enabled: bool = False
    fomo_key_file: Path | None = None
    fomo_url: str = "https://api.fomoapi.io"
    fomo_ws: str = "wss://api.fomoapi.io/ws/alerts"
    relay_url: str = "https://api.relay.link"
    candidates: int = Field(50, ge=1, le=150)
    follow: int = Field(5, ge=1, le=50)
    monthly_credits: int = Field(250000, ge=0)
    credit_reserve: int = Field(20000, ge=0)
    discovery_ttl: int = Field(7 * 86400, ge=60)
    fomo_min_request_interval: float = Field(1.1, ge=0.1)
    history_batch_blocks: int = Field(1000, ge=1, le=10000)
    history_refresh_seconds: int = Field(300, ge=30)
    history_pages: int = Field(3, ge=1, le=100)
    max_signal_age: int = Field(30, ge=1, le=30)
    rpc_poll_seconds: float = Field(2, ge=0.2, le=10)
    max_catchup_blocks: int = Field(1000, ge=1)
    auto_bridge: bool = False
    bridge_max_usd: Decimal = Field(Decimal("100"), gt=0)
    # Direct public-page extraction is discovery-only and opt-in.
    direct_fomo_urls: list[str] = Field(default_factory=list)
    history_import: Path | None = None

    @model_validator(mode="after")
    def coherent(self):
        if not any(c.enabled for c in self.chains):
            raise ValueError("enable at least one chain")
        if len({c.name for c in self.chains}) != len(self.chains):
            raise ValueError("duplicate chain names")
        if self.mode == "live":
            if self.auto_bridge:
                raise ValueError(
                    "automatic live bridging requires a bridge transaction decoder; unavailable"
                )
            if not self.live_enabled or self.live is None:
                raise ValueError("live requires live_enabled=true and explicit [live] limits")
            required = set(Limits.model_fields)
            if not required.issubset(self.live.model_fields_set):
                raise ValueError("all live limits must be explicitly configured")
            for chain in self.chains:
                if chain.enabled and (not chain.wallet or not chain.secret_file):
                    raise ValueError(
                        f"{chain.name}: live requires a dedicated wallet and secret_file"
                    )
        return self

    @property
    def limits(self):
        return self.live if self.mode == "live" else self.paper

    @property
    def socket_path(self) -> Path:
        return self.socket or self.state_dir / "daemon.sock"

    def chain(self, name: str) -> Chain:
        return next(c for c in self.chains if c.name == name)

    def key(self) -> str:
        return (
            read_secret(self.fomo_key_file) if self.fomo_key_file else os.getenv("FOMO_API_KEY", "")
        )


def read_secret(path: Path) -> str:
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("secret must be a regular file with mode 0600 or 0400")
    return path.read_text().strip()


def load_config(path: Path | None = None) -> Settings:
    if path:
        with path.open("rb") as f:
            return Settings.model_validate(tomllib.load(f))
    return Settings()

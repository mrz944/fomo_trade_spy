import pytest
from pydantic import ValidationError

from fomo_spy.config import Limits, Settings, read_secret
from fomo_spy.preflight import preflight


def test_paper_defaults_and_live_explicit_requirements():
    cfg = Settings()
    assert cfg.mode == "paper"
    assert cfg.paper.capital_usd == 1000
    assert cfg.paper.buy_usd == 25
    assert len(cfg.chains) == 7
    assert cfg.candidates == 50 and cfg.follow == 5
    with pytest.raises(ValidationError):
        Settings(mode="live")
    with pytest.raises(ValidationError):
        Settings(mode="live", live_enabled=True, live=Limits())


def test_secrets_reject_group_world_permissions_and_symlinks(tmp_path):
    path = tmp_path / "secret"
    path.write_text("not-a-real-key")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_secret(path)
    path.chmod(0o600)
    assert read_secret(path) == "not-a-real-key"
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        read_secret(link)


async def test_doctor_is_offline_and_never_submits(cfg):
    report = await preflight(cfg)
    assert report["funded_transactions_submitted"] == 0
    assert not report["network_checks"]


def test_nonfinite_amounts_and_perpetual_chains_rejected():
    from conftest import signal

    with pytest.raises(ValidationError):
        signal(timestamp=float("nan"))
    with pytest.raises(ValidationError):
        signal(chain="hyperliquid")


def test_chain_allocation_cannot_mint_capital_on_restart(engine):
    from fomo_spy.config import default_chains
    from fomo_spy.engine import Engine

    engine.cfg.chains.append(default_chains()[0])
    with pytest.raises(ValueError, match="allocation"):
        Engine(engine.cfg, engine.store, engine.quotes)


def test_initial_migration_is_versioned_and_idempotent(cfg):
    from sqlalchemy import inspect, text

    from fomo_spy.db import Store

    path = cfg.state_dir / "migrate.sqlite"
    for _ in range(2):
        store = Store(path)
        with store.engine.connect() as connection:
            assert (
                connection.execute(text("select version_num from alembic_version")).scalar()
                == "0004"
            )
            assert {"events", "orders", "positions", "cash", "ledger", "kv"}.issubset(
                inspect(connection).get_table_names()
            )
        store.close()

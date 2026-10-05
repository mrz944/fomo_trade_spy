import pytest

from fomo_spy.config import Settings, default_chains
from fomo_spy.db import Store
from fomo_spy.demo import DemoQuotes, seed
from fomo_spy.domain import D, Signal, now
from fomo_spy.engine import Engine


@pytest.fixture
def cfg(tmp_path):
    chain = next(c for c in default_chains() if c.name == "base")
    chain.historical_rpc_interval = 0
    chain.rpc_requests_per_second = 100
    return Settings(state_dir=tmp_path / "private", chains=[chain])


@pytest.fixture
def engine(cfg):
    store = Store(cfg.state_dir / "spy.sqlite")
    seed(store)
    result = Engine(cfg, store, DemoQuotes(cfg))
    yield result
    store.close()


def signal(side="buy", tx="tx1", trader="demo-7", **changes):
    values = dict(
        trader=trader,
        chain="base",
        token="token",
        side=side,
        quantity=D(100),
        source_before=D(0),
        source_after=D(100),
        token_decimals=6,
        tx=tx,
        block=10,
        block_hash="block10",
        timestamp=now(),
        finalized=True,
    )
    values.update(changes)
    return Signal(**values)

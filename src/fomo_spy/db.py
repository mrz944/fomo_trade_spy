from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import JSON, Float, Integer, String, Text, create_engine, event, inspect, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class KV(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class Event(Base):
    __tablename__ = "events"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text, default="")
    received: Mapped[float] = mapped_column(Float)


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String, index=True)
    trader: Mapped[str] = mapped_column(String)
    chain: Mapped[str] = mapped_column(String)
    token: Mapped[str] = mapped_column(String)
    decimals: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[str] = mapped_column(String, default="0")
    cost: Mapped[str] = mapped_column(String, default="0")
    realized: Mapped[str] = mapped_column(String, default="0")
    mark: Mapped[str] = mapped_column(String, default="0")
    marked_at: Mapped[float] = mapped_column(Float, default=0)
    needs_reconcile: Mapped[bool] = mapped_column(default=False)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String, index=True)
    event_key: Mapped[str] = mapped_column(String)
    position_id: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    created: Mapped[float] = mapped_column(Float)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    tx_hash: Mapped[str] = mapped_column(String, default="")
    signed: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")


class Cash(Base):
    __tablename__ = "cash"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    balance: Mapped[str] = mapped_column(String)


class Ledger(Base):
    __tablename__ = "ledger"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String, index=True)
    chain: Mapped[str] = mapped_column(String)
    timestamp: Mapped[float] = mapped_column(Float)
    cash_delta: Mapped[str] = mapped_column(String)
    realized: Mapped[str] = mapped_column(String)
    details: Mapped[dict] = mapped_column(JSON)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("database may not be a symlink")
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 10})

        @event.listens_for(self.engine, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")

        from alembic import command
        from alembic.config import Config

        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            tables = inspect(connection).get_table_names()
            # Only adopt the exact pre-migration development schema; never silently alter unknown tables.
            if "kv" in tables and "alembic_version" not in tables:
                expected = set(Base.metadata.tables)
                if set(tables) != expected:
                    raise ValueError("unversioned database schema is not recognized")
                for table in Base.metadata.tables.values():
                    actual = {
                        column["name"] for column in inspect(connection).get_columns(table.name)
                    }
                    if actual != set(table.columns.keys()):
                        raise ValueError("unversioned database columns are not recognized")
                command.stamp(config, "0001")
            command.upgrade(config, "head")
        os.chmod(path, 0o600)

    @contextmanager
    def session(self):
        with Session(self.engine, expire_on_commit=False) as s, s.begin():
            yield s

    def get(self, key: str, default=None):
        with self.session() as s:
            item = s.get(KV, key)
            return item.value if item else default

    def put(self, key: str, value):
        # JSON roundtrip avoids mutable references and rejects non-serializable objects.
        with self.session() as s:
            s.merge(KV(key=key, value=json.loads(json.dumps(value))))

    def items(self, prefix: str):
        with self.session() as s:
            return {k.key: k.value for k in s.scalars(select(KV).where(KV.key.startswith(prefix)))}

    def close(self):
        self.engine.dispose()

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from fomo_spy import db


def test_existing_cash_lots_orders_audit_survive_additive_policy_migration(tmp_path):
    path = tmp_path / "old.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    config = Config()
    config.set_main_option("script_location", str(Path(db.__file__).parent / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0003")
        connection.execute(text("INSERT INTO cash VALUES ('paper:base','974.99')"))
        connection.execute(
            text("""INSERT INTO positions
            (id,mode,trader,chain,token,decimals,quantity,cost,realized,mark,marked_at,needs_reconcile)
            VALUES ('p','paper','u','base','token',6,'100','25.01','2.35','24.5',123,0)""")
        )
        connection.execute(
            text("""INSERT INTO orders
            (id,mode,event_key,position_id,side,state,created,data,tx_hash,signed,error)
            VALUES ('o','paper','e','p','buy','filled',123,'{"fee_usd":"0.01"}','','','')""")
        )
        connection.execute(
            text("""INSERT INTO ledger
            (id,mode,chain,timestamp,cash_delta,realized,details)
            VALUES ('o','paper','base',123,'-25.01','0','{"fee_usd":"0.01"}')""")
        )
        connection.execute(
            text("""INSERT INTO events VALUES
            ('e','{"historical": false}','processed','',123)""")
        )
        before = {
            t: list(connection.execute(text("SELECT * FROM " + t)))
            for t in ("cash", "positions", "orders", "ledger", "events")
        }
    engine.dispose()
    store = db.Store(path)
    with store.engine.connect() as connection:
        after = {t: list(connection.execute(text("SELECT * FROM " + t))) for t in before}
        assert after["cash"] == before["cash"] and after["events"] == before["events"]
        assert tuple(after["positions"][0][:-1]) == tuple(before["positions"][0])
        assert after["positions"][0][-1] == "verified"
        for table, column in (("orders", 7), ("ledger", 6)):
            a, b = list(after[table][0]), list(before[table][0])
            payload = json.loads(a[column])
            assert payload.pop("selection_policy") == "verified"
            assert payload == json.loads(b[column])
            a[column] = b[column]
            assert a == b
        assert connection.execute(text("PRAGMA integrity_check")).scalar() == "ok"
    store.close()

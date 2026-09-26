import os
from pathlib import Path

import pytest
from conftest import signal

from fomo_spy.ipc import Control, request
from fomo_spy.providers import Unavailable
from fomo_spy.tui import SpyApp


@pytest.fixture
async def server(engine, tmp_path):
    # macOS Unix sockets have a short path limit; use a private short directory.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="fsp-") as root:
        path = Path(root) / "s"
        control = Control(engine)
        srv = await control.serve(path)
        yield path, control
        srv.close()
        await srv.wait_closed()
        path.unlink(missing_ok=True)


async def test_socket_permissions_pause_exclusion_and_single_use_confirmation(engine, server):
    path, _ = server
    assert path.stat().st_mode & 0o777 == 0o600
    await engine.event(signal())
    assert (await request(path, {"command": "pause"}))["paused"]
    assert (await request(path, {"command": "exclude", "trader": "demo-7"}))["watching"]
    result = await request(path, {"command": "close", "position": "all"})
    assert engine.snapshot()["positions"]
    await request(path, {"command": "confirm", "token": result["confirmation"]})
    assert not engine.snapshot()["positions"]
    with pytest.raises(Unavailable):
        await request(path, {"command": "confirm", "token": result["confirmation"]})


async def test_confirmation_invalidates_on_changed_inventory(engine, server):
    path, _ = server
    await engine.event(signal())
    result = await request(path, {"command": "close", "position": "all"})
    await engine.event(signal(tx="second"))
    with pytest.raises(Unavailable, match="changed"):
        await request(path, {"command": "confirm", "token": result["confirmation"]})


async def test_socket_rejects_non_socket_and_insecure_directory(engine, tmp_path):
    path = tmp_path / "x"
    os.chmod(tmp_path, 0o700)
    path.write_text("keep this file")
    with pytest.raises(Unavailable):
        await Control(engine).serve(path)
    assert path.read_text() == "keep this file"
    os.chmod(tmp_path, 0o755)
    with pytest.raises(Unavailable):
        await Control(engine).serve(tmp_path / "sock")


async def test_tui_attaches_controls_and_detaches_without_daemon_exit(engine, server):
    path, _ = server
    app = SpyApp(path)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause()
        assert app.last_snapshot["mode"] == "paper"
        assert len(app.last_snapshot["rankings"]) == 7
        await app.command("pause")
        assert engine.store.get("paused:paper") is True
        await app.command("resume")
        assert engine.store.get("paused:paper") is False
        await engine.event(signal())
        await app.command("close-all")
        await pilot.pause()
        await pilot.click("#cancel")
        assert engine.snapshot()["positions"]
        app.query_one("#command").focus()
        await pilot.press("ctrl+q")
        assert not app.is_running
    assert (await request(path, {"command": "ping"}))["alive"]


async def test_tui_disconnected_is_recoverable(tmp_path):
    app = SpyApp(tmp_path / "missing.sock")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.last_snapshot


async def test_complete_demo_daemon_start_stop_and_restart(tmp_path):
    import asyncio
    import tempfile

    from fomo_spy.config import Settings
    from fomo_spy.daemon import Daemon
    from fomo_spy.db import Store

    with tempfile.TemporaryDirectory(prefix="fspd-") as root:
        cfg = Settings(state_dir=Path(root))
        for _ in range(2):
            daemon = Daemon(cfg, demo=True)
            task = asyncio.create_task(daemon.run())
            try:
                for _ in range(100):
                    if cfg.socket_path.exists():
                        break
                    await asyncio.sleep(0.01)
                snapshot = await request(cfg.socket_path, {"command": "status"})
                assert snapshot["mode"] == "paper"
                assert len(snapshot["rankings"]) == 7
                await request(cfg.socket_path, {"command": "pause"})
            finally:
                daemon.stop.set()
                await asyncio.wait_for(task, 5)
            assert not cfg.socket_path.exists()
        store = Store(cfg.state_dir / "spy.sqlite")
        assert store.get("paused:paper") is True
        assert store.get("environment") == "synthetic-demo"
        store.close()

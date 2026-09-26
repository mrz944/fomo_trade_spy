import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from fomo_spy.daemon import Daemon
from fomo_spy.ipc import request


async def test_keyless_real_daemon_remains_controllable_without_discovery(cfg, monkeypatch):
    monkeypatch.delenv("FOMO_API_KEY", raising=False)
    with tempfile.TemporaryDirectory(prefix="fsp-") as root:
        cfg.socket = Path(root) / "s"
        daemon = Daemon(cfg)
        # No external requests are needed to exercise service/control startup.
        monkeypatch.setattr(daemon, "supervise_chain", AsyncMock())
        discovery = AsyncMock(side_effect=AssertionError("keyless discovery called"))
        monkeypatch.setattr(daemon.fomo, "discover", discovery)
        task = asyncio.create_task(daemon.run())
        try:
            for _ in range(100):
                if daemon.store.get("health:fomo_stream"):
                    break
                await asyncio.sleep(0.01)
            snapshot = await request(cfg.socket, {"command": "status"})
            assert snapshot["mode"] == "paper"
            assert snapshot["rankings"] == snapshot["positions"] == snapshot["orders"] == []
            assert daemon.store.get("environment") == "real"
            for provider in ("discovery", "fomo_stream"):
                health = snapshot["health"]["health:" + provider]
                assert health["state"] == "unavailable"
                assert "FOMO_API_KEY missing" in health["reason"]
            assert snapshot["credits"]["spent"] == 0
            assert (await request(cfg.socket, {"command": "pause"}))["paused"]
            assert not (await request(cfg.socket, {"command": "resume"}))["paused"]
            discovery.assert_not_called()
        finally:
            daemon.stop.set()
            await asyncio.wait_for(task, 5)

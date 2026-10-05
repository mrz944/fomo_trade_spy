import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from fomo_spy.daemon import Daemon
from fomo_spy.ipc import request


async def test_research_discovery_never_spends_on_history(cfg, monkeypatch):
    import httpx

    monkeypatch.setenv("FOMO_API_KEY", "fixture-not-a-credential")
    cfg.selection_policy = "paper_research"
    cfg.fomo_min_request_interval = 0.1
    daemon = Daemon(cfg)
    daemon.fomo.history = AsyncMock(side_effect=AssertionError("research requested history"))
    await daemon.http.aclose()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"traders": [{"userId": "u", "pnlUsd": 10}]})
        )
    ) as http:
        daemon.fomo.http = http
        task = asyncio.create_task(daemon.discovery())
        try:
            for _ in range(100):
                if daemon.store.get("current_leaderboard"):
                    break
                await asyncio.sleep(0.01)
            board = daemon.store.get("current_leaderboard")
            assert board["traders"] == ["u"]
            assert board["expires"] == daemon.fomo.discovery_received_at + 21600
            daemon.fomo.history.assert_not_called()
            assert daemon.fomo.credits.status()["spent"] == 250
        finally:
            daemon.stop.set()
            await task
            daemon.store.close()
            daemon.lockfile.close()


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

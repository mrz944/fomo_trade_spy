from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated

import typer

from .config import Settings, load_config
from .daemon import Daemon
from .db import Store
from .demo import DemoQuotes, seed
from .engine import Engine
from .ipc import request
from .preflight import preflight as check
from .tui import SpyApp

app = typer.Typer(no_args_is_help=True, help="FOMO discovery, evidence ranking and copy trading.")
Config = Annotated[Path | None, typer.Option("--config", "-c", help="Private TOML configuration")]


def output(value):
    typer.echo(json.dumps(value, indent=2))


def guarded(coro):
    try:
        return asyncio.run(coro)
    except (ValueError, OSError, RuntimeError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def run(config: Config = None):
    """Run the continuous daemon (use Quadlet for service management)."""
    os.umask(0o077)
    cfg = load_config(config)
    guarded(Daemon(cfg).run())


@app.command()
def demo(serve: bool = False, state_dir: Path = Path("state/demo")):
    """Offline synthetic demonstration; --serve exposes the attachable daemon."""
    os.umask(0o077)
    cfg = Settings(state_dir=state_dir)
    if serve:
        guarded(Daemon(cfg, demo=True).run())
    else:
        store = Store(state_dir / "spy.sqlite")
        if store.get("environment") not in (None, "synthetic-demo"):
            raise typer.BadParameter("demo needs a separate state directory")
        store.put("environment", "synthetic-demo")
        seed(store)
        engine = Engine(cfg, store, DemoQuotes(cfg))
        output(engine.snapshot())
        store.close()


@app.command()
def tui(config: Config = None, socket: Path | None = None):
    """Attach a terminal UI; detaching never stops the daemon."""
    SpyApp(socket or load_config(config).socket_path).run()


@app.command()
def status(config: Config = None, socket: Path | None = None):
    """Query the running daemon."""
    output(guarded(request(socket or load_config(config).socket_path, {"command": "status"})))


@app.command()
def healthcheck(socket: Path = Path("/run/fomo-spy/daemon.sock")):
    """Lightweight local service liveness; never performs provider calls."""
    result = guarded(request(socket, {"command": "ping"}))
    if not result.get("alive"):
        raise typer.Exit(1)


@app.command()
def doctor(config: Config = None):
    """Local configuration and infrastructure checks; no network or signing."""
    output(guarded(check(load_config(config))))


@app.command()
def preflight(config: Config = None, network: bool = False, quotes: bool = False):
    """Read-only network checks; --quotes requests unsigned executable-size quotes."""
    if quotes and not network:
        raise typer.BadParameter("--quotes requires --network")
    output(guarded(check(load_config(config), network, quotes)))


@app.command()
def control(command: str, target: str = "", config: Config = None, socket: Path | None = None):
    """pause/resume/exclude/include/close/close-all (close prompts for confirmation)."""
    path = socket or load_config(config).socket_path
    payload = {"command": command}
    if command in ("exclude", "include"):
        payload["trader"] = target
    if command == "reconcile":
        payload["position"] = target
    if command in ("close", "close-all"):
        payload.update(command="close", position="all" if command == "close-all" else target)
    result = guarded(request(path, payload))
    if "confirmation" in result:
        output(result["positions"])
        if typer.confirm(f"Close these positions in {result['mode'].upper()} mode?", default=False):
            result = guarded(request(path, {"command": "confirm", "token": result["confirmation"]}))
        else:
            result = {"cancelled": True}
    output(result)


if __name__ == "__main__":
    app()

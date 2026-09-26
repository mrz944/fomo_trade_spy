from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import socket
import stat
import struct
from pathlib import Path

from .domain import now
from .providers import Unavailable

MAX = 2**20


async def request(path: Path, payload: dict):
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path), limit=MAX), 5)
    try:
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 30))
        if not response.get("ok"):
            raise Unavailable(response.get("error", "daemon error"))
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


class Control:
    def __init__(self, engine, credits=None):
        self.engine, self.credits = engine, credits
        self.challenges = {}

    def close_state(self, target):
        positions = self.engine.snapshot()["positions"]
        chosen = positions if target == "all" else [p for p in positions if p["id"] == target]
        if not chosen:
            raise Unavailable("no matching open position")
        identity = [
            {k: p[k] for k in ("id", "quantity", "cost", "needs_reconcile")} for p in chosen
        ]
        fingerprint = hashlib.sha256(
            json.dumps(sorted(identity, key=lambda p: p["id"]), sort_keys=True).encode()
        ).hexdigest()
        return chosen, fingerprint

    async def dispatch(self, req):
        cmd = req.get("command", "status")
        e = self.engine
        if cmd in ("status", "ping"):
            result = e.snapshot() if cmd == "status" else {"alive": True, "at": now()}
            if self.credits and cmd == "status":
                result["credits"] = self.credits.status()
            return result
        if cmd in ("pause", "resume"):
            e.store.put("paused:" + e.cfg.mode, cmd == "pause")
            return {"paused": cmd == "pause"}
        if cmd in ("exclude", "include"):
            trader = req.get("trader", "")
            if not e.store.get("trader:" + trader):
                raise Unavailable("unknown trader ID")
            excluded = set(e.store.get("excluded", []))
            if cmd == "exclude":
                excluded.add(trader)
            else:
                excluded.discard(trader)
            e.store.put("excluded", sorted(excluded))
            return {"excluded": sorted(excluded), "watching": e.watched()}
        if cmd == "reconcile":
            return await e.reconcile_inventory(req.get("position", ""))
        if cmd == "close":
            target = req.get("position", "")
            chosen, fingerprint = self.close_state(target)
            self.challenges = {t: v for t, v in self.challenges.items() if v["expires"] > now()}
            token = secrets.token_urlsafe(24)
            self.challenges[token] = {
                "target": target,
                "fingerprint": fingerprint,
                "expires": now() + 30,
            }
            return {
                "confirmation": token,
                "mode": e.cfg.mode,
                "positions": chosen,
                "expires_seconds": 30,
            }
        if cmd == "confirm":
            challenge = self.challenges.pop(req.get("token", ""), None)
            if not challenge or challenge["expires"] < now():
                raise Unavailable("confirmation expired or already used")
            chosen, fingerprint = self.close_state(challenge["target"])
            if fingerprint != challenge["fingerprint"]:
                raise Unavailable("positions changed; request a new confirmation")
            return {p["id"]: await e.close_position(p["id"]) for p in chosen}
        raise Unavailable("unknown control command")

    async def connection(self, reader, writer):
        try:
            sock = writer.get_extra_info("socket")
            if hasattr(socket, "SO_PEERCRED"):
                _, uid, _ = struct.unpack(
                    "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                )
                if uid != os.getuid():
                    raise Unavailable("peer uid mismatch")
            line = await asyncio.wait_for(reader.readline(), 5)
            if len(line) > MAX:
                raise Unavailable("request too large")
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise Unavailable("request must be an object")
            result = await self.dispatch(payload)
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {
                "ok": False,
                "error": str(exc) if isinstance(exc, Unavailable) else type(exc).__name__,
            }
        try:
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def serve(self, path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise Unavailable("socket directory must be owned by daemon user and mode 0700")
        if path.exists() or path.is_symlink():
            entry = path.lstat()
            if not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.getuid():
                raise Unavailable("refusing to replace non-socket or foreign socket")
            try:
                reader, writer = await asyncio.open_unix_connection(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                writer.close()
                await writer.wait_closed()
                raise Unavailable("another daemon is already listening")
        server = await asyncio.start_unix_server(self.connection, path=str(path), limit=MAX)
        os.chmod(path, 0o600)
        return server

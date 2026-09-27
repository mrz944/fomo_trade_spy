from __future__ import annotations

import asyncio
import json
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static, TabbedContent, TabPane

from .ipc import request


class ConfirmClose(ModalScreen[bool]):
    CSS = """
    ConfirmClose { align: center middle; }
    #dialog { width: 65; height: 12; border: thick $warning; padding: 1 2; background: $surface; }
    #dialog Horizontal { height: 3; align: center middle; }
    """

    def __init__(self, result):
        super().__init__()
        self.result = result

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(
                f"Close {len(self.result['positions'])} position(s) in {self.result['mode'].upper()} mode?\n"
                "This requests fresh quotes and submits exits. Confirmation expires in 30 seconds."
            )
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Confirm close", id="confirm", variant="warning")

    @on(Button.Pressed)
    def pressed(self, event):
        self.dismiss(event.button.id == "confirm")


class SpyApp(App):
    TITLE = "FOMO Trade Spy"
    SUB_TITLE = "daemon client"
    CSS = """
    #summary { height: 4; padding: 0 1; background: $boost; }
    #message { height: 2; padding: 0 1; }
    DataTable { height: 1fr; }
    Input { dock: bottom; }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "Detach", priority=True),
        ("q", "quit", "Detach"),
        ("p", "pause", "Pause entries"),
        ("r", "resume", "Resume entries"),
    ]

    def __init__(self, socket: Path):
        super().__init__()
        self.socket = socket
        self.refresh_lock = asyncio.Lock()
        self.last_snapshot = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Connecting to daemon…", id="summary")
        with TabbedContent():
            for name, title in [
                ("rankings", "Rankings & evidence"),
                ("activity", "Activity"),
                ("positions", "Positions"),
                ("orders", "Orders"),
                ("providers", "Health / latency / credits"),
                ("chains", "Chains"),
            ]:
                with TabPane(title, id="tab-" + name):
                    yield DataTable(id=name)
        yield Static(
            "Commands: pause | resume | exclude ID | include ID | close POSITION | close-all",
            id="message",
        )
        yield Input(placeholder="Daemon command (q detaches; daemon keeps running)", id="command")
        yield Footer()

    async def on_mount(self):
        columns = {
            "rankings": [
                "Trader",
                "Selected",
                "Eligible",
                "95% bound",
                "Positions",
                "Tokens",
                "Days",
                "Net $",
                "Without best $",
                "Missing / reasons",
            ],
            "activity": ["Chain", "Trader", "Side", "Token", "Status", "Reason", "Delay ms"],
            "positions": [
                "Position ID",
                "Trader",
                "Chain",
                "Token",
                "Quantity",
                "Cost $",
                "Mark $",
                "Reconcile",
            ],
            "orders": ["Order", "Side", "State", "Transaction", "Error"],
            "providers": ["Provider / metric", "Status"],
            "chains": ["Chain", "Enabled", "Paper", "Live", "Halt"],
        }
        for name, cols in columns.items():
            self.query_one("#" + name, DataTable).add_columns(*cols)
        self.set_interval(2, self.refresh_status)
        await self.refresh_status()

    def table(self, name, rows):
        table = self.query_one("#" + name, DataTable)
        table.clear()
        for row in rows:
            table.add_row(*(str(cell) for cell in row))

    async def refresh_status(self):
        if self.refresh_lock.locked():
            return
        async with self.refresh_lock:
            try:
                s = await request(self.socket, {"command": "status"})
                self.last_snapshot = s
                self.query_one("#summary", Static).update(
                    f"{s['mode'].upper()}  |  Equity ${s['equity_usd']}  |  "
                    f"Entries {'PAUSED' if s['paused'] else 'enabled'}  |  "
                    f"Selected {len(s['selected'])} / Watching {len(s['watching'])}\n"
                    f"Workflow: {s.get('workflow', {}).get('state', 'unknown')} | "
                    f"Evaluated: {s.get('workflow', {}).get('evaluated', 0)}\n"
                    f"{'; '.join(s.get('workflow', {}).get('blockers', [])[:1]) or 'Awaiting fresh source activity'}"
                )
                self.table(
                    "rankings",
                    [
                        [
                            r["trader"],
                            r["trader"] in s["selected"],
                            r["eligible"],
                            f"{r['score']:.4f}" if r["score"] is not None else "unknown",
                            r["completed"],
                            r["tokens"],
                            r["days"],
                            r["net_usd"],
                            r["without_best_usd"],
                            "; ".join(r["reasons"] + r.get("missing", [])),
                        ]
                        for r in s["rankings"]
                    ],
                )
                self.table(
                    "activity",
                    [
                        [
                            a["chain"],
                            a["trader"],
                            a["side"],
                            a["token"],
                            a["status"],
                            a["reason"],
                            round((a["received"] - a["timestamp"]) * 1000),
                        ]
                        for a in s["activity"]
                    ],
                )
                self.table(
                    "positions",
                    [
                        [
                            p[k]
                            for k in [
                                "id",
                                "trader",
                                "chain",
                                "token",
                                "quantity",
                                "cost",
                                "mark",
                                "needs_reconcile",
                            ]
                        ]
                        for p in s["positions"]
                    ],
                )
                self.table(
                    "orders",
                    [
                        [o[k] for k in ["id", "side", "state", "tx_hash", "error"]]
                        for o in s["orders"]
                    ],
                )
                self.table(
                    "providers",
                    [[k, json.dumps(v)] for k, v in s["health"].items()]
                    + [
                        ["Latency", json.dumps(s["latency"])],
                        ["Credits", json.dumps(s.get("credits", {}))],
                        ["Chain cash", json.dumps(s["cash"])],
                        ["Workflow / coverage", json.dumps(s.get("workflow", {}))],
                    ],
                )
                self.table(
                    "chains",
                    [
                        [c[k] for k in ["name", "enabled", "paper", "live", "halt"]]
                        for c in s["chains"]
                    ],
                )
            except Exception as exc:
                self.query_one("#summary", Static).update(
                    f"Daemon unavailable: {type(exc).__name__}. Reconnecting…"
                )

    async def action_pause(self):
        await self.command("pause")

    async def action_resume(self):
        await self.command("resume")

    @on(Input.Submitted, "#command")
    async def submitted(self, event):
        await self.command(event.value)
        event.input.value = ""

    async def command(self, text):
        parts = text.split()
        if not parts:
            return
        cmd = parts[0]
        payload = {"command": "close" if cmd == "close-all" else cmd}
        if cmd in ("include", "exclude") and len(parts) == 2:
            payload["trader"] = parts[1]
        if cmd in ("close", "reconcile") and len(parts) == 2:
            payload["position"] = parts[1]
        if cmd == "close-all":
            payload["position"] = "all"
        try:
            result = await request(self.socket, payload)
            if "confirmation" in result:

                async def confirm(accepted):
                    if accepted:
                        try:
                            response = await request(
                                self.socket, {"command": "confirm", "token": result["confirmation"]}
                            )
                            self.query_one("#message", Static).update(json.dumps(response))
                        except Exception as exc:
                            self.query_one("#message", Static).update(str(exc))

                self.push_screen(ConfirmClose(result), confirm)
            else:
                self.query_one("#message", Static).update(json.dumps(result))
            await self.refresh_status()
        except Exception as exc:
            self.query_one("#message", Static).update(str(exc))

# FOMO Trade Spy

A separate Python 3.12 daemon, Textual terminal client, evidence-ranking engine,
and paper/live copy-trading implementation. This repository does not use or modify
`solana_spy_trader`.

**Status: implemented and offline-tested; production integration is incomplete.**
The executable demo works without keys. FOMO discovery/history/stream clients are
implemented against the current documentation, but authenticated payloads were not
available for validation. Automatic eligibility from those raw history payloads is
therefore blocked; audited normalized history can establish eligibility immediately.
There is no observation waiting period. Live execution is real, restricted code for
reviewed EVM v2 routers and Solana Raydium CPMM pools, not a simulated live mode.
Those live paths have not been exercised against funded wallets. General Relay live
signing, automatic live bridging, complete historical RPC indexing, and a normalized
direct-FOMO scraping feed remain unfinished. Do not treat this as production-ready.

## Run locally

```sh
cd /Users/marcinzadka/DEV/0_ai_trading/fomo-trade-spy
uv sync --locked
uv run fomo-spy demo
uv run fomo-spy demo --serve
```

In another terminal:

```sh
uv run fomo-spy tui --socket state/demo/daemon.sock
uv run fomo-spy status --socket state/demo/daemon.sock
```

`q` detaches the TUI. The daemon continues in its separate process. The synthetic
demo buys and sells a fictitious token every three seconds and labels its data.
Demo and real state directories cannot be mixed. Stop the foreground demo with
Ctrl-C. For a persistent service independent of the terminal, use Quadlet below.

If `uv` is not installed, this workspace has a private bootstrap installation:
`.bootstrap/bin/uv sync --locked` and `.bootstrap/bin/uv run ...`. That bootstrap
and the virtual environment are excluded from Git. For a fresh checkout, install
uv using your normal package manager or create a private bootstrap with
`python3 -m venv .bootstrap` followed by `.bootstrap/bin/pip install uv==0.12.19`.

## Real data, paper first

```sh
cp config.example.toml config.toml
chmod 600 config.toml
# Set fomo_key_file to a private 0600/0400 file, or export FOMO_API_KEY.
uv run fomo-spy doctor --config config.toml
uv run fomo-spy preflight --config config.toml --network --quotes
uv run fomo-spy run --config config.toml
# Another terminal:
uv run fomo-spy tui --config config.toml
```

`doctor` is offline. `preflight --network` reads public health, chain catalogs and
RPC identity. `--quotes` requests unsigned $25 executable-size quotes; it cannot
sign or submit a transaction and never reads a wallet secret. No paid account is
required by the implementation. A free FOMO data key is required for authenticated
discovery. Free public RPC defaults can be replaced with free-account endpoints.

Defaults: paper; $1,000 **total** split across enabled chains; $25 entries; $250
exposure; ten open positions; $50 daily equity-loss halt; 1% slippage plus a further
0.5% adverse paper adjustment. A $5 maximum quote fee is an additional guard.
Gas is separate from token input. Paper origin gas is charged in USD. Route fees
are reflected in executable quote output. No executable quote means no paper fill.

Discovery starts from FOMO's 30-day trader leaderboard, caches up to 50 candidates,
and selects up to five eligible traders. It never starts from generic trending
pools. Each source with copied inventory remains watched after deselection or
exclusion. The seven-day cache TTL is unrelated to eligibility. Imported matched
30-day evidence can qualify at startup; it never generates copy signals. See
[history format](docs/history-format.md).

Default eligibility requires 20 complete inventory cycles, five tokens, three
closing/trading days, positive fee-adjusted returns, positive profit without the
largest winning token, verified 30-day coverage, and a positive conservative
95% lower bound on token-group returns. Transfers, airdrops, unknown fees/costs,
and unmatched sells are excluded. Confidence is grouped by `(chain, token)`;
repeated fills of one winner cannot manufacture independent evidence.

## Monitoring and execution behavior

- FOMO social streaming and direct RPC run independently. Social hints without
  exact transaction identity never submit orders. The current implementation uses
  social activity for context and RPC for executable signals; it does not claim
  the incomplete app feed is a complete ledger.
- EVM new-head subscriptions wake a wallet-specific incoming/outgoing log scan.
  Solana slot subscriptions wake finalized per-wallet signature scans. Polling
  fallback is two seconds. Configured EVM confirmation depth and Solana finality
  add visible latency. Large catch-up gaps halt instead of silently jumping ahead.
- RPC normalization currently handles unambiguous single-token versus configured
  settlement-token balance exchanges. Native-currency routes, complex bundles,
  transfers and unknown routes are classified for reconciliation, not guessed.
  This limits coverage of FOMO traders using native-token swaps.
- Events deduplicate by chain/transaction/source/token. Source histories, replay,
  startup catch-up and events older than 30 seconds cannot open entries. Freshness
  is checked again after quoting and before live signing. Delayed exits may copy
  only when they are current-session events with known source inventory; historical
  exits are never replayed. A missed exit flags the position for reconciliation.
- Copied inventory is separate per source trader. Selling 25% of a source holding
  sells 25% of that source's copied lot, not another trader's lot. Quantity is
  rounded down to token precision. Unknown inventory blocks rather than estimates.
- Orders and signed bytes/hashes are persisted before broadcast. Ambiguous outcomes
  block further live sends on that chain until receipts resolve them. Restart
  recovery cancels unsigned intentions and reconciles signed ones. It never creates
  a replacement transaction just because a receipt is temporarily absent.
- Reorganizations preserve the audit ledger, halt the affected chain and flag
  inventory. Read-only reconciliation checks source and bot balances against
  attributed lots. A bot shortfall requires manual audit; surplus is unallocated.
- Entry controls account for pending exposure, position count, fresh liquidation
  marks, fees and daily equity drawdown. Pausing or a loss halt does not disable
  known-inventory exits. A stale liquidation quote blocks new entries.

The TUI shows ranking evidence, activity, positions, orders, health, latency,
credit usage and chain readiness. Readiness is descriptive and fail-closed, not a
promise that a catalog-listed chain has working live routes.

## Controls

Type commands in the TUI or use `fomo-spy control COMMAND [TARGET]`:

```sh
uv run fomo-spy control pause --config config.toml
uv run fomo-spy control resume --config config.toml
uv run fomo-spy control exclude FOMO_USER_ID --config config.toml
uv run fomo-spy control include FOMO_USER_ID --config config.toml
uv run fomo-spy control reconcile POSITION_ID --config config.toml
uv run fomo-spy control close POSITION_ID --config config.toml
uv run fomo-spy control close-all --config config.toml
```

Close commands show the exact positions/mode and require confirmation. Single-use
confirmation expires after 30 seconds and is invalidated by inventory changes.
`reconcile` performs read-only RPC checks and clears inventory flags only when
ledger quantities are backed; it does not replay missed sells. A confirmed close
can then liquidate remaining copied inventory with a fresh quote. Unknown outcomes
must be resolved first. Control sockets are mode 0600 in a 0700 directory; Linux
also checks peer UID. There is no unauthenticated TCP admin interface.

## Live configuration

`mode="live"`, `live_enabled=true`, **every** `[live]` limit, and per-chain dedicated
wallet addresses/secret files are required. Prefund input assets and native gas
independently on each chain. API keys are distinct from wallet signing secrets.
No wallet is generated or funded by the daemon. Paper/live ledger namespaces are
separate. Live capital is a spending allocation, not a claim about the on-chain
wallet's balance; RPC simulation/balance checks must also pass.

EVM live routes require a reviewed nonproxy v2 router address and runtime-bytecode
keccak hash. It constructs direct `swapExactTokensForTokens` routes, validates all
transaction fields and exact approvals, simulates, signs with `eth-account`, and
reconciles canonical receipts. Unsupported routers/routes fail closed.

Solana live requires reviewed `solana_pools` (token mint → CPMM pool), the SHA256 of
the deployed Raydium CPMM ProgramData account, a dedicated keypair and native fee
cap. `solders` builds a locally validated message with bounded compute budget,
idempotent output ATA creation, and exact-input/minimum-output swap. It verifies
pool ownership/layout, token programs, wallet-owned ATAs, program-data hash, and
simulation. Target Token-2022/freeze authority, arbitrary Relay instructions and
unconfigured pools are rejected. See [integration scope](docs/integrations.md).

`auto_bridge=true` currently enables **paper** bridging only, using current Relay
cross-chain quotes, fees and adverse output. Live automatic bridging is explicitly
rejected during configuration; it is not a fake live feature.

## Rootless Linux service

Container and Quadlet files are supplied but were **not runtime-tested** here:
this development host is macOS without Podman or systemd. Install rootless Podman
with Quadlet support on the target Linux host, then run these commands there:

```sh
podman build -f Containerfile -t localhost/fomo-trade-spy:0.1.0 .
install -d -m 700 ~/.config/fomo-trade-spy ~/.local/share/fomo-trade-spy
install -d -m 700 ~/.config/containers/systemd
cp deploy/config.container.example.toml ~/.config/fomo-trade-spy/config.toml
chmod 600 ~/.config/fomo-trade-spy/config.toml
podman secret create fomo-api /absolute/path/to/private-fomo-key
cp deploy/fomo-spy.container ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start fomo-spy.service
systemctl --user status fomo-spy.service
journalctl --user -u fomo-spy.service
```

The Quadlet install section attaches the generated service to the user's default
target. Enable lingering for the service user with `loginctl enable-linger USER`
if it must remain active after all login sessions end. This may require host
administrator privileges. No remote deployment was performed by this project.

Attach a host-installed TUI using
`fomo-spy tui --socket "$XDG_RUNTIME_DIR/fomo-spy/daemon.sock"`, or run it inside the
container with `podman exec -it fomo-trade-spy fomo-spy tui --socket /run/fomo-spy/daemon.sock`.
The rootless daemon uses a read-only filesystem, dropped capabilities, no-new-
privileges, separate persistent state/runtime mounts, mounted secrets, health
checks and graceful SIGTERM. The container user maps to the host service user.

Back up SQLite with its online backup API (or stop the service before copying the
DB and WAL). Do not copy an active `.sqlite` file alone. State, secrets, local
configuration, logs and virtual environments are ignored by Git and container
builds. Signed-but-unbroadcast bytes are sensitive operational data in the private
state directory. Alembic upgrades run at store startup; the initial migration is
checked in. Future schema migrations belong under `src/fomo_spy/migrations`.

## Verification

```sh
uv run ruff check src tests
uv run pytest -q
uv run fomo-spy demo
uv run fomo-spy doctor
uv run fomo-spy preflight --network --quotes
```

Tests cover accounting, ranking/fees/concentration, incomplete histories,
duplicates, stale/replayed events, quota exhaustion, stream replay/gap recovery,
partial exits, per-source inventory, reorg halts, risk limits, transaction
recipient/spend/approval/minimum validation, durable uncertain broadcasts, RPC
normalization, restricted Unix-socket controls and headless Textual interaction.
Public check results are in [the preflight report](docs/preflight-2026-09-26.json).
No funded transaction was submitted during development or verification.

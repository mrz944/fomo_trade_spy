# Verification record

## Initial development verification

Verified locally on 2026-09-26 with Python 3.12.14 on macOS. This section records
the initial development checks; the later deployment checks are recorded below.

- `pytest -q`: **67 passed**, including headless Textual controls, private Unix
  socket access, and complete demo daemon startup/shutdown/restart.
- `ruff check src tests`: passed.
- `python -m compileall -q src`: passed.
- `uv lock --check --offline`: passed; 56 resolved packages, checked-in `uv.lock`.
- `uv build --offline`: wheel and source distribution built successfully after
  fetching the pinned Hatchling 1.32.4 backend.
- Both sample TOML configurations load as paper with seven chains.
- Wheel contains the Alembic migration. Wheel/source archive inspection found no
  local configuration, secrets, databases, bootstrap or virtual environment.
- Offline demo and local doctor commands run successfully.
- Quadlet's configuration sections parse. **No Podman/systemd is installed**, so
  container build/run, generated systemd service and Linux UID/secret-mount behavior
  have not been runtime-verified on this host.

The initial sandbox denied Unix-socket binding. The full suite subsequently ran
with local socket access and passed. This was a verification-environment restriction,
not a skipped TUI test. Test execution uses synthetic data, mocked trading RPC, and
fresh unfunded test keys. No funded transaction was submitted.

## Public network checks

[Saved machine-readable preflight](preflight-2026-09-26.json): FOMO public health
responded; Relay catalog listed seven requested chains; all seven RPC network IDs
matched. Unsigned executable-size $25 buy quotes passed for Solana, Ethereum, Base,
BSC, Monad and Robinhood. Arc has no verified non-settlement probe token in the
catalog, so its quote route is not verified. Initial Solana probes exposed two
implementation defects (truncated genesis hash and a reserved quote-only recipient);
both were corrected and the final read-only preflight passed on Solana.

The reports do not measure actual trader-to-bot stream latency: no authenticated
FOMO key was configured. Real stream delay, actual free-account quotas, trader
history payload normalization/coverage, source wallet mapping and live signing
remain unverified. Live broadcast paths are implemented, but only their offline
validation/recovery behavior was tested. The project intentionally does not convert
this absence of verification into a claim of production readiness.

## Remaining work required for the full requested production system

1. Validate an authenticated FOMO fixture set and implement fee-aware history
   normalization plus complete historical RPC indexing. Currently raw FOMO history
   does not establish eligibility; audited normalized imports can qualify immediately.
2. Expand source decoding beyond a single target token exchanged with configured
   settlement: native-asset routes, multi-token bundles and historical USD gas
   valuations require additional protocol/indexer work.
3. Validate reviewed EVM router and Solana CPMM configurations using read-only
   simulation and paper operation with real trader data; funded execution was not
   authorized or performed for development.
4. Add independently validated general Relay transaction decoding and automatic
   live bridge settlement/recovery. Optional paper bridging is implemented.
5. Establish a permitted, stable direct-FOMO data contract. Public embedded-JSON
   capture works, but it does not currently supply normalized discovery/trade data.
6. Verify Arc token routes. The rootless Linux deployment is now exercised below.

These are real limitations. They are exposed in README, readiness, preflight and
errors; no synthetic fallback is used to pretend real integrations succeeded.

## Rootless deployment verification, 2026-09-26

Published `master` to `mrz944/fomo_trade_spy` and built the published checkout
on `cyberdev@100.111.109.33` using rootless Podman 6.1.2. The new missing-key
daemon test and a focused-input Ctrl-Q detach assertion pass: **68 tests passed**
on macOS and **68 passed** inside the Linux container, with lint passing on both.

The generated Quadlet validates using its full path under
`/run/user/1000/systemd/generator/`. `fomo-spy.service` starts successfully and
the container health check passes. Runtime inspection confirms UID/GID 1000,
read-only root, no effective capabilities, no-new-privileges and no published
ports. Config, environment file, SQLite database and Unix socket are 0600;
the state and socket directories are 0700. The default-target startup link is
present and user lingering is enabled.

Both installed TUI launchers were attached through real terminals. Controls
reach the same paper daemon and Ctrl-Q detaches without stopping it. The daemon
reports real (non-demo) paper state, $1,000 equity, zero candidates/positions/orders,
zero used FOMO credits, and discovery/streaming unavailable because the key is
missing. Public RPC connectivity does not establish trading integration readiness.

The final deployed revision, image ID, restart/persistence checks and comparison
of existing service identities are recorded in the private deployment receipt
`~/.fomo-trade-spy/deployment.json` on Linux. See the
[deployment guide](../deploy/README.md) for launchers, credentials and management.

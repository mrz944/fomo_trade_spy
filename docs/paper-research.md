# Paper research rollout

The previous deployed policy selected nobody because complete historical qualification
was unavailable. An empty selection meant no wallets were monitored for copy entries.
A healthy daemon/socket and successful component tests did not prove working trading.

The separately configured `paper_research` policy allows the current paper experiment
to proceed without certifying historical profitability. `verified` remains the default
and the only permitted live policy. Research selection never changes rankings or
coverage records. Existing financial limits and paper capital remain unchanged.

## Implemented behavior

- Current top-50 FOMO candidates, positive provider-reported PnL, valid wallets and
  per-chain current-data admission; no paid history calls by default.
- At most five selected traders, activity preference, minimum dwell, bounded refresh,
  expiry based on the actual leaderboard receipt time, and durable selection provenance.
- Current finalized Solana inventory for both token programs, including multiple
  accounts per mint. Unknown/mismatched inventory blocks entries and flags affected
  open lots. Per-program snapshots use their own finalized slot.
- Batched EVM wallet log scans, canonical receipts, native/wrapped movements,
  per-asset decoding blockers and durable wallet checkpoints.
- Shared endpoint request budgets, prioritized exits, rate-limit cooldowns and
  wallet-level failure isolation. Archival workers default to disabled in research.
- Initial/reselection baselines cannot submit entries. Restart catch-up is subject
  to the existing historical/freshness rejection. Exits remain watched after pause,
  exclusion, selection expiry or rotation.
- Exact-size executable buy and liquidation quotes before entry; conservative
  paper output, source-proportional exits and policy-tagged persistent accounting.
- TUI research banner and selection table, actual readiness/blockers, source activity,
  and separately reported policy performance. No socket-only operational claim.

Solana inventory uses the finalized context returned by
[getTokenAccountsByOwner](https://solana.com/docs/rpc/http/gettokenaccountsbyowner)
and transaction order from
[getBlock](https://solana.com/docs/rpc/http/getblock).
FOMO's [API reference](https://fomoapi.io/docs) describes its bounded swap history;
those records remain discovery evidence, not proof of complete coverage.

## Validation and production acceptance

The isolated regression scenario uses captured provider/RPC formats with controlled
transaction timings. Production Fomo, admission, Monitor, Relay and Engine components
complete discovery, selection, native purchase, 25% sale, full exit and restart with
reconciled cash/ledger. Separate Solana tests cover both token programs, account-wide
sale sizing, unavailable transactions, unexpected balance changes, restart deduplication
and reselection. Fixture events never enter production storage.

Migration 0004 adds research selections, current inventory checkpoints and policy
provenance. A seeded 0003 financial database verifies all existing money, order and audit
values survive; an isolated copy of the previous production backup also passes a
full-record semantic comparison and SQLite integrity check. The reusable verifier is
`deploy/verify_migration.py`.

Before rollout, build and test the exact published master commit on Linux, take a fresh
SQLite online backup, and run the same migration verifier against a separate copy.
Restart only `fomo-spy`. Preserve credentials and compare identities of other services.
Deployment receipts belong in the private host state directory, with commit/image and
validation evidence. `deploy/observe.py` records 24 hours of service state, selection,
activity, credits, fills/exits, policy accounting and integrity.

Regression success is not production acceptance. Record a fresh canonical source
transaction and correctly accounted paper execution before claiming observed trading.
Record exits separately; no generated transaction may satisfy that acceptance. The
24-hour observation is complete only when its persisted summary says so. Unsupported
chain capabilities or routes remain explicit blockers. Full historical reconstruction
remains a separate requirement for verified selection.

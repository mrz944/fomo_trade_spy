# Evidence import contract

FOMO discovery and parsing of authenticated `/swaps` payloads are automatic.
Rows deduplicate by user ID and swap ID. They are discovery/cross-check evidence;
missing transaction hashes, fees and capped coverage prevent treating them as a
complete ledger. Native chain reconstruction supplies ranking fills separately.

The legacy `history_import` interface remains readable at startup for diagnostics.
Its `complete_30d` flag is **not trusted for eligibility**. Independent chain scans
and inventory reconciliation must establish coverage. Imported records never enter
the signal queue or generate orders. See [current implementation and blockers](history-workflow.md).

```json
{
  "traders": [{
    "userId": "actual-fomo-user-id",
    "handle": "actual-handle",
    "wallets": {"evm": "actual-address", "solana": "actual-address"},
    "complete_30d": false,
    "provenance": "archive RPC export + contemporaneous USD valuations; explain gaps",
    "fills": [{
      "id": "chain:transaction:wallet:token",
      "trader": "actual-fomo-user-id",
      "chain": "base",
      "token": "token-contract-address",
      "side": "buy",
      "quantity": "100.25",
      "usd": "25",
      "fee_usd": "0.04",
      "timestamp": 1789999999,
      "tx": "actual-transaction-hash",
      "provenance": "RPC receipt + price source"
    }]
  }]
}
```

Use decimal strings. `side` is buy/sell/transfer/airdrop, never perpetual. The `usd`
value is net wallet consideration, already including route fees; only separately
measured network fees are added. Never charge route fees twice. Null fee/cost is unknown, not
zero. Include pre-window buys needed to match sells inside the 30-day window. A
completed position is a full inventory cycle from flat to flat; splitting one sell
into multiple fills does not manufacture completed positions. Entire contaminated
token histories are excluded conservatively. Token groups use `(chain, token)`.

Eligibility also requires a positive one-sided 95% Student-t lower bound on the
mean of token-group net returns. This is an approximate sampling bound, not a
prediction or proof of stationarity. Trader selection from a profitable leaderboard
creates selection bias. UI exposes sample size, missing coverage, positive days,
profit concentration and performance without the largest winning token.

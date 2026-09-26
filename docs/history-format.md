# Evidence import contract

FOMO discovery is automatic. Its public documentation does not provide a complete
individual-fill schema with historical fee valuations. Raw `/swaps` pages are cached,
with the coverage gap visible; their aggregate PnL is never treated as matched evidence.
Until an authenticated adapter has been validated, use `history_import` for audited
normalized records. This is an explicit integration limitation, not a seven-day wait.

The file is read at daemon startup. Each trader must use FOMO's stable `userId` and
verified on-chain main wallets, not throwaway app signers. `complete_30d` must reflect
actual coverage, including transfers and opening inventory. A `true` value is an
operator assertion, not something the software independently proves. The bot excludes
unknown-cost or unknown-fee token histories and rejects unmatched sells. It does not
invent historical prices. Imports never enter the signal queue or generate orders.

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
value excludes separately reported network/route fees. Null fee/cost is unknown, not
zero. Include pre-window buys needed to match sells inside the 30-day window. A
completed position is a full inventory cycle from flat to flat; splitting one sell
into multiple fills does not manufacture completed positions. Entire contaminated
token histories are excluded conservatively. Token groups use `(chain, token)`.

Eligibility also requires a positive one-sided 95% Student-t lower bound on the
mean of token-group net returns. This is an approximate sampling bound, not a
prediction or proof of stationarity. Trader selection from a profitable leaderboard
creates selection bias. UI exposes sample size, missing coverage, positive days,
profit concentration and performance without the largest winning token.

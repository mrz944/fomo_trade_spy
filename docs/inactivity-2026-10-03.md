# Why the paper bot did not trade

The service was alive but no candidate had a complete evaluation. All 50 candidates
were blocked by incomplete 30-day chain evidence; therefore selected and watched
counts were zero. The monitor had no wallets to copy. This was not evidence that
50 traders failed the profitability rules.

The Activity table previously contained only engine trade decisions. The social
callback discarded alerts from unselected traders, hiding the functioning feed.

## Changes in this repair

- Persist feed observations independently of executable signals. Activity shows
  incoming buy/sell alerts, historical evidence and per-chain blockers. Replayed
  alerts remain observations and cannot open positions. The feed table is bounded
  to 2,000 rows; the trading audit ledger is retained.
- Read Solana transaction versions through version 1. Normalization uses canonical
  balance deltas and `meta.fee`, including when another wallet paid the fee.
- Pace historical RPC calls (configurable `historical_rpc_interval`, default 0.6s)
  and share rate-limit cooldowns across requests to the same endpoint. Retry-After
  is respected up to one hour. Known endpoint errors produce safe, readable reasons.
- Retry the monitor's initial network check after RPC failure. A failed startup
  check previously terminated that chain's monitoring task until a daemon restart.
- Save each completed Solana signature so failures in a page resume at the failed
  transaction. Completed work survives process restarts.
- Locate the EVM window by bracketing backwards from the recent head. This avoids
  probing ancient midpoint blocks outside a provider's retained history.
- Add migration 0003 for observations. The new online production backup was
  migrated on an isolated copy: all original rows were preserved and SQLite
  integrity passed. See [migration evidence](migration-verification-2026-10-03.json).

Qualification rules, paper limits, credit budget/reserve, live-signing behavior,
credentials, cash, orders, positions and audit records are unchanged.

## Verification and remaining blockers

[Endpoint evidence](history-capabilities-2026-10-03.json) records all seven configured
chains and additional read-only probes. At inspection time:

| Chain | Blocker |
| --- | --- |
| Solana | Rate limits; old reader rejected newer transaction versions. Full closed-token-account enumeration and inventory reconciliation are still unimplemented, so successful pagination alone never establishes coverage. |
| Ethereum | Old window search failed on an ancient block; trace/history support still needs successful checks with the corrected search. |
| Base | Public endpoint rate limits; native discovery also timed out. |
| BSC | Endpoint explicitly requires a personal provider token for archive requests; tracing is unavailable. |
| Monad | Configured endpoint lacks historical state and denies trace methods. Both documented free alternatives returned historical balances but rejected `trace_filter`. |
| Robinhood | Historical state unavailable and trace methods unsupported; tested PublicNode alternative returned HTTP 403. |
| Arc | `trace_filter` unsupported; tested PublicNode alternative returned HTTP 403. |

No chain was disabled and no trader was forced eligible. Additional implementation
and suitable free historical data access are required before full operational
acceptance. Historical replay tests use isolated storage. No actual fresh-source
paper purchase or exit was observed on the deployment during this investigation.

Regression coverage includes persisted mid-page retries, shared HTTP/JSON-RPC rate
limits, safe error messages, recent block search, real captured Solana transfers,
unselected feed observations and the actual IPC/TUI rendering path. The existing
ranking → selection → quote → paper buy → partial/full sale integration remains
part of the suite. The version-1 transaction regression is synthetic; the captured
Solana transfer fixture is a real version-0 RPC response.

## Completed observation of the previous deployment

The [24-hour report](observation-2026-09-28-summary.json) covers 86,402 seconds with
1,387/1,387 successful samples and no service restart. Selections, watched traders,
trade decisions, fills and exits stayed at zero. No wallet reached complete
coverage. Accounting stayed consistent: $1,000 cash, no orders, no reconciliation
flags and no integrity errors. The local credit counter increased by 4,500; the
provider-reported remaining balance decreased by 1,750. These are separate counters
and should not be presented as an identical billing measure.

References: [Solana versioned transactions](https://solana.com/docs/core/transactions/versioned-transactions),
[FOMO feed fields and history limitations](https://fomoapi.io/docs),
[Monad free RPC endpoints](https://docs.monad.xyz/developer-essentials/network-information),
[Monad historical data](https://docs.monad.xyz/developer-essentials/historical-data).

# Paper workflow implementation and acceptance

## What changed

Discovery preserves the leaderboard order and cached candidate data under quota
exhaustion. Authenticated swaps are parsed into durable discovery records with
unknown fees; repeated pages and reported caps stop pagination. The provider's
`complete` flag is never coverage proof. Ranking thresholds are unchanged.

An independent worker probes each configured historical RPC before costly scans.
`historical_rpc` is optional per chain and defaults to `rpc`. Only existing free
endpoints and `https://coins.llama.fi/prices/historical` are used. FOMO credits and
the monitoring reserve are unchanged. No paid API, signing or funded transaction
is required or invoked by this worker.

EVM scans persist completed ranges, retrieve canonical receipts and traces, extend
backwards when opening token inventory is nonzero, and reconcile closing balances.
Token log ranges or native trace ranges that reach the result bound remain blocked.
Reorganized checkpoints remain blocked for audit; old evidence is not discarded.
History never enters the trading engine's signal queue. Both historical and fresh
paths use the same normalization for supported settlement/native exchanges.
Historical prices retain requested/returned timestamps and provenance; stale,
low-confidence or missing values remain unknown. Net wallet consideration already
includes route fees. Separately measured network gas is added once.

Five new tables store normalized fills, scan checkpoints, valuations, coverage gaps,
and provider swaps. Migration `0002` is additive. An online production backup was
migrated on an isolated local copy: integrity passed and every row in all six
original tables was identical. See [migration evidence](migration-verification-2026-09-27.json).

EVM monitoring now has independent trader/wallet checkpoints. New subscriptions
start at the confirmed head without replaying entries. Existing checkpoints recover
gaps; changed source inventory triggers reconciliation. Exit monitoring, pause,
freshness, paper limits and the accounting engine are retained. The legacy import
interface can no longer turn a manually asserted coverage flag into eligibility.

Status/TUI separate liveness, missing evidence, reconstruction, completed evaluation,
selection, and observed paper execution. They include capability failures, progress,
last data receipt, watched counts and the latest trade decision. A connected socket
does not prove the trading workflow.

## Observed endpoint blockers

Read-only probes ran from the deployment host on 2026-09-27 using its configured
seven chains. [Raw summarized results](history-capabilities-2026-09-27.json) preserve
the time and failing methods. HTTP errors in this first report do not establish
whether a failure is permanent; the daemon retries capability probes hourly.

| Chain | Observed missing capability |
| --- | --- |
| Solana | `getTransaction` with supported transaction version 0 returned `-32015` |
| Ethereum | historical balances/logs/native discovery had HTTP failures; `debug_traceTransaction` returned `-32601` |
| Base | `trace_filter` native discovery had an HTTP failure |
| BSC | historical balances/logs/receipt/native discovery had HTTP failures; trace method returned `-32601` |
| Monad | historical balances returned `-32602`; trace/discovery methods returned `-32053` |
| Robinhood | historical balances returned `-32000`; trace/discovery methods returned `-32601` |
| Arc | `trace_filter` returned `-32601`; sampled old block had no transaction to validate tracing |

Historical settlement prices answered for all seven. That does not establish
valuation for every traded token, native fee asset, or timestamp. No chain is
silently disabled to make a candidate eligible.

## Remaining implementation and acceptance work

The full requested result is **not complete**. Solana signature pagination and
normalization are implemented, but complete enumeration of closed token accounts
and historical inventory reconciliation are not. Solana coverage is explicitly
blocked even if a recent signature request succeeds. Standard owner-account RPC
alone does not establish the histories of already closed token accounts.

Native fee/pricing models beyond Ethereum, Base, BSC and Solana remain unknown;
unsupported or ambiguous transaction routes cannot establish profitable evidence.
The EVM reconstruction implementation is verified with fixtures, but the configured
endpoints have not allowed a complete real 30-day scan. Reorganization recovery
requires an audited evidence rebuild rather than silently accepting changed fills.

The integration test uses captured authenticated provider and Base RPC formats,
with an explicitly synthetic profitable scenario in temporary storage. It exercises
production ingestion, RPC normalization, historical valuation, coverage, ranking,
selection, Relay quote validation, paper purchase, 25% exit, full exit and persistent
reconciled accounting. It does not manufacture eligibility in deployed storage.

No qualifying real trader or fresh real-source paper execution has been observed.
Thus deployment and passing tests do not satisfy operational acceptance.

## Deployment observation

`deploy/observe.py` reads only status and SQLite and records service health, progress,
selections, decisions, fills/exits, credits and accounting separately. Run on the host:

```sh
python3 deploy/observe.py --output ~/.fomo-trade-spy/observation-2026-09-27
```

The default duration is 86,400 seconds; the same output directory resumes the same
deadline. `summary.json` is created only after the elapsed observation duration.
Starting this collector is not a completed 24-hour observation. The deployment
receipt records the published revision/image and the observation location.

Provider sources: [FOMO history limitations](https://fomoapi.io/docs),
[DefiLlama historical-price API](https://raw.githubusercontent.com/DefiLlama/api-docs/main/llms.txt),
[Geth callTracer](https://geth.ethereum.org/docs/developers/evm-tracing/built-in-tracers),
[Solana getTransaction](https://solana.com/docs/rpc/http/gettransaction).

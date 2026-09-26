# Integration research — 2026-09-26

Sources inspected:

- [FOMO API documentation](https://fomoapi.io/docs) and [OpenAPI](https://fomoapi.io/openapi.json).
- [FOMO public health](https://api.fomoapi.io/health).
- [Relay quote/v2 contract](https://docs.relay.link/references/api/get-quote-v2).
- [Relay live chain catalog](https://api.relay.link/chains).
- [Robinhood network documentation](https://docs.robinhood.com/chain/add-network-to-wallet/).
- [Raydium CPMM swap implementation](https://github.com/raydium-io/raydium-cp-swap/blob/master/programs/cp-swap/src/instructions/swap_base_input.rs),
  [pool layout](https://github.com/raydium-io/raydium-cp-swap/blob/master/programs/cp-swap/src/states/pool.rs),
  [program ID](https://github.com/raydium-io/raydium-cp-swap/blob/master/programs/cp-swap/src/lib.rs).
- [FOMO public website](https://fomo.family).

## FOMO provider

Independent/unofficial. The documented free plan has 250,000 monthly credits and a
15-second app-stream delay after seven days. A data key is required. Leaderboards
supply stable user IDs; historical position summaries cannot prove matched returns.
Individual fills use `/swaps`. History coverage and fees remain unresolved without
an authenticated sample and RPC backfill. App alerts cover large trades only and
can omit transaction identity/size. They serve as social context, not executable
orders. The paid on-chain stream is documented for Solana and Robinhood only;
its retraction semantics need additional validation before use. No production
access or completeness is claimed. Public health responded, with eight loaded
traders at the observed time; this does not establish leaderboard depth.

One full app stream is used, filtered locally by selected IDs plus open-position
owners. WS authentication follows the documented query parameter; URLs and raw
connection errors are never logged. Backfill `/alerts?since=` is bounded and marked
partial. REST discovery/history are cached. Budget reservations survive restarts,
include reconnects, preserve a monitoring reserve, and stop charged retries on 402.
Provider credit headers can tighten the remaining allowance. Paid tiers can be
configured by increasing the explicit budget; no subscription is required by code.

## Chain mapping

| Chain | FOMO / RPC ID | Relay ID | Default settlement |
|---|---:|---:|---|
| Solana | 1399811149 (FOMO) | 792703809 | USDC |
| Ethereum | 1 | 1 | USDC |
| Base | 8453 | 8453 | USDC |
| BSC | 56 | 56 | USDC, 18 decimals |
| Monad | 143 | 143 | USDC |
| Robinhood | 4663 | 4663 | USDG |
| Arc | 5042 | 5042 | ERC-20 USDC, 6 decimals |

Relay's public catalog lists all seven with deposits enabled. This is distinct from
quote availability or verified live execution. Arc native gas USDC uses 18 decimals;
its ERC-20 representation uses 6. The code never substitutes one for the other.
Solana's FOMO ID is deliberately different from its Relay ID. Public RPC services
can throttle or lack historical state. Configure free-account endpoints as needed.

See `preflight-2026-09-26.json` for actual read-only checks and quote failures. Unsigned quote responses were observed on Solana and five EVM chains. Arc did
not have a verified non-settlement probe asset in the catalog. An initial Solana
probe used an invalid recipient; after correcting the quote-only address, its
quote passed validation. No funded wallet was used.

## Execution scope

Paper requests exact-size Relay executable quotes, verifies currencies, chains,
recipient, decimals, input, minimum output and gas, then uses the quoted minimum
output plus a further adverse adjustment. Route fees are already reflected in
output; origin gas is separately debited. Values depend on provider quotes and
settlement-asset USD valuation, with a 1% input peg guard. Missing quote = no fill.

Live EVM code signs locally built `swapExactTokensForTokens` and exact-amount
approvals on a configured v2 router whose deployed bytecode hash is pinned. Only
direct settlement/token routes work. No arbitrary Relay calldata is trusted. A
proxy's bytecode hash does not pin its implementation: configure nonproxy reviewed
routers. A failed allowance reset, missing direct pair, opaque token behavior,
unsupported router, or insufficient prefunding blocks the order. Exact allowance
is granted only when current allowance is zero; nonzero insufficient allowance
requires an explicit external reset. A completed approval can remain after an
entry expires; its spender and exact amount are recorded in the order history.

Live Solana code builds Raydium CPMM `swap_base_input` from verified pool state,
using only original SPL Token, the dedicated wallet's associated accounts, bounded
compute fees, and a pinned deployed program-data hash. Token-2022, lookup tables,
unknown pools and target freeze authority are rejected. Simulations must succeed;
minimum output is enforced by the on-chain instruction. Only explicitly configured
settlement/token pools work. This path has offline safety tests but has not been
validated with a funded wallet. General Relay signing is not implemented.

Both live paths persist signed bytes/hash before broadcast. Ambiguous sends remain
uncertain until canonical finalized receipts resolve them. No fresh nonce or new
transaction is sent to replace an uncertain transaction. Receipts supply inventory
and actual fee deltas. Deep reorganizations halt the chain and mark inventory for
reconciliation; the bot never pretends an earlier external copy can be undone by
rolling back the source event in SQLite.

Optional automatic bridging is implemented only for paper with current Relay
cross-chain quotes. Live bridging remains blocked because safe bridge calldata,
recipient, output guarantees and destination settlement recovery are not yet
validated. Same-chain live swaps use prefunded per-chain wallets and gas.

## Direct FOMO option

The inspected public homepage was a marketing/download site, not a documented trade
API. Opt-in `direct_fomo_urls` fetches only public FOMO HTTPS pages, honors robots,
and extracts embedded JSON at the discovery cache interval. Captures are retained
for adapter work. This does not currently normalize traders or trades. No private
endpoint, authentication bypass or dependable scraping contract is invented.

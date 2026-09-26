"""Deterministic synthetic data. Never used as a fallback for real providers."""

from .domain import D, Fill, Quote, now
from .ranking import rank


class DemoQuotes:
    def __init__(self, cfg):
        self.cfg = cfg
        self.prices = {}

    async def quote(self, chain, token, side, amount, decimals):
        c = self.cfg.chain(chain)
        price = self.prices.get(token, D("2"))
        if side == "buy":
            usd = D(amount) / 10**c.decimals
            output = int(usd / price * 10**decimals)
        else:
            usd = D(amount) / 10**decimals * price
            output = int(usd * 10**c.decimals)
        return Quote(
            chain=chain,
            token=token,
            side=side,
            input_amount=amount,
            output_amount=output,
            minimum_output=output * 9900 // 10000,
            token_decimals=decimals,
            usd=usd,
            fee_usd=D(".05"),
            expires=now() + 10,
            provider="SYNTHETIC DEMO",
            raw={},
        )


def history(trader="demo-1", multiplier=D("1"), at=None):
    at = now() if at is None else at
    fills = []
    for cycle in range(25):
        token = "demo-token-" + str(cycle % 5)
        ts = at - (28 - cycle) * 86400
        entry = D("100")
        exit_value = entry + D(10 + cycle % 5) * multiplier
        for side, stamp, usd in [("buy", ts, entry), ("sell", ts + 3600, exit_value)]:
            fills.append(
                Fill(
                    id=f"{trader}-{cycle}-{side}",
                    trader=trader,
                    chain="base",
                    token=token,
                    side=side,
                    quantity=D(50),
                    usd=usd,
                    fee_usd=D(".2"),
                    timestamp=stamp,
                    tx=f"synthetic-{cycle}-{side}",
                    provenance="SYNTHETIC DEMO",
                )
            )
    return fills


def seed(store):
    for i in range(1, 8):
        trader = f"demo-{i}"
        score = rank(history(trader, D(i) / 5), complete_30d=True)
        score["provenance"] = "SYNTHETIC DEMO"
        store.put("ranking:" + trader, score)
        store.put("trader:" + trader, {"userId": trader, "handle": f"FomoDemo{i}", "wallets": {}})

#!/usr/bin/env python3
"""Entry for make.fun bounty u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1.

A trend-ranked hourly trading bot speaking agent-fun/bars-replay/v1.
The hello message names the universe, caps and fees; each batch carries
new bars and the account; the bot answers every batch with orders.

Strategy: per symbol it tracks closes and an EMA pair (8/21). Entries
need the fast EMA above the slow, price above the slow EMA, positive
12-bar momentum, and the SOL market proxy in an uptrend; candidates
rank by momentum per unit volatility and at most four positions open
at once, each sized at equity over the slots and skipped when the
modelled price impact is too large. Exits sell the whole position on
a trend flip, a 10% trailing stop from the peak since entry, a 12%
hard stop, or 50% profit. No clock, no randomness, no network:
identical bars give identical orders.
"""

import json
import sys

FAST = 8
SLOW = 21
MOM_BARS = 12
VOL_BARS = 12
TRAIL = 0.90
STOP = 0.88
TAKE = 1.50
MAX_IMPACT_BPS = 60
SLIPPAGE_BPS = 100
COOLDOWN = 3
TOP_K = 4
BPS = 10000


class Series:
    def __init__(self):
        self.closes = []
        self.fast = None
        self.slow = None
        self.fast_warm = []
        self.slow_warm = []
        self.peak = None
        self.cool = 0

    def push(self, close):
        self.closes.append(close)
        if len(self.closes) > 64:
            self.closes.pop(0)
        self.fast = self._step(self.fast, FAST, close, self.fast_warm)
        self.slow = self._step(self.slow, SLOW, close, self.slow_warm)
        if self.cool > 0:
            self.cool -= 1

    @staticmethod
    def _step(value, span, close, warm):
        if value is not None:
            return value + (2.0 / (span + 1)) * (close - value)
        warm.append(close)
        if len(warm) == span:
            return sum(warm) / span
        return None

    def score(self):
        if self.fast is None or self.slow is None or self.fast <= self.slow:
            return None
        if self.closes[-1] <= self.slow or len(self.closes) <= MOM_BARS:
            return None
        mom = self.closes[-1] / self.closes[-1 - MOM_BARS] - 1.0
        if mom <= 0:
            return None
        rets = [self.closes[i] / self.closes[i - 1] - 1.0 for i in range(len(self.closes) - VOL_BARS, len(self.closes))]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) * (r - mean) for r in rets) / len(rets)
        return mom / (var ** 0.5 + 0.002)


class Market:
    """The bot's view of the replay: account, positions, latest bars."""

    def __init__(self, hello):
        self.hello = hello
        self.symbols = {s["symbol"]: s for s in hello["symbols"]}
        self.caps = hello["caps"]
        self.min_notional = int(hello["fees"]["min_notional_quote_units"])
        self.quote_scale = hello["capital"]["quote_scale"]
        self.warmup_bars = hello.get("warmup_bars", 0)
        self.seq = -1
        self.cash = int(hello["capital"]["quote_units"])
        self.equity = self.cash
        self.positions = {}
        self.latest = {}
        self.series = {}
        self.next_id = 1

    @property
    def warming_up(self):
        return self.warmup_bars > 0 and self.seq < self.warmup_bars

    def apply(self, batch):
        self.seq = batch["seq"]
        fresh = set()
        for bar in batch["bars"]:
            self.latest[bar["symbol"]] = bar
            fresh.add(bar["symbol"])
            s = self.series.get(bar["symbol"])
            if s is None:
                s = Series()
                self.series[bar["symbol"]] = s
            s.push(float(int(bar["c"])))
        account = batch["account"]
        self.cash = int(account["cash_quote_units"])
        self.equity = int(account["equity_quote_units"])
        self.positions = {p["symbol"]: (int(p["qty"]), int(p["cost_quote_units"])) for p in account["positions"]}
        return fresh

    def value(self, symbol, qty):
        bar = self.latest.get(symbol)
        spec = self.symbols.get(symbol)
        if bar is None or spec is None:
            return 0
        return qty * int(bar["c"]) * 10 ** self.quote_scale // 10 ** (spec["qty_scale"] + spec["price_scale"])

    def weight_room(self, symbol):
        held = self.positions.get(symbol)
        room = self.caps["max_weight_bps"] * self.equity // BPS - self.value(symbol, held[0] if held else 0)
        return room if room > 0 else 0

    def impact_bps(self, symbol, amount):
        bar = self.latest.get(symbol)
        if bar is None:
            return None
        depth = [int(d) for d in bar["c_depth"]]
        prod = 1
        for d in depth:
            prod *= d
        total = 0
        for i in range(len(depth)):
            other = 1
            for j, d in enumerate(depth):
                if j != i:
                    other *= d
            total += other
        return amount * 1_000_000 * BPS * total // (10 ** self.quote_scale * prod)

    def order_id(self):
        cid = "o%d" % self.next_id
        self.next_id += 1
        return cid


def decide(mkt, fresh):
    orders = []
    for symbol in sorted(mkt.positions):
        if symbol not in fresh:
            continue
        s = mkt.series[symbol]
        qty, cost = mkt.positions[symbol]
        close = s.closes[-1]
        if s.peak is None or close > s.peak:
            s.peak = close
        value = mkt.value(symbol, qty)
        trend_down = s.fast is not None and s.slow is not None and s.fast < s.slow
        if trend_down or close < s.peak * TRAIL or value <= cost * STOP or value >= cost * TAKE:
            orders.append({"side": "sell", "client_id": mkt.order_id(), "symbol": symbol,
                           "qty": str(qty), "max_slippage_bps": SLIPPAGE_BPS})
            s.peak = None
            s.cool = COOLDOWN
    orders = orders[: mkt.caps["max_orders_per_bar"]]
    sol = mkt.series.get("SOL")
    if sol is None or sol.fast is None or sol.slow is None or sol.fast <= sol.slow:
        return orders
    slots = min(len(mkt.symbols), mkt.caps["max_open_positions"])
    sells = sum(1 for o in orders if o["side"] == "sell")
    room = min(slots, TOP_K) - len(mkt.positions) + sells
    if room <= 0:
        return orders
    stake = mkt.equity // slots
    cash = mkt.cash
    ranked = []
    for symbol in fresh:
        if symbol in mkt.positions:
            continue
        s = mkt.series[symbol]
        if s.cool > 0:
            continue
        sc = s.score()
        if sc is not None:
            ranked.append((sc, symbol))
    ranked.sort(reverse=True)
    for _sc, symbol in ranked:
        if room <= 0 or len(orders) >= mkt.caps["max_orders_per_bar"]:
            break
        spend = min(stake, mkt.weight_room(symbol), cash)
        if spend < mkt.min_notional or spend <= 0:
            continue
        impact = mkt.impact_bps(symbol, spend)
        if impact is not None and impact > MAX_IMPACT_BPS:
            continue
        orders.append({"side": "buy", "client_id": mkt.order_id(), "symbol": symbol,
                       "notional_quote_units": str(spend), "max_slippage_bps": SLIPPAGE_BPS})
        cash -= spend
        room -= 1
        mkt.series[symbol].peak = mkt.series[symbol].closes[-1]
    return orders


def main():
    mkt = None
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            mkt = Market(msg)
        elif kind == "batch":
            if mkt is None:
                return
            fresh = mkt.apply(msg)
            orders = [] if mkt.warming_up else decide(mkt, fresh)
            reply = json.dumps({"type": "reply", "seq": msg["seq"], "orders": orders},
                               separators=(",", ":"), ensure_ascii=False)
            sys.stdout.write(reply + "\n")
            sys.stdout.flush()
        elif kind == "done":
            return


if __name__ == "__main__":
    main()

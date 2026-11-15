#!/usr/bin/env python3
"""Race a BTC trading bot live for 24 hours (make.fun btc-race-24h-20261115-0828).

Speaks agent-fun/bars-replay/v1: one JSON line per minute (hello, then one
batch per bar, then done) on stdin, exactly one reply per batch on stdout.

Strategy (one asset, CBBTC, long only, $1,000 of paper money):
  1. V-bounce: after a deep drawdown (the close at least 2.5% below the
     240-bar high), buy with the full cash balance once the bounce is
     already underway (5-bar return >= 0.5% and 20-bar return >= 0.8%).
     Exit on a 0.8% trailing stop from the highest close since entry.
  2. Donchian breakout: while flat and no V-bounce fires, buy 25% of
     equity on a close above the 60-bar high. Exit below the 20-bar low
     or on a 1.0% trailing stop.
  A 20-bar cooldown after every exit keeps whipsaws from re-entering at
  once. Entries are rare and selective because a full-size round trip
  pays roughly 40 bps in fees, slippage and price impact on this pool.

Everything is integer arithmetic on the bars' own integer prices, so two
executions on the same data produce identical orders. No clock, no
network, no randomness, no files: the only inputs are the hello and batch
lines, and the only output is the reply to each batch.
"""

import json
import sys

SYMBOL = "CBBTC"

# --- strategy parameters (thresholds as integer per-mille ratios) ---
DD_WINDOW = 240        # bars of history for the drawdown reference high
DD_NUM = 975           # enter at or below 97.5% of the 240-bar high
RET5_NUM = 1005        # 5-bar return at least +0.5%
RET20_NUM = 1008       # 20-bar return at least +0.8%
TRAIL_V_NUM = 992      # V-bounce trailing stop: exit at/below 99.2% of peak
HI_N = 60              # Donchian breakout lookback, in bars
LO_N = 20              # Donchian exit lookback, in bars
TRAIL_D_NUM = 990      # Donchian trailing stop: exit at/below 99.0% of peak
DONCHIAN_BPS = 2500    # Donchian entries risk 25% of equity
COOLDOWN_BARS = 20     # bars to wait after an exit before a new entry
MAX_SLIPPAGE_BPS = 300  # refuse a fill more than 3% past the latest close


def main():
    closes = []  # CBBTC closes, integer price units, oldest first
    warmup_bars = 0
    min_notional = 0
    max_weight_bps = 10000
    cash = 0
    equity = 0
    qty = 0
    mode = None  # "v" or "d" while a position is held
    pending_mode = None  # mode of the buy sent on the previous batch
    peak = None  # highest close seen since the position opened
    cooldown_until = 0  # bar count before which no new entry is taken
    order_no = 0

    inp = sys.stdin.buffer
    out = sys.stdout
    while True:
        raw = inp.readline()
        if not raw:
            return
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            warmup_bars = msg.get("warmup_bars", 0)
            min_notional = int(msg["fees"]["min_notional_quote_units"])
            max_weight_bps = msg["caps"]["max_weight_bps"]
            cash = int(msg["capital"]["quote_units"])
            equity = cash
            continue
        if kind == "done":
            return
        if kind != "batch":
            # The harness never sends anything else; stay silent and keep
            # reading rather than break the protocol with a bad reply.
            continue
        seq = msg["seq"]
        for bar in msg["bars"]:
            if bar["symbol"] == SYMBOL:
                closes.append(int(bar["c"]))
        account = msg["account"]
        cash = int(account["cash_quote_units"])
        equity = int(account["equity_quote_units"])
        qty = 0
        for pos in account["positions"]:
            if pos["symbol"] == SYMBOL:
                qty = int(pos["qty"])
        orders = []
        warming_up = seq < warmup_bars
        n = len(closes)
        if qty == 0:
            mode = None
            peak = None
            if not warming_up and n >= DD_WINDOW + 1 and n >= cooldown_until:
                close = closes[-1]
                high = max(closes[-DD_WINDOW - 1:-1])
                vbounce = (
                    close * 1000 <= high * DD_NUM
                    and close * 1000 >= closes[-6] * RET5_NUM
                    and close * 1000 >= closes[-21] * RET20_NUM
                )
                if vbounce:
                    amount = cash
                    room = max_weight_bps * equity // 10000
                    if amount > room:
                        amount = room
                    if amount >= min_notional and amount > 0:
                        order_no += 1
                        orders.append(
                            {
                                "side": "buy",
                                "client_id": "o%d" % order_no,
                                "symbol": SYMBOL,
                                "notional_quote_units": str(amount),
                                "max_slippage_bps": MAX_SLIPPAGE_BPS,
                            }
                        )
                        pending_mode = "v"
                elif close > max(closes[-HI_N - 1:-1]):
                    amount = equity * DONCHIAN_BPS // 10000
                    if amount > cash:
                        amount = cash
                    room = max_weight_bps * equity // 10000
                    if amount > room:
                        amount = room
                    if amount >= min_notional and amount > 0:
                        order_no += 1
                        orders.append(
                            {
                                "side": "buy",
                                "client_id": "o%d" % order_no,
                                "symbol": SYMBOL,
                                "notional_quote_units": str(amount),
                                "max_slippage_bps": MAX_SLIPPAGE_BPS,
                            }
                        )
                        pending_mode = "d"
                if not orders:
                    pending_mode = None
            else:
                pending_mode = None
        else:
            if mode is None:
                mode = pending_mode or "d"
                peak = closes[-1] if closes else 0
            if closes and not warming_up:
                close = closes[-1]
                if close > peak:
                    peak = close
                exit_now = False
                if mode == "v":
                    exit_now = close * 1000 <= peak * TRAIL_V_NUM
                else:
                    if n >= LO_N + 1:
                        low = min(closes[-LO_N - 1:-1])
                        exit_now = close < low
                    if not exit_now:
                        exit_now = close * 1000 <= peak * TRAIL_D_NUM
                if exit_now:
                    order_no += 1
                    orders.append(
                        {
                            "side": "sell",
                            "client_id": "o%d" % order_no,
                            "symbol": SYMBOL,
                            "qty": str(qty),
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        }
                    )
                    cooldown_until = n + COOLDOWN_BARS
                    mode = None
                    peak = None
                    pending_mode = None
        out.write(
            json.dumps(
                {"type": "reply", "seq": seq, "orders": orders},
                separators=(",", ":"),
            )
            + "\n"
        )
        out.flush()


if __name__ == "__main__":
    main()

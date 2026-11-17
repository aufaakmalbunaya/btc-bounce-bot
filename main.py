"""Bounce-hunter bot for make.fun btc-race-24h-20261117-0603.

Long-only cbBTC/USDC on 1-minute bars, starting from 1,000 USDC paper money.
The idea: this market spends most of its time chopping inside a range, and the
rare sharp sell-offs that stretch price far below its 20-minute mean tend to
snap back. So the bot waits for a genuine extreme, then buys the turn, and
gets out quickly with a fixed target, a tight stop and a trailing stop.

Entry (flat only, after 60 bars of history): the close is at or below the
lower Bollinger band (20-minute mean minus 2 population standard deviations)
AND the 7-minute Wilder RSI is at or below 30 AND the RSI ticked up from the
previous bar. One position at a time, full cash.

Exits (checked on every close while holding, against the fill price):
  - take profit at +2.00%
  - stop loss at -1.00%
  - trailing stop 0.60% below the highest close since entry, once +0.50% up
  - max hold 90 bars

Fills land at the next bar's open, so the entry price is read back from the
fill the next batch reports. Everything is pure float math on closes: no
clock, no randomness, no environment, no network, so two runs agree exactly.
Standard library only.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
RSI_PERIOD = 7
RSI_BUY_MAX = 30.0
BB_PERIOD = 20
BB_MULT = 2.0
MIN_BARS = 60
TARGET_BPS = 200
STOP_BPS = 100
TRAIL_BPS = 60
TRAIL_ARM_BPS = 50
MAX_HOLD_BARS = 90
MAX_SLIPPAGE_BPS = 200

# History of closes in USD, one per bar, in order.
closes = []
# Fill price of the open position in USD (None while flat), and the bar it
# filled on. Peak is the highest close seen since the fill.
entry_price = None
entry_bar = None
peak = None
# Client id of a buy order whose fill has not been reported yet.
pending_buy_id = None


def rsi_last(values, period):
    """Wilder RSI of the last value of `values`, or None when too short.

    Seeded with the simple mean of the first `period` gains and losses, then
    Wilder-smoothed, exactly the textbook definition.
    """
    n = len(values)
    if n <= period:
        return None
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = values[i] - values[i - 1]
        if change > 0:
            gains[i] = change
        else:
            losses[i] = -change
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def lower_band(values, period, mult):
    """SMA minus mult * population standard deviation, or None when too short."""
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((v - mean) ** 2 for v in window) / period
    return mean - mult * variance ** 0.5


def price_scale(state):
    return state.symbols[SYMBOL]["price_scale"]


def on_batch(state, batch):
    global entry_price, entry_bar, peak, pending_buy_id
    # A fill for our buy reports the price the market actually gave us.
    for fill in batch.get("fills", []):
        if fill.get("client_id") == pending_buy_id:
            pending_buy_id = None
            if fill.get("status") == "filled":
                entry_price = int(fill["price"]) / (10 ** price_scale(state))
                # fill_bar is the bar's close time in ms, not its index: the
                # fill lands on this batch's bar, so the index is this seq.
                entry_bar = state.seq
                peak = entry_price
    bar = state.latest.get(SYMBOL)
    if bar is None:
        return []
    close = int(bar["c"]) / (10 ** price_scale(state))
    closes.append(close)
    held = state.positions.get(SYMBOL)
    if held is not None:
        if entry_price is None:
            # Fallback if the fill was never seen: back the price out of the
            # position's cost (cost is qty * fill price, fees included).
            qty = held.qty
            entry_price = held.cost * 100.0 / qty if qty else close
            entry_bar = state.seq
            peak = entry_price
        if close > peak:
            peak = close
        gain = (close - entry_price) / entry_price
        held_bars = state.seq - entry_bar
        trail_line = peak * (1 - TRAIL_BPS / 10000.0)
        should_exit = (
            gain >= TARGET_BPS / 10000.0
            or gain <= -STOP_BPS / 10000.0
            or (gain >= TRAIL_ARM_BPS / 10000.0 and close <= trail_line)
            or held_bars >= MAX_HOLD_BARS
        )
        if should_exit:
            return [{
                "side": "sell",
                "client_id": state.client_id(),
                "symbol": SYMBOL,
                "qty": held.qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]
        return []
    # Flat: look for an oversold bounce at the lower band.
    entry_price = None
    entry_bar = None
    peak = None
    if len(closes) <= MIN_BARS:
        return []
    rsi_now = rsi_last(closes, RSI_PERIOD)
    rsi_prev = rsi_last(closes[:-1], RSI_PERIOD)
    if rsi_now is None or rsi_prev is None:
        return []
    band = lower_band(closes, BB_PERIOD, BB_MULT)
    if band is None:
        return []
    if close <= band and rsi_now <= RSI_BUY_MAX and rsi_now > rsi_prev:
        client_id = state.client_id()
        pending_buy_id = client_id
        return [{
            "side": "buy",
            "client_id": client_id,
            "symbol": SYMBOL,
            "notional_quote_units": state.cash,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]
    return []


makefun_bars.run(on_batch)

"""Strategy functions for the Intraday Scanner. Pure: no I/O, no clock, no globals.

A candle is a dict {"t": start time (aware datetime), "o", "h", "l", "c", "v"}. Sides are "long" and "short".
"""
import datetime as dt
import math


def hhmm(s):
    h, m = (int(x) for x in s.split(":")[:2])
    return dt.time(h, m)


# ---------- indicators ----------
def ema(values, n):
    """Exponential moving average; the first n-1 values are None and the n-th is the simple average (the usual seed)."""
    out, k, e = [], 2 / (n + 1), None
    for i, v in enumerate(values):
        if i < n - 1:
            out.append(None)
            continue
        e = sum(values[:n]) / n if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


# ---------- candles ----------
def is_red(c):
    return c["c"] < c["o"]


def is_green(c):
    return c["c"] > c["o"]


def is_inside(c, prev):
    """Fully inside the prior candle's range. Equal highs or lows still count as inside."""
    return c["h"] <= prev["h"] and c["l"] >= prev["l"]


def is_setup_candle(c, prev, side):
    """Long: a red inside candle with lower volume than the prior candle. Short: a green one. Equal volume is not lower.
    A zero or missing volume (Yahoo sometimes reports 0) cannot confirm "lower volume", so it is not a setup."""
    if not c.get("v") or not prev.get("v"):
        return False
    colour = is_red(c) if side == "long" else is_green(c)
    return colour and is_inside(c, prev) and c["v"] < prev["v"]


def breakout_side(c, pdh, pdl):
    """'long' when the candle closes above the previous day's high, 'short' below its low, else None.
    A close exactly at PDH/PDL is not beyond it."""
    if pdh is not None and c["c"] > pdh:
        return "long"
    if pdl is not None and c["c"] < pdl:
        return "short"
    return None


def breakout_holds(c, side, pdh, pdl):
    """False once a candle closes back on the wrong side (below PDH for longs, above PDL for shorts)."""
    return c["c"] >= pdh if side == "long" else c["c"] <= pdl


def is_setup_time(t, cfg):
    """Setups ignore the first `skip_first_candles` candles (09:15-09:30 by default)."""
    first = dt.datetime.combine(t.date(), hhmm(cfg["market_open"]), tzinfo=t.tzinfo) + \
        dt.timedelta(minutes=cfg["candle_minutes"] * cfg["skip_first_candles"])
    return t >= first


# ---------- levels ----------
def entry_stop(c, side, tick=0.05, buffer_ticks=0):
    """(entry, stop) for a setup candle. Long: entry above its high, stop below its low. Short: the mirror.
    The trigger fires when price goes strictly beyond the entry level."""
    b = buffer_ticks * tick
    return (round(c["h"] + b, 2), round(c["l"] - b, 2)) if side == "long" else (round(c["l"] - b, 2), round(c["h"] + b, 2))


def crossed_entry(price, side, entry):
    return price > entry if side == "long" else price < entry


def hit_stop(price, side, stop):
    return price < stop if side == "long" else price > stop


def targets(entry, stop, side, rs=(2, 3)):
    """Reference targets at the given multiples of the risk (1:2, 1:3)."""
    risk = abs(entry - stop)
    sign = 1 if side == "long" else -1
    return [{"r": r, "price": round(entry + sign * r * risk, 2)} for r in rs]


def r_multiple(entry, stop, exit_price, side):
    risk = abs(entry - stop)
    if not risk:
        return None
    return round(((exit_price - entry) if side == "long" else (entry - exit_price)) / risk, 2)


def distance(ltp, level, tick=0.05):
    """From the live price to a level: % of price, rupees and ticks (signed: positive = level is above the price)."""
    if level is None or not ltp:
        return None
    d = level - ltp
    return {"level": level, "pct": round(d / ltp * 100, 3), "rs": round(d, 2), "ticks": int(round(abs(d) / tick))}


def is_near(ltp, level, pct, ticks, tick=0.05):
    """Within `pct` % or `ticks` ticks of a level (whichever is wider)."""
    if level is None or not ltp:
        return False
    d = abs(level - ltp)
    return d / ltp * 100 <= pct or d <= ticks * tick + 1e-9


# ---------- risk ----------
def position_size(capital, daily_risk_pct, trades_per_day, entry, stop, lot_size=1):
    """quantity = risk per trade / (entry - stop), rounded down to whole lots.
    Returns per-trade risk budget, quantity, lots and the rupee risk of that quantity."""
    budget = capital * daily_risk_pct / 100 / max(1, trades_per_day)
    per_share = abs(entry - stop)
    lot = max(1, int(lot_size or 1))
    if per_share <= 0:
        return {"budget": round(budget, 2), "per_share": 0, "qty": 0, "lots": 0, "risk": 0, "note": "entry equals stop"}
    lots = math.floor(budget / per_share / lot)
    qty = lots * lot
    note = "" if lots else f"one lot ({lot}) risks ₹{per_share * lot:,.0f}, more than the ₹{budget:,.0f} budget per trade"
    return {"budget": round(budget, 2), "per_share": round(per_share, 2), "qty": qty, "lots": lots, "lot_size": lot,
            "risk": round(qty * per_share, 2), "note": note}


def daily_loss_limit(capital, daily_risk_pct):
    return round(capital * daily_risk_pct / 100, 2)


# ---------- market ----------
def market_bias(advances, declines, cfg):
    """'long' / 'short' / 'neutral' from pre-open advances vs declines, or None when not entered."""
    if advances is None or declines is None:
        return None
    if declines == 0:
        return "long" if advances > 0 else "neutral"
    ratio = advances / declines
    b = cfg["bias"]
    if ratio >= b["long_if_adv_dec_ratio_at_least"]:
        return "long"
    if ratio <= b["short_if_adv_dec_ratio_at_most"]:
        return "short"
    return "neutral"


def market_status(now, cfg, trading_day=True):
    """'pre-open' (09:00-09:15), 'open' (09:15-15:30) or 'closed' (other times, weekends, non-trading days)."""
    if now.weekday() >= 5 or not trading_day:
        return "closed"
    t = now.time()
    if hhmm(cfg["pre_open"]) <= t < hhmm(cfg["market_open"]):
        return "pre-open"
    if hhmm(cfg["market_open"]) <= t < hhmm(cfg["market_close"]):
        return "open"
    return "closed"


def watchlist_check(daily, cfg):
    """Day-before watchlist rules on daily candles (oldest first, the last one = the day before the session):
    range-bound = yesterday's range <= ratio x the average range of the lookback days before it;
    not extended = yesterday's close within N % of its simple average.
    OI change is not part of this (Yahoo has no OI). Returns None when there is not enough history."""
    w = cfg["watchlist"]
    n, m = w["range_lookback_days"], w["sma_days"]
    if len(daily) < max(n, m) + 1:
        return None
    y = daily[-1]
    prior = daily[-1 - n:-1]
    avg_range = sum(d["h"] - d["l"] for d in prior) / n
    sma = sum(d["c"] for d in daily[-m:]) / m
    ratio = (y["h"] - y["l"]) / avg_range if avg_range else None
    ext = (y["c"] / sma - 1) * 100
    range_bound = ratio is not None and ratio <= w["range_bound_max_ratio"]
    not_extended = abs(ext) <= w["not_extended_max_pct_from_sma"]
    return {"range_ratio": round(ratio, 2) if ratio is not None else None, "pct_from_sma": round(ext, 2),
            "range_bound": range_bound, "not_extended": not_extended, "candidate": range_bound and not_extended}

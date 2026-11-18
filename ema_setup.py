"""20 EMA buy and sell setups on 5-minute candles. Pure functions, no I/O. The sell setup is the mirror image (sell_setups).

The setup, in order:
  1. Signal candle: the 20 EMA value of that candle lies inside its body (between open and close).
  2. Bearish candle: one of the next two candles closes below its open (the first one that does). Its body must stay
     above the EMA (its lower wick may touch or cross it); a body touching the EMA means the setup failed.
  3. Breakout: a later candle goes above the bearish candle's high, and no candle after the bearish candle
     (the breakout candle included) touches or goes below the 20 EMA before that happens.
     A touch first means the setup failed.

Options: bear_hold   the bearish candle's wick must also stay above the EMA (the whole candle, not just its body)
         close_break the breakout needs a close above the bearish high (default: the high crossing it is enough)
         green_signal the signal candle must be green
"""


def ema(values, n=20):
    """Exponential moving average. The first n-1 values are None, the n-th is the simple average (the usual seed)."""
    out, k, e = [], 2 / (n + 1), None
    for i, v in enumerate(values):
        if i < n - 1:
            out.append(None)
            continue
        e = sum(values[:n]) / n if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def buy_setups(rows, e, day, n_closed, bear_hold=False, close_break=False, green_signal=False):
    """Setups whose signal candle is on `day`.

    rows      5-minute candles {ts, o, h, l, c}, oldest first, several days (earlier days only warm up the EMA)
    e         EMA value per row (None while warming up)
    n_closed  rows[:n_closed] are finished candles; later rows (the forming candle) can only fail or trigger a setup
    Returns [{signal, bear, at, status, live}] with row indexes; status is triggered | pending | failed,
    `at` is the breakout or failing candle, `live` is True when that candle has not closed yet.
    """
    idx = [i for i, r in enumerate(rows) if r["ts"].date() == day]
    if not idx:
        return []
    lo, hi = idx[0], idx[-1] + 1
    closed = min(hi, n_closed)
    out, used = [], set()
    for i in range(lo, closed):
        r, ei = rows[i], e[i]
        if ei is None or not (min(r["o"], r["c"]) <= ei <= max(r["o"], r["c"])):
            continue
        if green_signal and r["c"] <= r["o"]:
            continue
        b = next((k for k in (i + 1, i + 2) if k < closed and rows[k]["c"] < rows[k]["o"]), None)
        if b is None or b in used:          # one setup per bearish candle (the earliest signal candle keeps it)
            continue
        used.add(b)
        s = {"signal": i, "bear": b, "at": None, "status": "pending", "live": False}
        body_low = min(rows[b]["o"], rows[b]["c"])
        if body_low <= e[b] or (bear_hold and rows[b]["l"] <= e[b]):     # its body must stay clear of the EMA (wick may touch)
            s.update(status="failed", at=b)
        else:
            level = rows[b]["h"]
            for j in range(b + 1, hi):
                rj = rows[j]
                if e[j] is not None and rj["l"] <= e[j]:        # a touch counts first: the rule says "without touching"
                    s.update(status="failed", at=j, live=j >= n_closed)
                    break
                if (rj["c"] if close_break else rj["h"]) > level:
                    s.update(status="triggered", at=j, live=j >= n_closed)
                    break
        out.append(s)
    return out


def _flip(r):
    """Mirror a candle through zero: a bullish candle becomes bearish, its low becomes the high."""
    return dict(r, o=-r["o"], h=-r["l"], l=-r["h"], c=-r["c"])


def sell_setups(rows, e, day, n_closed, bull_hold=False, close_break=False, red_signal=False):
    """The buy setup mirrored for shorts:
      1. the 20 EMA lies inside a candle's body,
      2. one of the next two candles is bullish (the first one that is) and its body stays below the EMA
         (its upper wick may touch or cross it),
      3. a later candle goes below that bullish candle's low, and no candle after the bullish candle (the breakdown
         candle included) touches or goes above the 20 EMA before that happens.
    Options mirror buy_setups: bull_hold (the bullish candle stays below the EMA), close_break (needs a close below
    its low), red_signal (the EMA-cross candle must be red). Same result shape; `bear` is the bullish candle's index.
    Mirroring every price through zero turns this into the buy setup exactly, so both sides share one implementation."""
    return buy_setups([_flip(r) for r in rows], [None if v is None else -v for v in e], day, n_closed, bull_hold, close_break, red_signal)

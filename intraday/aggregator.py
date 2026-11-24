"""Ticks or 1-minute bars -> 5-minute candles on IST market hours (09:15-15:30). Pure: the caller supplies every time.

Candles start at 09:15, 09:20, ... 15:25. Anything outside market hours is ignored, and so is anything older than the
candle being built (a late or out-of-order tick never rewrites a finished candle). Nothing is invented: a 5-minute
slot with no ticks simply has no candle.
"""
import datetime as dt

from .engine import hhmm


class CandleAggregator:
    def __init__(self, minutes=5, open_t="09:15", close_t="15:30"):
        self.step = dt.timedelta(minutes=minutes)
        self.open_t, self.close_t = hhmm(open_t), hhmm(close_t)
        self.rows = []              # candles, oldest first; the last may still be forming
        self.dropped = 0            # ticks ignored (outside hours, out of order, bad price)

    def bucket(self, ts):
        """Start of the candle that contains ts, or None outside market hours."""
        if not (self.open_t <= ts.time() < self.close_t):
            return None
        day0 = dt.datetime.combine(ts.date(), self.open_t, tzinfo=ts.tzinfo)
        return day0 + ((ts - day0) // self.step) * self.step

    def add_tick(self, ts, price, qty=0):
        """One trade or quote. qty = volume traded in this tick (0 when unknown)."""
        return self.add_bar({"t": ts, "o": price, "h": price, "l": price, "c": price, "v": qty})

    def add_bar(self, bar):
        """A smaller bar (e.g. 1-minute) or a tick as a bar. Returns True when it was used."""
        if bar["c"] is None or bar["c"] <= 0:
            self.dropped += 1
            return False
        b = self.bucket(bar["t"])
        if b is None or (self.rows and b < self.rows[-1]["t"]):
            self.dropped += 1
            return False
        if self.rows and self.rows[-1]["t"] == b:
            r = self.rows[-1]
            r["h"], r["l"], r["c"], r["v"] = max(r["h"], bar["h"]), min(r["l"], bar["l"]), bar["c"], r["v"] + (bar["v"] or 0)
        else:
            self.rows.append({"t": b, "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"], "v": bar["v"] or 0})
        return True

    def candles(self):
        return [dict(r) for r in self.rows]

    def closed(self, now):
        """Candles whose 5 minutes have fully passed by `now`."""
        return [dict(r) for r in self.rows if r["t"] + self.step <= now]

    def forming(self, now):
        r = self.rows[-1] if self.rows else None
        return dict(r) if r and r["t"] + self.step > now else None


def aggregate(bars, minutes=5, open_t="09:15", close_t="15:30"):
    """Convenience: a list of smaller bars (any days, oldest first) -> 5-minute candles."""
    a = CandleAggregator(minutes, open_t, close_t)
    for b in sorted(bars, key=lambda x: x["t"]):
        a.add_bar(b)
    return a.candles()

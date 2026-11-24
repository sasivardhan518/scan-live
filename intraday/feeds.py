"""Data-provider adapters for the Intraday Scanner.

Every feed implements the same small interface:
  now()            current market time (IST, aware)
  refresh()        pull new data; raises on failure (the service keeps the last good data and backs off)
  symbols()        the symbols this feed covers
  data(sym)        {"candles": 5-minute candles, several days, oldest first (the last may be forming),
                    "ltp": last price or None, "daily": daily candles before the session (for PDH/PDL)}
  closed_upto(sym) index: candles[:n] are finished
  info()           {"name", "kind": live|replay, "note", "data_time"}

Feeds never invent prices. Replay feeds reveal real (or clearly labelled mock) candles as a clock moves; inside a
candle the replay price walks open -> nearer extreme -> other extreme -> close, which is labelled as approximate.
"""
import csv
import datetime as dt
import json
import math
import os
import random
import time
import urllib.request

from . import HERE
from .engine import hhmm

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
FIVE = dt.timedelta(minutes=5)


# ------------------------------------------------------------------ universe and lot sizes
def load_lot_sizes(cfg, refresh=False):
    """{symbol: lot size} for NSE stock F&O, from NSE's lot-size file (cached in intraday/lot_sizes.json).
    Returns (lots, note)."""
    path = os.path.join(HERE, "lot_sizes.json")
    cached = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        if not refresh and cached.get("date") == dt.date.today().isoformat():
            return cached["lots"], "F&O lot sizes from NSE (today's cached copy)"
    try:
        req = urllib.request.Request(cfg["feed"]["lot_sizes_url"], headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"})
        text = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
        lots, stocks = {}, False
        for row in csv.reader(text.splitlines()):
            row = [x.strip() for x in row]
            if len(row) < 3:
                continue
            if row[0].lower().startswith("derivatives on individual securities"):
                stocks = True
                continue
            if stocks and row[1] and row[2].isdigit():
                lots[row[1]] = int(row[2])                    # the near-month lot size
        if len(lots) < 50:
            raise ValueError(f"only {len(lots)} stocks found")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"date": dt.date.today().isoformat(), "lots": lots}, f, indent=1)
        return lots, f"F&O lot sizes from NSE ({len(lots)} stocks)"
    except Exception as e:
        if cached.get("lots"):
            return cached["lots"], f"NSE lot-size file unreachable ({e}); using the copy from {cached.get('date')}"
        return {}, f"NSE lot-size file unreachable ({e}); no lot sizes, sizing in shares"


def candle_rows(df, symbols, suffix=".NS"):
    """yfinance multi-ticker DataFrame (group_by='ticker') -> {symbol: [candles]} in IST. Rows without a close are skipped."""
    out = {}
    for sym in symbols:
        try:
            sub = df[sym + suffix].dropna(subset=["Close"])
        except KeyError:
            continue
        rows = []
        for ts, r in sub.iterrows():
            t = ts.to_pydatetime()
            t = t.replace(tzinfo=IST) if t.tzinfo is None else t.astimezone(IST)
            v = r.get("Volume", 0)
            rows.append({"t": t, "o": round(float(r["Open"]), 2), "h": round(float(r["High"]), 2), "l": round(float(r["Low"]), 2),
                         "c": round(float(r["Close"]), 2), "v": int(v) if v == v and v is not None else 0})
        if rows:
            out[sym] = rows
    return out


# ------------------------------------------------------------------ live: Yahoo polling
class YahooFeed:
    """Live (polled) Yahoo data: 5-minute candles for five sessions (the EMA warms up on earlier days) and daily candles
    (PDH/PDL, watchlist), once per poll. Yahoo's NSE data can lag; the board shows the data time."""
    kind = "live"

    def __init__(self, symbols, cfg):
        import yfinance as yf
        self.yf, self.cfg, self.syms = yf, cfg, list(symbols)
        self.c5, self.daily, self.daily_day = {}, {}, None
        self.data_time = None

    def now(self):
        return dt.datetime.now(IST)

    def symbols(self):
        return self.syms

    def _dl(self, **kw):
        return self.yf.download([s + ".NS" for s in self.syms], group_by="ticker", threads=True, progress=False, auto_adjust=False, **kw)

    def refresh(self):
        c5 = candle_rows(self._dl(period="5d", interval="5m"), self.syms)
        if not c5:
            raise RuntimeError("Yahoo returned no 5-minute candles")
        if self.daily_day != self.now().date():
            d = candle_rows(self._dl(period="3mo", interval="1d"), self.syms)
            if d:
                self.daily = {s: [dict(x, t=x["t"].replace(hour=0, minute=0)) for x in rows] for s, rows in d.items()}
                self.daily_day = self.now().date()
        self.c5, self.data_time = c5, self.now()

    def data(self, sym):
        rows = self.c5.get(sym) or []
        sess = rows[-1]["t"].date() if rows else None
        return {"candles": rows, "ltp": rows[-1]["c"] if rows else None,
                "daily": [d for d in self.daily.get(sym, []) if sess and d["t"].date() < sess]}

    def closed_upto(self, sym):
        rows, now = self.c5.get(sym) or [], self.now()
        grace = dt.timedelta(seconds=self.cfg["feed"]["closed_candle_grace_seconds"])
        n = len(rows)
        if rows and rows[-1]["t"] + FIVE + grace > now:       # Yahoo keeps updating the newest candle until well after it ends
            n -= 1
        return n

    def info(self):
        return {"name": "Yahoo", "kind": "live", "note": "Polled from Yahoo every %ss; Yahoo's NSE data can lag your broker." %
                self.cfg["feed"]["poll_seconds"], "data_time": self.data_time.isoformat() if self.data_time else None}


# ------------------------------------------------------------------ replay
class ReplayFeed:
    """Replays a day: full candle lists are revealed as a clock runs from `start` at `speed` x real time.
    source = {symbol: candles over several days}; daily = {symbol: daily candles} (optional; otherwise PDH/PDL come
    from the previous session's 5-minute candles)."""
    kind = "replay"

    def __init__(self, source, day, cfg, speed=30.0, start="09:15", daily=None, label="Replay"):
        self.src, self.day, self.cfg, self.speed, self.label = source, day, cfg, float(speed), label
        self.daily_src = daily or {}
        self.t0 = dt.datetime.combine(day, hhmm(start), tzinfo=IST)
        self.wall0 = time.time()
        self.end = dt.datetime.combine(day, hhmm(cfg["market_close"]), tzinfo=IST)

    def now(self):
        return min(self.t0 + dt.timedelta(seconds=(time.time() - self.wall0) * self.speed), self.end + dt.timedelta(minutes=1))

    def refresh(self):
        pass

    def symbols(self):
        return sorted(self.src)

    def _visible(self, sym, now):
        rows = self.src.get(sym) or []
        done = [r for r in rows if r["t"] + FIVE <= now]
        cur = next((r for r in rows if r["t"] <= now < r["t"] + FIVE), None)
        if cur is None:
            return done, None
        frac = (now - cur["t"]) / FIVE                         # 0..1 through the candle: reveal a partial candle
        up = cur["c"] >= cur["o"]
        path = [cur["o"], cur["l"] if up else cur["h"], cur["h"] if up else cur["l"], cur["c"]]
        seg = min(2, int(frac * 3)); f = frac * 3 - seg
        px = round(path[seg] + (path[seg + 1] - path[seg]) * f, 2)
        seen = path[:seg + 1] + [px]
        part = {"t": cur["t"], "o": cur["o"], "h": max(seen), "l": min(seen), "c": px, "v": int(cur["v"] * frac)}
        return done, part

    def data(self, sym):
        now = self.now()
        done, part = self._visible(sym, now)
        rows = done + ([part] if part else [])
        daily = [d for d in self.daily_src.get(sym, []) if d["t"].date() < self.day]
        if not daily:                                          # previous session from 5-minute candles
            prev = [r for r in rows if r["t"].date() < self.day]
            if prev:
                pday = prev[-1]["t"].date()
                p = [r for r in prev if r["t"].date() == pday]
                daily = [{"t": dt.datetime.combine(pday, dt.time(0), tzinfo=IST), "o": p[0]["o"], "h": max(r["h"] for r in p),
                          "l": min(r["l"] for r in p), "c": p[-1]["c"], "v": sum(r["v"] for r in p)}]
        return {"candles": rows, "ltp": rows[-1]["c"] if rows else None, "daily": daily}

    def closed_upto(self, sym):
        done, _ = self._visible(sym, self.now())
        return len(done)

    def info(self):
        return {"name": self.label, "kind": "replay", "data_time": self.now().isoformat(),
                "note": f"{self.label} of {self.day.isoformat()} at {self.speed:g}x. Inside each candle the price path is approximated from its open, high, low and close."}


def yahoo_replay(symbols, day, cfg, speed=30.0, start="09:15"):
    """Replay a past session with real Yahoo 5-minute candles (Yahoo keeps about 60 days)."""
    import yfinance as yf
    tick = [s + ".NS" for s in symbols]
    kw = dict(group_by="ticker", threads=True, progress=False, auto_adjust=False)
    c5 = candle_rows(yf.download(tick, start=(day - dt.timedelta(days=8)).isoformat(), end=(day + dt.timedelta(days=1)).isoformat(), interval="5m", **kw), symbols)
    c5 = {s: [r for r in rows if r["t"].date() <= day] for s, rows in c5.items()}
    if not any(r["t"].date() == day for rows in c5.values() for r in rows):
        raise LookupError(f"Yahoo has no 5-minute candles for {day.isoformat()} (holiday, weekend or older than about 60 days)")
    d = candle_rows(yf.download(tick, start=(day - dt.timedelta(days=120)).isoformat(), end=day.isoformat(), interval="1d", **kw), symbols)
    return ReplayFeed(c5, day, cfg, speed, start, daily=d, label="Yahoo replay")


def csv_replay(path, cfg, speed=30.0, start="09:15", day=None):
    """Replay recorded candles: CSV with columns symbol,time,open,high,low,close,volume (time ISO, IST). The last
    date in the file is replayed; earlier dates warm up the EMA and give PDH/PDL."""
    src = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = dt.datetime.fromisoformat(r["time"])
            t = t.replace(tzinfo=IST) if t.tzinfo is None else t.astimezone(IST)
            src.setdefault(r["symbol"], []).append({"t": t, "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]),
                                                    "c": float(r["close"]), "v": int(float(r["volume"] or 0))})
    for rows in src.values():
        rows.sort(key=lambda x: x["t"])
    day = day or max(rows[-1]["t"].date() for rows in src.values())
    return ReplayFeed(src, day, cfg, speed, start, label="CSV replay")


def mock_replay(cfg, speed=30.0, start="09:15", day=None, seed=7):
    """A scripted MOCK market (clearly labelled) that walks through every state: long and short breakouts, inside
    candles, entries, EMA exits, stops and failed setups. For demos and tests after market hours."""
    day = day or dt.date.today()
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    prev_day = day - dt.timedelta(days=1)
    while prev_day.weekday() >= 5:
        prev_day -= dt.timedelta(days=1)
    rnd = random.Random(seed)
    scripts = ["long_win", "short_win", "long_stop", "short_fail", "long_late", "watch", "long_win", "short_win", "watch", "long_fail"]
    src = {}
    for k, kind in enumerate(scripts):
        sym = f"MOCK{k + 1:02d}"
        base = 200 + 150 * k
        src[sym] = _mock_day(prev_day, base, None, rnd) + _mock_day(day, base, kind, rnd, offset=k % 4)
    return ReplayFeed(src, day, cfg, speed, start, label="MOCK replay")


def _mock_day(day, p, kind, rnd, offset=0):
    """75 candles. Previous day (kind None): a range between p*0.98 and p*1.00, so PDL ~ 0.98p and PDH ~ 1.00p."""
    rows, t = [], dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)
    def bar(o, c, hi_pad=0.0006, lo_pad=0.0006, v=None):
        h, l = max(o, c) * (1 + hi_pad), min(o, c) * (1 - lo_pad)
        rows.append({"t": t + FIVE * len(rows), "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2),
                     "v": v or int(2000 + rnd.random() * 1500)})
        return round(c, 2)
    if kind is None:
        c = p * 0.99
        for i in range(75):
            c = bar(c, p * (0.99 + 0.0095 * math.sin(i / 6 + p)))
        rows[20]["h"], rows[50]["l"] = round(p * 1.0, 2), round(p * 0.98, 2)
        return rows
    sign = -1 if kind.startswith("short") else 1
    edge = p * (1.0 if sign > 0 else 0.98)
    c = p * 0.99
    quiet = 6 + 3 * offset + (12 if kind == "long_late" else 0)
    for _ in range(quiet):                                  # inside yesterday's range
        c = bar(c, p * 0.99 + rnd.uniform(-0.004, 0.004) * p)
    if kind == "watch":
        while len(rows) < 75:
            c = bar(c, p * 0.99 + rnd.uniform(-0.004, 0.004) * p)
        return rows
    c = bar(c, edge * (1 + sign * 0.004), v=6000)           # breakout close beyond PDH / PDL
    big = rows[-1]
    o2 = big["c"] - sign * p * 0.0004                       # inside candle, against the move, lower volume
    c2 = o2 - sign * p * 0.0015
    rows.append({"t": t + FIVE * len(rows), "o": round(o2, 2), "h": round(max(o2, c2) + p * 0.0002, 2),
                 "l": round(min(o2, c2) - p * 0.0002, 2), "c": round(c2, 2), "v": 2500})
    ins = rows[-1]
    c = ins["c"]
    tight = {"lo_pad": 0.00005} if sign > 0 else {"hi_pad": 0.00005}    # the trigger candle must not touch the stop first
    if kind in ("long_stop",):                              # triggers, then falls through the stop
        c = bar(c, ins["h"] + p * 0.001, **tight)
        for _ in range(4):
            c = bar(c, c - sign * p * 0.003)
    elif kind in ("short_fail", "long_fail"):               # breaks the inside candle the wrong way: invalidated
        c = bar(c, ins["l"] - p * 0.002 if sign > 0 else ins["h"] + p * 0.002)
    else:                                                    # trigger, trend, then close back through the 10 EMA
        for i in range(14):
            c = bar(c, c + sign * p * 0.0022, **(tight if i == 0 else {}))
        for i in range(4):
            c = bar(c, c - sign * p * 0.004)
    while len(rows) < 75:
        c = bar(c, c + rnd.uniform(-0.0015, 0.0015) * p)
    return rows[:75]


def record_yahoo_day(symbols, day, path):
    """Save Yahoo 5-minute candles of `day` (and the week before it) to a CSV for csv_replay."""
    import yfinance as yf
    c5 = candle_rows(yf.download([s + ".NS" for s in symbols], start=(day - dt.timedelta(days=8)).isoformat(),
                                 end=(day + dt.timedelta(days=1)).isoformat(), interval="5m", group_by="ticker",
                                 threads=True, progress=False, auto_adjust=False), symbols)
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "time", "open", "high", "low", "close", "volume"])
        for s, rows in sorted(c5.items()):
            for r in rows:
                if r["t"].date() <= day:
                    w.writerow([s, r["t"].isoformat(), r["o"], r["h"], r["l"], r["c"], r["v"]]); n += 1
    return n

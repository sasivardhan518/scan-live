#!/usr/bin/env python3
"""
9:20 Scanner live data server.

Serves the scanner app and the data it needs:
  GET /                      the app (index.html), already pointed at this server
  GET /snapshot              current quotes for the whole universe
  GET /snapshot?at=09:20     quotes as of that time today (used to lock the 9:20 shortlist)
  GET /candles?symbol=X&tf=5m|1d
  GET /health                provider status, symbols loaded, last error

Providers:  mock (synthetic, for testing)  |  yahoo (free, no account)  |  kite (Zerodha Kite Connect)
"""
import argparse, datetime as dt, json, math, os, random, sys, threading, time, zlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
OPEN = dt.time(9, 15)
CLOSE = dt.time(15, 30)
ONE_MIN = dt.timedelta(minutes=1)


def load_universe():
    with open(os.path.join(HERE, "universe.json")) as f:
        return json.load(f)


# Official Nifty 200 constituents. NSE rebalances the index in March and September.
NIFTY200_URLS = ["https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
                 "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv"]
# NSE industry -> heatmap sector, for stocks that are new to universe.json
INDUSTRY_SECTOR = {"Financial Services": "Fin Services", "Capital Goods": "Capital Goods", "Healthcare": "Pharma & Health",
                   "Automobile and Auto Components": "Auto", "Fast Moving Consumer Goods": "FMCG", "Metals & Mining": "Metals",
                   "Consumer Services": "Consumer", "Consumer Durables": "Consumer", "Textiles": "Consumer",
                   "Information Technology": "IT", "Power": "Power", "Oil Gas & Consumable Fuels": "Oil, Gas, Coal",
                   "Realty": "Realty", "Chemicals": "Chemicals", "Services": "Infra & Logistics",
                   "Construction": "Infra & Logistics", "Telecommunication": "Telecom", "Construction Materials": "Cement"}


def fetch_nifty200():
    """Download today's Nifty 200 list from NSE -> [{symbol, industry, name}], or raise."""
    import csv, io, urllib.request
    err = None
    for url in NIFTY200_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"})
            text = urllib.request.urlopen(req, timeout=20).read().decode("utf-8-sig")
            rows = [r for r in csv.DictReader(io.StringIO(text)) if r.get("Symbol")]
            if len(rows) >= 150:
                return [{"symbol": r["Symbol"].strip(), "industry": r.get("Industry", "").strip(), "name": r.get("Company Name", "").strip()} for r in rows]
            err = f"{url}: only {len(rows)} rows"
        except Exception as e:
            err = f"{url}: {e!r}"
    raise RuntimeError(err)


def fetch_csv_symbols(urls):
    """First URL that returns an NSE constituent CSV -> [symbols], else None."""
    import csv, io, urllib.request
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"})
            text = urllib.request.urlopen(req, timeout=20).read().decode("utf-8-sig")
            syms = [r["Symbol"].strip() for r in csv.DictReader(io.StringIO(text)) if r.get("Symbol")]
            if syms:
                return syms
        except Exception:
            pass
    return None


def load_sectors(uni, offline=False):
    """Heatmap sectors from sectors.json. Official NSE indices take their stock lists from NSE's constituent files
    (cached in sector_members.json for offline starts); custom sectors are built from the Nifty 200 by industry or symbol list."""
    cfg = json.load(open(os.path.join(HERE, "sectors.json"), encoding="utf-8"))
    cache_path = os.path.join(HERE, "sector_members.json")
    try:
        cache = json.load(open(cache_path))
    except Exception:
        cache = {}
    meta = {x["symbol"]: x for x in uni}
    out, failed = [], []
    for d in cfg["sectors"]:
        if d.get("csv"):
            members = None if offline else fetch_csv_symbols([h + d["csv"] for h in cfg["csv_hosts"]])
            if members:
                cache[d["name"]] = members
            else:
                members = cache.get(d["name"], [])
                failed.append(d["name"])
        elif d.get("industry"):
            members = [x for x in meta if meta[x].get("industry") in d["industry"]]
        else:
            members = d.get("symbols", [])
        out.append({"name": d["name"], "yahoo": d.get("yahoo"), "constituents": len(members), "members": [m for m in members if m in meta]})
    with open(cache_path, "w") as f:
        json.dump(cache, f, indent=1)
    return out, failed


def sync_universe(uni):
    """Make the universe match NSE's live Nifty 200 list. Keeps your sector edits; new stocks get NSE's industry and market cap.
    Saves universe.json so the next start works offline. Returns (universe, message)."""
    try:
        live = fetch_nifty200()
    except Exception as e:
        return uni, f"could not fetch the live Nifty 200 list ({e}); using universe.json"
    old = {x["symbol"]: x for x in uni}
    added = [r for r in live if r["symbol"] not in old]
    removed = sorted(set(old) - {r["symbol"] for r in live})
    if not added and not removed and all(x.get("industry") for x in uni):
        return uni, "Nifty 200 list is current (200 stocks)"
    caps = {}
    if added:
        try:
            import yfinance as yf
            for r in added:
                try:
                    fi = yf.Ticker(r["symbol"] + ".NS").fast_info
                    caps[r["symbol"]] = (fi["market_cap"], fi["last_price"])
                except Exception:
                    pass
        except ImportError:
            pass
    out = []
    for r in live:
        s = r["symbol"]
        if s in old:
            out.append(dict(old[s], industry=r["industry"]))
            continue
        mcap, px = caps.get(s, (None, None))
        is_bank = r["industry"] == "Financial Services" and " bank" in (" " + r["name"].lower())
        out.append({"symbol": s, "sector": "Banks" if is_bank else INDUSTRY_SECTOR.get(r["industry"], r["industry"] or "Other"),
                    "weight": max(1, round(mcap / 1e10)) if mcap else 10, "base": round(px) if px else 500, "name": r["name"], "industry": r["industry"]})
    with open(os.path.join(HERE, "universe.json"), "w") as f:
        json.dump(out, f, indent=1)
    return out, f"Nifty 200 list updated from NSE: added {[r['symbol'] for r in added]}, removed {removed}"


def r2(x):
    return round(float(x), 2)


# --------------------------------------------------------------------------
# Providers. Each returns candles as dicts: {ts: aware datetime, o, h, l, c, v}
#   daily(symbols)               -> {symbol: [daily candles, oldest first]}  (ts = that day at 00:00 IST)
#   intraday(symbols, upto)      -> {symbol: [1-minute candles of the latest session, oldest first]}
#   now()                        -> current IST datetime
# --------------------------------------------------------------------------
class MockProvider:
    """Synthetic market that behaves like a live one. Starts at --mock-time and moves with the clock."""
    name = "mock"

    def __init__(self, universe, start="09:19:30", speed=1.0):
        self.u = universe
        h, m, *s = [int(x) for x in start.split(":")]
        today = dt.datetime.now(IST).replace(hour=h, minute=m, second=(s[0] if s else 0), microsecond=0)
        self.start, self.t0, self.speed = today, time.time(), speed
        self.seed = int(today.strftime("%Y%m%d"))
        r = random.Random(self.seed)
        self.sector_r = {u["sector"]: r.gauss(0, 1.2) for u in universe}

    def now(self):
        return self.start + dt.timedelta(seconds=(time.time() - self.t0) * self.speed)

    def _rng(self, sym, salt=""):
        return random.Random(zlib.crc32((sym + salt + str(self.seed)).encode()))

    def daily(self, symbols):
        out = {}
        today = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        for u in self.u:
            sym = u["symbol"]
            r = self._rng(sym, "d")
            prev_close = u["base"] * (1 + r.gauss(0, 0.008))
            c, rows = 100.0, []
            for i in range(40):
                o = c * (1 + r.gauss(0, 0.003)); cl = o * (1 + r.gauss(0, 0.014))
                bh, bl = max(o, cl), min(o, cl)
                pick, up_m, dn_m = r.random(), 1.0, 1.0
                if i == 39:
                    if pick < 0.25: dn_m, up_m = 4.5, 0.5
                    elif pick < 0.5: up_m, dn_m = 4.5, 0.5
                else:
                    if pick < 0.15: dn_m = 3.5
                    elif pick < 0.3: up_m = 3.5
                rows.append([o, bh * (1 + abs(r.gauss(0, 1)) * 0.005 * up_m), bl * (1 - abs(r.gauss(0, 1)) * 0.005 * dn_m), cl])
                c = cl
            k = prev_close / rows[-1][3]
            d, cur = [], today
            days = []
            while len(days) < 40:
                cur -= dt.timedelta(days=1)
                if cur.weekday() < 5:
                    days.append(cur)
            days.reverse()
            for day, (o, h, l, cl) in zip(days, rows):
                d.append({"ts": day, "o": r2(o * k), "h": r2(h * k), "l": r2(l * k), "c": r2(cl * k), "v": 0})
            out[sym] = d
        return out

    def intraday(self, symbols, upto=None):
        now = self.now()
        day0 = now.replace(hour=9, minute=15, second=0, microsecond=0)
        n_done = max(0, int((now - day0).total_seconds() // 60))   # fully formed minutes
        n_done = min(n_done, 375)
        daily = self._daily_cache if hasattr(self, "_daily_cache") else None
        if daily is None:
            self._daily_cache = daily = self.daily(symbols)
        out = {}
        for u in self.u:
            sym = u["symbol"]
            r = self._rng(sym, "i")
            prev_close = daily[sym][-1]["c"]
            R = self.sector_r[u["sector"]] * 0.9 + r.gauss(0, 0.8) * (3.2 if r.random() < 0.07 else 1)
            gap = (0.55 * R + r.gauss(0, 0.25)) / 100
            px = prev_close * (1 + gap)
            T = math.log(1 + R / 100) - math.log(px / prev_close)
            w = [math.exp(-i / 25) + 0.2 for i in range(375)]; sw = sum(w)
            rows = []
            for i in range(n_done):
                sigma = 0.00032 * (1 + 2.2 * math.exp(-i / 20))
                ret = T * w[i] / sw + r.gauss(0, sigma)
                o, c = px, px * math.exp(ret)
                h = max(o, c) * (1 + abs(r.gauss(0, 1)) * 0.00018); l = min(o, c) * (1 - abs(r.gauss(0, 1)) * 0.00018)
                rows.append({"ts": day0 + i * ONE_MIN, "o": r2(o), "h": r2(h), "l": r2(l), "c": r2(c), "v": int(1000 + r.random() * 9000)})
                px = c
            out[sym] = rows
        return out


def parse_yf_frame(df, symbols, suffix=".NS"):
    """Turn a yfinance multi-ticker DataFrame (group_by='ticker') into {symbol: [candles]}."""
    out = {}
    for sym in symbols:
        try:
            sub = df[sym + suffix].dropna(subset=["Close"])
        except KeyError:
            continue
        rows = []
        for ts, row in sub.iterrows():
            t = ts.to_pydatetime()
            t = t.replace(tzinfo=IST) if t.tzinfo is None else t.astimezone(IST)
            rows.append({"ts": t, "o": r2(row["Open"]), "h": r2(row["High"]), "l": r2(row["Low"]), "c": r2(row["Close"]), "v": int(row.get("Volume", 0) or 0)})
        if rows:
            out[sym] = rows
    return out


class YahooProvider:
    """Free, no account. Yahoo's NSE data can be delayed, so check it against your broker before trusting a 9:20 scan."""
    name = "yahoo"

    def __init__(self, universe):
        import yfinance as yf
        self.yf = yf

    def now(self):
        return dt.datetime.now(IST)

    def _download(self, symbols, **kw):
        tickers = [s + ".NS" for s in symbols]
        return self.yf.download(tickers, group_by="ticker", threads=True, progress=False, auto_adjust=False, **kw)

    def daily(self, symbols):
        return parse_yf_frame(self._download(symbols, period="6mo", interval="1d"), symbols)

    def intraday(self, symbols, upto=None):
        raw = parse_yf_frame(self._download(symbols, period="5d", interval="1m"), symbols)
        if not raw:
            return {}
        latest = max(rows[-1]["ts"].date() for rows in raw.values())
        return {s: [c for c in rows if c["ts"].date() == latest] for s, rows in raw.items()}


class KiteProvider:
    """Zerodha Kite Connect. Needs KITE_API_KEY and a fresh KITE_ACCESS_TOKEN (the token expires every day)."""
    name = "kite"

    def __init__(self, universe):
        from kiteconnect import KiteConnect
        key, tok = os.environ.get("KITE_API_KEY"), os.environ.get("KITE_ACCESS_TOKEN")
        if not key or not tok:
            raise SystemExit("Set KITE_API_KEY and KITE_ACCESS_TOKEN before using --provider kite")
        self.k = KiteConnect(api_key=key)
        self.k.set_access_token(tok)
        self.tok = {i["tradingsymbol"]: i["instrument_token"] for i in self.k.instruments("NSE")}
        self._last = 0.0

    def now(self):
        return dt.datetime.now(IST)

    def _hist(self, sym, frm, to, interval):
        wait = 0.36 - (time.time() - self._last)          # Kite allows about 3 historical calls per second
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        rows = self.k.historical_data(self.tok[sym], frm, to, interval)
        out = []
        for r in rows:
            t = r["date"]
            t = t.replace(tzinfo=IST) if t.tzinfo is None else t.astimezone(IST)
            out.append({"ts": t, "o": r2(r["open"]), "h": r2(r["high"]), "l": r2(r["low"]), "c": r2(r["close"]), "v": int(r.get("volume", 0))})
        return out

    def daily(self, symbols):
        to = self.now().replace(tzinfo=None); frm = to - dt.timedelta(days=90)
        return {s: self._hist(s, frm, to, "day") for s in symbols if s in self.tok}

    def intraday(self, symbols, upto=None):
        now = self.now()
        frm = now.replace(hour=9, minute=15, second=0, microsecond=0, tzinfo=None)
        to = (upto or now).replace(tzinfo=None)
        return {s: self._hist(s, frm, to, "minute") for s in symbols if s in self.tok}

    def quotes(self, symbols):
        out = {}
        for i in range(0, len(symbols), 400):
            chunk = symbols[i:i + 400]
            q = self.k.quote(["NSE:" + s for s in chunk])
            for s in chunk:
                d = q.get("NSE:" + s)
                if d:
                    o = d["ohlc"]
                    out[s] = {"ltp": r2(d["last_price"]), "open": r2(o["open"]), "high": r2(o["high"]), "low": r2(o["low"]), "prevClose": r2(o["close"])}
        return out


# --------------------------------------------------------------------------
# Store: caches provider data and builds the API payloads
# --------------------------------------------------------------------------
class Store:
    def __init__(self, prov, universe, refresh, sectors=None):
        self.prov, self.u, self.refresh_s = prov, universe, refresh
        self.sectors = sectors or []
        self.sec_of = {}
        for sec in self.sectors:
            for m in sec["members"]:
                self.sec_of.setdefault(m, []).append(sec["name"])
        self.index_tickers = [sec["yahoo"] for sec in self.sectors if sec.get("yahoo")] if prov.name != "mock" else []
        self.idx, self.idx_err = {}, None
        self.syms = [x["symbol"] for x in universe]
        self.meta = {x["symbol"]: x for x in universe}
        self.daily, self.intra, self.at_cache = {}, {}, {}
        self.daily_at = self.intra_at = 0.0
        self.intra_now = None
        self.err, self.lock = None, threading.Lock()

    def refresh(self, force=False):
        try:
            if force or not self.daily or time.time() - self.daily_at > 6 * 3600:
                d = self.prov.daily(self.syms)
                with self.lock:
                    self.daily, self.daily_at = d, time.time()
            if not hasattr(self.prov, "quotes"):          # brokers with a quote API do not need minute candles for the live snapshot
                stamp = self.prov.now()
                i = self.prov.intraday(self.syms)
                with self.lock:
                    self.intra, self.intra_at, self.intra_now = i, time.time(), stamp
            self.err = None
        except Exception as e:                            # keep serving the last good data
            self.err = repr(e)
        self.refresh_indices()

    def refresh_indices(self):
        """1-minute candles for the official sector indices (Yahoo), used for the sector % on the heatmap."""
        if not self.index_tickers:
            return
        try:
            import yfinance as yf
            df = yf.download(self.index_tickers, period="5d", interval="1m", group_by="ticker", threads=True, progress=False, auto_adjust=False)
            got = parse_yf_frame(df, self.index_tickers, suffix="")
            if got:
                with self.lock:
                    self.idx.update(got)
            self.idx_err = None if got else "no index data"
            if time.time() - getattr(self, "idx_daily_at", 0) > 600:     # official closes, for the previous-close base
                dd = parse_yf_frame(yf.download(self.index_tickers, period="1mo", interval="1d", group_by="ticker", threads=True,
                                                progress=False, auto_adjust=False), self.index_tickers, suffix="")
                if dd:
                    self.idx_daily, self.idx_daily_at = {t: {c["ts"].date(): c["c"] for c in rows} for t, rows in dd.items()}, time.time()
        except Exception as e:
            self.idx_err = repr(e)

    def sectors_payload(self, sess, cutoff=None):
        """[{name, members, constituents, custom, value, prevClose, chg}] - chg is null for custom sectors (the app averages their stocks)."""
        out = []
        for sec in self.sectors:
            d = {"name": sec["name"], "members": sec["members"], "constituents": sec["constituents"], "custom": not sec.get("yahoo"),
                 "value": None, "prevClose": None, "chg": None}
            rows = self.idx.get(sec.get("yahoo")) or []
            today = [c for c in rows if c["ts"].date() == sess and (cutoff is None or c["ts"] + ONE_MIN <= cutoff)]
            prev = [c for c in rows if c["ts"].date() < sess]
            if today and prev:
                closes = getattr(self, "idx_daily", {}).get(sec.get("yahoo"), {})
                prev_day = prev[-1]["ts"].date()
                # official closing value when Yahoo has it for that exact day (index closes are not the last 1-minute print)
                d["prevClose"] = closes.get(prev_day, prev[-1]["c"])
                after_close = cutoff is None and today[-1]["ts"].time() >= dt.time(15, 29)
                d["value"] = closes.get(sess, today[-1]["c"]) if after_close else today[-1]["c"]
                d["chg"] = round((d["value"] / d["prevClose"] - 1) * 100, 3)
            out.append(d)
        return out

    def session_date(self):
        dates = [rows[-1]["ts"].date() for rows in self.intra.values() if rows]
        return max(dates) if dates else self.prov.now().date()

    def _prev_day(self, sym, sess):
        rows = [c for c in self.daily.get(sym, []) if c["ts"].date() < sess]
        return rows[-1] if rows else None

    def _pack(self, sym, ltp, o, h, l, prev_close, pd, first5=None):
        m = self.meta[sym]
        return {"symbol": sym, "sector": m["sector"], "weight": m["weight"], "ltp": ltp, "prevClose": prev_close, "open": o, "high": h, "low": l,
                "prevDay": {"o": pd["o"], "h": pd["h"], "l": pd["l"], "c": pd["c"]}, "first5": first5,
                "sectors": self.sec_of.get(sym, [])}

    @staticmethod
    def _first5(rows):
        """The 09:15-09:20 candle from 1-minute rows, or None until all five minutes are complete."""
        f = [c for c in rows if c["ts"].time() < dt.time(9, 20)]
        if len(f) < 5:
            return None
        return {"o": f[0]["o"], "h": max(c["h"] for c in f), "l": min(c["l"] for c in f), "c": f[-1]["c"]}

    def _first5_broker(self, now):
        """Broker quote path: fetch 09:15-09:20 minutes once per day (cached) after 09:20."""
        cutoff = self._cutoff(now.date(), "09:20")
        if now < cutoff + dt.timedelta(seconds=5):
            return {}
        key = cutoff.isoformat()
        if key not in self.at_cache:
            self.at_cache[key] = self.prov.intraday(self.syms, upto=cutoff)
        return {s: self._first5(rows) for s, rows in self.at_cache[key].items()}

    def snapshot(self, at=None):
        now = self.prov.now()
        # Live quote path (brokers)
        if at is None and hasattr(self.prov, "quotes"):
            q = self.prov.quotes(self.syms)
            sess = now.date()
            f5 = self._first5_broker(now)
            stocks = []
            for s, d in q.items():
                pd = self._prev_day(s, sess)
                if pd:
                    stocks.append(self._pack(s, d["ltp"], d["open"], d["high"], d["low"], d["prevClose"], pd, f5.get(s)))
            return {"time": now.isoformat(), "dataTime": now.isoformat(), "session": sess.isoformat(), "stocks": stocks,
                    "sectors": self.sectors_payload(sess)}
        # Candle path
        if hasattr(self.prov, "quotes"):                    # historical snapshot for a broker: fetch minutes up to the cut-off once and cache
            cutoff = self._cutoff(now.date(), at)
            key = cutoff.isoformat()
            if key not in self.at_cache:
                self.at_cache[key] = self.prov.intraday(self.syms, upto=cutoff)
            intra, sess = self.at_cache[key], now.date()
        else:
            intra, sess = self.intra, self.session_date()
        cutoff = self._cutoff(sess, at) if at else None
        if cutoff and not hasattr(self.prov, "quotes") and cutoff <= now and (self.intra_now is None or self.intra_now < cutoff + dt.timedelta(seconds=3)):
            self.refresh()                                  # the cache predates the cut-off: pull the final minutes before answering
            intra = self.intra
        stocks = []
        for s in self.syms:
            rows = intra.get(s) or []
            if cutoff:
                rows = [c for c in rows if c["ts"] + ONE_MIN <= cutoff]     # only fully formed minutes
            pd = self._prev_day(s, sess)
            if not rows or not pd:
                continue
            stocks.append(self._pack(s, rows[-1]["c"], rows[0]["o"], max(c["h"] for c in rows), min(c["l"] for c in rows), pd["c"], pd, self._first5(rows)))
        # dataTime: the market time the data reaches (last formed minute). Outside market hours it is the last session's close,
        # so the app shows that session as finished instead of guessing from the PC clock.
        last = max((rows[-1]["ts"] for rows in intra.values() if rows), default=None)
        data_time = min(cutoff, last + ONE_MIN) if (cutoff and last) else (last + ONE_MIN if last else now)
        return {"time": (cutoff or now).isoformat(), "dataTime": data_time.isoformat(), "session": sess.isoformat(), "stocks": stocks,
                "sectors": self.sectors_payload(sess, cutoff)}

    @staticmethod
    def _cutoff(day, at):
        h, m = [int(x) for x in at.split(":")[:2]]
        return dt.datetime.combine(day, dt.time(h, m), tzinfo=IST)

    def candles(self, sym, tf):
        if sym not in self.meta:
            return None
        sess = self.session_date() if self.intra else self.prov.now().date()
        if tf == "1d":
            rows = [c for c in self.daily.get(sym, []) if c["ts"].date() < sess][-30:]
            return [{"t": c["ts"].date().isoformat(), "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]} for c in rows]
        rows = self.intra.get(sym)
        if rows is None and hasattr(self.prov, "quotes"):
            now = self.prov.now()
            rows = self.prov.intraday([sym]).get(sym, [])
        rows = rows or []
        out, bucket = [], None
        for c in rows:
            idx = int((c["ts"] - c["ts"].replace(hour=9, minute=15, second=0, microsecond=0)).total_seconds() // 300)
            if bucket is None or bucket["i"] != idx:
                bucket = {"i": idx, "t": c["ts"].isoformat(), "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"]}
                out.append(bucket)
            else:
                bucket["h"], bucket["l"], bucket["c"], bucket["v"] = max(bucket["h"], c["h"]), min(bucket["l"], c["l"]), c["c"], bucket["v"] + c["v"]
        return [{k: v for k, v in b.items() if k != "i"} for b in out]

    def health(self):
        sess = self.session_date().isoformat() if self.intra else None
        return {"provider": self.prov.name, "now": self.prov.now().isoformat(), "symbols_expected": len(self.syms), "symbols_daily": len(self.daily),
                "symbols_intraday": len(self.intra), "session_date": sess, "last_error": self.err,
                "intraday_age_s": round(time.time() - self.intra_at, 1) if self.intra_at else None,
                "missing": [s for s in self.syms if s not in self.daily][:20],
                "sector_indices": f"{sum(1 for t in self.index_tickers if self.idx.get(t))}/{len(self.index_tickers)}", "index_error": self.idx_err}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def make_handler(store, app_path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype != "application/json" else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self._send(204, b"")

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    html = open(app_path, encoding="utf-8").read()
                    html = html.replace("<head>", "<head><script>window.SCAN_SERVED=location.origin;</script>", 1)
                    return self._send(200, html.encode(), "text/html")
                if u.path == "/health":
                    return self._send(200, store.health())
                if u.path == "/snapshot":
                    at = q.get("at")
                    if at and not (len(at) >= 4 and ":" in at):
                        return self._send(400, {"error": "at must look like 09:20"})
                    return self._send(200, store.snapshot(at))
                if u.path == "/candles":
                    rows = store.candles(q.get("symbol", ""), q.get("tf", "5m"))
                    return self._send(404, {"error": "unknown symbol"}) if rows is None else self._send(200, rows)
                self._send(404, {"error": "not found"})
            except Exception as e:
                self._send(500, {"error": repr(e)})
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=["mock", "yahoo", "kite"], default="yahoo")
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to open it from your phone on the same Wi-Fi")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--refresh", type=float, default=15, help="seconds between data refreshes")
    ap.add_argument("--mock-time", default="09:19:30", help="mock provider: pretend it is this time when the server starts")
    ap.add_argument("--mock-speed", type=float, default=1.0, help="mock provider: clock speed (60 = one minute per second)")
    ap.add_argument("--no-live-list", action="store_true", help="skip downloading the current Nifty 200 list from NSE at start")
    ap.add_argument("--app", default=os.path.join(HERE, "index.html"))
    a = ap.parse_args()

    uni = load_universe()
    if a.provider != "mock" and not a.no_live_list:
        uni, msg = sync_universe(uni)
        print(f"[universe] {msg}", flush=True)
    prov = MockProvider(uni, a.mock_time, a.mock_speed) if a.provider == "mock" else YahooProvider(uni) if a.provider == "yahoo" else KiteProvider(uni)
    sectors, failed = load_sectors(uni, offline=a.provider == "mock" or a.no_live_list)
    print(f"[sectors] {len(sectors)} sectors" + (f"; stock lists from cache for {failed}" if failed else ""), flush=True)
    store = Store(prov, uni, a.refresh, sectors)
    print(f"[{a.provider}] loading {len(uni)} symbols ...", flush=True)
    store.refresh(force=True)
    h = store.health()
    print(f"[{a.provider}] daily {h['symbols_daily']}, intraday {h['symbols_intraday']}, sector indices {h['sector_indices']}, session {h['session_date']}, error {h['last_error']}", flush=True)

    def loop():
        while True:
            time.sleep(a.refresh)
            store.refresh()
    threading.Thread(target=loop, daemon=True).start()

    srv = ThreadingHTTPServer((a.host, a.port), make_handler(store, a.app))
    print(f"Open  http://{'localhost' if a.host == '127.0.0.1' else a.host}:{a.port}/   (health: /health)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

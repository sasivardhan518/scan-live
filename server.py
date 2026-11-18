#!/usr/bin/env python3
"""
9:20 Scanner live data server.

Serves the scanner app and the data it needs:
  GET /                      the app (index.html), already pointed at this server
  GET /snapshot              current quotes for the whole universe
  GET /snapshot?at=09:20     quotes as of that time today (used to lock the 9:20 shortlist)
  GET /candles?symbol=X&tf=5m|1d
  GET /ema                   20 EMA buy and sell setups on 5-minute candles (see ema_setup.py)
  GET /health                provider status, symbols loaded, last error

Providers:  mock (synthetic, for testing)  |  yahoo (free, no account)  |  kite (Zerodha Kite Connect)
            fyers (Fyers API v3, real-time and free with an account; log in daily at /fyers/login)
            auto  (Fyers when logged in, Yahoo until then or if Fyers fails)
"""
import argparse, datetime as dt, json, math, os, random, sys, threading, time, zlib
from ema_setup import ema, buy_setups, sell_setups
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
OPEN = dt.time(9, 15)
CLOSE = dt.time(15, 30)
ONE_MIN = dt.timedelta(minutes=1)
FIVE_MIN = dt.timedelta(minutes=5)


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
        out.append({"name": d["name"], "yahoo": d.get("yahoo"), "fyers": d.get("fyers"), "constituents": len(members), "members": [m for m in members if m in meta]})
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


def bucket5(t):
    """Start of the 5-minute candle that contains t (candles start at 09:15, 09:20, ...)."""
    return t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)


def to5m(rows):
    """1-minute candles -> 5-minute candles."""
    out = []
    for c in rows:
        b = bucket5(c["ts"])
        if out and out[-1]["ts"] == b:
            x = out[-1]
            x["h"], x["l"], x["c"], x["v"] = max(x["h"], c["h"]), min(x["l"], c["l"]), c["c"], x["v"] + c["v"]
        else:
            out.append(dict(c, ts=b))
    return out


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

    def candles5(self, symbols, day=None):
        """5-minute candles for the 20 EMA scan. The mock market has only today, so its EMA starts at 09:15."""
        if day is not None:
            raise LookupError("the mock market has only today")
        return {s: to5m(rows) for s, rows in self.intraday(symbols).items()}


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

    def day_candles(self, symbols, day, suffix=".NS"):
        """5-minute candles for one past session (Yahoo keeps 5-minute data for about 60 days). Returns ({symbol: rows}, step minutes)."""
        df = self.yf.download([x + suffix for x in symbols], start=day.isoformat(), end=(day + dt.timedelta(days=1)).isoformat(),
                              interval="5m", group_by="ticker", threads=True, progress=False, auto_adjust=False)
        raw = parse_yf_frame(df, symbols, suffix)
        return {k: [c for c in v if c["ts"].date() == day] for k, v in raw.items()}, 5

    def intraday(self, symbols, upto=None):
        raw = parse_yf_frame(self._download(symbols, period="5d", interval="1m"), symbols)
        if not raw:
            return {}
        latest = max(rows[-1]["ts"].date() for rows in raw.values())
        return {s: [c for c in rows if c["ts"].date() == latest] for s, rows in raw.items()}

    def candles5(self, symbols, day=None):
        """5-minute candles for the 20 EMA scan: the session plus earlier days, so the EMA is warmed up (one download)."""
        if day is None:
            return parse_yf_frame(self._download(symbols, period="5d", interval="5m"), symbols)
        df = self.yf.download([s + ".NS" for s in symbols], start=(day - dt.timedelta(days=7)).isoformat(), end=(day + dt.timedelta(days=1)).isoformat(),
                              interval="5m", group_by="ticker", threads=True, progress=False, auto_adjust=False)
        return parse_yf_frame(df, symbols)


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

    def day_candles(self, symbols, day, suffix=None):
        """1-minute candles for one past session (about 70 s for 200 stocks at Kite's rate limit)."""
        frm = dt.datetime.combine(day, dt.time(9, 15)); to = dt.datetime.combine(day, dt.time(15, 30))
        return {s: self._hist(s, frm, to, "minute") for s in symbols if s in self.tok}, 1

    def candles5(self, symbols, day=None):
        """5-minute candles for the 20 EMA scan, a week back for the EMA warm-up (about 70 s at Kite's rate limit)."""
        day = day or self.now().date()
        frm = dt.datetime.combine(day - dt.timedelta(days=7), dt.time(9, 15)); to = dt.datetime.combine(day, dt.time(15, 30))
        return {s: self._hist(s, frm, to, "5minute") for s in symbols if s in self.tok}

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


class LoginNeeded(Exception):
    """The broker needs a fresh login before it will send data."""


class Loading(Exception):
    """The first download is still under way."""


FYERS_API = "https://api-t1.fyers.in/api/v3"
FYERS_DATA = "https://api-t1.fyers.in/data"
# Fyers sits behind Cloudflare, which refuses Python's default User-Agent ("error code: 1010"), so name the client.
FYERS_HEADERS = {"User-Agent": "Mozilla/5.0 (scan-live scanner)", "Accept": "application/json"}


def fyers_json(status, body):
    """Parse a Fyers reply; a non-JSON reply (a block page, an outage) becomes a readable error."""
    try:
        return json.loads(body or b"{}")
    except ValueError:
        text = body[:200].decode(errors="replace").strip()
        raise RuntimeError(f"Fyers sent a non-JSON reply (HTTP {status}): {text or 'empty'}")


class FyersProvider:
    """Fyers API v3: real-time NSE quotes and 1-minute candles, free with a Fyers account.
    Needs an app from myapi.fyers.in (App ID and Secret ID, in fyers.json or FYERS_APP_ID / FYERS_SECRET_KEY)
    and one login per day at /fyers/login. Sector % comes from Fyers' own index quotes."""
    name = "fyers"
    index_key = "fyers"            # sectors.json field with this provider's index symbol
    QUOTE_TTL = 5                  # seconds a quote stays fresh (Fyers allows 200 calls a minute in all)

    def __init__(self, universe, port=8787):
        import collections, secrets
        cfg = {}
        path = os.path.join(HERE, "fyers.json")
        if os.path.exists(path):
            with open(path) as f:
                cfg = json.load(f)
        self.app_id = os.environ.get("FYERS_APP_ID") or cfg.get("app_id")
        self.secret = os.environ.get("FYERS_SECRET_KEY") or cfg.get("secret_key")
        self.redirect = os.environ.get("FYERS_REDIRECT_URI") or cfg.get("redirect_uri") or f"http://127.0.0.1:{port}/fyers/callback"
        if not self.app_id or not self.secret:
            raise SystemExit("Put your Fyers App ID and Secret ID in fyers.json (see README) or set FYERS_APP_ID and FYERS_SECRET_KEY")
        self.token_path = os.path.join(HERE, "fyers_token.json")
        self.token = None
        try:
            with open(self.token_path) as f:
                t = json.load(f)
            if t.get("date") == self.now().date().isoformat():       # Fyers tokens last one trading day
                self.token = t["access_token"]
        except Exception:
            pass
        self.state = secrets.token_urlsafe(16)
        self.calls, self.rl = collections.deque(), threading.Lock()
        self.cool_until, self.partial = 0.0, {}                   # rate-limit pause; daily downloads in progress
        self.per_min = 150                                         # our own pace; lowered after any rate-limit reply

    def cooling(self):
        """True during a rate-limit pause (every Fyers call would wait for it to end)."""
        return time.time() < self.cool_until
        self.qcache, self.qlock = {}, threading.Lock()
        self.sd, self.sd_at = None, 0.0

    def now(self):
        return dt.datetime.now(IST)

    # ---- login ----
    def auth_url(self):
        from urllib.parse import urlencode
        return FYERS_API + "/generate-authcode?" + urlencode({"client_id": self.app_id, "redirect_uri": self.redirect,
                                                                "response_type": "code", "state": self.state})

    def finish_login(self, auth_code, state):
        import hashlib, urllib.request, urllib.error
        if state != self.state:
            raise ValueError("login state did not match; start again from /fyers/login")
        body = json.dumps({"grant_type": "authorization_code", "code": auth_code,
                           "appIdHash": hashlib.sha256(f"{self.app_id}:{self.secret}".encode()).hexdigest()}).encode()
        req = urllib.request.Request(FYERS_API + "/validate-authcode", data=body, headers=dict(FYERS_HEADERS, **{"Content-Type": "application/json"}))
        try:
            d = fyers_json(200, urllib.request.urlopen(req, timeout=20).read())
        except urllib.error.HTTPError as e:
            d = fyers_json(e.code, e.read())
        if d.get("s") != "ok" or not d.get("access_token"):
            raise RuntimeError(d.get("message") or "Fyers did not return an access token")
        self.token = d["access_token"]
        with open(self.token_path, "w") as f:
            json.dump({"date": self.now().date().isoformat(), "access_token": self.token}, f)
        self.sd_at = 0.0

    def logged_in(self):
        return bool(self.token)

    # ---- REST ----
    def _wait(self):
        """Stay well under Fyers' limits (10 calls a second, 200 a minute; repeated breaches can block the account for
        the day): at most 5 a second and 150 a minute, and nothing at all while a rate-limit pause is running."""
        with self.rl:
            while True:
                t = time.time()
                if t < self.cool_until:
                    time.sleep(min(1.0, self.cool_until - t))
                    continue
                while self.calls and t - self.calls[0] > 60:
                    self.calls.popleft()
                if len(self.calls) < self.per_min and sum(1 for c in self.calls if t - c < 1) < 5:
                    self.calls.append(t)
                    return
                time.sleep(0.05)

    def _get(self, path, params):
        import urllib.request, urllib.error
        from urllib.parse import urlencode
        if not self.token:
            raise LoginNeeded("Log in to Fyers to load market data")
        url = FYERS_DATA + path + "?" + urlencode(params)
        for attempt in range(4):
            self._wait()
            req = urllib.request.Request(url, headers=dict(FYERS_HEADERS, Authorization=f"{self.app_id}:{self.token}", version="3"))
            try:
                status, body = 200, urllib.request.urlopen(req, timeout=20).read()
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read()
            except urllib.error.URLError as e:
                if attempt == 3:
                    raise RuntimeError(f"Fyers unreachable: {e.reason}")
                time.sleep(1)
                continue
            try:
                d = json.loads(body)
            except ValueError:
                d = {"s": "error", "message": body[:200].decode(errors="replace")}
            code = d.get("code")
            if status == 429 or code == 429:                      # over the rate limit: every call pauses until the minute clears
                self.cool_until = max(self.cool_until, time.time() + 65)
                self.calls.clear()
                self.per_min = max(60, int(self.per_min * 0.7))   # and go slower from now on
                print(f"[fyers] rate limit reached on {path}; pausing all Fyers calls for 65 s, then at most {self.per_min} a minute"
                      f" ({d.get('message') or status})", flush=True)
                continue
            if status in (401, 403) or code in (-8, -15, -16, -17):
                self.token = None                                 # expired or revoked: ask for a new login
                raise LoginNeeded("Fyers login expired. Log in again")
            if d.get("s") not in ("ok", "no_data"):
                raise RuntimeError(f"Fyers {path}: {d.get('message') or d}")
            return d
        raise RuntimeError(f"Fyers {path}: still over the rate limit")

    @staticmethod
    def fy(sym):
        return sym if ":" in sym else f"NSE:{sym}-EQ"

    def _hist(self, sym, res, frm, to):
        d = self._get("/history", {"symbol": self.fy(sym), "resolution": res, "date_format": 1,
                                   "range_from": frm.isoformat(), "range_to": to.isoformat(), "cont_flag": 1})
        out = []
        for row in d.get("candles") or []:
            t, o, h, l, c = row[:5]
            ts = dt.datetime.fromtimestamp(t, IST)
            if res == "D":
                ts = ts.replace(hour=0, minute=0, second=0, microsecond=0)
            out.append({"ts": ts, "o": r2(o), "h": r2(h), "l": r2(l), "c": r2(c), "v": int(row[5]) if len(row) > 5 and row[5] else 0})
        return out

    def _many(self, symbols, res, frm, to):
        """Candles for many symbols, one call each. Daily candles survive an interrupted download (a rate-limit stop,
        a network drop): the next attempt continues from where it stopped instead of starting the 200 calls again."""
        key = (res, frm, to)
        out = self.partial.setdefault(key, {}) if res == "D" else {}
        for s in symbols:
            if s in out:
                continue
            rows = self._hist(s, res, frm, to)
            if rows:
                out[s] = rows
        self.partial.pop(key, None)
        return {s: out[s] for s in symbols if s in out}

    def daily(self, symbols):
        to = self.now().date()
        return self._many(symbols, "D", to - dt.timedelta(days=200), to)

    def intraday(self, symbols, upto=None):
        day = (upto or self.now()).date()
        return self._many(symbols, "1", day, day)

    def day_candles(self, symbols, day, suffix=None):
        """1-minute candles for one past session (about 1 minute for 200 stocks at Fyers' rate limit)."""
        return self._many(symbols, "1", day, day), 1

    def candles5(self, symbols, day=None):
        """5-minute candles for the 20 EMA scan, a week back for the EMA warm-up (about a minute at Fyers' rate limit)."""
        day = day or self.now().date()
        return self._many(symbols, "5", day - dt.timedelta(days=7), day)

    def session_day(self):
        """Date of the latest trading session: today once the market has traded, else the previous trading day."""
        if self.sd is None or (time.time() - self.sd_at > 60 and not self.cooling()):     # keep the known day during a pause
            today = self.now().date()
            rows = self._hist("NSE:NIFTY50-INDEX", "1", today - dt.timedelta(days=7), today)
            self.sd, self.sd_at = (rows[-1]["ts"].date() if rows else today), time.time()
        return self.sd

    def quotes(self, symbols, fresh=False):
        """{symbol: {ltp, open, high, low, prevClose}}, read at most every QUOTE_TTL seconds unless fresh."""
        key = tuple(symbols)
        with self.qlock:
            hit = self.qcache.get(key)
            if hit and not fresh and time.time() - hit[0] < self.QUOTE_TTL:
                return hit[1]
            out = {}
            for i in range(0, len(symbols), 50):                 # 50 symbols per call
                chunk = symbols[i:i + 50]
                back = {self.fy(s): s for s in chunk}
                d = self._get("/quotes", {"symbols": ",".join(back)})
                for item in d.get("d") or []:
                    v = item.get("v") or {}
                    s = back.get(item.get("n") or v.get("symbol"))
                    if not s or item.get("s") != "ok" or not v.get("lp"):
                        continue
                    lp = v["lp"]
                    out[s] = {"ltp": r2(lp), "open": r2(v.get("open_price") or lp), "high": r2(v.get("high_price") or lp),
                              "low": r2(v.get("low_price") or lp), "prevClose": r2(v.get("prev_close_price") or 0)}
            self.qcache[key] = (time.time(), out)
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
        # index symbols come from the provider itself when it has them (Fyers), else from Yahoo
        self.tkey = getattr(prov, "index_key", "yahoo")
        self.own_idx = self.tkey != "yahoo"
        self.index_tickers = [sec[self.tkey] for sec in self.sectors if sec.get(self.tkey)] if prov.name != "mock" else []
        self.idx, self.idx_err = {}, None
        self.caps = {}                                             # quotes read right at a scan time: {cutoff iso: {"q", "iq"}}
        self.at_lock, self.refresh_lock = threading.Lock(), threading.Lock()
        self.syms = [x["symbol"] for x in universe]
        self.meta = {x["symbol"]: x for x in universe}
        self.daily, self.intra, self.at_cache = {}, {}, {}
        self.daily_at = self.intra_at = 0.0
        self.intra_now = None
        self.err, self.lock = None, threading.Lock()
        self.days, self.day_lock = {}, threading.Lock()            # past sessions: {date: {"stocks", "idx", "step"}}
        # 20 EMA scan: 5-minute candles of the latest session (with earlier days for the warm-up) and of past days
        self.c5l, self.c5_err, self.c5_lock = None, None, threading.Lock()   # c5l = (fetched epoch, market time, {symbol: rows})
        self.c5days, self.c5day_lock = {}, threading.Lock()
        self.ticks = {}                                            # broker: forming 5-minute candle built from live quotes

    def refresh(self, force=False, wait=False):
        """Pull new data. A refresh already under way is not duplicated (wait=True waits for it and then refreshes)."""
        if not self.refresh_lock.acquire(blocking=wait):
            return
        try:
            self._refresh(force)
        finally:
            self.refresh_lock.release()

    def _refresh(self, force):
        try:
            stale = time.time() - self.daily_at > 6 * 3600
            if self.own_idx:                                  # rate-limited broker: daily candles once per day
                stale = dt.datetime.fromtimestamp(self.daily_at, IST).date() != self.prov.now().date()
            if force or not self.daily or stale:
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
        if self.own_idx:                                      # live index values come with each snapshot; only official closes here
            try:
                if dt.datetime.fromtimestamp(getattr(self, "idx_daily_at", 0), IST).date() != self.prov.now().date():
                    dd = self.prov.daily(self.index_tickers)
                    self.idx_daily, self.idx_daily_at = {t: {c["ts"].date(): c["c"] for c in rows} for t, rows in dd.items()}, time.time()
                self.idx_err = None
            except Exception as e:
                self.idx_err = repr(e)
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
                dd = parse_yf_frame(yf.download(self.index_tickers, period="4mo", interval="1d", group_by="ticker", threads=True,
                                                progress=False, auto_adjust=False), self.index_tickers, suffix="")
                if dd:
                    self.idx_daily, self.idx_daily_at = {t: {c["ts"].date(): c["c"] for c in rows} for t, rows in dd.items()}, time.time()
        except Exception as e:
            self.idx_err = repr(e)

    def sectors_payload(self, sess, cutoff=None, idx=None, step=ONE_MIN, live=None):
        """[{name, members, constituents, custom, value, prevClose, chg}] - chg is null for custom sectors (the app averages their stocks).
        live = {index symbol: quote} uses quoted index values instead of candles."""
        out = []
        idx = self.idx if idx is None else idx
        for sec in self.sectors:
            tk = sec.get(self.tkey)
            d = {"name": sec["name"], "members": sec["members"], "constituents": sec["constituents"], "custom": not tk,
                 "value": None, "prevClose": None, "chg": None}
            lq = (live or {}).get(tk)
            if lq is not None:
                if lq.get("prevClose"):
                    d["value"], d["prevClose"] = lq["ltp"], lq["prevClose"]
                    d["chg"] = round((lq["ltp"] / lq["prevClose"] - 1) * 100, 3)
                out.append(d)
                continue
            rows = idx.get(tk) or []
            today = [c for c in rows if c["ts"].date() == sess and (cutoff is None or c["ts"] + step <= cutoff)]
            prev = [c for c in rows if c["ts"].date() < sess]
            closes = getattr(self, "idx_daily", {}).get(tk, {})
            earlier = [k for k in closes if k < sess]
            if today and (prev or earlier):
                # official closing value when Yahoo has it for that exact day (index closes are not the last 1-minute print)
                prev_day = prev[-1]["ts"].date() if prev else max(earlier)
                d["prevClose"] = closes.get(prev_day, prev[-1]["c"] if prev else None)
                after_close = cutoff is None and (today[-1]["ts"] + step).time() >= dt.time(15, 30)
                d["value"] = closes.get(sess, today[-1]["c"]) if after_close else today[-1]["c"]
                if d["prevClose"]:
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
    def _first5(rows, step_min=1):
        """The 09:15-09:20 candle from 1- or 5-minute rows, or None until all five minutes are complete."""
        f = [c for c in rows if c["ts"].time() < dt.time(9, 20)]
        if len(f) * step_min < 5:
            return None
        return {"o": f[0]["o"], "h": max(c["h"] for c in f), "l": min(c["l"] for c in f), "c": f[-1]["c"]}

    def _first5_broker(self, now):
        """Broker quote path: the 09:15-09:20 candle. From quotes read right at 09:20 when the server caught that moment
        (day open, high and low so far, last price), else from 1-minute candles downloaded once in the background."""
        cutoff = self._cutoff(now.date(), "09:20")
        if now < cutoff:
            return {}
        cap = self.caps.get(cutoff.isoformat())
        if cap:
            return {s: {"o": q["open"], "h": q["high"], "l": q["low"], "c": q["ltp"]} for s, q in cap["q"].items()}
        if now < cutoff + dt.timedelta(seconds=5):
            return {}
        rows = self._at(cutoff, wait=False)
        return {s: self._first5(r) for s, r in rows.items()} if rows else {}

    def latest_session(self):
        if hasattr(self.prov, "session_day"):
            return self.prov.session_day()
        return self.prov.now().date() if hasattr(self.prov, "quotes") else self.session_date()

    def scan_clock(self, at="09:20"):
        """Read every quote the moment the scan time passes, so the first 5-minute candle and the lock are exact without
        waiting for 200 candle downloads (Fyers allows 200 calls a minute)."""
        while True:
            now = self.prov.now()
            cut = self._cutoff(now.date(), at)
            if now > cut + dt.timedelta(seconds=10):
                cut += dt.timedelta(days=1)                 # today's moment has passed: wait for tomorrow's
            wait = (cut - now).total_seconds() + 0.3
            if wait > 0:
                time.sleep(min(wait, 30))
                continue
            if cut.isoformat() not in self.caps and getattr(self.prov, "logged_in", lambda: True)():
                try:
                    self.capture(cut)                       # on a holiday this is never used: snapshots only read it for a live session
                except Exception as e:
                    self.err = repr(e)
            time.sleep(15)

    def sessions(self):
        """Trading days the app can show: the latest session plus past days with intraday data (about 60 days back)."""
        latest = self.latest_session()
        days = {c["ts"].date() for rows in self.daily.values() for c in rows[-70:]}
        days.add(latest)
        floor = self.prov.now().date() - dt.timedelta(days=59)
        out = sorted((d for d in days if floor <= d <= latest), reverse=True)
        return {"default": latest.isoformat(), "dates": [d.isoformat() for d in out]}

    def day_data(self, day):
        """Candles for one past session, fetched once and cached (the five most recent requests are kept)."""
        with self.day_lock:
            if day in self.days:
                return self.days[day]
            if not hasattr(self.prov, "day_candles"):
                raise LookupError("this data source has no past sessions")
            stocks, step = self.prov.day_candles(self.syms, day)
            idx, idx_step = {}, 5
            if self.index_tickers and self.own_idx:
                try:
                    idx, idx_step = self.prov.day_candles(self.index_tickers, day)
                except LookupError:
                    raise
                except Exception:
                    idx = {}
            elif self.index_tickers:
                try:
                    import yfinance as yf
                    # a few days back as well, so indices without daily history still get a previous close
                    df = yf.download(self.index_tickers, start=(day - dt.timedelta(days=7)).isoformat(), end=(day + dt.timedelta(days=1)).isoformat(),
                                     interval="5m", group_by="ticker", threads=True, progress=False, auto_adjust=False)
                    idx = {k: [c for c in v if c["ts"].date() <= day] for k, v in parse_yf_frame(df, self.index_tickers, suffix="").items()}
                except Exception:
                    idx = {}
            if not any(stocks.values()):
                raise LookupError(f"no market data for {day.isoformat()} (weekend, holiday, or older than about 60 days)")
            data = {"stocks": stocks, "idx": idx, "step": max(step, idx_step) if idx else step, "stock_step": step, "idx_step": idx_step}
            self.days[day] = data
            for old in sorted(self.days)[:-5]:
                self.days.pop(old, None)
            return data

    def _snapshot_day(self, day, at):
        dd = self.day_data(day)
        step = dt.timedelta(minutes=dd["stock_step"])
        cutoff = self._cutoff(day, at) if at else None
        stocks = []
        for s in self.syms:
            rows = dd["stocks"].get(s) or []
            if cutoff:
                rows = [c for c in rows if c["ts"] + step <= cutoff]
            pd = self._prev_day(s, day)
            if not rows or not pd:
                continue
            stocks.append(self._pack(s, rows[-1]["c"], rows[0]["o"], max(c["h"] for c in rows), min(c["l"] for c in rows), pd["c"], pd,
                                     self._first5(rows, dd["stock_step"])))
        last = max((rows[-1]["ts"] for rows in dd["stocks"].values() if rows), default=None)
        end = last + step if last else self._cutoff(day, "15:30")
        data_time = min(cutoff, end) if cutoff else end
        return {"time": data_time.isoformat(), "dataTime": data_time.isoformat(), "session": day.isoformat(), "past": True, "stocks": stocks,
                "sectors": self.sectors_payload(day, cutoff, dd["idx"], dt.timedelta(minutes=dd.get("idx_step", 5)))}

    def _quote_snapshot(self, sess, q, iq, f5, t, cutoff=None):
        stocks = []
        for s, d in q.items():
            pd = self._prev_day(s, sess)
            if pd and s in self.meta:
                stocks.append(self._pack(s, d["ltp"], d["open"], d["high"], d["low"], d["prevClose"], pd, f5.get(s)))
        secs = self.sectors_payload(sess, cutoff, live=iq) if self.own_idx else self.sectors_payload(sess, cutoff)
        return {"time": t.isoformat(), "dataTime": t.isoformat(), "session": sess.isoformat(), "stocks": stocks, "sectors": secs}

    def capture(self, cutoff):
        """Quotes read right after a scan time: what a terminal shows at that moment (day open/high/low up to then, last price)."""
        q = self.prov.quotes(self.syms, fresh=True)
        iq = self.prov.quotes(self.index_tickers, fresh=True) if self.index_tickers else {}
        if self.prov.now() - cutoff > dt.timedelta(seconds=12):     # read too late to stand for that moment
            return None
        self.caps[cutoff.isoformat()] = cap = {"q": q, "iq": iq}
        return cap

    def _at(self, cutoff, wait=True):
        """1-minute candles up to a cut-off for every stock (and the provider's own indices), fetched once and cached.
        wait=False starts the download in the background and returns None until it is ready."""
        key = cutoff.isoformat()
        if key in self.at_cache:
            return self.at_cache[key]
        if not wait:
            if not self.at_lock.locked():
                threading.Thread(target=self._at, args=(cutoff,), daemon=True).start()
            return None
        with self.at_lock:
            if key in self.at_cache:
                return self.at_cache[key]
            data = self.prov.intraday(self.syms + (self.index_tickers if self.own_idx else []), upto=cutoff)
            if self.prov.now() >= cutoff + dt.timedelta(seconds=5):  # never cache a cut-off that has not passed yet
                self.at_cache[key] = data
            return data

    def snapshot(self, at=None, date=None):
        if not self.daily:                                  # nothing to compare against yet (a broker loads in the background)
            if hasattr(self.prov, "logged_in") and not self.prov.logged_in():
                raise LoginNeeded("Log in to Fyers to load market data")
            raise Loading("Loading daily candles from " + self.prov.name.capitalize() + " (about a minute at its rate limit)"
                          + (f". Last error: {self.err}" if self.err and "LoginNeeded" not in self.err else ""))
        if date and date != self.latest_session():
            return self._snapshot_day(date, at)
        now = self.prov.now()
        if hasattr(self.prov, "quotes"):
            sess = self.latest_session()
            if sess != now.date():                          # before today's first trade, or a holiday: the last session from its candles
                return self._snapshot_day(sess, at)
            if at is None:                                  # live quotes
                iq = self.prov.quotes(self.index_tickers) if self.own_idx else None
                return self._quote_snapshot(sess, self.prov.quotes(self.syms), iq, self._first5_broker(now), now)
            cutoff = self._cutoff(sess, at)
            if self.own_idx and cutoff <= now:              # quotes read within seconds of the scan time
                cap = self.caps.get(cutoff.isoformat())
                if cap is None and now - cutoff <= dt.timedelta(seconds=10):
                    cap = self.capture(cutoff)
                if cap:
                    return self._quote_snapshot(sess, cap["q"], cap["iq"], self._first5_broker(now), cutoff, cutoff)
            intra = self._at(cutoff)                        # otherwise 1-minute candles up to the cut-off
        else:
            intra, sess = self.intra, self.session_date()
        cutoff = self._cutoff(sess, at) if at else None
        if cutoff and not hasattr(self.prov, "quotes") and cutoff <= now and (self.intra_now is None or self.intra_now < cutoff + dt.timedelta(seconds=3)):
            self.refresh(wait=True)                         # the cache predates the cut-off: pull the final minutes before answering
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
                "sectors": self.sectors_payload(sess, cutoff, intra if self.own_idx else None)}

    @staticmethod
    def _cutoff(day, at):
        h, m = [int(x) for x in at.split(":")[:2]]
        return dt.datetime.combine(day, dt.time(h, m), tzinfo=IST)

    def candles(self, sym, tf, date=None):
        if sym not in self.meta:
            return None
        broker = hasattr(self.prov, "quotes")
        latest = self.latest_session()
        sess = latest if (broker or self.intra) else self.prov.now().date()
        past = bool(date and date != latest)
        if past:
            sess = date
        if tf == "1d":
            rows = [c for c in self.daily.get(sym, []) if c["ts"].date() < sess][-30:]
            return [{"t": c["ts"].date().isoformat(), "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]} for c in rows]
        if broker and sess != self.prov.now().date():
            past = True                                     # before today's first trade: the last session's candles
        rows = self.day_data(sess)["stocks"].get(sym) if past else self.intra.get(sym)
        if rows is None and not past and broker:
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
        emap = self.ema_map(sym, sess) if out else {}
        for b in out:
            t = dt.datetime.fromisoformat(b["t"])
            e = emap.get(t.replace(hour=9, minute=15, second=0, microsecond=0) + b["i"] * FIVE_MIN)
            b["e"] = round(e, 2) if e is not None else None
        return [{k: v for k, v in b.items() if k != "i"} for b in out]

    # ---- 20 EMA buy setup on 5-minute candles ----
    def _c5_refresh(self):
        """Download 5-minute candles for the latest session. A download already under way is not duplicated."""
        if not self.c5_lock.acquire(blocking=False):
            return
        try:
            stamp = self.prov.now()
            data = self.prov.candles5(self.syms)
            if data:
                self.c5l, self.c5_err = (time.time(), stamp, data), None
            else:
                self.c5_err = "no 5-minute candles returned"
        except Exception as e:
            self.c5_err = repr(e)
        finally:
            self.c5_lock.release()

    def _c5_stale(self):
        if self.c5l is None:
            return True
        fetched, stamp, _ = self.c5l
        if hasattr(self.prov, "quotes"):        # rate-limited broker: once per finished candle; live quotes fill in between
            now = self.prov.now()
            return bucket5(now) > bucket5(stamp) and now - bucket5(now) >= dt.timedelta(seconds=3)
        return time.time() - fetched > (10 if self.prov.name == "mock" else 60)

    def _c5_past(self, day):
        with self.c5day_lock:
            if day not in self.c5days:
                data = self.prov.candles5(self.syms, day)
                if not any(any(c["ts"].date() == day for c in rows) for rows in data.values()):
                    raise LookupError(f"no 5-minute candles for {day.isoformat()} (weekend, holiday, or older than about 60 days)")
                self.c5days[day] = data
                for old in sorted(self.c5days)[:-5]:
                    self.c5days.pop(old, None)
            return self.c5days[day]

    def _c5_live(self):
        """(market time, {symbol: rows}) for the latest session. Brokers get the forming candle from live quotes."""
        if self._c5_stale():
            if self.c5l is None and not hasattr(self.prov, "quotes"):
                self._c5_refresh()                  # Yahoo and mock: one quick download, wait for it the first time
            else:
                threading.Thread(target=self._c5_refresh, daemon=True).start()
        if self.c5l is None:
            if hasattr(self.prov, "logged_in") and not self.prov.logged_in():
                raise LoginNeeded("Log in to Fyers to load market data")
            raise Loading("Loading 5-minute candles for the 20 EMA scan" + (" (about a minute at the broker's rate limit)" if hasattr(self.prov, "quotes") else "")
                          + (f". Last error: {self.c5_err}" if self.c5_err else ""))
        now, data = self.prov.now(), self.c5l[2]
        if hasattr(self.prov, "quotes") and OPEN <= now.time() < CLOSE:
            try:
                data = self._with_ticks(data, self.prov.quotes(self.syms), now)
            except Exception:
                pass
        return now, data

    def _with_ticks(self, data, quotes, now):
        """Add the forming 5-minute candle from live quotes (high and low as seen by this server since the candle began)."""
        b, out = bucket5(now), {}
        for s, rows in data.items():
            q = quotes.get(s)
            if not q or not rows or rows[-1]["ts"] < b - FIVE_MIN or rows[-1]["ts"].date() != b.date():
                out[s] = rows                        # no quote, or the download is too old to join on
                continue
            px, t = q["ltp"], self.ticks.get(s)
            if not t or t["ts"] != b:
                t = {"ts": b, "o": px, "h": px, "l": px, "c": px, "v": 0}
            t = dict(t, h=max(t["h"], px), l=min(t["l"], px), c=px)
            if rows[-1]["ts"] == b:                  # the download already has part of this candle
                r = rows[-1]
                t = dict(t, o=r["o"], h=max(t["h"], r["h"]), l=min(t["l"], r["l"]))
                rows = rows[:-1]
            self.ticks[s] = t
            out[s] = rows + [t]
        return out

    def ema_scan(self, date=None, hold=False, close_break=False, color_signal=False):
        """Stocks with a 20 EMA buy or sell setup (see ema_setup.py) on the latest session or a past day.
        Options apply to both sides: hold = the pullback candle (bearish for buys, bullish for sells) stays on its side of
        the EMA, close_break = the break needs a close beyond its high/low, color_signal = the EMA-cross candle is green
        for buys, red for sells."""
        past = date is not None and date != self.latest_session()
        if past:
            data = self._c5_past(date)
            day, now = date, dt.datetime.combine(date, CLOSE, tzinfo=IST)
        else:
            now, data = self._c5_live()
            day = max((r[-1]["ts"].date() for r in data.values() if r), default=now.date())
        out, last = [], None
        for s in self.syms:
            rows = [c for c in data.get(s) or [] if c["ts"].date() <= day]
            if not rows or rows[-1]["ts"].date() != day:
                continue
            e = ema([c["c"] for c in rows])
            n_closed = sum(1 for c in rows if c["ts"] + FIVE_MIN <= now)
            closed_today = [c for c in rows[:n_closed] if c["ts"].date() == day]
            if closed_today:
                last = max(last or closed_today[-1]["ts"], closed_today[-1]["ts"])
            found = [dict(x, side="buy") for x in buy_setups(rows, e, day, n_closed, hold, close_break, color_signal)] + \
                    [dict(x, side="sell") for x in sell_setups(rows, e, day, n_closed, hold, close_break, color_signal)]
            if not found:
                continue

            def bar(i):
                return {"t": rows[i]["ts"].isoformat(), "o": rows[i]["o"], "h": rows[i]["h"], "l": rows[i]["l"], "c": rows[i]["c"],
                        "ema": round(e[i], 2) if e[i] is not None else None}
            m = self.meta[s]
            out.append({"symbol": s, "sector": m["sector"], "ltp": rows[-1]["c"], "ema": round(e[-1], 2) if e[-1] is not None else None,
                        # pull: the pullback candle (bearish for buys, bullish for sells); level: its high (buys) or low (sells)
                        "setups": [{"side": x["side"], "status": x["status"], "live": x["live"], "signal": bar(x["signal"]), "pull": bar(x["bear"]),
                                    "level": rows[x["bear"]]["h" if x["side"] == "buy" else "l"],
                                    "at": bar(x["at"]) if x["at"] is not None else None} for x in found]})
        return {"session": day.isoformat(), "past": past, "time": now.isoformat(),
                "lastCandle": last.isoformat() if last else None, "ema": 20, "tf": "5m", "stocks": out,
                "rules": {"hold": hold, "close_break": close_break, "color_signal": color_signal},
                "error": None if past else self.c5_err}

    def ema_map(self, sym, day):
        """{5-minute candle start: 20 EMA} for one stock from data already downloaded (nothing is downloaded here)."""
        data = self.c5days.get(day) or (self.c5l[2] if self.c5l else {})
        rows = [c for c in data.get(sym) or [] if c["ts"].date() <= day]
        return {c["ts"]: v for c, v in zip(rows, ema([c["c"] for c in rows])) if v is not None and c["ts"].date() == day}

    def health(self):
        try:
            busy = getattr(self.prov, "cooling", lambda: False)() and not getattr(self.prov, "sd", None)   # never wait out a pause here
            sess = None if busy else self.latest_session().isoformat() if (self.intra or hasattr(self.prov, "quotes")) else None
        except Exception:
            sess = None
        own = getattr(self, "idx_daily", {}) if self.own_idx else self.idx
        out = {"provider": self.prov.name, "now": self.prov.now().isoformat(), "symbols_expected": len(self.syms), "symbols_daily": len(self.daily),
               "symbols_intraday": len(self.intra), "session_date": sess, "last_error": self.err,
               "intraday_age_s": round(time.time() - self.intra_at, 1) if self.intra_at else None,
               "missing": [s for s in self.syms if s not in self.daily][:20],
               "sector_indices": f"{sum(1 for t in self.index_tickers if own.get(t))}/{len(self.index_tickers)}", "index_error": self.idx_err}
        if hasattr(self.prov, "logged_in"):
            out["logged_in"] = self.prov.logged_in()
        if hasattr(self.prov, "cooling"):
            out["rate_limit_pause_s"] = max(0, round(self.prov.cool_until - time.time())) if self.prov.cooling() else 0
            out["calls_per_minute_cap"] = self.prov.per_min
            out["login_url"] = "/fyers/login"
        return out


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def make_handler(store, app_path, backup=None):
    """store answers every request. With a backup (--provider auto: store = Fyers, backup = Yahoo) each data request goes
    to Fyers when it is logged in and loaded, and to Yahoo otherwise, or when Fyers fails (then Fyers rests a minute)."""
    def ready(st):
        return (getattr(st.prov, "logged_in", lambda: True)() and bool(st.daily) and time.time() >= getattr(st, "rest_until", 0)
                and not getattr(st.prov, "cooling", lambda: False)())    # in a rate-limit pause Yahoo answers at once

    def order():
        if backup is None:
            return [store]
        return [store, backup] if ready(store) else [backup]

    def extra(st):
        """Which source answered, and why Fyers did not (for the app's header)."""
        if backup is None:
            return {}
        d = {"source": st.prov.name}
        if st is backup:
            if not store.prov.logged_in():
                d["fyersLogin"], d["fyersNote"] = "/fyers/login", "Fyers is not logged in today"
            elif not store.daily:
                d["fyersNote"] = "Fyers is still loading (about a minute after login)"
            else:
                d["fyersNote"] = ("Fyers rate limit reached, back in about a minute" if store.prov.cooling() else
                                  "Fyers had an error, retrying shortly" + (f": {store.err}" if store.err else ""))
        return d

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
                    if backup is None:
                        return self._send(200, store.health())
                    return self._send(200, {"active": order()[0].prov.name, store.prov.name: store.health(), backup.prov.name: backup.health()})
                if u.path.startswith("/fyers/"):
                    return self._fyers(u.path, q)
                stores = order()
                for i, st in enumerate(stores):
                    try:
                        code, body = self._data(st, u.path, q)
                    except Exception as e:
                        if i + 1 == len(stores):
                            raise
                        if not isinstance(e, (LoginNeeded, Loading)):    # a real failure: let Yahoo answer for a minute
                            st.rest_until, st.err = time.time() + 60, repr(e)
                        continue
                    if code == 200 and isinstance(body, dict):
                        body = dict(body, **extra(st))
                    return self._send(code, body)
            except LoginNeeded as e:
                self._send(401, {"error": str(e), "login": "/fyers/login"})
            except Loading as e:
                self._send(503, {"error": str(e), "loading": True})
            except Exception as e:
                self._send(500, {"error": repr(e)})

        def _data(self, store, path, q):
            """(status, body) for one data request from one store. Raises LoginNeeded, Loading or other errors."""
            date = None
            if q.get("date"):
                try:
                    date = dt.date.fromisoformat(q["date"])
                except ValueError:
                    return (400, {"error": "date must look like 2026-11-17"})
                if date > store.prov.now().date():
                    return (404, {"error": "that date is in the future"})
            if path == "/sessions":
                return (200, store.sessions())
            if path == "/snapshot":
                at = q.get("at")
                if at and not (len(at) >= 4 and ":" in at):
                    return (400, {"error": "at must look like 09:20"})
                try:
                    return (200, store.snapshot(at, date))
                except LookupError as e:
                    return (404, {"error": str(e)})
            if path == "/ema":
                flag = lambda k: q.get(k) in ("1", "true")
                try:
                    return (200, store.ema_scan(date, flag("hold"), flag("close_break"), flag("color")))
                except LookupError as e:
                    return (404, {"error": str(e)})
            if path == "/candles":
                try:
                    rows = store.candles(q.get("symbol", ""), q.get("tf", "5m"), date)
                except LookupError as e:
                    return (404, {"error": str(e)})
                return (404, {"error": "unknown symbol"}) if rows is None else (200, rows)
            return 404, {"error": "not found"}

        def _page(self, title, msg, home=True):
            html = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>" + title +
                    "</title><body style='font:16px system-ui;max-width:520px;margin:15vh auto;padding:0 16px;color:#1a212c;background:#f2f5f9'><h2>" +
                    title + "</h2><p>" + msg + "</p>" + ("<p><a href='/'>Back to the scanner</a></p>" if home else "") + "</body>")
            self._send(200, html.encode(), "text/html")

        def _fyers(self, path, q):
            prov = store.prov
            if not hasattr(prov, "auth_url"):
                return self._send(404, {"error": "the server is not running with --provider fyers or auto (with fyers.json)"})
            if path == "/fyers/login":                     # off to Fyers' own login page; credentials never pass through here
                self.send_response(302)
                self.send_header("Location", prov.auth_url())
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if path == "/fyers/callback":
                if q.get("s") == "error" or not q.get("auth_code"):
                    return self._page("Fyers login failed", "Fyers did not send a login code. <a href='/fyers/login'>Try again</a>.")
                try:
                    prov.finish_login(q["auth_code"], q.get("state"))
                except Exception as e:
                    import html as h
                    return self._page("Fyers login failed", h.escape(str(e)) + " <a href='/fyers/login'>Try again</a>.")
                store.err = None
                threading.Thread(target=store.refresh, kwargs={"force": True}, daemon=True).start()
                self.send_response(302)                    # logged in: straight back to the app
                self.send_header("Location", "/")
                self.end_headers()
                return
            self._send(404, {"error": "not found"})
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=["auto", "mock", "yahoo", "kite", "fyers"], default="yahoo",
                    help="auto = Fyers when logged in, Yahoo otherwise (needs fyers.json; without it, Yahoo only)")
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
    backup_prov = None
    if a.provider == "mock":
        prov = MockProvider(uni, a.mock_time, a.mock_speed)
    elif a.provider == "auto":
        backup_prov = YahooProvider(uni)
        try:
            prov = FyersProvider(uni, a.port)
        except SystemExit as e:
            print(f"[auto] Fyers is not set up ({e}). Running on Yahoo only.", flush=True)
            prov, backup_prov = backup_prov, None
    elif a.provider == "yahoo":
        prov = YahooProvider(uni)
    elif a.provider == "kite":
        prov = KiteProvider(uni)
    else:
        prov = FyersProvider(uni, a.port)
    sectors, failed = load_sectors(uni, offline=a.provider == "mock" or a.no_live_list)
    print(f"[sectors] {len(sectors)} sectors" + (f"; stock lists from cache for {failed}" if failed else ""), flush=True)
    store = Store(prov, uni, a.refresh, sectors)
    backup = Store(backup_prov, uni, a.refresh, sectors) if backup_prov else None
    if backup:
        print(f"[auto] Fyers when logged in, Yahoo until then (or if Fyers fails)", flush=True)

    def loop(st, background):
        first = background
        while True:
            if not first:
                time.sleep(a.refresh)
            first = False
            st.refresh()
            if background and st.daily and not getattr(st, "_told", False):
                st._told = True
                print(f"[fyers] ready: daily {len(st.daily)}, error {st.err}", flush=True)

    for st in [store] + ([backup] if backup else []):
        name = st.prov.name
        background = name == "fyers"            # about a minute of downloads at Fyers' rate limit: serve the app (and its login) meanwhile
        if background:
            login = "" if st.prov.logged_in() else f"  ->  log in: http://localhost:{a.port}/fyers/login"
            print(f"[fyers] loading {len(uni)} symbols in the background{login}", flush=True)
            threading.Thread(target=st.scan_clock, daemon=True).start()
        else:
            print(f"[{name}] loading {len(uni)} symbols ...", flush=True)
            st.refresh(force=True)
            h = st.health()
            print(f"[{name}] daily {h['symbols_daily']}, intraday {h['symbols_intraday']}, sector indices {h['sector_indices']}, session {h['session_date']}, error {h['last_error']}", flush=True)
        threading.Thread(target=loop, args=(st, background), daemon=True).start()

    # Windows lets a second server bind the same port and the old one keeps answering, so refuse to start instead
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
    try:
        srv = ThreadingHTTPServer((a.host, a.port), make_handler(store, a.app, backup))
    except OSError as e:
        raise SystemExit(f"Port {a.port} is already in use ({e}). Another copy of server.py is probably still running: "
                         f"stop it (Task Manager, or: Get-NetTCPConnection -LocalPort {a.port} -State Listen | "
                         f"% {{ Stop-Process -Id $_.OwningProcess }}) and start again, or use --port.")
    print(f"Open  http://{'localhost' if a.host == '127.0.0.1' else a.host}:{a.port}/   (health: /health)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

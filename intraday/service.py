"""Runs the per-stock state machines on a background thread and builds the Scanner board's data.

Also keeps: the alert log, the manual market bias, the risk settings (intraday/settings.json) and the day's journal of
trades you marked as taken (intraday/journal/YYYY-MM-DD.json). Nothing here places orders.
"""
import collections
import copy
import datetime as dt
import json
import os
import threading
import time
import traceback

from . import HERE, engine as E, state as S

STATE_ORDER = {S.APPROACHING_EXIT: 0, S.APPROACHING_ENTRY: 1, S.EXIT_SIGNAL: 2, S.ENTRY_TRIGGERED: 3, S.IN_TRADE: 4,
               S.SETUP_FORMING: 5, S.BREAKOUT_CONFIRMED: 6, S.CLOSED: 7, S.INVALIDATED: 8, S.WATCHING: 9}


def _iso(x):
    return x.isoformat() if isinstance(x, (dt.datetime, dt.date)) else x


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return _iso(o)


class ScannerService:
    def __init__(self, feed, cfg, lots=None, sectors=None, lots_note=""):
        self.feed, self.cfg = feed, cfg
        self.lots, self.lots_note = lots or {}, lots_note
        self.sectors = sectors or {}                       # symbol -> sector name
        self.lock = threading.RLock()
        self.states, self.seen, self.session = {}, {}, None
        self.active, self.last_done = set(), {}            # stocks that had a setup today keep their card; their last outcome
        self.alerts, self.seq = collections.deque(maxlen=300), 0
        self.err, self.err_at, self.ok_at, self.backoff = None, None, None, 0
        self.payload, self.version = None, 0
        self.settings_path = os.path.join(HERE, "settings.json")
        self.settings = {"risk": dict(cfg["risk"]), "bias": {"advances": None, "declines": None},
                         "watchlist": list(cfg["watchlist"]["manual_symbols"])}
        if os.path.exists(self.settings_path):
            try:
                with open(self.settings_path, encoding="utf-8") as f:
                    saved = json.load(f)
                self.settings["risk"].update(saved.get("risk") or {})
                self.settings["watchlist"] = saved.get("watchlist", self.settings["watchlist"])
                if saved.get("bias_date") == dt.date.today().isoformat():   # the pre-open numbers belong to one day
                    self.settings["bias"] = saved.get("bias") or self.settings["bias"]
            except Exception:
                pass
        self.journal = []

    # ---------------------------------------------------------------- loop
    def run(self):
        while True:
            t0 = time.time()
            try:
                self.feed.refresh()
                self.tick()
                self.err, self.backoff, self.ok_at = None, 0, time.time()
            except Exception as e:                          # keep the last good board; back off up to 5 minutes
                self.err, self.err_at = f"{type(e).__name__}: {e}", time.time()
                self.backoff = min(300, max(self.cfg["feed"]["poll_seconds"], self.backoff * 2 or 30))
                traceback.print_exc()
                self._build()
            wait = 2 if self.feed.kind == "replay" else (self.backoff or self.cfg["feed"]["poll_seconds"])
            time.sleep(max(0.5, wait - (time.time() - t0)))

    def tick(self):
        now = self.feed.now()
        with self.lock:
            for sym in self.feed.symbols():
                d = self.feed.data(sym)
                rows = d["candles"]
                if not rows:
                    continue
                sess = rows[-1]["t"].date()
                if sess != self.session:
                    self._new_day(sess)
                n_closed = self.feed.closed_upto(sym)
                closed = rows[:n_closed]
                ema = E.ema([r["c"] for r in closed], self.cfg["ema_period"])
                pd = d["daily"][-1] if d["daily"] else None
                if pd is None:
                    continue                                 # no previous day: no PDH/PDL, never guessed
                st = self.states.setdefault(sym, S.new_state())
                last = self.seen.get(sym)
                for i, c in enumerate(closed):
                    if c["t"].date() != sess or (last and c["t"] <= last):
                        continue
                    prev = closed[i - 1] if i and closed[i - 1]["t"].date() == sess else None
                    st, ev = S.on_candle(st, c, prev, ema[i], pd["h"], pd["l"], self.cfg)
                    self._log(sym, ev)
                    self.seen[sym] = c["t"]
                ema_last = next((v for v in reversed(ema) if v is not None), None)
                st, ev = S.on_price(st, d["ltp"], now, ema_last, self.cfg)
                self._log(sym, ev)
                self.states[sym] = st
                if st["state"] != S.WATCHING:
                    self.active.add(sym)
                if st["state"] in S.DONE:
                    self.last_done[sym] = {"state": st["state"], "side": st["side"], "note": st["note"], "at": _iso(st["since"]),
                                           "r": (st["result"] or {}).get("r")}
            self._build()

    def _new_day(self, day):
        self.session, self.states, self.seen = day, {}, {}
        self.active, self.last_done = set(), {}
        self.journal = self._load_journal(day)
        self.alerts.append({"seq": self._next(), "at": dt.datetime.now(dt.timezone.utc).isoformat(), "symbol": "",
                            "type": "info", "to": "", "side": None, "msg": f"Session {day.isoformat()}: scanner started"})

    def _next(self):
        self.seq += 1
        return self.seq

    def _log(self, sym, events):
        for e in events:
            self.alerts.append(_jsonable(dict(e, symbol=sym, seq=self._next())))

    # ---------------------------------------------------------------- user actions
    def action(self, a):
        kind = a.get("type")
        with self.lock:
            if kind == "bias":
                adv, dec = a.get("advances"), a.get("declines")
                self.settings["bias"] = {"advances": int(adv) if adv not in (None, "") else None,
                                         "declines": int(dec) if dec not in (None, "") else None}
            elif kind == "risk":
                r = self.settings["risk"]
                for k in ("capital", "daily_risk_pct", "trades_per_day"):
                    if a.get(k) not in (None, ""):
                        v = float(a[k])
                        if v <= 0:
                            raise ValueError(f"{k} must be more than 0")
                        r[k] = int(v) if k == "trades_per_day" else v
            elif kind == "watchlist":
                self.settings["watchlist"] = sorted({s.strip().upper() for s in a.get("symbols", []) if s.strip()})
            elif kind == "take":
                sym = a["symbol"]
                st = self.states.get(sym) or {}
                tr = st.get("trade") or {}
                setup = st.get("setup") or {}
                entry = float(a.get("entry") or tr.get("entry") or setup.get("entry"))
                stop = float(a.get("stop") or tr.get("stop") or setup.get("stop"))
                qty = int(a["qty"])
                if qty <= 0:
                    raise ValueError("quantity must be more than 0")
                self.journal.append({"symbol": sym, "side": st.get("side") or a.get("side"), "entry": entry, "stop": stop, "qty": qty,
                                     "at": self.feed.now().isoformat(), "exit": None, "pnl": None})
            elif kind == "exit":
                sym, price = a["symbol"], float(a["price"])
                open_ = [j for j in self.journal if j["symbol"] == sym and j["exit"] is None]
                if not open_:
                    raise ValueError(f"no open trade marked for {sym}")
                j = open_[-1]
                j["exit"], j["exit_at"] = price, self.feed.now().isoformat()
                j["pnl"] = round((price - j["entry"]) * j["qty"] * (1 if j["side"] == "long" else -1), 2)
            else:
                raise ValueError(f"unknown action {kind!r}")
            self._save()
            self._build()
            return {"ok": True}

    def _save(self):
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump({"risk": self.settings["risk"], "watchlist": self.settings["watchlist"], "bias": self.settings["bias"],
                       "bias_date": dt.date.today().isoformat()}, f, indent=1)
        if self.session:
            os.makedirs(os.path.join(HERE, "journal"), exist_ok=True)
            with open(os.path.join(HERE, "journal", self.session.isoformat() + ".json"), "w", encoding="utf-8") as f:
                json.dump(self.journal, f, indent=1)

    def _load_journal(self, day):
        p = os.path.join(HERE, "journal", day.isoformat() + ".json")
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    # ---------------------------------------------------------------- board data
    def risk_summary(self):
        r = self.settings["risk"]
        limit = E.daily_loss_limit(r["capital"], r["daily_risk_pct"])
        realised = round(sum(j["pnl"] for j in self.journal if j["pnl"] is not None), 2)
        open_risk = round(sum(abs(j["entry"] - j["stop"]) * j["qty"] for j in self.journal if j["exit"] is None), 2)
        loss = max(0.0, -realised)
        return {"capital": r["capital"], "daily_risk_pct": r["daily_risk_pct"], "trades_per_day": r["trades_per_day"],
                "loss_limit": limit, "realised_pnl": realised, "open_risk": open_risk, "loss_used": round(loss, 2),
                "used_pct": round(loss / limit * 100, 1) if limit else 0, "trades_taken": len(self.journal),
                "stop_trading": loss >= limit > 0, "journal": self.journal}

    def _build(self):
        now = self.feed.now()
        cfg, tick = self.cfg, self.cfg["tick_size"]
        bias_in = self.settings["bias"]
        bias = E.market_bias(bias_in.get("advances"), bias_in.get("declines"), cfg)
        risk = self.risk_summary()
        sector_moves = collections.defaultdict(list)
        datas = {}
        for sym in self.feed.symbols():
            d = self.feed.data(sym)
            datas[sym] = d
            if d["candles"] and d["daily"]:
                sector_moves[self.sectors.get(sym, "")].append(d["ltp"] / d["daily"][-1]["c"] - 1)
        sec_dir = {k: (sum(v) / len(v)) for k, v in sector_moves.items() if k and v}
        cards, watch = [], []
        for sym in self.feed.symbols():
            d = datas[sym]
            rows = d["candles"]
            if not rows:
                continue
            pd = d["daily"][-1] if d["daily"] else None
            st = self.states.get(sym) or S.new_state()
            ltp = d["ltp"]
            sess = rows[-1]["t"].date()
            today = [r for r in rows if r["t"].date() == sess]
            n_closed = self.feed.closed_upto(sym)
            closed = rows[:n_closed]
            ema_all = E.ema([r["c"] for r in closed], cfg["ema_period"])
            ema_by_t = {r["t"]: v for r, v in zip(closed, ema_all)}
            ema_last = next((v for v in reversed(ema_all) if v is not None), None)
            side = st["side"]
            lvl = st.get("trade") or st.get("setup") or {}
            sec = self.sectors.get(sym, "")
            sd = sec_dir.get(sec)
            wl = E.watchlist_check(d["daily"], cfg) if len(d["daily"]) > 20 else None
            card = {
                "symbol": sym, "sector": sec or None, "lot": self.lots.get(sym), "ltp": ltp,
                "chg": round((ltp / pd["c"] - 1) * 100, 2) if pd and ltp else None,
                "pdh": pd["h"] if pd else None, "pdl": pd["l"] if pd else None, "prev_close": pd["c"] if pd else None,
                "state": st["state"], "side": side, "note": st["note"], "since": _iso(st["since"]),
                "breakout": _jsonable(st["breakout"]), "setup": _jsonable(st["setup"]), "trade": _jsonable(st["trade"]),
                "exit": _jsonable(st["exit"]), "result": _jsonable(st["result"]), "square_off": st["square_off"],
                "ema": round(ema_last, 2) if ema_last is not None else None,
                "prox": {"entry": E.distance(ltp, lvl.get("entry"), tick) if st["state"] in S.ARMED else None,
                         "stop": E.distance(ltp, lvl.get("stop"), tick) if st["state"] in S.ARMED + S.TRADE else None,
                         "ema": E.distance(ltp, ema_last, tick) if st["state"] in S.TRADE else None},
                "flags": {"oi_leader": None,                 # Yahoo has no open-interest data: unknown, not guessed
                          "sector_same_dir": (sd > 0) == (side == "long") if side and sd is not None and sd != 0 else None,
                          "sector_move_pct": round(sd * 100, 2) if sd is not None else None,
                          "bias_match": (bias == side) if side and bias in ("long", "short") else None},
                "watchlist": wl, "manual_watch": sym in self.settings["watchlist"],
                "size": E.position_size(risk["capital"], risk["daily_risk_pct"], risk["trades_per_day"], lvl["entry"], lvl["stop"],
                                        self.lots.get(sym) or 1) if lvl.get("entry") else None,
                "taken": [j for j in self.journal if j["symbol"] == sym],
                "last": self.last_done.get(sym) if st["state"] == S.WATCHING else None,   # watching again after a setup ended
                "candles": [[r["t"].strftime("%H:%M"), r["o"], r["h"], r["l"], r["c"], r["v"],
                             round(ema_by_t[r["t"]], 2) if ema_by_t.get(r["t"]) is not None else None, i < len(today) - (len(rows) - n_closed)]
                            for i, r in enumerate(today)],
            }
            # a stock keeps its card for the rest of the day once it had a setup, so the board never shrinks under the reader
            keep = st["state"] != S.WATCHING or card["manual_watch"] or sym in self.active
            (cards if keep else watch).append(card)
            if st["state"] == S.WATCHING and (card["manual_watch"] or (wl and wl["candidate"])):
                card["watch_reason"] = "manual" if card["manual_watch"] else "range-bound, not extended"
        cards.sort(key=lambda c: (STATE_ORDER.get(c["state"], 9), c["symbol"]))
        lite = sorted(({"symbol": c["symbol"], "sector": c["sector"], "ltp": c["ltp"], "chg": c["chg"], "pdh": c["pdh"], "pdl": c["pdl"],
                        "to_pdh": E.distance(c["ltp"], c["pdh"], tick), "to_pdl": E.distance(c["ltp"], c["pdl"], tick),
                        "candidate": bool(c["watchlist"] and c["watchlist"]["candidate"])} for c in watch),
                      key=lambda c: min(abs((c["to_pdh"] or {}).get("pct", 99)), abs((c["to_pdl"] or {}).get("pct", 99))))
        info = self.feed.info()
        status = E.market_status(now, cfg, trading_day=self.session == now.date() or info["kind"] == "replay")
        self.version += 1
        self.payload = {
            "version": self.version, "now": now.isoformat(), "session": _iso(self.session), "market": status, "feed": info,
            "error": self.err, "bias": {"value": bias, **bias_in}, "risk": risk,
            "lots_note": self.lots_note, "oi_note": "Open interest is not available from Yahoo, so OI leaders and OI tags are not shown.",
            "config": {k: cfg[k] for k in ("approach_entry_pct", "approach_entry_ticks", "approach_exit_pct", "approach_exit_ticks",
                                            "targets_r", "square_off_warning", "ema_period", "skip_first_candles", "tick_size")} | {"ui": cfg["ui"]},
            "watchlist_manual": self.settings["watchlist"],
            "cards": cards, "watching": lite, "alerts": list(self.alerts)[-150:], "seq": self.seq,
        }

    def board(self, since=None):
        with self.lock:
            if self.payload is None:
                return {"loading": True, "error": self.err, "feed": self.feed.info()}
            if since is not None and str(since) == str(self.version):
                return {"unchanged": True, "version": self.version}
            return self.payload


def start(feed, cfg, lots=None, sectors=None, lots_note=""):
    svc = ScannerService(feed, cfg, lots, sectors, lots_note)
    threading.Thread(target=svc.run, daemon=True, name="intraday-scanner").start()
    return svc

"""Per-stock state machine for the Intraday Scanner. Pure: every function takes a state dict and returns a new one
plus a list of events; nothing is changed in place.

  WATCHING -> BREAKOUT_CONFIRMED -> SETUP_FORMING (armed) <-> APPROACHING_ENTRY -> ENTRY_TRIGGERED -> IN_TRADE
           <-> APPROACHING_EXIT -> EXIT_SIGNAL -> CLOSED          (any setup state -> INVALIDATED)

on_candle() takes each CLOSED 5-minute candle in order (setup conditions use closed candles only).
on_price()  takes the live price (entry triggers, stops, exits, proximity and the clock).
"""
import copy
import datetime as dt

from . import engine as E

WATCHING = "WATCHING"
BREAKOUT_CONFIRMED = "BREAKOUT_CONFIRMED"
SETUP_FORMING = "SETUP_FORMING"
APPROACHING_ENTRY = "APPROACHING_ENTRY"
ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
IN_TRADE = "IN_TRADE"
APPROACHING_EXIT = "APPROACHING_EXIT"
EXIT_SIGNAL = "EXIT_SIGNAL"
CLOSED = "CLOSED"
INVALIDATED = "INVALIDATED"

ARMED = (SETUP_FORMING, APPROACHING_ENTRY)
SETUP = (BREAKOUT_CONFIRMED,) + ARMED
TRADE = (ENTRY_TRIGGERED, IN_TRADE, APPROACHING_EXIT, EXIT_SIGNAL)
DONE = (CLOSED, INVALIDATED)


def new_state():
    return {"state": WATCHING, "side": None, "since": None, "breakout": None, "setup": None, "trade": None,
            "exit": None, "result": None, "note": "", "trades": 0, "square_off": False}


def _go(s, to, at, msg, events, kind="state"):
    if s["state"] != to:
        events.append({"type": kind, "from": s["state"], "to": to, "at": at, "side": s["side"], "msg": msg})
        s["state"], s["since"] = to, at
    s["note"] = msg


def _close(s, at, price, why, events):
    tr = s["trade"]
    s["result"] = {"exit": price, "at": at, "why": why, "r": E.r_multiple(tr["entry"], tr["stop"], price, s["side"])}
    _go(s, CLOSED, at, why, events)


def _reset(s):
    keep = s["trades"]
    s.clear()
    s.update(new_state(), trades=keep)


def on_candle(state, c, prev, ema_value, pdh, pdl, cfg):
    """Feed one closed candle. prev = the candle before it (same session or None); ema_value = 10 EMA at this candle."""
    s, ev, t = copy.deepcopy(state), [], c["t"]
    tick = cfg["tick_size"]
    end = t + dt.timedelta(minutes=cfg["candle_minutes"])
    sq = dt.datetime.combine(t.date(), E.hhmm(cfg["square_off_warning"]), tzinfo=t.tzinfo)

    if s["state"] in DONE:                                   # the old setup is over; look for a new one if allowed
        if not cfg["allow_reentry"] or s["trades"] >= cfg["max_trades_per_stock"]:
            return s, ev
        _reset(s)
    if s["state"] == ENTRY_TRIGGERED:
        s["state"] = IN_TRADE

    if s["state"] == WATCHING:
        if not E.is_setup_time(t, cfg) or t >= sq:
            return s, ev
        side = E.breakout_side(c, pdh, pdl)
        if side:
            s["side"] = side
            level = pdh if side == "long" else pdl
            s["breakout"] = {"t": t, "close": c["c"], "level": level}
            _go(s, BREAKOUT_CONFIRMED, end, f"Closed {'above PDH' if side == 'long' else 'below PDL'} {level:.2f} at {c['c']:.2f}", ev)
        return s, ev

    side = s["side"]
    if s["state"] in SETUP:
        st = s["setup"]
        if s["state"] in ARMED:                               # the live price may have missed a move inside this candle
            hit_e = c["h"] > st["entry"] if side == "long" else c["l"] < st["entry"]
            hit_s = E.hit_stop(c["l"] if side == "long" else c["h"], side, st["stop"])
            if hit_e and hit_s:
                _go(s, INVALIDATED, end, "Entry and stop were both crossed inside one candle (order unknown)", ev)
                return s, ev
            if hit_s:
                _go(s, INVALIDATED, end, f"Broke {'below' if side == 'long' else 'above'} the setup candle before entry", ev)
                return s, ev
            if hit_e:
                _trigger(s, st["entry"], t, ev, cfg, note="Triggered inside the candle (seen at its close)")
                return s, ev
        if not E.breakout_holds(c, side, pdh, pdl):
            _go(s, INVALIDATED, end, f"Closed back {'below PDH' if side == 'long' else 'above PDL'}", ev)
            return s, ev
        if prev is not None and E.is_setup_candle(c, prev, side) and t < sq:
            entry, stop = E.entry_stop(c, side, tick, cfg["entry_buffer_ticks"])
            s["setup"] = {"t": t, "h": c["h"], "l": c["l"], "v": c["v"], "prev_v": prev["v"], "entry": entry, "stop": stop,
                          "targets": E.targets(entry, stop, side, cfg["targets_r"])}
            colour = "red" if side == "long" else "green"
            msg = f"Armed: {colour} inside candle, lower volume. Entry {'above' if side == 'long' else 'below'} {entry:.2f}, stop {stop:.2f}"
            if s["state"] in ARMED:
                s["note"] = msg                               # a newer inside candle replaces the levels
                ev.append({"type": "rearm", "from": s["state"], "to": s["state"], "at": end, "side": side, "msg": msg})
            else:
                _go(s, SETUP_FORMING, end, msg, ev)
        return s, ev

    if s["state"] in (IN_TRADE, APPROACHING_EXIT, EXIT_SIGNAL):
        tr = s["trade"]
        if s["state"] == EXIT_SIGNAL:
            lvl = s["exit"]["level"]
            if (c["l"] < lvl) if side == "long" else (c["h"] > lvl):
                _close(s, end, lvl, "Exit: broke the signal candle's " + ("low" if side == "long" else "high"), ev)
                return s, ev
        if E.hit_stop(c["l"] if side == "long" else c["h"], side, tr["stop"]):
            _close(s, end, tr["stop"], "Stop-loss hit", ev)
            return s, ev
        if ema_value is not None:
            below = c["c"] < ema_value if side == "long" else c["c"] > ema_value
            if below:
                lvl = c["l"] if side == "long" else c["h"]
                s["exit"] = {"t": t, "level": lvl, "ema": round(ema_value, 2)}
                _go(s, EXIT_SIGNAL, end, f"Closed {'below' if side == 'long' else 'above'} the 10 EMA {ema_value:.2f}: exit "
                                         f"{'below' if side == 'long' else 'above'} {lvl:.2f}", ev)
            elif s["state"] == EXIT_SIGNAL:
                s["exit"] = None
                _go(s, IN_TRADE, end, "Closed back on the right side of the 10 EMA: exit signal cancelled", ev)
    return s, ev


def _trigger(s, price, at, ev, cfg, note="Entry triggered"):
    st = s["setup"]
    s["trade"] = {"entry": st["entry"], "stop": st["stop"], "at": at, "price": price, "targets": st["targets"]}
    s["trades"] += 1
    _go(s, ENTRY_TRIGGERED, at, f"{note}: {'long above' if s['side'] == 'long' else 'short below'} {st['entry']:.2f}", ev)


def on_price(state, ltp, now, ema_live, cfg):
    """Feed the live price. ema_live = the latest 10 EMA (from closed candles)."""
    s, ev = copy.deepcopy(state), []
    if ltp is None:
        return s, ev
    side, tick = s["side"], cfg["tick_size"]
    sq = dt.datetime.combine(now.date(), E.hhmm(cfg["square_off_warning"]), tzinfo=now.tzinfo)
    close = dt.datetime.combine(now.date(), E.hhmm(cfg["market_close"]), tzinfo=now.tzinfo)

    if s["state"] == ENTRY_TRIGGERED:
        _go(s, IN_TRADE, now, "In trade", ev)

    if s["state"] in (BREAKOUT_CONFIRMED,) + ARMED and now >= sq:
        _go(s, INVALIDATED, now, f"Time expired: no entry by {cfg['square_off_warning']}", ev)
        return s, ev

    if s["state"] in ARMED:
        st = s["setup"]
        if E.hit_stop(ltp, side, st["stop"]):
            _go(s, INVALIDATED, now, f"Price {ltp:.2f} broke the setup candle's {'low' if side == 'long' else 'high'} before entry", ev)
        elif E.crossed_entry(ltp, side, st["entry"]):
            _trigger(s, ltp, now, ev, cfg)
        elif E.is_near(ltp, st["entry"], cfg["approach_entry_pct"], cfg["approach_entry_ticks"], tick):
            _go(s, APPROACHING_ENTRY, now, f"Price {ltp:.2f} near entry {st['entry']:.2f}", ev)
        else:
            _go(s, SETUP_FORMING, now, s["note"] if s["state"] == SETUP_FORMING else f"Armed, entry {st['entry']:.2f}", ev)
        return s, ev

    if s["state"] in (IN_TRADE, APPROACHING_EXIT, EXIT_SIGNAL):
        tr = s["trade"]
        if now >= close:
            _close(s, now, ltp, "Market closed: squared off", ev)
            return s, ev
        if now >= sq and not s["square_off"]:
            s["square_off"] = True
            ev.append({"type": "square_off", "from": s["state"], "to": s["state"], "at": now, "side": side,
                       "msg": f"{cfg['square_off_warning']}: square off intraday positions"})
        if E.hit_stop(ltp, side, tr["stop"]):
            _close(s, now, tr["stop"], "Stop-loss hit", ev)
            return s, ev
        if s["state"] == EXIT_SIGNAL:
            lvl = s["exit"]["level"]
            if (ltp < lvl) if side == "long" else (ltp > lvl):
                _close(s, now, lvl, "Exit: broke the signal candle's " + ("low" if side == "long" else "high"), ev)
            return s, ev
        near = [name for name, lvl in [("10 EMA", ema_live), ("stop", tr["stop"])] + [(f"1:{t['r']} target", t["price"]) for t in tr["targets"]]
                if E.is_near(ltp, lvl, cfg["approach_exit_pct"], cfg["approach_exit_ticks"], tick)]
        if near:
            _go(s, APPROACHING_EXIT, now, f"Price {ltp:.2f} near the {', '.join(near)}", ev)
        else:
            _go(s, IN_TRADE, now, "In trade", ev)
    return s, ev

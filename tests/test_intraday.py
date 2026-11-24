"""Unit tests for the Intraday Scanner engine, state machine and aggregator.  Run:  python -m unittest discover -s tests"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intraday import load_config, engine as E, state as S              # noqa: E402
from intraday.aggregator import CandleAggregator, aggregate           # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = dt.date(2026, 11, 18)                                            # a Wednesday
CFG = load_config()


def at(hm, day=DAY):
    h, m = (int(x) for x in hm.split(":"))
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


def C(hm, o, h, l, c, v=1000):
    return {"t": at(hm), "o": o, "h": h, "l": l, "c": c, "v": v}


# ------------------------------------------------------------------ aggregator
class TestAggregator(unittest.TestCase):
    def test_ticks_build_5min_candles(self):
        a = CandleAggregator()
        for hm, s, p, q in [("09:15", 5, 100, 10), ("09:16", 0, 102, 5), ("09:18", 30, 99, 7), ("09:19", 59, 101, 3), ("09:20", 0, 103, 1)]:
            a.add_tick(at(hm) + dt.timedelta(seconds=s), p, q)
        c = a.candles()
        self.assertEqual(len(c), 2)
        self.assertEqual((c[0]["t"], c[0]["o"], c[0]["h"], c[0]["l"], c[0]["c"], c[0]["v"]), (at("09:15"), 100, 102, 99, 101, 25))
        self.assertEqual((c[1]["t"], c[1]["o"], c[1]["v"]), (at("09:20"), 103, 1))

    def test_outside_market_hours_ignored(self):
        a = CandleAggregator()
        a.add_tick(at("09:14") + dt.timedelta(seconds=59), 100)
        a.add_tick(at("15:30"), 100)
        a.add_tick(at("15:29") + dt.timedelta(seconds=59), 101)
        self.assertEqual([r["t"] for r in a.candles()], [at("15:25")])
        self.assertEqual(a.dropped, 2)

    def test_out_of_order_and_bad_ticks_dropped(self):
        a = CandleAggregator()
        a.add_tick(at("09:21"), 100)
        a.add_tick(at("09:16"), 50)              # older candle: never rewrites it
        a.add_tick(at("09:22"), 0)               # bad price
        self.assertEqual(len(a.candles()), 1)
        self.assertEqual(a.candles()[0]["l"], 100)
        self.assertEqual(a.dropped, 2)

    def test_gaps_are_not_filled(self):
        a = CandleAggregator()
        a.add_tick(at("09:15"), 100)
        a.add_tick(at("09:31"), 101)
        self.assertEqual([r["t"] for r in a.candles()], [at("09:15"), at("09:30")])

    def test_closed_and_forming(self):
        a = CandleAggregator()
        a.add_tick(at("09:15"), 100)
        a.add_tick(at("09:21"), 101)
        now = at("09:22")
        self.assertEqual([r["t"] for r in a.closed(now)], [at("09:15")])
        self.assertEqual(a.forming(now)["t"], at("09:20"))
        self.assertEqual(len(a.closed(at("09:25"))), 2)

    def test_one_minute_bars(self):
        bars = [{"t": at("09:%02d" % m), "o": 100 + m, "h": 101 + m, "l": 99 + m, "c": 100.5 + m, "v": 10} for m in range(15, 25)]
        c = aggregate(bars)
        self.assertEqual(len(c), 2)
        self.assertEqual((c[0]["o"], c[0]["h"], c[0]["l"], c[0]["c"], c[0]["v"]), (115, 120, 114, 119.5, 50))


# ------------------------------------------------------------------ indicators and candle rules
class TestEngine(unittest.TestCase):
    def test_ema_10(self):
        vals = list(range(1, 15))
        e = E.ema(vals, 10)
        self.assertIsNone(e[8])
        self.assertAlmostEqual(e[9], 5.5)                      # seed = average of the first 10
        self.assertAlmostEqual(e[10], 11 * 2 / 11 + 5.5 * 9 / 11)
        self.assertEqual(len(e), len(vals))
        self.assertEqual(E.ema([5] * 12, 10)[-1], 5)

    def test_inside_candle(self):
        prev = C("09:30", 100, 105, 95, 104, 1000)
        self.assertTrue(E.is_inside(C("09:35", 103, 104, 96, 101), prev))
        self.assertTrue(E.is_inside(C("09:35", 103, 105, 95, 101), prev))     # equal high and low still inside
        self.assertFalse(E.is_inside(C("09:35", 103, 105.05, 96, 101), prev))
        self.assertFalse(E.is_inside(C("09:35", 103, 104, 94.95, 101), prev))

    def test_setup_candle_long_and_short(self):
        prev = C("09:30", 100, 105, 95, 104, 1000)
        self.assertTrue(E.is_setup_candle(C("09:35", 103, 104, 96, 101, 999), prev, "long"))
        self.assertFalse(E.is_setup_candle(C("09:35", 103, 104, 96, 101, 1000), prev, "long"))   # equal volume is not lower
        self.assertFalse(E.is_setup_candle(C("09:35", 101, 104, 96, 103, 500), prev, "long"))   # green: not a long setup
        self.assertFalse(E.is_setup_candle(C("09:35", 101, 104, 96, 101, 500), prev, "long"))   # doji: neither colour
        self.assertTrue(E.is_setup_candle(C("09:35", 101, 104, 96, 103, 500), prev, "short"))
        self.assertFalse(E.is_setup_candle(C("09:35", 103, 104, 96, 101, 500), prev, "short"))
        self.assertFalse(E.is_setup_candle(C("09:35", 103, 104, 96, 101, 0), prev, "long"))          # missing volume
        self.assertFalse(E.is_setup_candle(C("09:35", 103, 104, 96, 101, 500), dict(prev, v=0), "long"))

    def test_breakout(self):
        self.assertEqual(E.breakout_side(C("09:30", 99, 101, 98, 100.05), 100, 90), "long")
        self.assertIsNone(E.breakout_side(C("09:30", 99, 101, 98, 100), 100, 90))             # close AT PDH is not above
        self.assertEqual(E.breakout_side(C("09:30", 91, 92, 89, 89.95), 100, 90), "short")
        self.assertIsNone(E.breakout_side(C("09:30", 91, 92, 89, 90), 100, 90))
        self.assertIsNone(E.breakout_side(C("09:30", 95, 101, 89, 95), 100, 90))              # wicks do not count
        self.assertTrue(E.breakout_holds(C("09:35", 101, 102, 99, 100), "long", 100, 90))
        self.assertFalse(E.breakout_holds(C("09:35", 101, 102, 99, 99.95), "long", 100, 90))
        self.assertFalse(E.breakout_holds(C("09:35", 89, 91, 88, 90.05), "short", 100, 90))

    def test_setup_time_skips_first_three_candles(self):
        self.assertFalse(E.is_setup_time(at("09:15"), CFG))
        self.assertFalse(E.is_setup_time(at("09:25"), CFG))
        self.assertTrue(E.is_setup_time(at("09:30"), CFG))

    def test_entry_stop_targets(self):
        c = C("09:40", 103, 104, 96, 101)
        self.assertEqual(E.entry_stop(c, "long"), (104, 96))
        self.assertEqual(E.entry_stop(c, "short"), (96, 104))
        self.assertEqual(E.entry_stop(c, "long", 0.05, 2), (104.1, 95.9))
        self.assertEqual([t["price"] for t in E.targets(104, 96, "long")], [120, 128])
        self.assertEqual([t["price"] for t in E.targets(96, 104, "short")], [80, 72])
        self.assertTrue(E.crossed_entry(104.05, "long", 104))
        self.assertFalse(E.crossed_entry(104, "long", 104))                               # must go beyond
        self.assertTrue(E.hit_stop(95.95, "long", 96))
        self.assertEqual(E.r_multiple(104, 96, 120, "long"), 2)
        self.assertEqual(E.r_multiple(96, 104, 100, "short"), -0.5)

    def test_distance_and_near(self):
        d = E.distance(100, 100.5)
        self.assertEqual((d["pct"], d["rs"], d["ticks"]), (0.5, 0.5, 10))
        self.assertTrue(E.is_near(100, 100.25, 0.25, 0))
        self.assertTrue(E.is_near(1000, 1000.5, 0.01, 10))                                # 10 ticks wins over 0.01 %
        self.assertFalse(E.is_near(100, 101, 0.25, 10))

    def test_position_size(self):
        p = E.position_size(500000, 1, 2, 104, 96, 1)                                     # 2,500 per trade / 8
        self.assertEqual((p["budget"], p["qty"], p["risk"]), (2500, 312, 2496))
        p = E.position_size(500000, 1, 2, 104, 96, 125)                                   # whole lots: 312 -> 250
        self.assertEqual((p["qty"], p["lots"], p["risk"]), (250, 2, 2000))
        p = E.position_size(100000, 1, 3, 104, 96, 500)                                   # one lot is too much
        self.assertEqual((p["qty"], p["lots"]), (0, 0))
        self.assertIn("more than", p["note"])
        self.assertEqual(E.position_size(100000, 1, 1, 100, 100)["qty"], 0)
        self.assertEqual(E.daily_loss_limit(500000, 1), 5000)

    def test_market_bias_and_status(self):
        self.assertEqual(E.market_bias(1500, 500, CFG), "long")
        self.assertEqual(E.market_bias(400, 1200, CFG), "short")
        self.assertEqual(E.market_bias(900, 1000, CFG), "neutral")
        self.assertIsNone(E.market_bias(None, 10, CFG))
        self.assertEqual(E.market_status(at("09:05"), CFG), "pre-open")
        self.assertEqual(E.market_status(at("09:15"), CFG), "open")
        self.assertEqual(E.market_status(at("15:30"), CFG), "closed")
        self.assertEqual(E.market_status(at("10:00", dt.date(2026, 11, 21)), CFG), "closed")   # Saturday

    def test_watchlist(self):
        daily = [{"h": 110, "l": 100, "c": 105} for _ in range(20)] + [{"h": 107, "l": 103, "c": 105}]
        w = E.watchlist_check(daily, CFG)
        self.assertTrue(w["range_bound"] and w["not_extended"] and w["candidate"])
        daily[-1] = {"h": 125, "l": 103, "c": 120}
        w = E.watchlist_check(daily, CFG)
        self.assertFalse(w["range_bound"] or w["not_extended"])
        self.assertIsNone(E.watchlist_check(daily[:5], CFG))


# ------------------------------------------------------------------ state machine
PDH, PDL = 100.0, 90.0


def feed(state, candles, emas=None):
    """Run closed candles through on_candle; returns (state, all events)."""
    evs = []
    for i, c in enumerate(candles):
        prev = candles[i - 1] if i else None
        state, ev = S.on_candle(state, c, prev, (emas or {}).get(i), PDH, PDL, CFG)
        evs += ev
    return state, evs


LONG = [C("09:30", 99, 101, 98.5, 100.6, 2000),        # 0 breakout: closes above PDH 100
        C("09:35", 100.5, 100.9, 99.2, 100.2, 1200)]   # 1 red, inside 0, lower volume -> armed: entry 100.9 stop 99.2
SHORT = [C("09:30", 91, 91.5, 89, 89.4, 2000),          # closes below PDL 90
         C("09:35", 89.5, 90.8, 89.2, 89.9, 1500)]      # green inside, lower volume -> entry 89.2 stop 90.8


class TestStateMachine(unittest.TestCase):
    def test_watching_to_breakout(self):
        s, ev = feed(S.new_state(), LONG[:1])
        self.assertEqual((s["state"], s["side"]), (S.BREAKOUT_CONFIRMED, "long"))
        self.assertEqual(ev[0]["to"], S.BREAKOUT_CONFIRMED)

    def test_first_three_candles_ignored(self):
        s, _ = feed(S.new_state(), [C("09:25", 99, 101, 98.5, 100.6)])
        self.assertEqual(s["state"], S.WATCHING)

    def test_no_new_breakouts_after_square_off_time(self):
        s, _ = feed(S.new_state(), [C("15:15", 99, 101, 98.5, 100.6)])
        self.assertEqual(s["state"], S.WATCHING)

    def test_breakout_to_setup_forming(self):
        s, _ = feed(S.new_state(), LONG)
        self.assertEqual(s["state"], S.SETUP_FORMING)
        self.assertEqual((s["setup"]["entry"], s["setup"]["stop"]), (100.9, 99.2))
        self.assertEqual([t["r"] for t in s["setup"]["targets"]], [2, 3])

    def test_short_mirror(self):
        s, _ = feed(S.new_state(), SHORT)
        self.assertEqual((s["state"], s["side"], s["setup"]["entry"], s["setup"]["stop"]), (S.SETUP_FORMING, "short", 89.2, 90.8))
        s, ev = S.on_price(s, 89.15, at("09:41"), None, CFG)
        self.assertEqual(s["state"], S.ENTRY_TRIGGERED)

    def test_not_inside_or_equal_volume_does_not_arm(self):
        s, _ = feed(S.new_state(), [LONG[0], C("09:35", 100.5, 101.2, 99.2, 100.2, 1200)])   # high above prior high
        self.assertEqual(s["state"], S.BREAKOUT_CONFIRMED)
        s, _ = feed(S.new_state(), [LONG[0], C("09:35", 100.5, 100.9, 99.2, 100.2, 2000)])   # equal volume
        self.assertEqual(s["state"], S.BREAKOUT_CONFIRMED)

    def test_inside_candle_later_after_breakout(self):
        s, _ = feed(S.new_state(), [LONG[0], C("09:35", 100.6, 101.5, 100.4, 101.4, 2500), C("09:40", 101.3, 101.4, 100.6, 100.8, 900)])
        self.assertEqual((s["state"], s["setup"]["entry"]), (S.SETUP_FORMING, 101.4))

    def test_breakout_invalidated_by_close_back_below_pdh(self):
        s, ev = feed(S.new_state(), [LONG[0], C("09:35", 100.5, 100.9, 99.2, 99.9, 3000)])
        self.assertEqual(s["state"], S.INVALIDATED)
        self.assertIn("below PDH", s["note"])

    def test_armed_to_approaching_entry_and_back(self):
        s, _ = feed(S.new_state(), LONG)
        s, ev = S.on_price(s, 100.85, at("09:41"), None, CFG)
        self.assertEqual(s["state"], S.APPROACHING_ENTRY)
        s, ev = S.on_price(s, 99.9, at("09:42"), None, CFG)
        self.assertEqual(s["state"], S.SETUP_FORMING)

    def test_entry_trigger_then_in_trade(self):
        s, _ = feed(S.new_state(), LONG)
        s, ev = S.on_price(s, 100.95, at("09:41"), None, CFG)
        self.assertEqual(s["state"], S.ENTRY_TRIGGERED)
        self.assertEqual((s["trade"]["entry"], s["trade"]["stop"], s["trades"]), (100.9, 99.2, 1))
        s, ev = S.on_price(s, 101.5, at("09:42"), 99.5, CFG)
        self.assertEqual(s["state"], S.IN_TRADE)

    def test_entry_must_go_beyond(self):
        s, _ = feed(S.new_state(), LONG)
        s, _ = S.on_price(s, 100.9, at("09:41"), None, CFG)
        self.assertEqual(s["state"], S.APPROACHING_ENTRY)

    def test_stop_before_entry_invalidates(self):
        s, _ = feed(S.new_state(), LONG)
        s, _ = S.on_price(s, 99.15, at("09:41"), None, CFG)
        self.assertEqual(s["state"], S.INVALIDATED)

    def test_closed_candle_catches_missed_trigger_and_ambiguity(self):
        s, _ = feed(S.new_state(), LONG + [C("09:40", 100.3, 101.2, 100.1, 101.0, 1100)])
        self.assertEqual(s["state"], S.ENTRY_TRIGGERED)
        s, _ = feed(S.new_state(), LONG + [C("09:40", 100.3, 101.2, 99.0, 100.5, 1100)])
        self.assertEqual(s["state"], S.INVALIDATED)
        self.assertIn("both", s["note"])

    def test_newer_inside_candle_rearms(self):
        s, ev = feed(S.new_state(), LONG + [C("09:40", 100.6, 100.8, 99.5, 100.4, 800)])
        self.assertEqual((s["state"], s["setup"]["entry"], s["setup"]["stop"]), (S.SETUP_FORMING, 100.8, 99.5))
        self.assertEqual(ev[-1]["type"], "rearm")

    def test_time_expired(self):
        s, _ = feed(S.new_state(), LONG)
        s, _ = S.on_price(s, 100.0, at("15:15"), None, CFG)
        self.assertEqual(s["state"], S.INVALIDATED)
        self.assertIn("Time expired", s["note"])

    def _in_trade(self):
        s, _ = feed(S.new_state(), LONG)
        s, _ = S.on_price(s, 101.0, at("09:41"), None, CFG)
        s, _ = S.on_price(s, 102.0, at("09:42"), 99.0, CFG)
        self.assertEqual(s["state"], S.IN_TRADE)
        return s

    def test_approaching_exit_near_ema_stop_or_target(self):
        s = self._in_trade()
        s2, _ = S.on_price(s, 102.0, at("09:43"), 101.9, CFG)
        self.assertEqual(s2["state"], S.APPROACHING_EXIT)
        self.assertIn("10 EMA", s2["note"])
        s2, _ = S.on_price(s, 99.3, at("09:43"), 95, CFG)
        self.assertIn("stop", s2["note"])
        s2, _ = S.on_price(s, 104.2, at("09:43"), 95, CFG)                                # 1:2 target = 104.3
        self.assertIn("1:2 target", s2["note"])

    def test_stop_hit_closes(self):
        s = self._in_trade()
        s, ev = S.on_price(s, 99.1, at("09:50"), 99.0, CFG)
        self.assertEqual((s["state"], s["result"]["exit"], s["result"]["r"]), (S.CLOSED, 99.2, -1))

    def test_ema_exit_signal_then_close(self):
        s = self._in_trade()
        c = C("10:00", 102, 102.2, 101.1, 101.3, 900)
        s, ev = S.on_candle(s, c, None, 101.5, PDH, PDL, CFG)                            # closed below the 10 EMA
        self.assertEqual((s["state"], s["exit"]["level"]), (S.EXIT_SIGNAL, 101.1))
        s, _ = S.on_price(s, 101.2, at("10:06"), 101.5, CFG)
        self.assertEqual(s["state"], S.EXIT_SIGNAL)
        s, _ = S.on_price(s, 101.05, at("10:07"), 101.5, CFG)
        self.assertEqual((s["state"], s["result"]["exit"]), (S.CLOSED, 101.1))

    def test_exit_signal_cancelled_by_close_back_above_ema(self):
        s = self._in_trade()
        s, _ = S.on_candle(s, C("10:00", 102, 102.2, 101.1, 101.3), None, 101.5, PDH, PDL, CFG)
        s, _ = S.on_candle(s, C("10:05", 101.3, 102.0, 101.2, 101.9), None, 101.55, PDH, PDL, CFG)
        self.assertEqual(s["state"], S.IN_TRADE)
        self.assertIsNone(s["exit"])

    def test_exit_signal_candle_breaks_low(self):
        s = self._in_trade()
        s, _ = S.on_candle(s, C("10:00", 102, 102.2, 101.1, 101.3), None, 101.5, PDH, PDL, CFG)
        s, _ = S.on_candle(s, C("10:05", 101.3, 101.4, 100.9, 101.0), None, 101.45, PDH, PDL, CFG)
        self.assertEqual((s["state"], s["result"]["exit"]), (S.CLOSED, 101.1))

    def test_square_off_warning_and_market_close(self):
        s = self._in_trade()
        s, ev = S.on_price(s, 102.0, at("15:15"), 99.0, CFG)
        self.assertTrue(s["square_off"])
        self.assertEqual(ev[0]["type"], "square_off")
        s, ev = S.on_price(s, 102.0, at("15:16"), 99.0, CFG)
        self.assertFalse(any(e["type"] == "square_off" for e in ev))                      # warned once
        s, _ = S.on_price(s, 102.4, at("15:30"), 99.0, CFG)
        self.assertEqual((s["state"], s["result"]["exit"]), (S.CLOSED, 102.4))

    def test_reentry_after_close(self):
        s, _ = S.on_price(self._in_trade(), 99.0, at("09:50"), 99.0, CFG)
        self.assertEqual(s["state"], S.CLOSED)
        s, _ = S.on_candle(s, C("10:30", 100, 101, 99.5, 100.7), None, None, PDH, PDL, CFG)
        self.assertEqual((s["state"], s["trades"]), (S.BREAKOUT_CONFIRMED, 1))

    def test_no_reentry_past_limit(self):
        cfg = dict(CFG, max_trades_per_stock=1)
        s, _ = S.on_price(self._in_trade(), 99.0, at("09:50"), 99.0, cfg)
        s, _ = S.on_candle(s, C("10:30", 100, 101, 99.5, 100.7), None, None, PDH, PDL, cfg)
        self.assertEqual(s["state"], S.CLOSED)

    def test_pure_functions_do_not_mutate_input(self):
        s0 = S.new_state()
        feed(s0, LONG)
        self.assertEqual(s0, S.new_state())


if __name__ == "__main__":
    unittest.main()

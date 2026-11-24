"""Replay test: the scripted MOCK market through the real feed + service, on a controlled clock."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intraday import load_config, feeds, service, state as S          # noqa: E402

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DAY = dt.date(2026, 11, 18)


class Clock:
    def __init__(self, t):
        self.t = t


def run_day(feed, clock, cfg, step_min=1):
    svc = service.ScannerService(feed, cfg)
    svc.settings_path = os.devnull                                     # never write settings from a test
    seen = {}
    end = dt.datetime.combine(DAY, dt.time(15, 31), tzinfo=IST)
    while clock.t <= end:
        svc.tick()
        for sym, st in svc.states.items():
            if not seen.get(sym) or seen[sym][-1] != st["state"]:
                seen.setdefault(sym, []).append(st["state"])
        clock.t += dt.timedelta(minutes=step_min)
    return svc, seen


class TestMockReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock = Clock(dt.datetime.combine(DAY, dt.time(9, 15), tzinfo=IST))
        cfg = load_config()
        cfg["allow_reentry"] = False                                    # one setup per stock: final states are predictable
        feed = feeds.mock_replay(cfg, day=DAY)
        feed.now = lambda: cls.clock.t
        cls.svc, cls.seen = run_day(feed, cls.clock, cfg)

    def transitions(self, sym):
        """Every state the alert log recorded for a stock (catches states that last less than one clock step)."""
        return [a["to"] for a in self.svc.alerts if a.get("symbol") == sym]

    def final(self, sym):
        return self.svc.states[sym]

    def test_long_winner_walks_every_trade_state(self):
        path = self.transitions("MOCK01")
        for st in (S.BREAKOUT_CONFIRMED, S.SETUP_FORMING, S.ENTRY_TRIGGERED, S.IN_TRADE, S.EXIT_SIGNAL, S.CLOSED):
            self.assertIn(st, path)
        f = self.final("MOCK01")
        self.assertEqual(f["side"], "long")
        self.assertGreater(f["result"]["r"], 0)

    def test_short_winner(self):
        f = self.final("MOCK02")
        self.assertEqual((f["state"], f["side"]), (S.CLOSED, "short"))
        self.assertGreater(f["result"]["r"], 0)

    def test_long_stop(self):
        f = self.final("MOCK03")
        self.assertEqual((f["state"], f["result"]["r"], f["result"]["why"]), (S.CLOSED, -1, "Stop-loss hit"))

    def test_failed_setups_invalidated(self):
        self.assertEqual(self.final("MOCK04")["state"], S.INVALIDATED)
        self.assertEqual(self.final("MOCK10")["state"], S.INVALIDATED)

    def test_watching_stays_watching(self):
        self.assertEqual(self.seen["MOCK06"], [S.WATCHING])

    def test_approach_states_seen(self):
        all_states = {s for p in self.seen.values() for s in p}
        self.assertIn(S.APPROACHING_ENTRY, all_states)
        self.assertIn(S.APPROACHING_EXIT, all_states)

    def test_board_payload(self):
        b = self.svc.board()
        self.assertEqual(b["feed"]["kind"], "replay")
        self.assertTrue(b["alerts"])
        self.assertTrue(all(c["candles"] for c in b["cards"]))
        self.assertIsNone(b["cards"][0]["flags"]["oi_leader"])          # no OI from Yahoo: unknown, never invented
        import json
        json.dumps(b)                                                   # the board must be JSON-serialisable


if __name__ == "__main__":
    unittest.main()

"""HTTP glue for the Intraday Scanner, so server.py only needs a few lines.

  GET  /scanner/state?since=<version>   board data ({"unchanged": true} when nothing changed)
  GET  /intraday/ui/scanner.js|.css     the Scanner tab's script and styles
  POST /scanner/action                  {"type": "bias"|"risk"|"watchlist"|"take"|"exit", ...}

Feed modes (server.py --scanner):  yahoo (live, default) | off | replay-mock | replay-yahoo:YYYY-MM-DD | replay-csv:PATH
"""
import datetime as dt
import json
import os
import threading
import traceback

from . import HERE, load_config, feeds, service

UI = os.path.join(HERE, "ui")
TYPES = {".js": "application/javascript", ".css": "text/css"}


class Holder:
    """The service once it is built (building can take a while: lot sizes, first download)."""
    def __init__(self, mode):
        self.mode, self.svc, self.err = mode, None, None


def start(mode, universe, speed=30.0, start_at="09:15"):
    """Build the feed and the service on a background thread. universe = [{symbol, sector}, ...] (the app's universe.json)."""
    h = Holder(mode)

    def build():
        try:
            cfg = load_config()
            sectors = {u["symbol"]: u.get("sector") for u in universe}
            if mode == "replay-mock":
                feed, lots, note = feeds.mock_replay(cfg, speed, start_at), {}, "MOCK market: no lot sizes (sizing in shares)"
            else:
                lots, note = feeds.load_lot_sizes(cfg)
                syms = sorted(lots) or sorted(sectors)          # NSE F&O stocks; the app's Nifty 200 list if NSE is unreachable
                if not lots:
                    note += "; scanning the Nifty 200 list instead of F&O stocks"
                if mode == "yahoo":
                    feed = feeds.YahooFeed(syms, cfg)
                elif mode.startswith("replay-yahoo:"):
                    feed = feeds.yahoo_replay(syms, dt.date.fromisoformat(mode.split(":", 1)[1]), cfg, speed, start_at)
                elif mode.startswith("replay-csv:"):
                    feed = feeds.csv_replay(mode.split(":", 1)[1], cfg, speed, start_at)
                else:
                    raise ValueError(f"unknown --scanner mode {mode!r}")
            h.svc = service.start(feed, cfg, lots, sectors, note)
            print(f"[scanner] {feed.info()['name']}: {len(feed.symbols())} symbols. {note}", flush=True)
        except Exception as e:
            h.err = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            print(f"[scanner] could not start: {h.err}", flush=True)

    threading.Thread(target=build, daemon=True, name="intraday-build").start()
    return h


def handle_get(h, path, q):
    """(status, body, content type) for a scanner GET."""
    if path.startswith("/intraday/ui/"):
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1]
        full = os.path.join(UI, name)
        if ext not in TYPES or not os.path.isfile(full):
            return 404, {"error": "not found"}, "application/json"
        with open(full, "rb") as f:
            return 200, f.read(), TYPES[ext]
    if path == "/scanner/state":
        if h is None:
            return 200, {"off": True, "error": "The scanner is off (server started with --scanner off)."}, "application/json"
        if h.svc is None:
            return 200, {"loading": True, "error": h.err, "mode": h.mode}, "application/json"
        return 200, h.svc.board(q.get("since")), "application/json"
    return 404, {"error": "not found"}, "application/json"


def handle_post(h, path, body):
    if path != "/scanner/action":
        return 404, {"error": "not found"}
    if h is None or h.svc is None:
        return 503, {"error": "the scanner is not running yet"}
    try:
        return 200, h.svc.action(json.loads(body or b"{}"))
    except (ValueError, KeyError, TypeError) as e:
        return 400, {"error": str(e) or type(e).__name__}

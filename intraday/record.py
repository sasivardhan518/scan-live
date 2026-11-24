"""Record a Yahoo session to CSV for replay:  python -m intraday.record 2026-11-18 recorded.csv
Then:  python server.py --scanner replay-csv:recorded.csv"""
import datetime as dt
import sys

from . import load_config, feeds

if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    day, path = dt.date.fromisoformat(sys.argv[1]), sys.argv[2]
    lots, note = feeds.load_lot_sizes(load_config())
    if not lots:
        sys.exit("No F&O stock list: " + note)
    n = feeds.record_yahoo_day(sorted(lots), day, path)
    print(f"Saved {n} candles for {len(lots)} F&O stocks to {path}")

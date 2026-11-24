"""Intraday Scanner: PDH/PDL breakout + inside-candle setups on 5-minute candles, with a per-stock state machine.

Layers (each its own module):
  feeds.py       data-provider adapters (Yahoo polling, replay)      - I/O
  aggregator.py  ticks / 1-minute bars -> 5-minute candles (IST)       - pure
  engine.py      strategy functions (EMA, inside candle, sizing ...)   - pure
  state.py       per-stock state machine                               - pure
  service.py     runs the state machines on a background thread        - I/O
Signals and alerts only: nothing here places orders.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config(path=None):
    with open(path or os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)

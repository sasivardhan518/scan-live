# 9:20 Scanner: live server

Needs Python 3.9+. From this folder:

    pip install -r requirements.txt
    python server.py --provider yahoo          # free, no account
    # then open http://localhost:8787/

## Providers
- `yahoo`  Free. NSE data on Yahoo can be delayed, so compare it with your broker screen before trusting a 9:20 scan.
- `kite`   Zerodha Kite Connect (real-time). Set `KITE_API_KEY` and `KITE_ACCESS_TOKEN` (the token expires daily), then
           `python server.py --provider kite`. The first start fetches 90 days of daily candles for ~130 stocks at Kite's
           rate limit, so allow about a minute.
- `mock`   Synthetic market for testing: `python server.py --provider mock --mock-time 09:19:30`

## Use it on your phone
    python server.py --provider yahoo --host 0.0.0.0
Open `http://<your-computer-ip>:8787/` on the same Wi-Fi. There is no password, so use it on a trusted network only.

## Endpoints
- `GET /snapshot`            current quotes for all stocks
- `GET /snapshot?at=09:20`   quotes as of that time today (the app uses this to lock the 9:20 shortlist, even if you open it later)
- `GET /candles?symbol=X&tf=5m|1d`
- `GET /health`              provider status, symbols loaded, last error

## Notes
- Sectors come from `sectors.json`. Official NSE indices (NIFTY PSU BANK, NIFTY IT, ...) use the real live index value from Yahoo, and their stock lists are downloaded from NSE on every start (cached in `sector_members.json`). Sectors marked CUSTOM (Capital Goods, Power, Telecom, Consumer Services, Insurance) have no NSE index, so their % is the average of their Nifty 200 stocks. A stock can be in several sectors; it passes confirmation 1 if any of them is a top pick. Only Nifty 200 stocks appear in the drilldown (NIFTY MEDIA has none, so it is never picked).
- On every start (except `mock`) the server downloads the current Nifty 200 list from NSE and updates `universe.json`: new stocks are added with NSE's industry as their sector, removed ones are dropped, your sector edits are kept. If NSE is unreachable it uses the saved file. Skip it with `--no-live-list`.
- Confirmation 4: the 09:15-09:20 candle must be green for buys and red for sells (turn it off in Settings). `/snapshot` returns it as `first5`.
- The stock list is in `universe.json` (symbol, sector, weight). It matches the official NSE Nifty 200 list as of Nov 2026 (weight = market cap in thousand crore). Edit sectors freely; refresh it when NSE rebalances the index (March and September).
- Outside market hours, Yahoo returns the last session, so you can dry-run the whole flow at night.
- Only the mock provider has been run end to end with the app. The Yahoo parser was tested on a synthetic frame and the Kite adapter has not been run against a real account.

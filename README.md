# 9:20 Scanner: live server

Needs Python 3.9+. From this folder:

    pip install -r requirements.txt
    python server.py --provider yahoo          # free, no account
    # then open http://localhost:8787/

## Providers
- `fyers`  Fyers API v3 (real-time, free with a Fyers account). Recommended. Setup below.
- `yahoo`  Free. NSE data on Yahoo can be delayed, so compare it with your broker screen before trusting a 9:20 scan.
- `kite`   Zerodha Kite Connect (real-time). Set `KITE_API_KEY` and `KITE_ACCESS_TOKEN` (the token expires daily), then
           `python server.py --provider kite`. The first start fetches 90 days of daily candles for ~130 stocks at Kite's
           rate limit, so allow about a minute.
- `mock`   Synthetic market for testing: `python server.py --provider mock --mock-time 09:19:30`

## Fyers setup (once)
1. Sign in at https://myapi.fyers.in/ and create an app. Set its Redirect URL to exactly `http://127.0.0.1:8787/fyers/callback`
   (use your port if you run with `--port`). Note the App ID (looks like `XXXXXXXX-100`) and the Secret ID.
2. Create `fyers.json` in this folder (it is git-ignored, keep it private):

       {"app_id": "XXXXXXXX-100", "secret_key": "YOUR_SECRET_ID"}

   or set the environment variables `FYERS_APP_ID` and `FYERS_SECRET_KEY` instead.
3. `python server.py --provider fyers`, open http://localhost:8787/ and press **Log in to Fyers**. You sign in on Fyers'
   own page and come straight back. Do this once each trading day (Fyers tokens last one day; the token is saved in the
   git-ignored `fyers_token.json`, so a restart the same day needs no new login).

How it uses Fyers:
- Live prices and sector indices come from Fyers quotes, refreshed every 5 seconds (`/snapshot` never waits on a download).
- At 09:20:00 the server reads every quote once. Day open, high and low at that instant are the first 5-minute candle,
  and the last price is the 09:20 price, so the shortlist locks at 09:20 with exact numbers. If the server missed that
  moment (started later), it uses 1-minute candles instead, which give the same values but take about a minute to download.
- Daily candles (for the wick rule and the daily chart) load once a day in the background: about a minute at Fyers'
  limit of 200 requests a minute. The app shows "Loading" meanwhile. Past days take about a minute the first time, then are cached.
- Before 09:15 the app shows the previous session, and switches to today with the first trade.
- NIFTY CEMENT has no Fyers index symbol, so with Fyers its % is the average of its stocks.

## Use it on your phone
    python server.py --provider yahoo --host 0.0.0.0
Open `http://<your-computer-ip>:8787/` on the same Wi-Fi. There is no password, so use it on a trusted network only.

## EMA 20 screen (buy and sell setups on 5-minute candles)
The **EMA 20** tab lists stocks where, today:

| Step | Buy | Sell |
|---|---|---|
| 1 | the 20 EMA (5-minute) lies inside a candle's body (between its open and close) | same |
| 2 | one of the next two candles is bearish (the first one that is); its body stays above the EMA, its wick may touch it | one of the next two candles is bullish; its body stays below the EMA, its wick may touch it |
| 3 | a later candle goes above the bearish candle's high, and no candle after the bearish candle (up to and including the breakout) touches or goes below the EMA | a later candle goes below the bullish candle's low, and no candle after the bullish candle (up to and including the breakdown) touches or goes above the EMA |

A touch of the EMA before the break means the setup has failed. Switch between **Buy** and **Sell** at the top;
**Triggered** shows every break of the day, newest first. A stock can hit several times a day; each hit is its own
card, marked "Hit 2 of 3" and so on. **Watching** shows setups still waiting for their break.
Tap a card to open its chart with the EMA line (S = EMA cross, P = pullback candle, ▲ breakout, ▼ breakdown).
Settings apply to both sides: require a close beyond the pullback candle, require the pullback candle's wick to stay clear
of the EMA as well as its body, or require the EMA-cross candle to be green (buys) / red (sells). The logic is in `ema_setup.py`;
the sell side is the buy rule mirrored.

Data: the EMA is warmed up on earlier sessions (about a week of 5-minute candles). Yahoo downloads them in one call and
rescans every minute. Fyers and Kite fetch them once per finished candle (about a minute for 200 stocks at their rate
limit), and live quotes fill in the forming candle, so a breakout shows within seconds. The mock market has only today, so
its EMA starts at 09:15. The screen needs the data server; it does not run in the browser-only demo.

## Endpoints
- `GET /snapshot`            current quotes for all stocks
- `GET /snapshot?at=09:20`   quotes as of that time today (the app uses this to lock the 9:20 shortlist, even if you open it later)
- `GET /candles?symbol=X&tf=5m|1d`
- `GET /ema`                 20 EMA buy and sell setups (options `hold=1`, `close_break=1`, `color=1`); the 5-minute `/candles` rows carry the EMA as `e`
- `GET /sessions`            trading days the app can show (latest first) and the default day
- Add `&date=YYYY-MM-DD` to `/snapshot` and `/candles` for a past session. Past days use 5-minute candles (Yahoo keeps about 60 days); the latest session uses 1-minute candles.
- `GET /health`              provider status, symbols loaded, last error

## Notes
- Sectors come from `sectors.json`. Official NSE indices (NIFTY PSU BANK, NIFTY IT, ...) use the real live index value from Yahoo, and their stock lists are downloaded from NSE on every start (cached in `sector_members.json`). Sectors marked CUSTOM (Capital Goods, Power, Telecom, Consumer Services, Insurance) have no NSE index, so their % is the average of their Nifty 200 stocks. A stock can be in several sectors; it passes confirmation 1 if any of them is a top pick. Only Nifty 200 stocks appear in the drilldown (NIFTY MEDIA has none, so it is never picked).
- On every start (except `mock`) the server downloads the current Nifty 200 list from NSE and updates `universe.json`: new stocks are added with NSE's industry as their sector, removed ones are dropped, your sector edits are kept. If NSE is unreachable it uses the saved file. Skip it with `--no-live-list`.
- Confirmation 4: the 09:15-09:20 candle must be green for buys and red for sells (turn it off in Settings). `/snapshot` returns it as `first5`.
- The stock list is in `universe.json` (symbol, sector, weight). It matches the official NSE Nifty 200 list as of Nov 2026 (weight = market cap in thousand crore). Edit sectors freely; refresh it when NSE rebalances the index (March and September).
- Outside market hours, Yahoo returns the last session, so you can dry-run the whole flow at night.
- The Fyers provider was tested end to end against a stand-in that serves Fyers' documented endpoints and response shapes, not against a real Fyers account yet. Yahoo has been used live. The Kite adapter has not been run against a real account.

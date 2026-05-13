# Polymarket US Sports Moneyline Notes

Session-derived workflow for MLB/NFL/NBA/NHL-style Polymarket US sports markets.

## Key Lesson

Polymarket `.com` sports URLs and Polymarket US tradable market slugs can differ.

Example from MLB:
- Public event/page slug: `mlb-atl-lad-2026-05-10`
- US moneyline market slug discovered by SDK search: `aec-mlb-atl-lad-2026-05-10`

The raw gateway call `GET https://gateway.polymarket.us/v1/market/slug/mlb-atl-lad-2026-05-10` returned `market not found`, while SDK `client.search.query({"query": "Atlanta Braves Los Angeles Dodgers"})` found the event and markets.

## Correct Discovery Flow

Use the Python SDK when sports market mapping matters. If missing, install it in the active agent environment:

```bash
python -m pip install polymarket-us
```

Then query exact matchup markets:

```python
from polymarket_us import PolymarketUS

client = PolymarketUS()
results = client.search.query({"query": "Atlanta Braves Los Angeles Dodgers"})
for event in results.get("events", []):
    for market in event.get("markets", []):
        if market.get("sportsMarketType") == "moneyline" or market.get("slug", "").startswith("aec-"):
            print(market["slug"], market.get("question"), market.get("outcomes"), market.get("outcomePrices"))
client.close()
```

The SDK docs also expose `client.markets.list({"sportsMarketTypes": ["MONEYLINE"]})`, but search by exact matchup is often faster.

## Moneyline Mapping

Polymarket US sports moneyline markets may be framed around one team.

For `aec-mlb-atl-lad-2026-05-10`:
- `ORDER_INTENT_BUY_LONG` preview mapped to `Atlanta Braves`
- `ORDER_INTENT_BUY_SHORT` preview mapped to `Los Angeles Dodgers`

Never infer team side from:
- URL slug
- side names like `YES`/`NO`
- price alone
- `.com` clob token ordering

Trust only authenticated preview:

```python
preview = client.orders.preview({"request": order})
outcome = preview["order"]["marketMetadata"]["outcome"]
assert outcome == "Los Angeles Dodgers"
```

If the preview outcome is wrong, stop and rebuild the proposal. If the corrected slug/intent changes, generate a new approval token and wait for Jerry to approve again.

## Short-Side Cost and App Display

For a short-side moneyline order, the app may show cost as `(1 - price) * shares`.

Example:
- Dodgers side represented as `BUY_SHORT` at `0.56`
- Exchange quantity: `26`
- Collateral/cost shown: `(1 - 0.56) * 26 = $11.44`
- If Dodgers win, payout is `$26`; profit is `$26 - $11.44 = $14.56`

The app may also show the long/framing team price (e.g. Braves `0.44`) while the short-side team is implied at `0.56`. Explain this plainly to Jerry.

## Orders vs Positions

If an order appears in Orders but not Positions:
- check `cumQuantity`
- `cumQuantity: 0` means the limit order is resting and unfilled
- no position exists until shares fill

Limit orders protect price; market orders eat the book and can slip through multiple price levels.

## SDK Helper

Use the repo helper so execution is just guarded code, not hand-built API calls:

```bash
python skills/sports-picks/scripts/polymarket_us_sdk_bet.py health
python skills/sports-picks/scripts/polymarket_us_sdk_bet.py search-moneyline --query "Atlanta Braves Los Angeles Dodgers"
```

Proposal example:

```bash
python skills/sports-picks/scripts/polymarket_us_sdk_bet.py propose-moneyline \
  --market-slug aec-mlb-atl-lad-2026-05-10 \
  --intent ORDER_INTENT_BUY_SHORT \
  --expected-outcome "Los Angeles Dodgers" \
  --order-type ORDER_TYPE_MARKET \
  --price 0.56 \
  --cash-order-qty 15 \
  --max-notional 15 \
  --notes "MLB official lock: Los Angeles Dodgers"
```

Live execution repeats the exact order, requires the proposal token, re-previews before placing, and can write the heartbeat watchlist only after the order receives non-zero filled shares:

```bash
python skills/sports-picks/scripts/polymarket_us_sdk_bet.py order-moneyline \
  --market-slug aec-mlb-atl-lad-2026-05-10 \
  --intent ORDER_INTENT_BUY_SHORT \
  --expected-outcome "Los Angeles Dodgers" \
  --order-type ORDER_TYPE_MARKET \
  --price 0.56 \
  --cash-order-qty 15 \
  --max-notional 15 \
  --approval-token <proposal_token> \
  --execute \
  --i-accept-live-trading \
  --write-watchlist \
  --notes "MLB standing authorization: official lock"
```

Guardrails in the helper:
- loads `POLYMARKET_KEY_ID` / `POLYMARKET_SECRET_KEY` from env or `~/.hermes/.env`
- refuses proposals without authenticated preview
- refuses live orders when preview outcome differs from `--expected-outcome`
- computes proposal approval tokens from request + preview outcome + caps
- re-previews immediately before live order
- compiles `ORDER_TYPE_MARKET` sports entries into capped IOC limits because the SDK/API currently rejects true market bodies during preview
- treats `--price` as the selected outcome's price; for `BUY_SHORT` outcomes it converts to the inverse long-side orderbook price before preview/order (`Cleveland at 0.60` sends orderbook price `0.40`)
- writes proposal/live/error receipts under `.picks/receipts/polymarket/`
- writes `.picks/watchlist/polymarket/*.json` only after a live order has non-zero filled shares when `--write-watchlist` is passed; accepted-but-unfilled or expired orders are not positions and must not create active watchers

## Receipt Requirements

For any SDK proposal/live order:
- save proposal receipt with request, preview, approval token, max notional, and verified outcome
- save live order receipt with preview-before-order, response, order id, and error if any
- create/update watchlist only after a live order is accepted

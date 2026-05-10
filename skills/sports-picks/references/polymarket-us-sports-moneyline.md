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

## Receipt Requirements

For any SDK proposal/live order:
- save proposal receipt with request, preview, approval token, max notional, and verified outcome
- save live order receipt with preview-before-order, response, order id, and error if any
- create/update watchlist only after a live order is accepted

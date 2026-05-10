# Polymarket US Trading Guardrail

Use this only after the normal sports-picks gate has produced an official pick.
Polymarket trading is execution, not analysis.

Source docs:
- Quickstart: https://docs.polymarket.us/getting-started/quickstart
- Auth: https://docs.polymarket.us/api-reference/authentication
- Orders: https://docs.polymarket.us/api-reference/orders/create-order
- SDK markets: https://docs.polymarket.us/api-reference/sdks/python/markets
- Balances: https://docs.polymarket.us/api-reference/account/get-account-balances
- MLB auto-bet policy: `references/mlb-polymarket-auto-bets.md`
- Sports moneyline SDK/session notes: `references/polymarket-us-sports-moneyline.md`

---

## Hard Boundary

Never place a live Polymarket order from a sports pick automatically.

Allowed without approval:
- search/list markets
- fetch BBO/order book
- check authenticated balances/positions if credentials exist
- create a dry-run proposal
- calculate exposure and approval token

Requires explicit Jerry approval in the current chat/session:
- create order
- cancel order
- modify order
- any action that spends, reserves, sells, or changes exposure

Do not use cron jobs for live order placement. Cron may monitor and propose only.

MLB exception:
- Jerry has granted standing authorization for MLB official locks within `references/mlb-polymarket-auto-bets.md` caps.
- That standing authorization covers entry orders only.
- Profit-taking exits still require Jerry approval unless he later grants an explicit exit rule.

---

## Credentials

Polymarket US authenticated endpoints use API keys from the developer portal.
Store these in `~/.hermes/.env`, never in the skill repo:

```bash
POLYMARKET_KEY_ID=...
POLYMARKET_SECRET_KEY=...
```

The secret key is shown only once. If lost, revoke and create a new key.

Authenticated API:
- base URL: `https://api.polymarket.us`
- auth headers: `X-PM-Access-Key`, `X-PM-Timestamp`, `X-PM-Signature`
- signature message: `timestamp + HTTP_METHOD + path`
- signature type: Ed25519, base64 encoded
- timestamp must be within 30 seconds of server time

Public API:
- base URL: `https://gateway.polymarket.us`
- market by slug: `GET /v1/market/slug/{slug}`
- BBO: `GET /v1/markets/{slug}/bbo`
- book: `GET /v1/markets/{slug}/book`

---

## Required Order Flow

1. Run the normal sports-picks workflow.
2. Confirm the Polymarket market maps exactly to the game/outcome.
   - For sports moneylines, prefer the Python SDK discovery path in `references/polymarket-us-sports-moneyline.md`.
   - Use `client.search.query()` or `client.markets.list({"sportsMarketTypes": ["MONEYLINE"]})` to find the US tradable moneyline slug; `.com` event slugs can be non-tradable through the US API.
3. Fetch market details and BBO from `gateway.polymarket.us` or the SDK.
4. Create a dry-run SDK proposal with `scripts/polymarket_us_sdk_bet.py propose-moneyline` for US sports moneylines. Use legacy `scripts/polymarket_us_guard.py propose` only for non-SDK/gateway-compatible markets.
5. Authenticated-preview the exact order and verify `preview.order.marketMetadata.outcome` equals the intended team before showing the proposal.
6. Show Jerry the proposal:
   - market slug
   - verified outcome/team
   - intent/outcome side/action
   - type
   - limit price or market-order slippage
   - quantity/cash amount
   - max notional/exposure
   - current best bid/ask
   - approval token
7. Wait for explicit approval containing the token or exact order terms.
8. Re-preview the exact order immediately before execution; stop and re-propose if the previewed outcome, price, quantity, side, market, or token changes.
9. Place the live order only after preview passes, then save and show the receipt path plus exchange order id.

If anything changes before execution — price, quantity, side, market, token, BBO, previewed outcome, or user approval scope — stop and re-propose.

---

## Order Fields

Create order endpoint: `POST /v1/orders`

Common limit buy shape:

```json
{
  "marketSlug": "chiefs-super-bowl",
  "outcomeSide": "OUTCOME_SIDE_YES",
  "action": "ORDER_ACTION_BUY",
  "type": "ORDER_TYPE_LIMIT",
  "price": {"value": "0.55", "currency": "USD"},
  "quantity": 100,
  "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
  "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"
}
```

Intent equivalents:
- Buy YES: `ORDER_INTENT_BUY_LONG`
- Sell YES: `ORDER_INTENT_SELL_LONG`
- Buy NO: `ORDER_INTENT_BUY_SHORT`
- Sell NO: `ORDER_INTENT_SELL_SHORT`

Prefer `outcomeSide` + `action` for clarity.

---

## Safety Defaults

Default order type:
- Use capped market/cash buy when the current executable odds are inside the approved range and the goal is immediate entry.
- Use `ORDER_TYPE_LIMIT` when enforcing a target price, a hard max price, or maker-only/resting behavior.

Default TIF:
- `TIME_IN_FORCE_DAY` for same-day limit orders
- market/cash buys should use slippage protection and current-price preview rather than resting TIF
- `TIME_IN_FORCE_GOOD_TILL_CANCEL` only if Jerry explicitly asks

Default market-style entries:
- allowed for approved small sports entries when current odds are already acceptable
- use `scripts/polymarket_us_sdk_bet.py --order-type ORDER_TYPE_MARKET --price <current executable price> --cash-order-qty <cap>`; the helper compiles this into a capped IOC limit because SDK preview rejects true market bodies for sports moneylines
- require cash/notional cap and preview-verified outcome
- do not use market-style entries from cron or unattended sessions

Exposure caps:
- require `--max-notional` or `cashOrderQty`
- reject proposals above cap
- reject missing cap for buys

Mapping cap:
- never trade if the market slug is loosely related sentiment instead of exact game/outcome
- never trade if the market is hidden, closed, halted, suspended, expired, or terminated

---

## Receipt Requirements

Every proposal and live order must save JSON under:

```text
.picks/receipts/polymarket/YYYYMMDD-HHMMSS-<action>-<slug>.json
```

Receipt must include:
- timestamp UTC
- mode: dry_run or live
- request body
- BBO snapshot
- market snapshot
- exposure estimate
- approval token
- response or error
- order id when returned

No receipt, no claim of success.

---

## Setup Check

```bash
python skills/sports-picks/scripts/polymarket_us_sdk_bet.py health
python skills/sports-picks/scripts/polymarket_us_sdk_bet.py search-moneyline --query "Atlanta Braves Los Angeles Dodgers"
python skills/sports-picks/scripts/polymarket_us_sdk_bet.py propose-moneyline \
  --market-slug aec-mlb-atl-lad-2026-05-10 \
  --intent ORDER_INTENT_BUY_SHORT \
  --expected-outcome "Los Angeles Dodgers" \
  --order-type ORDER_TYPE_MARKET \
  --price 0.56 \
  --cash-order-qty 15 \
  --max-notional 15 \
  --notes "MLB official lock: Los Angeles Dodgers"
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

# Legacy public/gateway helper remains available for non-SDK checks:
python skills/sports-picks/scripts/polymarket_us_guard.py health
python skills/sports-picks/scripts/polymarket_us_guard.py market --market-slug <slug>
python skills/sports-picks/scripts/polymarket_us_guard.py balances
python skills/sports-picks/scripts/polymarket_us_guard.py positions
python skills/sports-picks/scripts/polymarket_us_guard.py open-orders
python skills/sports-picks/scripts/polymarket_us_guard.py watch-once --market-slug <slug> --outcome-side OUTCOME_SIDE_YES --entry-price <price> --quantity <shares>
```

`balances`, `positions`, `open-orders`, and `preview` need credentials. `market` and `watch-once` use public market data.

---

## Failure Rules

- 401: credentials missing/invalid; do not retry repeatedly.
- timestamp/auth error: check system clock, then retry once.
- market not found: stop; do not guess a slug.
- For Polymarket sports pages like `/sports/mlb/mlb-atl-lad-YYYY-MM-DD`, the public `gateway.polymarket.us/v1/market/slug/{slug}` endpoint may return 404 even when the Polymarket page exists. In that case, verify exact mapping from the page data, extract the moneyline outcome/token IDs, and fetch the CLOB book with `https://clob.polymarket.com/book?token_id=<outcome_token_id>` before proposing. Do not use spread/total/NRFI tokens by accident.
- Before live execution, run authenticated preview (`POST https://api.polymarket.us/v1/order/preview` with `{ "request": <order> }`, or SDK `client.orders.preview({"request": order})`). The preview's `order.marketMetadata.outcome` must exactly match the intended team. If preview says `market not found`, try SDK `client.search.query()` for the US event slug; sports markets may use a prefixed slug such as `aec-mlb-atl-lad-YYYY-MM-DD`.
- Polymarket US sports moneyline markets can be framed around one team: `ORDER_INTENT_BUY_LONG` may buy the first/listed team, while `ORDER_INTENT_BUY_SHORT` buys the opponent. Do not infer this from slug or price. Trust only authenticated preview metadata. Example: `aec-mlb-atl-lad-2026-05-10` preview mapped `BUY_LONG` to Atlanta and `BUY_SHORT` to Los Angeles Dodgers.
- If a corrected SDK-discovered slug/intent differs from the already-approved proposal, create a fresh proposal token and wait for Jerry's new approval before live order.
- order rejected: show rejection receipt; do not resubmit with changed terms unless Jerry approves again.
- partial fill / synchronous execution response: show executions exactly; do not summarize away risk.

Numbers do not lie. Receipts do not lie either.

# Polymarket US Trading Guardrail

Use this only after the normal sports-picks gate has produced an official pick.
Polymarket trading is execution, not analysis.

Source docs:
- Quickstart: https://docs.polymarket.us/getting-started/quickstart
- Auth: https://docs.polymarket.us/api-reference/authentication
- Orders: https://docs.polymarket.us/api-reference/orders/create-order
- Balances: https://docs.polymarket.us/api-reference/account/get-account-balances
- MLB auto-bet policy: `references/mlb-polymarket-auto-bets.md`

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
3. Fetch market details and BBO from `gateway.polymarket.us`.
4. Create a dry-run proposal with `scripts/polymarket_us_guard.py propose`.
5. Show Jerry the proposal:
   - market slug
   - outcome side
   - action
   - type
   - limit price or market-order slippage
   - quantity/cash amount
   - max notional/exposure
   - current best bid/ask
   - approval token
6. Wait for explicit approval containing the token or exact order terms.
7. Re-run the same command with `order --execute --approval-token <token> --i-accept-live-trading`.
8. Save and show the receipt path plus exchange order id.

If anything changes before execution — price, quantity, side, market, token, BBO, or user approval scope — stop and re-propose.

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
- `ORDER_TYPE_LIMIT`

Default TIF:
- `TIME_IN_FORCE_DAY` for same-day sports
- `TIME_IN_FORCE_GOOD_TILL_CANCEL` only if Jerry explicitly asks

Default market orders:
- avoid them
- if unavoidable, require `cashOrderQty` and slippage tolerance
- do not use market orders from cron or unattended sessions

Exposure caps:
- require `--max-notional`
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
- order rejected: show rejection receipt; do not resubmit with changed terms unless Jerry approves again.
- partial fill / synchronous execution response: show executions exactly; do not summarize away risk.

Numbers do not lie. Receipts do not lie either.

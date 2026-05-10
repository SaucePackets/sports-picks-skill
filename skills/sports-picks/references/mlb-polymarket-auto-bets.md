# MLB Polymarket Auto-Bet Policy

This is Jerry's standing authorization for MLB official picks only.

When the MLB slate workflow produces a locked official pick, place a capped Polymarket US limit order if and only if every execution gate below passes.

---

## Standing Authorization

Scope:
- sport: MLB only
- bet type: moneyline / exact winner market only
- exchange: Polymarket US only
- order type: limit only
- time-in-force: `TIME_IN_FORCE_DAY`
- session context: only when Jerry asks for the MLB slate / official card / locks

Not authorized:
- props
- parlays
- live in-game bets unless Jerry explicitly asks in that session
- market orders
- GTC orders unless Jerry explicitly asks
- any loose sentiment/futures/series market that does not map exactly to today's game winner
- any bet after the game starts unless Jerry explicitly asks for live betting
- any order that exceeds the per-pick or daily cap

---

## Sizing Defaults

Base units:
- High confidence: **$25** max notional
- Medium confidence: **$15** max notional
- Elite confidence: **$25** unless Jerry explicitly raises it

Daily cap:
- **$75** total max notional across MLB Polymarket bets

Slate adjustment:
- If one locked pick: use its full unit.
- If two locked picks: use full units if total is <= $75.
- If three locked picks: use full units if total is <= $75.
- If more than three locked picks: something is probably wrong; re-run the hard gate and prefer fewer picks.
- If total units would exceed $75, scale down proportionally and round down to sane dollar amounts.

Never increase unit size just because fewer teams are selected. Fewer picks means less exposure, not bigger gambling brain. Cute trap.

---

## Execution Gate

A locked pick authorizes a Polymarket bet only if:

1. The sports-picks hard gate passed.
2. The Polymarket market maps exactly to the game and side.
3. The market is open, active, and not halted/suspended/expired/closed.
4. The current BBO price is at or better than the predicted bettable price.
5. Limit price is within the pick's price-discipline threshold.
6. The order fits the per-pick unit and remaining daily cap.
7. A dry-run proposal receipt is created first.
8. A live-order receipt is saved after execution.

If any gate fails, output:

```text
Pick locked, bet skipped — [reason]
```

Do not chase price. Do not convert a skipped bet into a worse bet.

---

## Price Discipline

Polymarket prices are probabilities.

Bet only when market price is equal to or better than the fair/bettable price from the analysis.

Default slippage tolerance:
- limit buys: max 1 cent worse than the checked ask **only if still under bettable price**
- limit sells/profit exits: max 1 cent worse than checked bid

If price moved beyond the bettable threshold after the pick was locked, skip the bet.

---

## Receipt Rule

Every auto-bet must produce two receipts:

1. proposal receipt from dry-run proposal
2. live order receipt from execution

Receipt directory:

```text
.picks/receipts/polymarket/
```

The final response must include:
- official pick
- bet placed / skipped
- notional amount
- limit price
- order id if returned
- receipt path

No receipt, no success claim.

---

## Watch-for-Profit Rule

After a Polymarket bet is placed, create or run a watch check for that market.

Default profit alert threshold:
- alert if exit bid is at least **8 cents above entry price**, or
- alert if mark-to-market profit is at least **20%** before fees/spread

Default protection threshold:
- alert if price moves **10 cents against entry**

Watch behavior:
- monitoring may alert/propose an exit
- monitoring may not sell automatically unless Jerry explicitly approves a profit-taking exit rule later
- if an exit opportunity appears, output a proposed sell order with current bid, estimated profit, and receipt/proposal path

For now: watch recommends exits; it does not auto-sell.

---

## Preferred Command Shape

Proposal:

```bash
python skills/sports-picks/scripts/polymarket_us_guard.py propose \
  --market-slug <slug> \
  --outcome-side OUTCOME_SIDE_YES \
  --action ORDER_ACTION_BUY \
  --order-type ORDER_TYPE_LIMIT \
  --price <limit_price> \
  --quantity <shares> \
  --max-notional <unit_size> \
  --notes "MLB official lock: <team>"
```

Execution under standing MLB authorization:

```bash
python skills/sports-picks/scripts/polymarket_us_guard.py order \
  --market-slug <slug> \
  --outcome-side OUTCOME_SIDE_YES \
  --action ORDER_ACTION_BUY \
  --order-type ORDER_TYPE_LIMIT \
  --price <limit_price> \
  --quantity <shares> \
  --max-notional <unit_size> \
  --approval-token <proposal_token> \
  --execute \
  --i-accept-live-trading \
  --notes "MLB standing authorization: official lock"
```

Watch once:

```bash
python skills/sports-picks/scripts/polymarket_us_guard.py watch-once \
  --market-slug <slug> \
  --outcome-side OUTCOME_SIDE_YES \
  --entry-price <fill_or_limit_price> \
  --quantity <shares> \
  --profit-cents 0.08 \
  --loss-cents 0.10
```

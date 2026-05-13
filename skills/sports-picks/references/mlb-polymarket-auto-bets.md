# MLB Polymarket Auto-Bet Policy

This is Jerry's standing authorization for MLB official picks only.

Standing authorization is **not** triggered by vague phrasing like `lock picks`.

Command meanings:
- `deep analysis` / `deeper pass`: analysis only, no lock, no bet.
- `lock official picks only`: ledger/Console lock only, no Polymarket proposal or live order.
- `lock and propose bets`: ledger/Console lock plus dry-run Polymarket proposals; no live order.
- `lock and place authorized MLB bets`: ledger/Console lock plus capped Polymarket US orders if every execution gate below passes.

When Jerry says exactly `lock and place authorized MLB bets`, the MLB slate workflow may place a capped Polymarket US order if and only if every execution gate below passes.

---

## Standing Authorization

Scope:
- sport: MLB only
- bet type: moneyline / exact winner market only
- exchange: Polymarket US only
- default order type: market buy when the current executable odds are inside the approved/bettable range and Jerry's goal is entry now; limit order when targeting a specific better/max price or enforcing a hard cap
- time-in-force: `TIME_IN_FORCE_DAY` for limits; market orders must use cash/notional caps and slippage protection
- session context: only when Jerry asks for the MLB slate / official card / locks

Not authorized:
- props
- parlays
- live in-game bets unless Jerry explicitly asks in that session
- uncapped market orders or market orders when current price is outside the approved/bettable range
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

Bet only when the executable market price is equal to or better than the approved/bettable price from the analysis.

Execution rule:
- If the current executable price is already acceptable and Jerry wants the bet entered now, use a capped market buy / cash-notional order with slippage protection.
- If the current price is worse than approved, or Jerry explicitly wants a target entry, use a limit order and accept that it may not fill.
- Do not use a resting limit merely out of habit when the current odds already satisfy the thesis; that can miss a valid edge.

Default slippage tolerance:
- market/cash buys: cap notional at the approved unit and cap slippage at 1 cent worse than the checked executable price, only if still inside the bettable threshold
- limit buys: max 1 cent worse than the checked ask only if still under bettable price
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

Current heartbeat implementation:
- Watch cron job: `c98f238efb0d` — `MLB Polymarket Heartbeat — in-game watch alerts`
- Watch script: `/home/clawdbot/.hermes/scripts/mlb_polymarket_heartbeat.py`
- Watch schedule: every 5 minutes
- Watch mode: `no_agent=true`, script-only, writes receipts only, deliver=`local`
- Judgement/postgame cron job: `0ecc7d117a97` — `MLB Heartbeat — postgame settlement + alert judgement`
- Judgement/postgame script: `/home/clawdbot/.hermes/scripts/mlb_polymarket_alert_review.py`
- Judgement/postgame schedule: every 5 minutes, one minute after the watch script
- Judgement/postgame model: spawned `hermes chat` pinned to `openai-codex / gpt-5.5`
- Judgement output: reply exactly `NO_OPPORTUNITY` to stay silent; only message Jerry for real opportunities worth considering
- User-facing delivery: Rebecca's Picks topic `telegram:-1003740149270:4`
- Watch files: `.picks/watchlist/polymarket/*.json`
- Alert receipts: `.picks/heartbeat/*.json`
- Reviewed markers: `.picks/heartbeat-reviewed/*.done`
- Postgame settlement markers: `.picks/postgame-reviewed/*.done`

Watch file schema:

```json
{
  "active": true,
  "market_slug": "<polymarket-game-slug>",
  "outcome_side": "OUTCOME_SIDE_YES",
  "entry_price": "0.52",
  "quantity": "25",
  "profit_cents": "0.08",
  "loss_cents": "0.10",
  "espn_event_id": "<optional ESPN event id>",
  "label": "<optional human label>"
}
```

Default cadence policy:
- pregame after order: every 15 minutes until first pitch
- innings 1-6: every 10 minutes
- innings 7-9/extras: every 5 minutes
- stop when market closes, game ends, position exits, or no actionable bid remains

The current script checks every 5 minutes and stays silent unless a threshold fires. This is intentionally conservative: no auto-sell, no auto-live-entry, no order modification.

Default profit alert threshold:
- alert if exit bid is at least **8 cents above entry price**, or
- alert if mark-to-market profit is at least **20%** before fees/spread

Default protection threshold:
- alert if price moves **10 cents against entry**

Live-game judgement gate:
- Before recommending an exit or new live entry, verify current score, inning, outs/base state if available, probable/current pitchers, bullpen state, and any obvious injury/weather delay context.
- A market-price move alone is not enough. Pair it with game state.
- If the position is profitable because the original read is working and the game state is still structurally strong, prefer holding unless late-game variance risk is rising.
- If the price offers profit but the game state is deteriorating — starter losing command, bullpen mismatch looming, lineup stranded too many chances, injury/weather weirdness — propose an exit.
- If the price moves against us but the original thesis remains intact and the game state supports it, do not panic-sell; report hold/review.

Watch behavior:
- monitoring may alert/propose an exit
- monitoring may not sell automatically unless Jerry explicitly approves a profit-taking exit rule later
- if an exit opportunity appears, output a proposed sell order with current bid, estimated profit, game-state reason, and receipt/proposal path

For now: watch recommends exits; it does not auto-sell.

---

## Passed-Price Watchlist

If a pick passes the confidence gate but the pregame Polymarket price is too expensive, add it to a watchlist instead of forcing a bet.

Use this for:
- official-confidence sides skipped only because price was bad
- close confidence plays where the team was right but market price needed to come back
- games where live conditions could create a better entry

Watchlist gate:
- must have a documented pregame thesis
- must have a target/bettable price
- must map to an exact Polymarket game-winner market
- must check live game state before proposing entry

Live entry proposal requires:
- current Polymarket price at or better than target
- game state still supports the original thesis
- no new injury/weather/bullpen/starter info that breaks the hard gate
- remaining daily cap
- explicit note whether it is pregame-skipped opportunity or live-bet request

For now, live entries from passed-price watchlist are proposal-only unless Jerry explicitly asks for live betting in that session.

---

## Postgame Reflection Rule

After games settle, run a postgame review for proposed candidates, official locks, placed bets, and price-watch passes.

Current official-pick settlement implementation:
- Cron job `0ecc7d117a97` checks active/pending MLB official picks every 5 minutes.
- It queries Sovereign Console `/chat/picks?status=active&result=pending&limit=100`.
- For each MLB pick with `game_id`, it verifies the ESPN final summary and waits until the game is complete.
- The no-agent script must stay under the scheduler timeout; it settles the verified win/loss directly through `/chat/picks/{pick_id}/settle` and emits a concise result message.
- Do **not** spawn a long `hermes chat` postgame reviewer inside this every-5-minute no-agent script; it can exceed the scheduler's 120s script timeout after a final score.
- Deeper process reflection and durable skill patches belong in the separate postgame reflection workflow, not the heartbeat settlement loop.
- Settlement markers live under `.picks/postgame-reviewed/*.done` to avoid duplicate messages.

Reflection must distinguish:
- good read, good result
- good read, bad variance
- bad read / missed signal
- correct pass
- wrong pass / missed opportunity
- incomplete data / still live

Review evidence should include final score and enough game script to judge the original thesis: starter performance, early innings, run support, stranded chances, bullpen survival, injuries/weather/delays, and decisive volatility such as homers, errors, two-out damage, blown saves, or extras.

If a repeatable missed signal appears, save a dated review note under:

```text
.picks/reviews/mlb-postgame/YYYY-MM-DD.md
```

Only patch the skill when the improvement is durable, specific, and not just one-game outcome chasing. If it is variance, log it and leave the skill alone. No mystery gambling machine learns by superstition.

Patch decision guardrail:
- If the review says `wrong pass`, `missed opportunity`, `watch`, `do one more stress test`, or `closer than we gave it`, explicitly compare the missed-signal wording against the existing skill text.
- Do not stop at "a general rule already exists." Ask whether the existing rule would have forced the next slate scan to behave differently.
- If the existing rule is too generic to change future behavior, patch the skill with the smallest concrete trigger/action wording.
- If no patch is made, state why the current wording is already specific enough to catch the next similar slate.

---

## Daily Automation Roadmap

Eventually, the desired fully automated season workflow is:

1. Daily MLB slate scan during season.
2. Generate official picks or passes.
3. Place capped Polymarket entry bets for locked MLB picks within standing rails.
4. Create heartbeat watchers for placed bets.
5. Create watchlist heartbeat for passed-price confidence plays.
6. Alert/propose exits or live entries when thresholds and game-state gates align.
7. Settlement and reflection after games.

Do not jump straight to full autonomy. Build in stages:
- Stage 1: manual slate request + auto entries + proposal-only exits.
- Stage 2: scheduled daily slate scan with proposed card only.
- Stage 3: scheduled daily slate scan + standing-authorized entries.
- Stage 4: automated watchlist and exit proposals.
- Stage 5: auto exits only after Jerry separately approves exact sell rules.

Autonomy increases only with receipts, caps, and review logs. No mystery gambling machine.

---

## Preferred Command Shape

These CLI shapes are for legacy/gateway-compatible markets. For Polymarket US sports moneylines, prefer the SDK workflow in `references/polymarket-us-sports-moneyline.md` so preview metadata verifies the team and cash market buys can be capped correctly.

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

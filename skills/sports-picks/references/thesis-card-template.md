# Official Pick Thesis Card Template

Use this only for official picks, execution proposals, and serious price-watch entries. It is the receipt for why the pick existed before the game happened.

---

## Canonical Template

```text
Pick thesis
- Side: <team/side + market>
- Price: <book/exchange + current price>
- Max acceptable price: <worst price before pass>
- Confidence: <Low|Medium|Medium-High|High>
- Edge: <the specific reason this side wins more often than price implies>
- Why now: <why this price/game state is actionable today>
- Win path: <how the pick wins through early/middle/late game>
- Failure path: <the most likely way this loses>
- Hard gates: <passed gates, or do not lock>
- Review trigger: <what postgame should check first>
```

If `Failure path` feels hard to write, the thesis is not ready.

---

## Minimal Stored Version

For compact ledgers or database metadata:

```json
{
  "edge": "specific edge",
  "price": "book/exchange price",
  "max_acceptable_price": "pass point",
  "why_now": "timing or market reason",
  "win_path": "how it wins",
  "failure_path": "how it loses",
  "review_trigger": "first postgame question"
}
```

---

## Quality Bar

Good thesis:
- names a concrete edge, not vibes
- separates team quality from current price
- includes the opponent's best counter-path
- can be falsified after the game
- tells postgame review what to inspect first

Bad thesis:
- `team is hot`
- `value at plus money`
- `opponent is bad`
- `starter edge` with no command/contact/run-prevention detail
- no max acceptable price
- no failure path

---

## Price Discipline

Always define the pass point before execution.

Examples:
- `Playable to -135; pass at -145 or worse.`
- `Need +115 or better; +100 kills the edge.`
- `Polymarket playable to 0.57; pass above 0.60.`

If price moves beyond the pass point, the pick can remain a good read and still become a no-bet.

---

## Failure Path Examples

Use direct language:

- `Their starter has a real shutdown path and can erase the bullpen edge.`
- `Our bullpen walk traffic turns a close lead into chaos.`
- `Road favorite price is too rich if the offense is only average today.`
- `The opponent's power profile punishes this pitcher's fly-ball shape.`
- `Market is pricing lineup news before we confirmed it.`

The failure path is not pessimism. It is how you stop lying to yourself.

# Postgame Attribution

Use this after a pick settles. The question is not `did it win?` The question is `was the process right?`

---

## Attribution Labels

Assign exactly one primary label:

- `Good read / good result`: thesis held and the result matched.
- `Good read / bad result`: thesis held, but variance or a narrow swing beat it.
- `Bad read / good result`: result won despite flawed reasoning.
- `Bad read / bad result`: thesis failed and result exposed it.
- `Price right / side wrong`: number was defensible, but winner conviction was not.
- `Side right / price bad`: team read was right, but entry price was undisciplined.
- `Thesis failed before market mattered`: injury, lineup, starter, weather, or game-state assumption broke the case.

Secondary tags are allowed, but one primary label must lead.

---

## Review Sequence

1. Pull final score and status from a live source.
2. Pull box score / play-by-play / core stat line for the sport.
3. Read the original thesis card.
4. Check the thesis edge first.
5. Check the stated failure path second.
6. Compare entry price to close / live market if available.
7. Assign attribution label.
8. Write one durable process lesson only if it will matter again.

Do not invent the thesis after the game. If no thesis card exists, say so and grade the process as incomplete.

---

## Reflection Template

```text
## <YYYY-MM-DD> — <Side> <price> vs <Opponent> — <W/L/Scratched>

Attribution: <primary label>

Original thesis
- Edge: <from thesis card>
- Price: <entry and max acceptable price>
- Failure path: <from thesis card>

What happened
- Score: <final score>
- Deciding stretch: <inning/quarter/period/drive/sequence>
- Key stat line: <starter/QB/goalie/bullpen/offense/etc.>

Process read
- Thesis held? <yes/no/mixed>
- Failure path showed up? <yes/no>
- Price discipline held? <yes/no>
- Main miss, if any: <one sentence>

Rule change
- <none, or one concrete reusable rule>
```

---

## What Counts As Good Process

Good process can lose when:
- the stated edge appeared
- the failure path did not dominate
- the price was within the defined pass point
- the loss came from normal variance or a narrow event

Bad process can win when:
- the failure path showed up but the pick survived anyway
- the price was too expensive for the edge
- the thesis depended on reputation, vibes, or opponent fade
- key live information was available and ignored

Do not let green rows hide rotten reasoning.

---

## Promotion Rule

Promote a lesson to `.picks/PROCESS.md` only when:
- the same miss appears twice
- the rule is sport-independent or recurring within a sport
- it would have changed the pregame decision
- it is specific enough to enforce

Bad rule:
- `be careful with favorites`

Good rule:
- `Do not lay road-favorite chalk when both bullpens are hot and the opposing starter has a credible shutdown path unless there is a separate offensive or defensive mismatch.`

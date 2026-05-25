# Sports Picks Process Lanes

Use this to choose the right depth before doing analysis. The goal is more discipline, not longer writeups.

---

## Default Flow

```text
Slate scan → candidate queue → deep analysis only on candidates → thesis card only for official picks → postgame attribution
```

Do not turn every game into a full memo. Most games should die fast.

---

## Lane 1 — Slate Scan

Use for broad daily cards or first-pass schedule review.

Purpose:
- identify obvious no-plays quickly
- surface only games with enough signal for deeper work
- avoid action-chasing across the whole slate

Required checks:
- current line / market price
- starter, QB, goalie, or primary-player status
- recent form baseline
- injury / rest / lineup flags
- obvious weather / venue factors
- one-sentence reason to continue or pass

Allowed outputs:
- `Ignore`
- `Log`
- `Monitor`
- `Research deeper`
- `Official candidate`
- `Pass`

Slate scan does not create an official pick by itself.

---

## Lane 2 — Quick Card

Use for normal user-facing cards after the scan.

Purpose:
- state official picks and passes cleanly
- show enough evidence to trust the decision
- stay readable

Shape:

```text
Official card right now
- <Side price> — <confidence>. <one-line edge>.

Why it sticks out
- Form: <short verified signal>.
- Matchup: <starter/QB/goalie/star edge>.
- Late-game path: <bullpen/defense/availability>.
- Price: <current line and price discipline>.
- Gate: <all passed, or failed gate → pass>.
```

Use this unless the user asks for deeper analysis or the candidate needs stress testing.

---

## Lane 3 — Full Handicap

Use only for serious candidates, disputed reads, or explicit `deep analysis` requests.

Required sections:
- Market setup: book/exchange, current price, implied probability, acceptable max price
- Team form: last 5-7 games, run/point differential, quality of opponents when relevant
- Primary matchup: starter/QB/goalie/star availability and current form
- Late-game path: bullpen/defense/bench/special teams/closing risk
- Injuries / lineup / rest: verified current status
- Weather / venue / travel: only if meaningful
- Price discipline: whether the side is still worth paying for
- What would make this wrong?
- Verdict: official pick, pass, monitor, or price-watch

Full handicap can still end in PASS. That is usually the point.

---

## Lane 4 — Thesis Card

Use only when a pick is locked as official or proposed for execution.

Purpose:
- capture the edge before result bias contaminates review
- make postgame attribution possible
- define the price and failure path up front

Load `references/thesis-card-template.md` for the canonical format.

---

## Lane 5 — Postgame Attribution

Use after settlement, win or loss.

Purpose:
- separate result from process
- identify whether the thesis actually held
- promote repeated failure modes into `.picks/PROCESS.md`

Load `references/postgame-attribution.md` before writing a reflection.

---

## Candidate Queue States

Use these internally or in runtime ledgers:

- `Ignore`: no real angle; do not revisit unless news changes.
- `Log`: useful data point, no current action.
- `Monitor`: price/news dependent; define the trigger.
- `Research deeper`: enough signal for a full handicap.
- `Official candidate`: passed scan and deserves final hard-gate review.
- `Pass`: analyzed and rejected; include the killed gate.
- `Executed`: official pick saved or bet placed under local runtime policy.

Every state above `Log` needs a reason and a next trigger. No mystery buckets.

---

## Depth Discipline

- Do not deep-dive games that failed the scan unless the user explicitly asks.
- Do not create unofficial lean buckets by accident.
- Do not lock a pick without a thesis card if the runtime has storage for one.
- Do not review a result without comparing it to the original thesis.
- If the only reason to continue is `interesting price`, kill it unless winner conviction is real.

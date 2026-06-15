# Sports Betting — Manual Bets Ledger (Template)

Structured ledger for manual/exploratory bets outside the automated pipeline. One file per sport (e.g. `wc-manual-bets.md`, `ufc-manual-bets.md`).

**Purpose:** Build data for new sports, markets, and bet types before they're ready for automated gates.

**Does NOT affect:** Official bankroll record (picks.json, INDEX.md), bankroll math, or betting-operations gates.

---

## Current Session: [Session Name — Date]

| Date | Match/Fight | Bet | Stake | Odds | Result | P&L | Platform | Lesson Logged? |
|------|-------------|-----|-------|------|--------|-----|----------|----------------|
| [Date] | [Teams] | [Bet type] | [$X] | [+/-XXX] | [✅/❌/🔄] | [+$X.XX/-$X.XX] | [Platform] | [Yes/No/Pending] |

### Session totals
- **W-L:** [X-Y-Z]
- **Net P&L:** [+/-$X.XX]
- **Platforms used:** [Platforms]

### Session lessons applied
- [Lesson 1]
- [Lesson 2]

---

## Instructions

### Adding a new bet:
1. Add a row with Date, Match, Bet, Stake, Odds, and Platform
2. Leave Result and P&L blank (pending)

### Settling a bet:
1. Update Result (✅ / ❌ / 🔄)
2. Update P&L with actual dollar profit/loss
3. Update Lesson Logged? column once reviewed

### When a session ends:
1. Copy the session block to the archive section below
2. Start a fresh session block

---

## Archived Sessions

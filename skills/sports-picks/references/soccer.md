# Soccer / World Cup Pick Workflow

## Overview

Soccer betting framework for FIFA World Cup and other leagues. Uses three-way moneylines (Home / Draw / Away) with sport-specific gate criteria.

## Key Differences from MLB

| MLB | Soccer |
|-----|--------|
| Two-way moneyline | Three-way (Home / Draw / Away) |
| Pitcher-centric gates | Form, attack, defense, draw risk, rest, context |
| Every game has a home team | Knockout stage is neutral sites |
| ~3-hour games | Match windows span afternoon + evening |
| Polymarket via US Sports SDK | Polymarket CLOB contracts (SDK doesn't find them) |

## Data Sources

- **ESPN scoreboard:** `site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates=YYYYMMDD`
  - Other leagues: `eng.1` (Premier), `esp.1` (La Liga), `ger.1` (Bundesliga), `ita.1` (Serie A), `fra.1` (Ligue 1), `uefa.champions` (Champions League)
- **Team form:** FootyStats (web_extract from team pages)
- **Polymarket prices:** `polymarket.com/sports/fifa-world-cup/games` (web_extract)
- **Player data:** `sports-skills` CLI via ESPN athlete IDs, or web search

## The 7 Soccer Gates

Apply these in order before any official pick:

### 1. Form Gate
Last 5 matches (W-D-L record). A team with 4W-1D-0L and +8 GD has strong form. A team with 1W-1D-3L is cold — requires compensating factors.
**Data source:** FootyStats (last 10), web search for recent friendlies.

### 2. Attack Gate
Goals per game, xG For, shots on target. GF > 1.5/game is strong. xG For > 1.5 confirms chances are being created.
**Data source:** FootyStats.

### 3. Defense Gate
Clean sheet %, goals conceded, xG Against. Clean sheet > 50% is strong. GA < 0.8/game is stingy.
**Data source:** FootyStats.

### 4. Draw Risk Assessment
Critical for three-way analysis. Historical draw rate for each team. Low-scoring profile means draw is always live. Rule of thumb: most matches draw ~20-30% of the time.
**Context matters:** Group openers draw more. Knockout has no draw after extra time.
**Data source:** FootyStats common scorelines + draw rate.

### 5. Rest / Availability Gate
Days between matches (typically 3-4 in group stage). Key injuries and suspensions matter more than in MLB — one star's absence can shift the line. Red card accumulation.
**Data source:** Web search, official updates.

### 6. Tournament / Match Context Gate
- Matchday 1: cautious, draw-friendly
- Matchday 2: more open, teams feel pressure
- Matchday 3: simultaneous kickoffs, scenario-dependent play
- Knockout: no draws after 90+30, penalties create different risk profile
**Weight:** Medium-High, especially for group stage.

### 7. Price Discipline
Three-way market means probability must be distributed across all three outcomes. Compare estimated P(Home Win), P(Draw), P(Away Win) against market price. Edge only exists where estimated probability exceeds the market price for that specific outcome.

## Polymarket Price Format

WC match markets use cents per outcome:
- Mexico: 69¢ → 69% implied probability
- Draw: 22¢ → 22% implied
- South Africa: 12¢ → 12% implied

**Edge check:** If estimated win probability > market price → edge exists.
**Draw must always be accounted for** — a 22¢ draw with 30% estimated probability is an edge on the draw, not the team side.

## Player Tracking

For each analyzed match, track 3-4 key players per side:
- **Striker / Forward** — scoring form, minutes, injury status
- **Playmaker / #10** — assists, chance creation, set pieces
- **Defensive anchor / CB** — cards, injuries, defensive solidity
- **Goalkeeper** — form, clean sheets, injury

**Data sources:** `sports-skills` CLI for club season stats, web search for squad announcements and injury news.

## Sample Analysis

```
## Mexico vs South Africa

**Form:** Mexico 5W-4D-1L last 10, +8 GD, 1.2 GF, 0.4 GA, 70% clean sheets
**Attack:** 1.2 GF/match, 1.32 xG — solid but not elite
**Defense:** 70% CS, 0.4 GA/match, 0.89 xGA — genuinely stingy
**Draw risk:** Mexico 40% draws, low-scoring profile
**Polymarket:** Mexico 69¢ | Draw 22¢ | South Africa 12¢
**Estimated:** Mexico 60% | Draw 25% | South Africa 15%

**Verdict:** Mexico -215 implied = ~68%. Actual estimate ~60%. No edge on Mexico.
Draw at 22¢ with 25% estimate is marginal. PASS.
```

## League IDs (ESPN)

| League | ESPN ID |
|--------|---------|
| Premier League | `eng.1` |
| La Liga | `esp.1` |
| Bundesliga | `ger.1` |
| Serie A | `ita.1` |
| Ligue 1 | `fra.1` |
| Champions League | `uefa.champions` |
| World Cup | `fifa.world` |

# World Cup Betting — Data & Analysis Reference (Template)

Fill in tournament-specific data before each tournament. Gate criteria and data sources are reusable.

---

## Tournament Overview

- **Dates:** [Start date] – [End date], [Year]
- **Hosts:** [Countries]
- **Format:** [e.g. 48 teams → 12 groups of 4 → Round of 32 → ...]
- **Total matches:** [Number]

---

## Data Sources

### 1. ESPN Scoreboard (schedule, odds, results)
```
https://site.api.espn.com/apis/site/v2/sports/soccer/[league]/scoreboard?dates=YYYYMMDD&limit=50
```
- Match schedule with event IDs
- Competitors with team IDs and scores
- Status (scheduled, in-progress, final)
- Moneyline, point spread, over/under in `competitions[0].odds[0]`

### 2. Team Form Data
- FootyStats (https://footystats.org/) — form, xG, goal timing, BTTS rates
- ESPN team results (https://www.espn.com/soccer/team/results/_/id/{teamId})
- Web search for recent international results

### 3. Polymarket
- Web: https://polymarket.com/sports/[league]/games
- Three-way moneyline in cents (e.g. 47¢, 28¢, 26¢)
- Spread/handicap and over/under markets available

---

## Team IDs (Add all teams in the tournament)

| Team | ID | Team | ID |
|------|-----|------|-----|
| [Team A] | [ID] | [Team B] | [ID] |
| [Team C] | [ID] | [Team D] | [ID] |

---

## Soccer Analysis Gates

### 1. Form Gate
- Last 5 matches (W-D-L record), goal difference
- Weight less for matches against weak opposition
- **Data source:** FootyStats, web search

### 2. Attack Gate
- Goals per game, xG For, shots on target
- GF > 1.5/game = strong attack
- Compare to opponent's defensive metrics

### 3. Defense Gate
- Clean sheet %, goals conceded, xG Against
- GA < 0.8/game = stingy
- GS > 50% = strong defense

### 4. Draw Risk Gate (critical for 3-way moneylines)
- Historical draw rate, low-scoring profile
- Opening matches, group stage openers = draw-friendly
- Knockout = no draw (extra time + penalties)

### 5. Rest / Availability Gate
- Days between matches, key injuries, suspensions
- Card accumulation (2 yellows = 1-match ban in group stage)

### 6. Tournament Context Gate
- Group standing, must-win vs draw-friendly
- Match 1 (opener): both teams cautious
- Match 3 (simultaneous kickoffs): scenario-based
- Host nation boost

### 7. Price Gate
- Polymarket price vs estimated probability
- 3-way market: distribute across Home / Draw / Away
- Convert American odds to probability if comparing to sportsbooks

---

## Template Match Entry

```
## [Team A] vs [Team B] — [Date] ([Round])

**Final score:** [X-Y]

**Odds (DK):** [Team A] [odds] / Draw [odds] / [Team B] [odds]
**Polymarket:** [Team A] [¢] / Draw [¢] / [Team B] [¢]
**O/U:** [X.5]

**Gates assessment:**
- Form: [notes]
- Attack: [notes]
- Defense: [notes]
- Draw risk: [notes]
- Rest: [notes]
- Context: [notes]
- Price: [notes]

**Verdict:** [pass / candidate / lean]
```

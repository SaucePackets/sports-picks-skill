# NBA Pick Workflow

## Data Priority Order

1. **Rest days** — back-to-back is the single biggest edge in NBA handicapping
2. **Pace/efficiency** — offensive/defensive rating, net rating
3. **Home/away splits** — home court matters more in NBA than other sports
4. **Injury report** — star players drive huge win% swings
5. **Polymarket odds** — market consensus

## Key Stats to Pull

### Efficiency
- Offensive rating, defensive rating, net rating
- eFG%, turnover rate, pace

### Situational
- Rest days (0 = back-to-back, major disadvantage)
- Home/away record + home/away net rating splits
- Last 10 game record

### Player
- Star player availability (injury/rest)
- Usage rate of top options

## Commands

```bash
sports-skills nba get_teams                          # resolve team IDs
sports-skills nba get_team_stats --team_id=<id>      # season stats
sports-skills nba get_injuries                       # injury report
sports-skills nba get_standings --season=<year>      # W-L, streak
sports-skills nba get_scoreboard                     # recent results + rest calc
sports-skills nba get_leaders --season=<year>        # stat leaders
sports-skills polymarket search_markets --sport=nba --query="<TeamA> <TeamB>" --sports_market_types=moneyline
```

## Output Format

```
**NBA Pick: [Team A] vs [Team B]**
📅 Date | 🏟️ Venue

**Pick: [Team]** (Confidence: High/Medium/Low)

**Key factors:**
- Rest edge: [team] — x days vs x days
- Efficiency edge: [team] — net rating +x.x vs +x.x
- Star availability: [notable status]
- Home court: [team]
- Market: [Team] favored at XX%

**Flip risk:** [one sentence]
```

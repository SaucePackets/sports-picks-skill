# NFL Pick Workflow

## Data Priority Order

1. **Season stats** — team offense/defense rankings, points per game, yards per play
2. **Injury report** — QB, starting RB, top WR, key DB/pass rusher
3. **Rest/schedule edge** — short week, bye coming off, travel
4. **Home/away** — home teams cover at slightly higher rate
5. **Polymarket odds** — market consensus

## Key Stats to Pull

### Offense
- Points per game, yards per play, 3rd down conversion %
- QB rating/passer rating, rushing yards per attempt

### Defense
- Points allowed per game, sacks, turnover differential
- Pass rush grade, red zone defense %

### Situational
- Home/away record
- Division game (tighter, more unpredictable)
- Weather (outdoor stadiums — wind/rain kills passing games)
- Rest days (short week = injury risk, poor prep)

## Commands

```bash
sports-skills nfl get_teams                          # resolve team IDs
sports-skills nfl get_team_stats --team_id=<id>      # season stats
sports-skills nfl get_injuries                       # injury report
sports-skills nfl get_standings --season=<year>      # W-L, division
sports-skills nfl get_scoreboard                     # recent results
sports-skills nfl get_leaders --season=<year>        # stat leaders
sports-skills polymarket search_markets --sport=nfl --query="<TeamA> <TeamB>" --sports_market_types=moneyline
```

## Output Format

```
**NFL Pick: [Team A] vs [Team B]**
📅 Date | 🏟️ Venue

**Pick: [Team]** (Confidence: High/Medium/Low)

**Key factors:**
- QB edge: [team] — [QB name], rating xx.x
- Defense edge: [team] — x.x pts/game allowed
- Injuries: [notable absences]
- Rest edge: [team or none]
- Market: [Team] favored at XX%

**Flip risk:** [one sentence]
```

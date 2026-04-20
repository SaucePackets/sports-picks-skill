# NHL Pick Workflow

## Data Priority Order

1. **Goalie matchup** — most predictive single factor; starting goalie + recent SV%
2. **Power play / penalty kill** — PP% and PK% are strong team quality signals
3. **Injury report** — goalie changes and top-line forwards
4. **Home/away** — home teams win ~54% in NHL
5. **Polymarket odds** — market consensus

## Key Stats to Pull

### Goaltending
- Starting goalie SV%, GAA, record last 5 starts
- Backup goalie quality (injury risk)

### Team
- Goals per game, goals against per game
- PP%, PK%
- Corsi/Fenwick (shot attempt differential) if available

### Situational
- Back-to-back (goalie fatigue)
- Recent form (last 10)

## Commands

```bash
sports-skills nhl get_teams                          # resolve team IDs
sports-skills nhl get_team_stats --team_id=<id>      # season stats
sports-skills nhl get_injuries                       # injury report
sports-skills nhl get_standings --season=<year>      # W-L, pts, streak
sports-skills nhl get_scoreboard                     # recent results
sports-skills nhl get_leaders --season=<year>        # stat leaders
sports-skills polymarket search_markets --sport=nhl --query="<TeamA> <TeamB>" --sports_market_types=moneyline
```

## Output Format

```
**NHL Pick: [Team A] vs [Team B]**
📅 Date | 🏟️ Venue

**Pick: [Team]** (Confidence: High/Medium/Low)

**Key factors:**
- Goalie edge: [team] — [Goalie], SV% .xxx
- PP/PK edge: [team] — PP x.x%, PK x.x%
- Injuries: [notable absences]
- Home ice: [team]
- Market: [Team] favored at XX%

**Flip risk:** [one sentence]
```

# Soccer / International Tournament Betting Reference

## Overview

Soccer betting framework for FIFA World Cup, continental tournaments, and other international competitions. Uses three-way moneylines (Home / Draw / Away) with sport-specific gate criteria adapted from the universal 8-gate structure.

## Key Differences from MLB

| MLB | Soccer |
|-----|--------|
| Two-way moneyline | Three-way (Home / Draw / Away) |
| Pitcher-centric gates | Form, attack, defense, draw risk, bench depth, context |
| Every game has a home team | Knockout stage is neutral sites |
| ~3-hour games | Match windows span afternoon + evening |
| Polymarket via US Sports SDK | Polymarket via CLOB or Gamma API |

---

## Venue / Neutral-Site Verification (Required Pre-Gate Check)

**The "home" label in fixture data (ESPN, FootyStats) is a tournament convention — not real home advantage on neutral soil.** Verify actual venue location before applying any home-field reasoning in gate analysis.

- **Extract** venue `fullName`, `address.city`, and `address.country` from the competition data for every match
- **Compare** the venue country to the "home" team's home country:
  - **Neutral site** (venue country ≠ home team's country): Strip ALL home-field advantage reasoning from analysis. Do not reference "at home" in any gate commentary. The home/away label is meaningless for venue context.
  - **True home game** (venue country = home team's country): Home advantage is real but the market already prices it. Note as neutral context — do not use as a tiebreaker bias.
- **Host nation rule:** When a host nation plays at home in a tournament (e.g., USA in a US-hosted World Cup), that IS a true home game. Note it explicitly. The market prices it — flag it rather than argue from it.
- **Club/tournament default:** For international tournaments (World Cup, Champions League, Copa America, Euros, AFCON), default to neutral site unless the venue country matches one team's home federation. For domestic league matches (EPL, La Liga, etc.), the "home" label IS real — assume home advantage unless the fixture is at a neutral venue (cup final, neutral-site derby).
- **Data source:** Venue block from competition data — `fullName`, `address.city`, `address.country`
- **Pitfall:** Do NOT use the competitor home/away designation as a proxy for venue. A match listed as "Netherlands (home)" at NRG Stadium in Houston is not a home game.

---

## The 8 Soccer Gates (adapted from PROCESS.md)

Apply these in order before any official pick. Same numbered structure as PROCESS.md — only the content inside each gate changes.

### 1. Defensive Floor
Can my side's defensive structure survive the first 60-70 minutes without conceding?
- **Check:** CB partnership, goalkeeper form, defensive midfielder screen, tactical setup (deep block vs high press)
- **For favorites:** Can we keep a clean sheet or limit the opponent to 0-1 goals while we find our scoring rhythm?
- **For underdogs:** Can we survive the opening pressure wave and stay in the match?
- **Friendly data filter:** Defensive data from matches where the opponent was at less than full strength is **noise**. Discount by 50% unless the opposing lineup is confirmed.
- **Data sources:** ESPN rosters, FootyStats GA/game and clean sheet %, form of GK and CBs

### 2. Opposing Defense Shutdown Path
Can the opponent's defense/keeper realistically suppress our attack for 90 minutes?
- **Check:** Opponent clean sheet %, organized defense profile, goalkeeper quality, CB partnership
- **For favorites:** Does the opponent have a legitimate path to stifle our star attacker? A possession-heavy team missing its primary dribble-penetration wingers can get held scoreless even by a weaker defense — the attack becomes horizontal and predictable.
- **For underdogs:** Can we create chances against this defense at all?
- **Elite attacker override:** If our side has a top-5 world attacker who is healthy and playing, this gate is much harder for the opponent to pass. Stars create chances that normal metrics miss. Discount the opponent's defensive rating by 15-20% in this gate.
- **Data sources:** FootyStats clean sheet %, opponent CB quality, tactical profile (deep block vs high line)

### 3. Late-Game Defensive Survival
If the script is close late (1-goal margin or draw going into final 30 min), do I trust my side's defensive organization and goalkeeper?
- **Check:** Substitution patterns (both sides), defensive subs available, goalkeeper composure under pressure
- **Tournament context matters:** Matchday 1 vs knockout. Debutants often tire late as the intensity is higher than they've faced before.
- **For favorites holding a lead:** Can we see out a 1-0? Do we have defensive subs (fresh CBs, defensive midfielder)?
- **For underdogs:** Do we have the legs to maintain our defensive shape?
- **Bench depth as draw-breaker:** When betting a draw, check whether the opponent carries attacking subs who can change a 0-0 after 70'. A triple substitution of fresh attackers at ~70-75' is a structural draw-breaker — tired defenders vs fresh legs creates goals even in low-event matches. Also check the opponent's **recent substitution patterns** — do they have a history of impactful attacking subs in prior games? A bench with multiple goal-scoring subs is a draw threat that pre-match starting-XI stats won't capture.
- **Data sources:** FootyStats late-goal trends (goals conceded in 75-90'), quality of bench defenders, ESPN summary keyEvents for recent sub impact

### 4. My-Side Defensive Concern Check
If my side has a key injury/suspension in defense (starting CB out, GK missing, defensive midfielder on a yellow card risk), is the edge big enough to survive?
- **Check:** Is a key defender out? Yellow card accumulation risk? Fatigue from previous match?
- **For favorites:** A missing CB against a counter-attacking team changes the risk profile significantly
- **For underdogs:** A missing keeper against a star attacker is often a death sentence
- **Data sources:** ESPN rosters for lineup confirmation, web search for injury news

### 5. Both Sides Defensive Concern Check
If both teams have defensive vulnerabilities (missing CBs/keeper, leaky profiles), is my side's attack strong enough to win a high-scoring game?
- **Check:** Does this gate suggest Over 2.5 or BTTS? Both teams defensive issues can create goals
- **Elite attacker premium:** When a star attacker is on the pitch and the opponent's defense is compromised, expect multiple goals. This is the prime scenario for -1.5 handicaps and star props.
- **Data sources:** Same as Gate 4, plus combined GA/game for both teams

### 6. Cold-Form Reset Check
If fading a team on a poor run (1W or fewer in last 5, <1 GF/game), have they shown reset signs?
- **Check:** Any key attacker returned from injury? Did they just break a losing streak? Did the lineup change materially?
- **Tournament context:** A team that lost Matchday 1 may approach Matchday 2 completely differently (more attacking, desperation)
- **Soccer equivalent of "cold offense":** A team that hasn't scored in 2+ matches is in a goal drought — but a favorable matchup (weak defense, set piece opportunity) can break it
- **Data sources:** ESPN lastFiveGames, match timeline for recent goal scorers

### 7. Price Discipline
Is the number inside the bettable-to threshold without needing the price to create the pick?
- **Convert American odds to probability:** Negative: |odds| / (|odds| + 100). Positive: 100 / (odds + 100).
- **3-way market note:** Draw must always be accounted for. Example: France -215 (68% implied) + Draw +360 (22%) + Senegal +600 (14%).
- **No-Polymarket penalty:** When Polymarket match markets are not available, require a 10%+ edge (not the normal 5%) to compensate for the single price source.
- **Draw pricing:** Treat draw the same as any other side — estimate probability from form, context, and scoring profile, compare to market, calculate edge. The standard edge thresholds apply (5% with multiple price sources, 10% with one).
- **Draw risk flags** (Very High/High/Medium/Low) help identify matches where a draw outcome is structurally likely, but they do not carry hard price minimums. A +260 draw with 35% estimated probability has a real edge. A +320 draw with 22% estimated probability has no edge. Do the math.
- **Elite attacker override:** When the favorite has a top-5 world attacker healthy and playing, discount your estimated draw probability by 15-20%. These players create chances that structured defenses cannot contain — a draw becomes harder to hold.
- **Hard pass rule — 30¢+ movement away from your side:** If the line moves 30¢ or more against your chosen side, pass unless an independently verifiable event resolves in your favor.
- **Data sources:** ESPN odds, Polymarket if available, sportsbooks

### 8. Winner Conviction
Do I actually believe this side/draw wins most often?
- **For moneyline:** Do I genuinely think this team wins most often, or am I betting the line?
- **For draw:** Do I think this match is more likely to end level than either side winning outright? If the draw is +360, I need >21.7% conviction (without juice).
- **For props:** Do I believe this player scores most matches at this price?
- **The line from PROCESS.md applies:** "Winner conviction first. Current form first. Price filters the pick. Reputation never."

---

## Formation Evaluation — Shape vs Function

A formation label (5-4-1, 5-3-2, 4-5-1) tells you the **structure**, not the **function**. A defensive shape can be one of two things:

| Function | What it looks like | Projection |
|:---------|:-------------------|:-----------|
| **Parked bus** | Sits deep, minimal attacking ambition, 1-2 shots per match, 0-0 or 0-1 type results. Typical of true minnows against elite opposition. | Low-event match. Under 2.5, draw, or 1-goal favorite win. |
| **Counterattacking shape** | Same formation but with a plan to break — fast transitions, a creative attacker who can carry the ball, recent results showing goals. | **Chaos at both ends.** Both teams can score. Goals, cards, and events are likely. |

**The question to ask when you see a 5-4-1 (or similar deep shape):**

1. **Recent goals scored?** Have they scored in 3+ of last 5 matches? A team playing a 5-4-1 that scored in 4/5 recent matches (including a 4-1 win) is not parking the bus — that's a team that creates chances.
2. **Top-5 league attacker?** Does the team have a starter in a major European league (EPL, La Liga, Serie A, Bundesliga, Ligue 1)? A top-5 league attacker on a supposedly "defensive" team means counterattacking threat, not bus-parking.
3. **Recent competitive form?** Unbeaten run? Draw streaks against quality opposition? A team that's been competitive against better sides absorbs pressure and hits back — they don't just pray for 0-0.

**When the answer is yes to 2+ of these, the shape is a counterattacking formation — not a low-event match.** Treat Gate 5 and Gate 1 accordingly:

- **Gate 1 (defensive floor):** A counterattacking 5-4-1 can and will score. Your side's defense *will* face real chances. Do not project a clean sheet.
- **Gate 5 (both sides vulnerable):** Even if "both sides" looks defensive on paper, a counterattacking shape means both sides generate chances. This gate should produce **Over 2.5 or BTTS** considerations, not the opposite.
- **Under 2.5 thesis is weaker:** A team that scores in 4/5 recent matches while playing a defensive formation is scoring *because* of the counterattack, not despite it. The Under needs a separate thesis beyond "they play 5-4-1."

**Corollary — formation alone is never a pick thesis.** A pick built on "they play 5-4-1, therefore Under" is a formation guess, not a data thesis. Always verify with recent goals scored and top-5 league attackers before projecting low events.

---

## Hard Pass Rules (Soccer-Specific)

Pass when:
- current price is worse than bettable-to price
- the pick exists mainly because the number is attractive, not because you believe the side/draw wins most often
- case depends mostly on reputation (team name, historical brand), not evidence
- team is averaging **<1 GF/game over last 5** and you're laying heavy juice
- edge is weak and you cannot explain it clearly
- market sharply disagrees (30¢+ movement off your side) and you cannot explain why
- you are chasing a moved number late
- a friendly result was cited without verifying the opponent was at full strength
- A draw play where the favorite has an elite attacker healthy and starting (discount estimated draw probability by 15-20% and re-check edge)
- A draw play where the favorite has **attacking bench depth** that can break a 0-0 after 70' (triple sub of fresh attackers ≈ structural draw-breaker; check recent sub patterns in prior games)
- A goalscorer prop where the player is a secondary striker on a team with a top-5 world attacker (15-20% shot share discount applies)

**No pick is better than a bad pick.**

---

## Bet Type Reference — Given Gate Profile

The gates determine what to bet:

| Gate Profile | Bet Type | Why |
|:------------|:---------|:----|
| **Gates 1, 2, 3, 7, 8 pass** (defensive floor + attack can score + can hold lead + good price + conviction) | Favorite ML. Consider -1.5 handicap if elite attacker present. | Strong side, multiple gates support. |
| **Gates 1, 3, 4, 6, 8 pass** (structure holds + can survive late + no defensive concerns + faded team has no reset + conviction) | Draw at estimated-edge price. Under 2.5. No player props. | Low-event match, draw is the outcome. |
| **Gates 2, 5, 7, 8 pass** (opponent can't suppress attack + both teams have defensive issues + good price + conviction) | Over 2.5 or BTTS. Star attacker anytime prop. | Goals expected from both sides. |
| **Gates 1, 2, 4, 8 pass** (defensive floor + no shutdown path + no defensive concerns + conviction) | Star attacker anytime + 2+. Handicap -1.5 if at +130 or better. | Comfortable favorite win. |
| **Gates 1, 6, 7, 8 pass** (defensive floor + faded team no reset + good price + conviction on dog) | Dog ML if +350+. Dog's primary attacker anytime. Dog +0.5 handicap. | Upset thesis has gate support. |
| **Fewer than 4 gates pass** | Pass. | No edge. Don't force it. |

**Star 2+ after first goal (live market):** When a star scores in the first half, live 2+ at -150 or better is worth a sprinkle. The first goal confirms the player has rhythm, the defense is reeling, and the hard part is done — the second goal often follows faster than the first.

**Sub protection (FanDuel, mismatch only):** When the gate profile supports a comfortable favorite win, sub protection premium (3-5¢) is worth paying. Starter likely pulled ~65th minute, replacement faces tired defense. Tight competitive match → sub protection is wasted money.

**Elite-primary scorer ladders:** In clear favorite mismatches, 2+ goal ladders are playable only for the true first-option scorer or penalty-taking focal point (top-5 world attacker profile). Secondary forwards on the same favorite need role/minutes confirmation and should not inherit the star's projection.

**Correlated scorer parlays/free bets:** Use only when every leg is independently bettable. A parlay does not create edge; it concentrates already-valid edges. Do not add a weak secondary-scorer leg just to inflate payout.

**Long dog ML discipline:** A +600/+700 dog needs a structural path — favorite rotation, major injury, tactical mismatch, or draw-first setup. Price alone is not a thesis.

---

## Data Sources

### Scoreboard & Odds
- **ESPN scoreboard:** `site.api.espn.com/apis/site/v2/sports/soccer/[league]/scoreboard?dates=YYYYMMDD`
- League IDs: `fifa.world` (World Cup), `eng.1` (Premier), `esp.1` (La Liga), `ger.1` (Bundesliga), `ita.1` (Serie A), `fra.1` (Ligue 1), `uefa.champions` (Champions League)

### Match Details
- **ESPN summary:** `site.api.espn.com/apis/site/v2/sports/soccer/[league]/summary?event={event_id}`
  - Returns keyEvents (goals, subs, cards), rosters (formations, lineups, per-player stats), boxscore (team stats), lastFiveGames, headToHeadGames

### Team & Player Data
- **FootyStats:** Team form, xG, goal timing, BTTS rates, clean sheet %
- **`sports-skills` CLI:** `football search_team`, `football get_event_statistics`, `football get_player_season_stats`

### Market Data
- **Polymarket:** Web at `polymarket.com/sports/...` or Gamma API for CLOB-only markets
- **Sportsbooks:** ESPN odds (DK), FanDuel (player props), DraftKings

---

## Post-Match Gate Audit

After each settled match, audit which gates were right/wrong:

| Gate | Verdict | What happened |
|:----:|:-------:|:--------------|
| 1 — Defensive floor | ✅/❌/⚠️ | Did our side survive the first 60-70 min? |
| 2 — Opposing shutdown | ✅/❌/⚠️ | Could the opponent suppress our attack? |
| 3 — Late-game survival | ✅/❌/⚠️ | Did sub patterns or bench depth decide it? |
| 4 — Defensive concern | ✅/❌/⚠️ | Were our defensive injuries a factor? |
| 5 — Both vulnerable | ✅/❌/⚠️ | Did both teams create chances? |
| 6 — Cold-form reset | ✅/❌/⚠️ | Did the faded team show reset signs? |
| 7 — Price discipline | ✅/❌/⚠️ | Was the price fair in retrospect? |
| 8 — Winner conviction | ✅/❌/⚠️ | Was the thesis directionally correct? |

Extract durable lessons and promote them into permanent rules.

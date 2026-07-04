# International Soccer — Data & Analysis Reference

> **Currently loaded:** FIFA World Cup 2026 (June 11 – July 19). This file adapts the PROCESS.md structure for soccer. The same numbered gates and checklist, soccer-specific content inside.

## Current Tournament: FIFA World Cup 2026
- **Dates:** June 11 – July 19, 2026
- **Hosts:** USA, Canada, Mexico (16 cities)
- **Format:** 48 teams → 12 groups of 4 → Round of 32 → Round of 16 → QF → SF → Bronze → Final
- **Total matches:** 104

---

## Team IDs (ESPN)

| Team | ID | Team | ID |
|------|---|------|---|
| Algeria | 109 | Angola | 103 |
| Argentina | 138 | Belgium | 169 |
| Cape Verde | 2268 | Curaçao | 3909 |
| Czechia | 161 | Ecuador | 209 |
| Egypt | 2620 | England | 448 |
| France | 478 | Germany | 481 |
| Ghana | 4469 | Haiti | 2654 |
| Iran | 469 | Iraq | 4375 |
| Ivory Coast | 4789 | Japan | 627 |
| Jordan | 2917 | Mexico | 203 |
| Morocco | 2869 | Netherlands | 449 |
| New Zealand | 2666 | Norway | 464 |
| Panama | 2659 | Paraguay | 210 |
| Portugal | 482 | Qatar | 4398 |
| Saudi Arabia | 655 | Scotland | 580 |
| Senegal | 654 | South Africa | 467 |
| South Korea | 451 | Spain | 164 |
| Sweden | 466 | Switzerland | 475 |
| Tunisia | 659 | Türkiye | 465 |
| United States | 660 | Uruguay | 212 |
| Uzbekistan | 2570 |

---

## Official Pick Gates (Soccer-Adapted)

Same numbered structure as PROCESS.md. Each gate adapted for soccer. Failed gate = pass.

### 1. Defensive floor (starter floor equivalent)
Can my side's defensive structure survive the first 60-70 minutes without conceding?
- **Check:** CB partnership, goalkeeper form, defensive midfielder screen, tactical setup (deep block vs high press)
- **For favorites:** Can we keep a clean sheet or limit the opponent to 0-1 goals while we find our scoring rhythm?
- **For underdogs:** Can we survive the opening pressure wave and stay in the match?
- **Friendly data filter:** Defensive data from matches where the opponent was at less than full strength is **noise**. Discount by 50% unless the opposing lineup is confirmed.
- **Data sources:** ESPN rosters (who's in the back line), FootyStats GA/game and clean sheet %, form of GK and CBs

### 2. Opposing defense shutdown path
Can the opponent's defense/keeper realistically suppress our attack for 90 minutes?
- **Check:** Opponent clean sheet %, organized defense profile, goalkeeper quality, CB partnership
- **For favorites:** Does the opponent have a legitimate path to stifle our star attacker? A possession-heavy team missing its primary dribble-penetration wingers can get held scoreless even by a weaker defense — the attack becomes horizontal and predictable.
- **For underdogs:** Can we create chances against this defense at all?
- **Elite attacker override:** If our side has a top-5 world attacker who is healthy and playing, this gate is much harder for the opponent to pass. Stars create chances that normal metrics miss. Discount the opponent's defensive rating by 15-20% in this gate.
- **Data sources:** FootyStats clean sheet %, opponent CB quality, tactical profile (deep block vs high line)

### 3. Late-game defensive survival (bullpen equivalent)
If the script is close late (1-goal margin or draw going into final 30 min), do I trust my side's defensive organization and goalkeeper?
- **Check:** Substitution patterns, defensive subs available, goalkeeper composure under pressure
- **Tournament context matters:** Matchday 1 vs knockout. Debutants often tire late as the intensity is higher than they've faced before.
- **For favorites holding a lead:** Can we see out a 1-0? Do we have defensive subs (fresh CBs, defensive midfielder)?
- **For underdogs:** Do we have the legs to maintain our defensive shape?
- **Bench depth as draw-breaker:** When betting a draw, check whether the opponent carries attacking subs who can change a 0-0 after 70'. A triple substitution of fresh attackers at ~70-75' is a structural draw-breaker — tired defenders vs fresh legs creates goals even in low-event matches. Also check the opponent's **recent substitution patterns** — do they have a history of impactful attacking subs in prior games? A bench with multiple goal-scoring subs is a draw threat that pre-match statistics alone won't capture.
- **Data sources:** FootyStats late-goal trends (goals conceded in 75-90'), quality of bench defenders, ESPN summary keyEvents for recent sub impact

### 4. My-side defensive concern check
If my side has a key injury/suspension in defense (starting CB out, GK missing, defensive midfielder on a yellow card risk), is the edge big enough to survive?
- **Check:** Is a key defender out? Yellow card accumulation risk? Fatigue from previous match?
- **For favorites:** A missing CB against a counter-attacking team changes the risk profile significantly
- **For underdogs:** A missing keeper against a star attacker is often a death sentence
- **Data sources:** ESPN rosters for lineup confirmation, web search for injury news

### 5. Both sides defensive concern check
If both teams have defensive vulnerabilities (missing CBs/keeper, leaky profiles), is my side's attack strong enough to win a high-scoring game?
- **Check:** Does this gate suggest Over 2.5 or BTTS? Both teams defensive issues can create goals
- **Elite attacker premium:** When a star attacker is on the pitch and the opponent's defense is compromised, expect multiple goals. This is the prime scenario for -1.5 handicaps and star props.
- **Data sources:** Same as Gate 4, plus combined GA/game for both teams

### 6. Cold-form reset check
If fading a team on a poor run (1W or fewer in last 5, <1 GF/game), have they shown reset signs?
- **Check:** Any key attacker returned from injury? Did they just break a losing streak? Did the lineup change materially?
- **Tournament context:** A team that lost Matchday 1 may approach Matchday 2 completely differently (more attacking, desperation)
- **Soccer equivalent of "cold offense":** A team that hasn't scored in 2+ matches is in a goal drought — but a favorable matchup (weak defense, set piece opportunity) can break it
- **Data sources:** ESPN lastFiveGames, match timeline for recent goal scorers

### 7. Price discipline
Is the number inside the bettable-to threshold without needing the price to create the pick?

**⚠️ CRITICAL — Edge must come from an independent probability model, not from cross-market comparison.**
Comparing DK 90-min implied + a subjective "ET bump" to PM To Advance price is NOT edge discovery. It's comparing apples to oranges. Both markets already know the format. The model must produce its own probability estimate from form data, THEN compare to the market price for the SAME event type.

**Model-based edge calculation (required for all picks):**

Use `scripts/intl_soccer_model.py` to estimate probabilities:
```bash
python3 scripts/intl_soccer_model.py <team_a> <team_b> --stage R16 \
  --adj-gf-a X --adj-ga-a X --wins-a N --draws-a N --losses-a N \
  --adj-gf-b X --adj-ga-b X --wins-b N --draws-b N --losses-b N
```

The model:
1. Computes team strength ratings from quality-adjusted form (GF/g, GA/g) + tournament win rate
2. Converts rating difference to 90-min outcome probabilities (win/draw/loss) via logistic regression calibrated to 324 WC matches
3. Derives To Advance probabilities from 90-min outcomes + ET/pens model
4. Compares model probabilities to market prices WITHIN THE SAME MARKET TYPE

**Edge comparison rules (hard):**

| Market | Compare model to | Do NOT compare to |
|---|---|---|
| DK/ESPN 90-min ML | DK/ESPN implied probability | PM To Advance price |
| PM To Advance | PM YES price | DK 90-min implied |
| Draw (DK 90-min) | DK draw implied probability | PM draw binary |

**Edge thresholds (same-market):**
- 2%+: candidate if 4+ gates pass
- 4%+: Medium confidence
- 7%+: High confidence
- Under 2%: PASS

**No subjective ET bump.** The model derives advance probability from team strength — it doesn't add an arbitrary percentage to DK's number. If the model shows no edge, there is no edge.

- **Convert American odds to probability:**
  - Negative: prob = |odds| / (|odds| + 100)
  - Positive: prob = 100 / (odds + 100)
- **3-way market note:** Draw must always be accounted for. Example: France -215 (68% implied) + Draw +360 (22%) + Senegal +600 (14%)
- **Polymarket pricing:** WC match markets are available via `polymarket_wc_markets.py --date YYYY-MM-DD`. This script returns binary (to-advance) prices for all three outcomes (home/draw/away). Always run this script during the slate scan to get a second independent price source for every match.
- **Draw pricing:** Treat draw the same as any other side — estimate probability from form, context, and scoring profile, compare to market, calculate edge. Use the model for draw probability estimation.
- **Draw risk flags** (Very High/High/Medium/Low) help identify matches where a draw outcome is structurally likely, but they do not carry hard price minimums. A +260 draw with 35% estimated probability has a real edge (+260 = 27.8% implied; 35% estimated = 7.2% edge). A +320 draw with 22% estimated probability has no edge. Do the math.
- **Elite attacker override:** When the favorite has a top-5 world attacker healthy and playing, discount your estimated draw probability by 15-20%. These players create chances that structured defenses cannot contain — a draw becomes harder to hold.
- **Data sources:** ESPN odds (DK), Polymarket via `polymarket_wc_markets.py`, model via `intl_soccer_model.py`

### 8. Winner conviction
Do I actually believe this side/draw wins most often?
- **For moneyline:** Do I genuinely think this team wins most often, or am I betting the line?
- **For draw:** Do I think this match is more likely to end level than either side winning outright? If the draw is +360, I need >21.7% conviction (without juice).
- **For props:** Do I believe this player scores most matches at this price?
- **The line from PROCESS.md applies: "Winner conviction first. Current form first. Price filters the pick. Reputation never."**

---

## Required Pre-Pick Checklist

### 0. Venue / Neutral-Site Verification
**Required check before any gate analysis.** The "home" label in ESPN data is a tournament fixture convention, NOT real home advantage on neutral soil.

- **Extract** venue `fullName`, `address.city`, and `address.country` from the ESPN scoreboard `competitions[0].venue` block for every match
- **Compare** the venue country to the "home" team's home country:
  - **Neutral site** (venue country ≠ home team's country): Strip ALL home-field advantage reasoning from the analysis. Do not reference "at home" in any gate commentary. The ESPN home/away label is meaningless for venue context.
  - **True home game** (venue country = home team's country): Home advantage is real but the market already prices it. Note it as neutral context — do not use it as a tiebreaker bias.
- **Host nation rule:** When a host nation plays at home (e.g., USA in a US-hosted World Cup), that IS a true home game. Note it explicitly. The market will still price it in — flag it rather than argue from it.
- **Club/tournament default:** For international tournaments (World Cup, Champions League, Copa America, Euros, AFCON), default to neutral site unless the venue country matches one team's home federation. For domestic league matches (EPL, La Liga, etc.), the "home" label IS real — assume home advantage unless the fixture is at a neutral venue (cup final, neutral-site derby).
- **Data source:** ESPN scoreboard `competitions[0].venue` block: `.fullName`, `.address.city`, `.address.country`
- **Pitfall:** Do NOT use the ESPN competitor `homeAway` field as a proxy for venue. Always check actual venue data. NRG Stadium in Houston is not "home" for Netherlands.

### 1. Current form (last 5 matches)
- W-D-L record, goals scored, goals conceded for both teams
- **Opponent-quality weighting (critical):** Raw W-D-L is misleading without opponent context. A 3-1-1 against weak teams is not better than 2-2-1 against strong teams.
  - **Check who each win/draw came against.** A win over a tournament minnow is not equivalent to a draw against a contender.
  - **GF/game needs opponent context.** 5 goals against a bottom-tier team inflates the average. Note which goals came against comparable opposition.
  - **Clean sheets matter more against quality attacks.** 3 clean sheets against weak attacks is noise; 1 clean sheet against a contender is a real signal.
  - **Draws are not equal.** A draw against Belgium is a positive result. A draw against a minnow is a negative. Tag each draw by opponent tier.
- **Quality-adjustment guardrail (2026-07-04):** When computing quality-adjusted GF/g and GA/g, exclude the **bottom 2 opponents by FIFA rank or Elo rating only** — not every non-elite team. Costa Rica and Congo DR are tournament-level sides. Only minnows like Haiti, Jordan, Uzbekistan, Panama, Qatar, New Zealand should be filtered out. Example from Colombia vs Ghana audit: Pass 1 incorrectly excluded Costa Rica and Congo DR as "minnows," producing a false 0.5 adj GF/g. Pass 2 corrected to 1.33 by only excluding Jordan and Uzbekistan. **If in doubt about whether a team is a minnow, include them in the quality-adjusted sample.** Under-filtering produces noise; over-filtering produces phantom edges.
- **Shared-opponent comparison is single-point evidence, not a trend.** One common opponent between two teams gives directional data, not a conclusion. "Team A beat Norway 3-1, Team B beat Norway 2-1" says Team A was better *that day*, not that Team A is the better team overall. Require 2+ shared opponents or corroborating data before using it as a primary thesis.
- **Friendly data filter:** If a result came against a weakened opponent (rested starters, unknown lineup), discount by 50%
- **Case example — Sweden vs Netherlands (June 20):** Sweden's 3-1-1 (14 GF) was treated as clearly superior to Netherlands' 2-2-1 (7 GF). But Netherlands' draws came against Belgium and Japan — stronger opposition than Sweden's wins. The GF gap was inflated by 5 goals against Tunisia. Shared-opponent comp (both beat Norway) was one data point, not a trend. Result: Netherlands won 2-0.
- **Data source:** ESPN lastFiveGames (check opponent names in each), FootyStats

### 2. Lineup/formation (probable starters equivalent)
- Who's starting? Formation shape? (4-3-3 vs 4-4-2 changes everything)
- Key injuries/suspensions? Star attacker available?
- **Elite attacker check:** Top-5 world player starting? Healthy? Motivated (opener, knockout, rivalry)?
- **Data source:** ESPN summary → rosters

### 3. Star attacker current form (starter current form equivalent)
- Last 3 matches: goals scored, shots on target, minutes played
- Is the star delivering? Or in a quiet patch?
- **Secondary attacker check:** Who else scores? (Creative midfielder, set piece threat, secondary forward)
- **Data source:** sports-skills football search_player, ESPN keyEvents from recent matches

### 4. Defensive context (bullpen context equivalent)
- Who's at CB? Goalkeeper form? Defensive midfielder quality?
- Tactical profile: High press? Deep block? Vulnerable to counters?
- **Soccer equivalent of:** who protects a lead late
- **Data source:** ESPN rosters, FootyStats defensive metrics

### 5. Lineup/injury context
- Current roster truth only. Known injuries, suspensions, yellow card risks
- Any late-breaking lineup news?
- **Data source:** ESPN summary → rosters, web search

### 6. Market price
- Current line (American odds + implied probability)
- Bettable-to line
- Pass point
- Price can veto a pick; it cannot create one by itself

### 7. Market sanity check
- Line movement: open vs current. 10¢+ off your side → concern. 30¢+ off → pass unless independently verifiable event resolves in your favor.
- Draw shortening while favorite drifts = sharp money on the draw
- **Data source:** ESPN compare `moneyline.home.open.odds` to `moneyline.[side].close.odds`

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

**MLB analogy Jerry confirmed:** Same as evaluating a bullpen by ERA vs FIP, or a batter by stranded runners vs hard-hit rate. The surface number (formation, ERA) can lie. The underlying data (recent goals, shots created, top-5 league players) tells the real story.

**Corollary — formation alone is never a pick thesis.** A pick built on "they play 5-4-1, therefore Under" is a formation guess, not a data thesis. Always verify with recent goals scored and top-5 league attackers before projecting low events.

---

## Hard Pass Rules

Pass when:
- current price is worse than bettable-to price
- the pick exists mainly because the number is attractive, not because you believe the side/draw wins most often
- case depends mostly on reputation (team name, historical brand), not evidence
- team is averaging **<1 GF/game over last 5** and you're laying heavy juice
- edge is weak and you cannot explain it clearly
- market sharply disagrees (30¢+ movement off your side) and you cannot explain why
- you are chasing a moved number late
- a friendly result was cited without verifying the opponent was at full strength
- **No pick is better than a bad pick**

**Soccer-specific hard passes:**
- A pick justified entirely by "+51% dog ROI since 2022" without match-specific gate support
- A star attacker play whose price was set before a key injury/suspension was announced
- A draw play where the favorite has an elite attacker healthy and starting (discount estimated draw probability by 15-20% and re-check edge)
- A draw play where the favorite has attacking bench depth that can break a 0-0 after 70' (triple sub of fresh attackers ≈ structural draw-breaker; check recent sub patterns in prior games)
- A goalscorer prop where the player is a secondary striker on a team with a top-5 world attacker (15-20% shot share discount applies)

---

## Market Type Awareness: 90-Minute vs To Advance (Extra Time Included)

**Critical distinction for knockout matches (R16 onward):**

| Market | Provider | Includes ET? | Includes Pens? | Settlement |
|--------|----------|:---:|:---:|-----------|
| 3-way Moneyline (ML) | DK, ESPN, most sportsbooks | **No** | **No** | 90 min + stoppage time only |
| Binary YES/NO | Polymarket (`polymarket_wc_markets.py`) | **Yes** | **Yes** | Team advances to next round |
| To Advance / To Qualify | Some sportsbooks | **Yes** | **Yes** | Team advances to next round |

**What this means for picks:**

1. **Polymarket picks ARE "To Advance" picks.** When the cron proposes Colombia ML at 68.5¢ on Polymarket, it's betting Colombia advances — not Colombia wins in 90 minutes. If Colombia draws 1-1 and wins in extra time, the Polymarket pick WINS. The same pick on DK would LOSE.

2. **DK/ESPN picks are 90-minute only.** A draw in regulation is a draw settlement, even if the team wins in ET or pens. This is the standard for 3-way moneylines on all sportsbooks.

3. **For knockout matches, Polymarket prices embed a structural edge for favorites.** The elimination of the draw outcome means the favorite's probability is higher on PM than on DK. Example: Argentina -700 DK (87.5% to win in 90 min) vs Argentina 85.5¢ PM (85.5% to advance). The PM price is actually LOWER because it already bakes in the draw-to-ET risk. Always compare PM vs DK with this structure in mind — a favorite at 60¢ PM and -150 DK is NOT a pricing discrepancy; it's the expected gap between "to advance" and "90-min win."

4. **When both PM and DK are available on a knockout match:** Use PM for edge estimation when the pick is placed on PM. Use DK for DK-placed picks. Do NOT mix the two market types in a single edge calculation.

5. **If placing a draw pick:** PM draw binary markets include ET — the YES pays if the match is drawn at 90 min AND stays drawn through ET (before pens). This is different from a DK draw bet which settles at 90 min. DO NOT bet the draw on Polymarket binary markets — the ET inclusion makes the draw substantially harder to hit. Draw picks should only go on DK/ESPN 3-way moneylines.

### Choosing the right market for your thesis (knockout matches only)

**Step 0: Verify extra time exists in this competition and round.** Not all tournaments have ET. Do not assume.

| Competition | ET in group stage? | ET in knockout? | Notes |
|:---|:---:|:---:|:---|
| FIFA World Cup | No | Yes (R16+) | ET in all knockout rounds |
| UEFA Euros | No | Yes (R16+) | ET in all knockout rounds |
| Copa América | No | Yes (QF+) | No ET in group stage; knockout from QF onward has ET |
| CONCACAF Gold Cup | No | Varies | Check per-tournament rules; some rounds skip ET |
| AFC Asian Cup | No | Yes (R16+) | ET in all knockout rounds |
| Africa Cup of Nations | No | Yes (R16+) | ET in all knockout rounds |
| Olympics | No | Yes (QF+) | ET in knockout from quarterfinals |
| Friendlies | No | No | Draw is final. No ET, no pens. |
| Club competitions (UCL, etc.) | N/A | Yes (R16+) | ET in two-legged ties (2nd leg only) and finals |

**If extra time does NOT exist in this match:** To Advance = 90-min result. The two market types are identical — use 90-min ML (DK) since it typically has better liquidity and no PM binary confusion. The draw is a final result.

**If extra time DOES exist**, proceed to the decision table:

For knockout matches with ET, ask: **does my edge thesis say the favorite wins in 90 minutes, or just that they'll advance?**

| Thesis says... | Use | Why |
|:---|---|:---|
| Favorite dominates early, opponent folds | 90-min ML (DK) | Edge is in the regulation mismatch. ET would mean the thesis was wrong. |
| Favorite is clearly better but opponent can defend deep (0-0 risk) | To Advance (PM) | Ghana held England 0-0. Colombia should advance but 0-0/1-1 at 90' is live. The thesis is "better team advances," not "better team wins in 90." |
| Draw risk is HIGH (both teams draw-prone, knockout caution) | To Advance (PM) for favorite; 90-min Draw (DK) for draw thesis | If the draw is a real threat, the 90-min ML carries too much draw risk. To Advance insulates against it. |
| Favorite has an elite attacker (Messi, Mbappé) | 90-min ML (DK) | These players break draws. If you believe the favorite wins in regulation, take the better payout. |
| Fading a favorite whose attack is anemic (can't score) | To Advance (PM) on the underdog, or 90-min Draw (DK) | Belgium-Senegal: Belgium couldn't score against Iran. If you think the favorite might go to ET, the underdog To Advance at long odds has structural value. |

**Hard rule — ⚠️ EDGE METHODOLOGY UPDATED 2026-07-04:**
- **Edge must come from `intl_soccer_model.py`, not from cross-market comparison.**
- The model produces independent probabilities from form data. Compare model output to market prices for the SAME event type.
- PM To Advance prices already include ET/pens in the market consensus. DK 90-min already excludes them. Comparing one to the other + a subjective bump is NOT edge discovery — it's comparing different bets.
- The model solves this: it estimates 90-min outcomes and derives advance probability from team strength, then each is compared to its own market.
- Example (Jul 4 Morocco): DK 90-min implied 58.3%, PM To Advance 56.5%. The old method said "58.3% + ET bump ≈ 62% → edge vs 56.5%." The model says "Morocco advance probability = 55.0% → -1.5% edge." The model was right — comparing different market types created a phantom edge.

### Penalties Risk (applies to all To Advance picks)

A To Advance bet that reaches penalties is effectively a coin flip, regardless of how dominant the favorite was over 120 minutes. Penalty shootouts convert at ~75% per kick — the favorite's edge from open play is destroyed.

**This means the To Advance price overstates the favorite's true probability by not discounting for penalties variance.** If Colombia is 72% to advance but there's a 15% chance the match reaches pens, the true probability of advancing is closer to 72% − (15% × ~25% pens loss rate) ≈ 68%.

| When To Advance is the pick | Penalties adjustment |
|:---|:---|
| Favorite clearly dominant, should win in 90 | Minimal — if the thesis is right, pens never happen. No adjustment needed. |
| Favorite better but opponent can defend deep (draw risk HIGH) | **Cap confidence at Medium.** The path to pens is real. The edge is in "advances eventually," not "wins comfortably." Don't size this like a lock. |
| Underdog To Advance | Pens risk is *favorable* to the dog — they just need to survive to the coin flip. The To Advance price on the underdog may *understate* their true probability. This is one of the few scenarios where the dog To Advance has structural value beyond the 90-min ML. |
| Both teams have poor penalty records or neither has a shootout specialist | Additional variance — mentally discount the edge by 3-5% if neither side has a clear pens edge. |

**When to avoid To Advance entirely:** If the draw risk is HIGH and neither team has a dominant penalty record, the match is a genuine coin flip. The To Advance pick becomes a lottery ticket, not an edge play. Pass or size down.

---

## Bet Type Reference — Given Gate Profile

The gates determine what to bet, same as MLB:

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

**Long dog ML discipline:** A +600/+700 WC dog needs a structural path — favorite rotation, major injury, tactical mismatch, or draw-first setup. Price alone is not a thesis.

---

## Post-Match Gate Audit

After each settled match, audit which gates were right/wrong:

1. **Defensive floor gate** — Did our side's defensive structure hold?
2. **Opposing defense shutdown path** — Could the opponent suppress our attack?
3. **Late-game defensive survival** — Did late defensive organization matter?
4. **My-side defensive concern** — Did our defensive absences matter?
5. **Both sides defensive concern** — Did defensive issues create goals?
6. **Cold-form reset check** — Did the frozen team show signs of life?
7. **Price discipline** — Was the price fair in hindsight?
8. **Winner conviction** — Was the read right?

**Lesson Extraction:**
- Same as MLB: if a gate misfired, update it. If the same mistake repeats, make it a hard rule.
- Log findings to intl-soccer-lessons.md.

---

## Useful Commands Quick Reference

### Schedule & Odds
```bash
curl -s "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates=$(date +%Y%m%d)&limit=50"
```

### Match Timeline (goals, assists, subs, cards)
```bash
curl -s "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event={event_id}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for ev in d.get('keyEvents', []):
    etype = ev.get('type', {})
    if isinstance(etype, dict) and etype.get('type') in ['goal', 'substitution', 'yellow-card', 'red-card']:
        clock = ev.get('clock',{}).get('displayValue','')
        text = ev.get('text','')
        print(f'  {etype.get(\"type\")} {clock}: {text[:200]}')
"
```

### Per-Player Match Stats
```bash
curl -s "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event={event_id}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for roster in d.get('rosters', []):
    team = roster.get('team',{}).get('displayName','?')
    for entry in roster.get('roster', []):
        ath = entry.get('athlete',{}).get('displayName','?')
        starter = entry.get('starter', False)
        stats = {s.get('name',''): s.get('displayValue','0') for s in entry.get('stats', [])}
        print(f'{ath} ({team}): goals={stats.get(\"totalGoals\",\"0\")}, shots={stats.get(\"totalShots\",\"0\")}, SOT={stats.get(\"shotsOnTarget\",\"0\")}, assists={stats.get(\"goalAssists\",\"0\")}')
"
```

### Match Statistics
```bash
sports-skills football get_event_statistics --event_id={event_id}
```

### Team Form (Last 5 Games)
```bash
curl -s "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event={event_id}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for team_data in d.get('lastFiveGames', []):
    tn = team_data.get('team',{}).get('displayName','?')
    for game in team_data.get('events', []):
        print(f'{tn}: {game.get(\"gameDate\",\"?\")[:10]} \u2014 {game.get(\"homeTeamName\",\"?\")} {game.get(\"homeTeamScore\",\"?\")}-{game.get(\"awayTeamScore\",\"?\")} {game.get(\"awayTeamName\",\"?\")}')
"
```

### Player Search & Profile
```bash
sports-skills football search_player --query="Player Name"
sports-skills football get_player_season_stats --player_id={espn_athlete_id}
```

### Head-to-Head
```bash
sports-skills football get_head_to_head --team_id={team_id_1} --team_id_2={team_id_2}
```

### Team Form from FootyStats
```bash
web_extract "https://footystats.org/clubs/{team-name-slug}-{id}"
```

---

## External Data Sources

| Source | What it provides | Best for | How to access |
|--------|-----------------|----------|--------------|
| **Action Network** | Per-game win probabilities, projected scores, goal totals | Cross-reference pre-slate | `web_extract` on matchup tool page |
| **PicksAndParlays** | Historical ROI across 24 markets, 324 WC matches | Understanding which markets have edge | Data below |
| **Sporting Life** | Tournament trends, group vs knockout patterns | Tournament-level context | Data below |
| **VSiN** | Pro analyst picks with reasoning | Sharp betting approach | `web_search` for specific match + VSiN |
| **FootyStats** | Team form, xG, goal timing, BTTS rates, clean sheet % | Pre-match team analysis | `web_extract` on team page |
| **ESPN Summary API** | Match timeline, per-player stats, team form, lineups | Post-match analysis, player tracking | `curl -s "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event={event_id}"` |
| **sports-skills CLI** | Match stats, player search, player season stats, H2H | Pre-match scouting | `sports-skills football get_event_statistics --event_id={id}` |
| **intl_soccer_model.py** | Team strength ratings, 90-min & To Advance probabilities, edge calculation | Independent probability model — required for all edge calculations | `python3 scripts/intl_soccer_model.py <team_a> <team_b> --stage R16 --adj-gf-a X ...` |

**Note on xG for World Cup:** Neither Understat nor free scrapable sites provide xG data for WC matches. fbref.com has the data but is behind Cloudflare anti-bot protection. Use **shots on target** as an xG proxy.

---

## Established WC Betting Trends (2006-2026, 324 matches)

### Which markets actually profit?

| Market | Record | Win% | Avg Price | ROI | Verdict |
|--------|--------|------|-----------|-----|---------|
| **Underdog ML** | 65-259 | 20.1% | +520 | **+8.32%** | Best straight bet |
| **1H Under 0.5** | 122-202 | 37.7% | +184 | **+6.17%** | Goals scarce first 30 min |
| **Under 1.5** | 100-223 | 31.0% | +247 | +1.66% | Selective, low-event matches |
| Under 3.5 | 251-72 | 77.7% | -340 | +0.25% | Best parlay leg |
| Favorite ML | 177-147 | 54.6% | -116 | **-3.29%** | Loses money blindly |
| Over 2.5 | 146-176 | 45.3% | +118 | -4.24% | Needs strong reason |
| BTTS Yes | — | 45.7% | — | **-5.33%** | Avoid as default |
| Favorite -1.5 AH | 91-233 | 28.1% | +263 | **-11.28%** | Blowout pricing expensive |

### Underdog ML trend by window

| Window | ROI |
|--------|:---:|
| Since 2006 | +8.32% |
| Since 2010 | +27.98% |
| Since 2014 | +24.47% |
| Since 2018 | **+33.80%** |
| Since 2022 | **+51.38%** |

### Historical tournament data

| Metric | Value |
|--------|-------|
| Avg goals — group stage | 2.69/match |
| Avg goals — knockout | 2.31/match |
| Draw rate — group stage | ~22% |
| Draw rate — knockout (90 min) | ~27% |
| Clean sheet — group stage | 34% |
| Team scoring first wins final | 14 of last 16 (87.5%) |

### Group stage patterns
- **Upsets are frequent** — South Korea beat Germany (2018), Saudi beat Argentina (2022)
- **Favorites lose money blindly** (-3.29% ROI) — need a reason beyond name value
- **First-half goals are rare** — 1H Under 0.5 is +6.17% ROI
- **BTTS bleeds money** (-5.33%) — don't default to it

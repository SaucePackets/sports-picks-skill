# MLB refusal hypothesis scan — slate prose

## This decides nothing

A hypothesis generator over the days whose slate prose happened to be verbose enough to parse. It decides nothing: no rule it suggests may change a gate, a threshold or a policy, and any candidate rule would still have to survive leave-one-month-out.

## Selection bias

10 of 23 slate files in the window carry per-game price lines. The population is days that were WRITTEN a certain way, not a sample of slates, so every rate below is confounded with how tersely a given run wrote. This is the blind spot the game_reads recording change exists to close, and it cannot be corrected for here.

Enumeration scope: ~/.buzz/.scratch/claude-freshcorpus-20260831/slate walked recursively for <date>*.md; only files at the top level are treated as the MLB slate lane. Paths are rendered with the running account's home directory replaced by ~.

Corpus: Slate writeups copied from the VPS runtime .picks on 2026-08-31 (~/.buzz/.scratch/claude-freshcorpus-20260831), the same corpus the drought diagnostic used.

## Population

- slate files in window: **23**
- eligible (carry per-game price lines): **10**
- game sections parsed: **109**
- sections with a classified rail: **108**
- sections with no classified rail: **1**
- sections with a resolvable value side: **79**
- sections with an attached outcome: **105**

## Extraction failures, by field

Nothing here is imputed. A field the parser could not read is counted, not filled in.

| field | sections it could not be read from |
|---|---|
| `dk_fair_prob` | 4 |
| `polymarket_ask` | 28 |
| `refusing_rails` | 1 |
| `teams` | 1 |

## By rail

The value side is defined mechanically as the larger of `dk_fair_prob - polymarket_ask`. It is NOT a reconstruction of the side the run was considering — nobody recorded that — and every zero below is a real zero over the population above, not a missing number.

Base rate over the same population: the value side won **37 of 76** reads that have both a resolvable value side and a known outcome. Compare every row below against that, not against 50%.

A read may name more than one rail, so the per-rail counts sum to more than the number of reads. They are not a partition.

| rail | sections naming it | value side resolved | value side won | outcome unknown |
|---|---|---|---|---|
| `bullpen_close_game_survival` | 38 | 28 | 11 | 0 |
| `cold_fade_reset` | 0 | 0 | 0 | 0 |
| `daily_volume_cap` | 0 | 0 | 0 | 0 |
| `game_already_started` | 1 | 0 | 0 | 0 |
| `incomplete_input_data` | 8 | 5 | 4 | 0 |
| `lineups_unconfirmed` | 9 | 8 | 3 | 0 |
| `no_dk_price` | 2 | 0 | 0 | 0 |
| `no_polymarket_market` | 16 | 0 | 0 | 0 |
| `opposing_starter_shutdown_path` | 3 | 3 | 0 | 0 |
| `park_environment_cap` | 28 | 28 | 13 | 2 |
| `price_discipline` | 39 | 26 | 14 | 0 |
| `real_winner_conviction` | 41 | 28 | 16 | 3 |
| `starter_floor` | 4 | 4 | 2 | 0 |
| `starter_unannounced` | 2 | 1 | 0 | 0 |

## Refusals the classifier did not recognise

Quoted verbatim so the reader can see what the vocabulary missed, rather than having it disappear into an `other` bucket.

- **2026-08-24 — Philadelphia Phillies at Seattle Mariners — 01:40Z / 8:40 PM CT**: DK -108/+101 -> de-vigged PHI fair 0.511. Polymarket ask PHI 0.515 (source timestamp 2026-08-24T15:32:47Z); provisional handicap PHI 0.550, net edge +0.035. Opponent check: SEA 0.450 minus 0.490 ask = -0.040. **Watchlist, not a pick:** lineup confirmation is the sole open blocker; recheck all inputs at 00:25Z and reject if the ask moves or the orders break the thesis.

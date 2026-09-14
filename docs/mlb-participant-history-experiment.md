# Participant evidence and pitcher-history coverage

This is a separate offline experiment. The original pinned appearance, history,
and model contracts are unchanged. It creates candidate reconstructed pitcher
features, not an admitted predictor or executable picks.

The original rule treats every unresolved earlier game as a possible missing
appearance for every pitcher. PR #109 showed that recovering 189 games still
produced zero complete pitcher-feature pairs. This experiment preserves the
selected last-three appearances and their cutoffs, but tests whether retained
records can establish that a particular gap cannot change that selection.

## Evidence permitted to exclude a gap

1. A corroborated final participant list does not include the pitcher. Require
   game/team identity, a unique matching final schedule occurrence, agreeing final
   scores, matching pitcher lists and player records, per-player/team count sums,
   play/substitution identities, and full pitch-count reconciliation. Both teams
   are checked. The complete game must finish strictly before the target cutoff.
2. The same certificate establishes that the unresolved game completed strictly
   before **all three** selected appearances. The pitcher may be listed in that
   older game; its missing counts cannot affect the latest-three selection.
   Equality and fewer than three known appearances remain blocked.
3. A postponed occurrence has a corroborated interval before its explicit later
   reschedule start. Require the schedule's Postponed/D status, matching game and
   teams, an explicit reschedule timestamp matching the later feed, changed official
   date, no suspension/resumption markers, and all retained plays starting at or
   after that later timestamp. This excludes only the original occurrence and only
   before the reschedule start. A moved date alone is insufficient.

The third case uses retained later metadata for retrospective reconstruction; it
is **not** proof of what was published at the original cutoff. There are no
independently verified pregame timestamps in this experiment.

Final participant evidence may cover corroborated early rain endings. A resumed
final must link explicitly to its original start and may exclude a pitcher only
once the complete resumed game is over. These distinctions matter because a
[suspended game continues from its stopping point](https://www.mlb.com/glossary/rules/suspended-game).
The original appearance-statistics refusals remain intact for both cases.

The final completion bound is the latest completed play end, not merely the last
array element. Nonmonotonic ordering is retained without admitting its statistics;
each play's own start/end must remain valid, and the bound must follow the actual
resume/start time. A reversed individual interval, incomplete/index-invalid play,
unknown participant, conflicting count, missing feed, or unmatched final schedule
cannot certify a participant exclusion.

Every excluded occurrence retains a reason and a hash of its full certificate.
The report retains accepted and refused certificates, source hashes, source
pointers, contributing appearances, remaining blockers, and the original replay
hash. Certificates describe structural corroboration within retained MLB records;
they are not independent provider authentication.

## Retained-data result

| Split | Starter identities | Pitcher pairs before | Pitcher pairs after | With team feature and usable label |
|---|---:|---:|---:|---:|
| Train | 433 | 0 | 90 | 87 |
| Validation | 390 | 0 | 148 | 148 |
| Test | 387 | 0 | 58 | 57 |

The retained 137 gap occurrences cover 117 unique games. Participant lists were
corroborated for 108 games, alongside 23 postponed-occurrence intervals. Five
late makeups lack a final schedule inside the retained window; their postponed
intervals remain usable only before rescheduling. Four other games retain
participant-census, play-index/completion, or individual start/end contradictions.
Two of those have reversed individual play intervals and remain refused even
though the discrepancy is less than three seconds. No timestamp is repaired.

The join uses the existing chronological admission report, keyed by game ID,
source date, and split. Pitcher-feature pairs are not automatically usable labels.
The full-report hash and the separate team-admission replay hash are retained in
the compact result. These counts establish improved coverage, not probability
accuracy. No model was fitted, scored, or admitted. The test intersection remains
far too small for the installed 300-game live evaluation minimum.

The full suite passed: **1,864 tests and 714 subtests**, with one optional dependency
skip. The exact supported retained-data replay matches the final adapter digest.

PR #109 was deployed at `81cdd17324f2ee2dd1d3bbaaf80c955807d1e06e`, receipt
`.deploy/receipt-20260914-193506.txt`, with 28 matching profile scripts. Its natural
14:45 CT research run refreshed two games and started the producer for recovered
Dodgers–Reds inputs. That producer completed research but failed writer landing
because unavailable exchange quotes were confused with available sportsbook
prices. The separate exchange-quote recording fix addresses that observed failure;
this offline participant experiment does not alter the runtime writer or policy.

## Reproduce

```sh
python scripts/mlb_participant_history_experiment.py \
  --bundle <chronological-acquisition> \
  --snapshot-dir <bulk-starter-acquisition> \
  --baseline-replay <bulk-starter-directory>/R2_TIP_REPLAY.json > participants.json
PYTHONPATH=scripts python -m pytest -q
```

The supported command replays the pinned appearance recovery from original retained
bytes, verifies its checkpoint, reconstructs participant evidence from those same
sources, and applies the exclusions. No network access, runtime state, risk policy,
model registry, or order path is used. Full reports belong outside the checkout;
`mlb-participant-history-result.json` records the compact result and full-report hash.

## Verification scope

Adversarial tests cover source/identity binding, missing players and substitutions,
count contradictions, zero-BF substitutes, strict cutoffs, postponed versus suspended
records, early endings, resumption links, latest-play completion bounds, and aging
out a gap only before all three selected appearances. Unknown gaps remain blocking.
No source is repaired, no missing pitching statistics are imputed, and no model
is fitted or scored by this change.

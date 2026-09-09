# Retained appearance census

The offline `scripts/mlb_appearance_census.py` classifies all **326** appearance
occurrences refused by the merged PR #95 replay. It reconstructs and compares
all 1,291 regular occurrence results against that byte-pinned baseline before
accepting the denominator. Repeated IDs remain separate dated occurrences.
The [checkpoint](mlb-appearance-census.json) binds the report and source inputs.

| Original refusal group | Occurrences | Recovered accounting | Still refused |
|---|---:|---:|---:|
| Unsupported PA event | 221 | 189 | 32 |
| Duplicate ID | 40 | 0 | 40 |
| Ambiguous substitution | 28 | 0 | 28 |
| Before scheduled start | 22 | 0 | 22 |
| Other | 15 | 0 | 15 |
| Total | 326 | 189 | 137 |

Recovery means the game's complete pitcher BF/K/BB accounting corroborates in
retained bytes. It creates no feature rows or histories. The existing admission
and snapshot replay callers retain their original behavior: the new classifier
is opt-in and used only by this census. No acquisition, fitting, scoring, odds,
eligibility, policy, runtime, deployment, or performance result is produced.
Provenance-checkpoint enforcement and the pre-manifest call-site test remain
separate follow-ups; this command does not claim to resolve them.

## Explicit event semantics

The retained feed labels some runner-only terminal plays `type: atBat`. That
label alone cannot establish a completed PA. The classifier accepts only named
caught-stealing and pickoff outs in its explicit `RUNNER_OUT_EVENTS` set. Each
requires a complete, nonscoring third-out play, fewer than four balls and three
strikes, a runner distinct from the matchup batter who started on an occupied
base, matching event type and out base, exactly one matching third out, and a
valid indexed play-event reference whose own `details.isOut` is exactly true.
False, missing, or nonboolean out flags refuse recovery; an unrelated event
cannot substitute for the referenced event. In-play or terminal PA event details conflict
with this interpretation and refuse it. Generic `other_out`, wild pitches,
stolen bases, pickoff errors, and unknown labels remain insufficient evidence.
The retained game 778557 play 66 projection supplies a concrete fixture; its
synthetic game envelope exercises the parser and is not additional retained data.

Every play still passes the original sequential-index, start-time, matchup
identity, defensive-side and substitution checks. Every listed pitcher must
have positive completed PAs and exact boxscore BF/K/BB totals. Non-PA runner
plays add zero to those totals and keep separate `non_pa_play_pointers`.
A pitcher with no completed PA remains refused rather than being dropped.
All original final, date and completion checks run first. No cutoff/history gate
is changed or invoked by the new command.

The 32 remaining first-slice refusals are: 18 unsupported event cases, five
zero-completed-PA pitchers, four unmatched runner-out corroborations, two
mid-appearance substitutions, one scoring conflict, one terminal-runner-out
conflict, and one substitution identity mismatch. These include failures hidden
behind the baseline's first unsupported event. The other 15 are independently
reported as five final/schedule mismatches, four nonfinal games, three chronology
conflicts, two substitution identity mismatches, and one zero-PA appearance.
No promised recovery count determines any gate.

## Evidence and reproduction

Each refused occurrence retains game/date/split, schedule digest and pointer,
feed digest, the original first failing gate, the remaining gate, all play
pointers with event/pitcher/batter/times, and complete recovered pitcher records
when permitted. An empty feed pointer means the root-level outcome gate; a null
pointer means refusal before feed examination (for example duplicate IDs).
The play inventory classifies event semantics only; it is not an independent
admission verdict. Its full list preserves plays after the first failing gate.

From the repository root, with `EVIDENCE` set to the owner-controlled workspace
`RESEARCH` directory:

```sh
python3 scripts/mlb_appearance_census.py \
  --bundle "$EVIDENCE/MLB_CHRONOLOGICAL_ADMISSION_2026_09_07/ACQUISITION" \
  --baseline-replay "$EVIDENCE/MLB_BULK_STARTER_SNAPSHOTS_2026_09_07/R2_TIP_REPLAY.json" \
  > census.json
shasum -a 256 census.json
```

Compare the byte count and SHA-256 with the committed checkpoint. The full
report and logs are retained in its evidence directory, outside Git. GitHub
alone does not supply the source bundle. No source reacquisition is needed or
permitted by the command. Input integrity/denominator failures exit 2; exit 0
means classification completed, not that an evaluation dataset is available.
Hashes bind retained bytes, not independent provider authenticity.

Full-package pytest comparison uses Python 3.14.7 and the same environment at
base and tip. The four baseline failures concern execution locking and three
lineup-watchlist expectations; they remain outside this scope. The final exact
counts and commands are recorded in the private evidence note and PR.

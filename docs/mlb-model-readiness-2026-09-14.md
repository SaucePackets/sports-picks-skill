# MLB model evidence readiness — 2026-09-14

## Current runtime

Cleanup PR #107 is deployed at `87166d7669da6e5508333f156d21f6fcf9e71f28`.
The deployment receipt is `.deploy/receipt-20260914-183012.txt`; all 26 managed
profile copies matched. Risk SHA256 remains
`bb0f140db8d22dceb7cc9a5c4c7dfe6ab0273ee36a2bbfc7a3ca73e38027cfc6`.
188 deployed regression tests and 32 subtests passed. Live calibration and the
empty-ledger postgame path exited successfully. The natural review at 13:30 CT
completed and wrote its audit at `2026-09-14T18:30:29.103104+00:00`.

All nine cron workdirs and referenced scripts were verified in the prior
[cron matrix](repo-audit-2026-09-14.md). Full repaired morning/evening/NFL agent
runs with successful delivery remain separate verification work; old error rows
have not been overwritten. No execution job was resumed and no orders were sent.

A tools-disabled, one-turn Vig provider smoke also returned `CONNECTED` using
the configured provider; it sent no outbound Buzz message. That verifies model
transport, not a complete sports-agent run or delivery.

The deployed model-evaluation policy is installed: minimum 300 evaluation games,
Brier improvement 0.005, log-loss improvement 0.01, no calibration/score regression.
`mlb_deployed_models` is null. Installing policy did not admit a predictor.
The live schedule still has zero candidates and zero watchlist entries in this
inspection. The preceding fresh 20-side price comparison found no 5pp fallback
edge; that is a dated observation, not a guarantee about later prices.

## Retained evidence was reverified, not reacquired

The supported `mlb_provenance_checkpoint.py` replay against the owner-controlled
chronological bundle and bulk snapshot directory returned:

- `pinned_replay_verified: true`
- source manifest `d8983606ce75093d924d53caffe73c31b9fdbd404c0ca086ad3873d73f2dc7aa`
- snapshot manifest `acd3e7544f718b9321a5eb293e54a322d5cca6d9a94d7027fb3b36f8f5a5191b`
- replay digest `09cf7472f85105395f37d44684ba008447e059f9904ee41de0145094ad6d7bcd`
- zero feature rows, fitting/scoring disabled, eligible false, historical performance null.

The appearance census was independently rerun from retained bytes and matched
its committed checkpoint: 11,019,723 bytes, SHA256
`b84341afc63ca8449cf7521857017a68063ab1075fefde46d5839fb28e3bc2e3`.
It again classified 326 original refused occurrences: 189 recovered accounting
cases and 137 remaining refusals. No pinned adapter or contract was changed.

The later bulk acquisition already retained 1,246 timestamp indexes and 1,213
snapshots, yielding 1,210 starter identity candidates. The old three-game feasibility
probe is not the current acquisition state. Starter identity candidates are not
complete history-feature pairs, and the opt-in census recovery has not been
integrated into the admission/history path.

## Next model implementation boundary

1. Integrate the already-reviewed appearance classifications into a separately
   versioned history experiment. Preserve the original pinned replay. First
   determine whether any pitcher histories become complete under the unchanged
   cutoff/coverage rules; do not presume that 189 recovered games produces a
   usable training dataset.
2. Diagnose the remaining 137 occurrences from retained pointers. The largest
   remaining groups include 40 duplicate-date occurrences, 30 ambiguous
   mid-appearance substitutions across groups, 22 before-start cases, and 18
   unsupported PA-event cases. Recover only where bytes support the event
   semantics; keep legitimate ambiguity refused. No promised recovery count.
3. For the proposed simple team-only baseline, freeze a distinct experiment
   specification before scoring. Existing team-history admission reports usable
   lagged pairs, but the current three-feature challenger contract explicitly
   forbids substitution with a fallback challenger. A team-only experiment must
   have its own identity and cannot silently relax that contract.
4. Freeze training/validation/test dates, feature transformations and missing-data
   treatment, then report all-game coverage and held-out probability metrics.
   Label reconstructed historical experiments accurately; they cannot become
   original pregame predictions merely by adding local timestamps or hashes.
5. Prospective comparison against a sportsbook baseline still needs a verified
   same-book quote source/market contract and an accepted timing-evidence path.
   The current shadow module implements fixture receipts only. Its eligible flag
   must stay false until an explicitly reviewed live path exists.

The two user-identified Hermes env files were checked for configured variable
names containing ODDS, TSA, or TIMESTAMP; none were present. This is not proof
that no provider exists elsewhere. No credential values were printed and no
unrelated credential store was searched. Provider/configuration information was
requested so that the next source check can use existing authorized access.

The research queue improves operational follow-ups and starts collecting useful
observations. It does not itself resolve model evidence admission. No model was
trained, scored, admitted, or substituted for market fallback in this change.
